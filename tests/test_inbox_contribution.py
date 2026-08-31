from datetime import datetime, timezone

import pytest

from revenue_bridge.capital import CapitalDecision, CapitalMode
from revenue_bridge.contribution import (
    ContributionKind,
    PaymentObservation,
    PaymentVerificationState,
)
from revenue_bridge.economics import OpportunityEconomics
from revenue_bridge.github import GitHubInboundNormalizer
from revenue_bridge.inbox import RevenueInbox


NOW = datetime(2026, 8, 31, 18, 0, tzinfo=timezone.utc)


def make_ready_inbox() -> tuple[RevenueInbox, str]:
    inbox = RevenueInbox()
    event = GitHubInboundNormalizer.load_fixture("claude_code")
    opp = inbox.ingest(event)

    inbox.record_economics(
        opp.opportunity_id,
        OpportunityEconomics(
            opportunity_id=opp.opportunity_id,
            sale_probability=0.25,
            offer_price_usd=50,
            proposed_acquisition_cost_usd=5,
            estimated_fulfillment_cost_usd=4,
            estimated_model_api_cost_usd=2,
            estimated_payment_platform_fees_usd=1,
        ),
    )
    return inbox, opp.opportunity_id


def test_default_capital_evaluation_uses_ledger_hard_stop():
    inbox, opportunity_id = make_ready_inbox()

    inbox.contribution_ledger.record_cost(
        entry_id="loss_60",
        opportunity_id=opportunity_id,
        kind=ContributionKind.FULFILLMENT_COST,
        amount_usd=60,
        evidence_ref="receipt:loss",
        occurred_at=NOW,
    )

    _, evaluation = inbox.evaluate_capital(opportunity_id)

    assert evaluation.mode == CapitalMode.HARD_STOP
    assert evaluation.decision == CapitalDecision.REJECT


def test_verified_payment_reduces_unrecovered_loss():
    inbox, opportunity_id = make_ready_inbox()

    inbox.contribution_ledger.record_cost(
        entry_id="loss_60",
        opportunity_id=opportunity_id,
        kind=ContributionKind.FULFILLMENT_COST,
        amount_usd=60,
        evidence_ref="receipt:loss",
        occurred_at=NOW,
    )

    inbox.record_verified_payment(
        PaymentObservation(
            payment_id="pay_50",
            opportunity_id=opportunity_id,
            amount_usd=50,
            state=PaymentVerificationState.VERIFIED,
            evidence_ref="receipt:pay_50",
            observed_at=NOW,
        )
    )

    snapshot = inbox.capital_snapshot()
    _, evaluation = inbox.evaluate_capital(opportunity_id)

    assert snapshot.unrecovered_loss_usd == 10.0
    assert evaluation.mode == CapitalMode.NORMAL
    assert evaluation.decision == CapitalDecision.PROPOSE_SPEND


def test_unverified_payment_cannot_reopen_capital():
    inbox, opportunity_id = make_ready_inbox()

    inbox.contribution_ledger.record_cost(
        entry_id="loss_60",
        opportunity_id=opportunity_id,
        kind=ContributionKind.FULFILLMENT_COST,
        amount_usd=60,
        evidence_ref="receipt:loss",
        occurred_at=NOW,
    )

    with pytest.raises(ValueError, match="requires VERIFIED"):
        inbox.record_verified_payment(
            PaymentObservation(
                payment_id="pay_unverified",
                opportunity_id=opportunity_id,
                amount_usd=100,
                state=PaymentVerificationState.UNVERIFIED,
                evidence_ref="observation:unverified",
                observed_at=NOW,
            )
        )

    assert inbox.capital_snapshot().unrecovered_loss_usd == 60.0


def test_payment_for_unknown_opportunity_is_refused():
    inbox = RevenueInbox()

    with pytest.raises(KeyError, match="not found"):
        inbox.record_verified_payment(
            PaymentObservation(
                payment_id="pay_unknown",
                opportunity_id="missing",
                amount_usd=50,
                state=PaymentVerificationState.VERIFIED,
                evidence_ref="receipt:unknown",
                observed_at=NOW,
            )
        )


def test_explicit_snapshot_path_remains_supported():
    from revenue_bridge.capital import CapitalSnapshot

    inbox, opportunity_id = make_ready_inbox()

    _, evaluation = inbox.evaluate_capital(
        opportunity_id,
        CapitalSnapshot(),
    )

    assert evaluation.decision == CapitalDecision.PROPOSE_SPEND
