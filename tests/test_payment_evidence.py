import pytest

from revenue_bridge.contribution import PaymentVerificationState
from revenue_bridge.payment import (
    PaymentObservationMode,
    SimulatedPaymentEvidenceAdapter,
)


def test_verified_mode_produces_verified_observation():
    adapter = SimulatedPaymentEvidenceAdapter(
        mode=PaymentObservationMode.VERIFIED
    )

    observation = adapter.observe(
        payment_id="pay_1",
        opportunity_id="opp_1",
    )

    assert observation.state == PaymentVerificationState.VERIFIED
    assert observation.amount_usd == 50.0


def test_unverified_mode_stays_unverified():
    adapter = SimulatedPaymentEvidenceAdapter(
        mode=PaymentObservationMode.UNVERIFIED
    )

    observation = adapter.observe(
        payment_id="pay_2",
        opportunity_id="opp_1",
    )

    assert observation.state == PaymentVerificationState.UNVERIFIED


def test_indeterminate_mode_stays_indeterminate():
    adapter = SimulatedPaymentEvidenceAdapter(
        mode=PaymentObservationMode.INDETERMINATE
    )

    observation = adapter.observe(
        payment_id="pay_3",
        opportunity_id="opp_1",
    )

    assert observation.state == PaymentVerificationState.INDETERMINATE


def test_observation_contains_evidence_and_provider_reference():
    adapter = SimulatedPaymentEvidenceAdapter(
        provider_name="simulated_provider"
    )

    observation = adapter.observe(
        payment_id="pay_4",
        opportunity_id="opp_1",
    )

    assert observation.evidence_ref.startswith("payev_")
    assert observation.provider_reference == "simulated_provider:pay_4"


def test_negative_payment_amount_is_refused():
    adapter = SimulatedPaymentEvidenceAdapter(observed_amount_usd=-1)

    with pytest.raises(ValueError, match="must be >= 0"):
        adapter.observe(
            payment_id="pay_5",
            opportunity_id="opp_1",
        )


def test_caller_cannot_inject_claimed_payment_amount():
    adapter = SimulatedPaymentEvidenceAdapter(observed_amount_usd=50)

    with pytest.raises(TypeError):
        adapter.observe(
            payment_id="pay_claim",
            opportunity_id="opp_1",
            amount_usd=5000,
        )


def test_blank_payment_id_is_refused():
    adapter = SimulatedPaymentEvidenceAdapter()

    with pytest.raises(ValueError):
        adapter.observe(
            payment_id="",
            opportunity_id="opp_1",
            )


def test_adapter_mode_must_be_explicit_enum():
    with pytest.raises(TypeError, match="PaymentObservationMode"):
        SimulatedPaymentEvidenceAdapter(mode="verified")


def test_payment_observer_has_no_financial_execution_authority():
    adapter = SimulatedPaymentEvidenceAdapter()

    assert not hasattr(adapter, "charge")
    assert not hasattr(adapter, "refund")
    assert not hasattr(adapter, "transfer")
    assert not hasattr(adapter, "send_payment")
