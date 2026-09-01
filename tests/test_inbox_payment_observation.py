import pytest

from revenue_bridge.contribution import PaymentVerificationState
from revenue_bridge.github import GitHubInboundNormalizer
from revenue_bridge.inbox import RevenueInbox
from revenue_bridge.payment import (
    PaymentObservationMode,
    SimulatedPaymentEvidenceAdapter,
)


def make_inbox() -> tuple[RevenueInbox, str]:
    inbox = RevenueInbox()
    event = GitHubInboundNormalizer.load_fixture("claude_code")
    opportunity = inbox.ingest(event)
    return inbox, opportunity.opportunity_id


def test_observing_verified_payment_does_not_change_accounting():
    inbox, opportunity_id = make_inbox()
    adapter = SimulatedPaymentEvidenceAdapter(
        mode=PaymentObservationMode.VERIFIED,
        observed_amount_usd=50,
    )

    observation = inbox.observe_payment(
        opportunity_id=opportunity_id,
        payment_id="pay_1",
        adapter=adapter,
    )

    assert observation.state == PaymentVerificationState.VERIFIED
    assert observation.amount_usd == 50.0
    assert inbox.capital_snapshot().verified_customer_revenue_usd == 0.0


def test_verified_observation_must_be_explicitly_recorded():
    inbox, opportunity_id = make_inbox()
    adapter = SimulatedPaymentEvidenceAdapter(
        mode=PaymentObservationMode.VERIFIED,
        observed_amount_usd=50,
    )

    observation = inbox.observe_payment(
        opportunity_id=opportunity_id,
        payment_id="pay_2",
        adapter=adapter,
    )

    inbox.record_verified_payment(observation)

    assert inbox.capital_snapshot().verified_customer_revenue_usd == 50.0


def test_unverified_observation_cannot_change_accounting():
    inbox, opportunity_id = make_inbox()
    adapter = SimulatedPaymentEvidenceAdapter(
        mode=PaymentObservationMode.UNVERIFIED,
        observed_amount_usd=50,
    )

    observation = inbox.observe_payment(
        opportunity_id=opportunity_id,
        payment_id="pay_3",
        adapter=adapter,
    )

    assert observation.state == PaymentVerificationState.UNVERIFIED

    with pytest.raises(ValueError, match="requires VERIFIED"):
        inbox.record_verified_payment(observation)

    assert inbox.capital_snapshot().verified_customer_revenue_usd == 0.0


def test_indeterminate_observation_cannot_change_accounting():
    inbox, opportunity_id = make_inbox()
    adapter = SimulatedPaymentEvidenceAdapter(
        mode=PaymentObservationMode.INDETERMINATE,
        observed_amount_usd=50,
    )

    observation = inbox.observe_payment(
        opportunity_id=opportunity_id,
        payment_id="pay_4",
        adapter=adapter,
    )

    assert observation.state == PaymentVerificationState.INDETERMINATE

    with pytest.raises(ValueError, match="requires VERIFIED"):
        inbox.record_verified_payment(observation)

    assert inbox.capital_snapshot().verified_customer_revenue_usd == 0.0


def test_unknown_opportunity_is_refused_before_adapter_observation():
    inbox = RevenueInbox()
    adapter = SimulatedPaymentEvidenceAdapter()

    with pytest.raises(KeyError, match="not found"):
        inbox.observe_payment(
            opportunity_id="missing",
            payment_id="pay_5",
            adapter=adapter,
        )
