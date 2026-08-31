from datetime import date, datetime, timezone

import pytest

from revenue_bridge.capital import CapitalMode, CapitalPolicy
from revenue_bridge.contribution import (
    ContributionKind,
    ContributionLedger,
    PaymentObservation,
    PaymentVerificationState,
)


NOW = datetime(2026, 8, 31, 18, 0, tzinfo=timezone.utc)


def payment(
    *,
    payment_id: str = "pay_1",
    amount: float = 50.0,
    state: PaymentVerificationState = PaymentVerificationState.VERIFIED,
) -> PaymentObservation:
    return PaymentObservation(
        payment_id=payment_id,
        opportunity_id="opp_1",
        amount_usd=amount,
        state=state,
        evidence_ref=f"receipt:{payment_id}",
        observed_at=NOW,
        provider_reference=f"provider:{payment_id}",
    )


def test_verified_payment_counts_as_customer_revenue():
    ledger = ContributionLedger()
    ledger.record_verified_payment(payment(amount=50))

    snapshot = ledger.capital_snapshot(as_of_date=date(2026, 8, 31))

    assert snapshot.verified_customer_revenue_usd == 50.0


def test_unverified_payment_cannot_count_as_revenue():
    ledger = ContributionLedger()

    with pytest.raises(ValueError, match="requires VERIFIED"):
        ledger.record_verified_payment(
            payment(state=PaymentVerificationState.UNVERIFIED)
        )

    assert ledger.capital_snapshot().verified_customer_revenue_usd == 0.0


def test_indeterminate_payment_cannot_count_as_revenue():
    ledger = ContributionLedger()

    with pytest.raises(ValueError, match="requires VERIFIED"):
        ledger.record_verified_payment(
            payment(state=PaymentVerificationState.INDETERMINATE)
        )

    assert ledger.capital_snapshot().verified_customer_revenue_usd == 0.0


def test_verified_payment_cannot_be_counted_twice():
    ledger = ContributionLedger()
    observation = payment()

    ledger.record_verified_payment(observation)

    with pytest.raises(ValueError, match="already counted"):
        ledger.record_verified_payment(observation)

    assert ledger.capital_snapshot().verified_customer_revenue_usd == 50.0


def test_revenue_cannot_be_injected_through_cost_api():
    ledger = ContributionLedger()

    with pytest.raises(ValueError, match="verified payment"):
        ledger.record_cost(
            entry_id="fake_revenue",
            opportunity_id="opp_1",
            kind=ContributionKind.VERIFIED_CUSTOMER_REVENUE,
            amount_usd=50,
            evidence_ref="manual:claim",
            occurred_at=NOW,
        )


def test_cost_categories_build_capital_snapshot():
    ledger = ContributionLedger()

    ledger.record_cost(
        entry_id="acq_1",
        opportunity_id="opp_1",
        kind=ContributionKind.ACQUISITION_COST,
        amount_usd=5,
        evidence_ref="receipt:acq",
        occurred_at=NOW,
    )
    ledger.record_cost(
        entry_id="fulfill_1",
        opportunity_id="opp_1",
        kind=ContributionKind.FULFILLMENT_COST,
        amount_usd=4,
        evidence_ref="receipt:fulfill",
        occurred_at=NOW,
    )
    ledger.record_cost(
        entry_id="api_1",
        opportunity_id="opp_1",
        kind=ContributionKind.MODEL_API_COST,
        amount_usd=2,
        evidence_ref="receipt:api",
        occurred_at=NOW,
    )
    ledger.record_cost(
        entry_id="fee_1",
        opportunity_id="opp_1",
        kind=ContributionKind.PAYMENT_PLATFORM_FEE,
        amount_usd=1,
        evidence_ref="receipt:fee",
        occurred_at=NOW,
    )

    snapshot = ledger.capital_snapshot(as_of_date=date(2026, 8, 31))

    assert snapshot.acquisition_spend_usd == 5.0
    assert snapshot.fulfillment_cost_usd == 4.0
    assert snapshot.model_api_cost_usd == 2.0
    assert snapshot.payment_platform_fees_usd == 1.0
    assert snapshot.total_cost_usd == 12.0
    assert snapshot.today_acquisition_spend_usd == 5.0


def test_verified_revenue_offsets_real_costs_not_gross_spend_forever():
    ledger = ContributionLedger()

    ledger.record_cost(
        entry_id="acq",
        opportunity_id="opp_1",
        kind=ContributionKind.ACQUISITION_COST,
        amount_usd=45,
        evidence_ref="receipt:acq",
        occurred_at=NOW,
    )
    ledger.record_cost(
        entry_id="fulfill",
        opportunity_id="opp_1",
        kind=ContributionKind.FULFILLMENT_COST,
        amount_usd=4,
        evidence_ref="receipt:fulfill",
        occurred_at=NOW,
    )
    ledger.record_cost(
        entry_id="api",
        opportunity_id="opp_1",
        kind=ContributionKind.MODEL_API_COST,
        amount_usd=3,
        evidence_ref="receipt:api",
        occurred_at=NOW,
    )
    ledger.record_cost(
        entry_id="fees",
        opportunity_id="opp_1",
        kind=ContributionKind.PAYMENT_PLATFORM_FEE,
        amount_usd=3,
        evidence_ref="receipt:fees",
        occurred_at=NOW,
    )
    ledger.record_verified_payment(payment(amount=100))

    snapshot = ledger.capital_snapshot(as_of_date=date(2026, 8, 31))

    assert snapshot.total_cost_usd == 55.0
    assert snapshot.verified_customer_revenue_usd == 100.0
    assert snapshot.net_contribution_usd == 45.0
    assert snapshot.unrecovered_loss_usd == 0.0


def test_sixty_dollars_unrecovered_loss_enters_hard_stop():
    ledger = ContributionLedger()

    ledger.record_cost(
        entry_id="loss_60",
        opportunity_id="opp_1",
        kind=ContributionKind.FULFILLMENT_COST,
        amount_usd=60,
        evidence_ref="receipt:loss",
        occurred_at=NOW,
    )

    snapshot = ledger.capital_snapshot(as_of_date=date(2026, 8, 31))

    assert snapshot.unrecovered_loss_usd == 60.0
    assert snapshot.mode(CapitalPolicy()) == CapitalMode.HARD_STOP


def test_today_acquisition_spend_uses_requested_accounting_date():
    ledger = ContributionLedger()

    ledger.record_cost(
        entry_id="yesterday",
        opportunity_id="opp_1",
        kind=ContributionKind.ACQUISITION_COST,
        amount_usd=5,
        evidence_ref="receipt:yesterday",
        occurred_at=datetime(2026, 8, 30, 18, 0, tzinfo=timezone.utc),
    )
    ledger.record_cost(
        entry_id="today",
        opportunity_id="opp_1",
        kind=ContributionKind.ACQUISITION_COST,
        amount_usd=7,
        evidence_ref="receipt:today",
        occurred_at=NOW,
    )

    snapshot = ledger.capital_snapshot(as_of_date=date(2026, 8, 31))

    assert snapshot.acquisition_spend_usd == 12.0
    assert snapshot.today_acquisition_spend_usd == 7.0


def test_duplicate_entry_id_is_rejected():
    ledger = ContributionLedger()

    kwargs = dict(
        entry_id="same",
        opportunity_id="opp_1",
        kind=ContributionKind.MODEL_API_COST,
        amount_usd=1,
        evidence_ref="receipt:api",
        occurred_at=NOW,
    )

    ledger.record_cost(**kwargs)

    with pytest.raises(ValueError, match="duplicate contribution entry_id"):
        ledger.record_cost(**kwargs)


def test_payment_roundtrip_preserves_fingerprint():
    first = payment()
    restored = PaymentObservation.from_dict(first.to_dict())

    assert restored.fingerprint == first.fingerprint


def test_ledger_has_no_spending_or_payment_execution_authority():
    ledger = ContributionLedger()

    assert not hasattr(ledger, "dispatch")
    assert not hasattr(ledger, "approve")
    assert not hasattr(ledger, "charge")
    assert not hasattr(ledger, "send_payment")
