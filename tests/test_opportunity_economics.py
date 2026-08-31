from dataclasses import replace

import pytest

from revenue_bridge.economics import OpportunityEconomics
from revenue_bridge.github import GitHubInboundNormalizer
from revenue_bridge.inbox import RevenueInbox


def economics(
    opportunity_id: str = "ev_gh_issue_10928374",
    *,
    probability: float = 0.25,
    acquisition: float = 5.0,
) -> OpportunityEconomics:
    return OpportunityEconomics(
        opportunity_id=opportunity_id,
        sale_probability=probability,
        offer_price_usd=50.0,
        proposed_acquisition_cost_usd=acquisition,
        estimated_fulfillment_cost_usd=4.0,
        estimated_model_api_cost_usd=2.0,
        estimated_payment_platform_fees_usd=1.0,
        evidence_refs=("event:verified",),
        assumptions=("sale_probability is an explicit estimate, not observed fact",),
    )


def test_expected_contribution_formula():
    record = economics()
    assert record.estimated_margin_if_sold_usd == 43.0
    assert record.expected_sale_revenue_usd == 12.5
    assert record.expected_contribution_usd == 5.75


def test_break_even_probability_is_explicit():
    assert economics().break_even_sale_probability == 0.1163


def test_negative_expected_contribution_is_visible():
    record = economics(probability=0.05, acquisition=5)
    assert record.expected_contribution_usd == -2.85
    assert record.is_expected_positive is False


def test_probability_bounds_are_enforced():
    with pytest.raises(ValueError, match="between 0 and 1"):
        economics(probability=1.01)

    with pytest.raises(ValueError, match="between 0 and 1"):
        economics(probability=-0.01)


def test_costs_cannot_be_negative():
    with pytest.raises(ValueError, match="must be >= 0"):
        OpportunityEconomics(
            opportunity_id="opp",
            sale_probability=0.5,
            offer_price_usd=50,
            proposed_acquisition_cost_usd=-1,
            estimated_fulfillment_cost_usd=0,
            estimated_model_api_cost_usd=0,
        )


def test_roundtrip_preserves_fingerprint():
    first = economics()
    restored = OpportunityEconomics.from_dict(first.to_dict())

    assert restored.fingerprint == first.fingerprint
    assert restored.expected_contribution_usd == first.expected_contribution_usd


def test_material_estimate_change_changes_fingerprint():
    first = economics()
    changed = replace(first, sale_probability=0.30)

    assert changed.fingerprint != first.fingerprint


def test_ingestion_does_not_fabricate_economics():
    inbox = RevenueInbox()
    event = GitHubInboundNormalizer.load_fixture("claude_code")
    opp = inbox.ingest(event)

    assert opp.is_qualified
    assert opp.economics is None


def test_explicit_economics_can_be_attached():
    inbox = RevenueInbox()
    event = GitHubInboundNormalizer.load_fixture("claude_code")
    opp = inbox.ingest(event)
    record = economics(opportunity_id=opp.opportunity_id)

    stored = inbox.record_economics(opp.opportunity_id, record)

    assert stored is record
    assert opp.economics is record
    assert opp.economics.expected_contribution_usd == 5.75


def test_mismatched_opportunity_id_is_rejected():
    inbox = RevenueInbox()
    event = GitHubInboundNormalizer.load_fixture("claude_code")
    opp = inbox.ingest(event)

    with pytest.raises(ValueError, match="does not match"):
        inbox.record_economics(
            opp.opportunity_id,
            economics("wrong-opportunity"),
        )


def test_unqualified_opportunity_cannot_receive_economics():
    inbox = RevenueInbox()
    event = GitHubInboundNormalizer.load_fixture("non_relevant")
    opp = inbox.ingest(event)

    with pytest.raises(ValueError, match="unqualified"):
        inbox.record_economics(
            opp.opportunity_id,
            economics(opportunity_id=opp.opportunity_id),
        )


def test_economics_record_has_no_execution_authority():
    record = economics()

    assert not hasattr(record, "dispatch")
    assert not hasattr(record, "approve")


def test_capital_evaluation_requires_explicit_economics():
    from revenue_bridge.capital import CapitalSnapshot

    inbox = RevenueInbox()
    event = GitHubInboundNormalizer.load_fixture("claude_code")
    opp = inbox.ingest(event)

    with pytest.raises(ValueError, match="economics must be recorded"):
        inbox.evaluate_capital(opp.opportunity_id, CapitalSnapshot())


def test_positive_economics_can_only_propose_spend():
    from revenue_bridge.capital import CapitalDecision, CapitalSnapshot

    inbox = RevenueInbox()
    event = GitHubInboundNormalizer.load_fixture("claude_code")
    opp = inbox.ingest(event)
    inbox.record_economics(opp.opportunity_id, economics(opp.opportunity_id))

    spend_proposal, evaluation = inbox.evaluate_capital(
        opp.opportunity_id,
        CapitalSnapshot(),
    )

    assert spend_proposal.requested_spend_usd == 5.0
    assert spend_proposal.expected_contribution_usd == 5.75
    assert spend_proposal.requires_creator_approval is True
    assert evaluation.decision == CapitalDecision.PROPOSE_SPEND
    assert "no spending authority granted" in evaluation.reason


def test_negative_unit_economics_are_rejected_by_governor():
    from revenue_bridge.capital import CapitalDecision, CapitalSnapshot

    inbox = RevenueInbox()
    event = GitHubInboundNormalizer.load_fixture("claude_code")
    opp = inbox.ingest(event)
    inbox.record_economics(
        opp.opportunity_id,
        economics(opp.opportunity_id, probability=0.05),
    )

    _, evaluation = inbox.evaluate_capital(
        opp.opportunity_id,
        CapitalSnapshot(),
    )

    assert evaluation.decision == CapitalDecision.REJECT


def test_exception_tier_must_be_explicit():
    from revenue_bridge.capital import (
        CapitalDecision,
        CapitalSnapshot,
        SpendTier,
    )

    inbox = RevenueInbox()
    event = GitHubInboundNormalizer.load_fixture("claude_code")
    opp = inbox.ingest(event)
    inbox.record_economics(
        opp.opportunity_id,
        economics(opp.opportunity_id, probability=0.40, acquisition=8.0),
    )

    _, normal = inbox.evaluate_capital(
        opp.opportunity_id,
        CapitalSnapshot(),
    )
    _, exception = inbox.evaluate_capital(
        opp.opportunity_id,
        CapitalSnapshot(),
        spend_tier=SpendTier.EXCEPTION,
    )

    assert normal.decision == CapitalDecision.REJECT
    assert exception.decision == CapitalDecision.PROPOSE_SPEND


def test_capital_proposal_fingerprint_binds_recorded_economics():
    from revenue_bridge.capital import CapitalSnapshot

    inbox = RevenueInbox()
    event = GitHubInboundNormalizer.load_fixture("claude_code")
    opp = inbox.ingest(event)

    first = economics(opp.opportunity_id, probability=0.25)
    inbox.record_economics(opp.opportunity_id, first)
    first_proposal, _ = inbox.evaluate_capital(
        opp.opportunity_id,
        CapitalSnapshot(),
    )

    changed = replace(first, sale_probability=0.30)
    inbox.record_economics(opp.opportunity_id, changed)
    changed_proposal, _ = inbox.evaluate_capital(
        opp.opportunity_id,
        CapitalSnapshot(),
    )

    assert first_proposal.proposal_id != changed_proposal.proposal_id
    assert first_proposal.fingerprint != changed_proposal.fingerprint
