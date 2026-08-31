from dataclasses import replace

import pytest

from revenue_bridge.capital import (
    CapitalDecision,
    CapitalGovernor,
    CapitalMode,
    CapitalPolicy,
    CapitalSnapshot,
    SpendProposal,
    SpendTier,
)


def proposal(
    spend: float,
    *,
    tier: SpendTier = SpendTier.NORMAL,
    expected: float = 20.0,
) -> SpendProposal:
    return SpendProposal(
        proposal_id="spend_001",
        opportunity_id="opp_001",
        requested_spend_usd=spend,
        spend_tier=tier,
        expected_contribution_usd=expected,
        acquisition_channel="github",
        purpose="Acquire one bounded Reliability Check customer",
    )


def test_frozen_v01_policy_limits():
    policy = CapitalPolicy()
    assert policy.total_loss_ceiling_usd == 60.0
    assert policy.caution_loss_threshold_usd == 40.0
    assert policy.normal_per_opportunity_limit_usd == 5.0
    assert policy.exception_per_opportunity_limit_usd == 10.0
    assert policy.daily_acquisition_spend_limit_usd == 20.0


def test_verified_revenue_offsets_costs_not_gross_spend():
    snapshot = CapitalSnapshot(
        acquisition_spend_usd=45,
        fulfillment_cost_usd=4,
        model_api_cost_usd=4,
        payment_platform_fees_usd=2,
        verified_customer_revenue_usd=100,
    )
    assert snapshot.total_cost_usd == 55.0
    assert snapshot.net_contribution_usd == 45.0
    assert snapshot.unrecovered_loss_usd == 0.0
    assert snapshot.mode() == CapitalMode.NORMAL


def test_caution_mode_starts_at_40_unrecovered_loss():
    snapshot = CapitalSnapshot(acquisition_spend_usd=40)
    assert snapshot.mode() == CapitalMode.CAUTION


def test_hard_stop_starts_at_60_unrecovered_loss():
    snapshot = CapitalSnapshot(acquisition_spend_usd=60)
    assert snapshot.mode() == CapitalMode.HARD_STOP


def test_normal_opportunity_cannot_exceed_5():
    result = CapitalGovernor().evaluate(
        CapitalSnapshot(),
        proposal(5.01),
    )
    assert result.decision == CapitalDecision.REJECT


def test_exception_opportunity_cannot_exceed_10():
    result = CapitalGovernor().evaluate(
        CapitalSnapshot(),
        proposal(10.01, tier=SpendTier.EXCEPTION),
    )
    assert result.decision == CapitalDecision.REJECT


def test_daily_acquisition_ceiling_is_enforced():
    result = CapitalGovernor().evaluate(
        CapitalSnapshot(today_acquisition_spend_usd=16),
        proposal(5),
    )
    assert result.decision == CapitalDecision.REJECT


def test_projected_loss_cannot_exceed_60():
    result = CapitalGovernor().evaluate(
        CapitalSnapshot(acquisition_spend_usd=56),
        proposal(5),
    )
    assert result.decision == CapitalDecision.REJECT
    assert result.projected_unrecovered_loss_usd == 61.0


def test_spend_that_reaches_exactly_60_can_only_be_proposed():
    result = CapitalGovernor().evaluate(
        CapitalSnapshot(acquisition_spend_usd=55),
        proposal(5),
    )
    assert result.decision == CapitalDecision.PROPOSE_SPEND
    assert result.projected_unrecovered_loss_usd == 60.0
    assert "no spending authority granted" in result.reason


def test_hard_stop_refuses_all_further_paid_acquisition():
    result = CapitalGovernor().evaluate(
        CapitalSnapshot(acquisition_spend_usd=60),
        proposal(1),
    )
    assert result.mode == CapitalMode.HARD_STOP
    assert result.decision == CapitalDecision.REJECT


def test_caution_mode_demands_stronger_expected_economics():
    governor = CapitalGovernor()
    snapshot = CapitalSnapshot(acquisition_spend_usd=40)

    weak = governor.evaluate(snapshot, proposal(5, expected=9.99))
    strong = governor.evaluate(snapshot, proposal(5, expected=10.00))

    assert weak.decision == CapitalDecision.REJECT
    assert strong.decision == CapitalDecision.PROPOSE_SPEND


def test_non_positive_expected_contribution_never_justifies_paid_acquisition():
    result = CapitalGovernor().evaluate(
        CapitalSnapshot(),
        proposal(5, expected=0),
    )
    assert result.decision == CapitalDecision.REJECT


def test_zero_cost_opportunity_is_free_only():
    result = CapitalGovernor().evaluate(
        CapitalSnapshot(),
        proposal(0, expected=0),
    )
    assert result.decision == CapitalDecision.FREE_ONLY

def test_spend_proposal_always_requires_creator_approval():
    with pytest.raises(ValueError, match="MUST require creator approval"):
        replace(proposal(5), requires_creator_approval=False)


def test_proposal_fingerprint_binds_exact_spend():
    first = proposal(5)
    changed = replace(first, requested_spend_usd=4.99)
    assert first.fingerprint != changed.fingerprint


def test_proposal_serialization_roundtrip_preserves_fingerprint():
    first = proposal(5, expected=18)
    restored = SpendProposal.from_dict(first.to_dict())
    assert restored.fingerprint == first.fingerprint


def test_personal_payroll_is_not_a_capital_snapshot_input():
    with pytest.raises(TypeError):
        CapitalSnapshot(payroll_usd=350)  # type: ignore[call-arg]
