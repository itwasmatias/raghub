"""Creator Test Drive for MissionaryX Revenue Bridge v0.1.

Demonstrates the complete end-to-end governed revenue workflow (Steps A through N):
  A. GitHub-like opportunity arrives
  B. Normalized to AppEvent
  C. Revenue qualifier identifies exact evidence
  D. Proposal is prepared
  E. Dispatch before approval is refused
  F. Creator approval is recorded
  G. Simulated adapter executes
  H. Evidence records what actually happened
  I. Indeterminate scenario does not blindly retry
  J. Email-shaped event follows the same common contract
  K. Explicit opportunity economics are recorded
  L. Capital governor evaluates acquisition eligibility
  M. Simulated realized costs and payment evidence remain separate
  N. Verified payment updates contribution and unrecovered loss

Can be executed with a single command:
  python -m revenue_bridge.demo
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from revenue_bridge.approval import (
    ApprovalDecision,
    ApprovalInvalidatedError,
    ApprovalRequiredError,
    CreatorApprovalBoundary,
)
from revenue_bridge.capital import CapitalDecision
from revenue_bridge.contribution import ContributionKind
from revenue_bridge.economics import OpportunityEconomics
from revenue_bridge.effects import (
    ApprovedAppAction,
    BlindRetryRefusedError,
    EffectOutcomeMode,
    EffectState,
    SimulatedAppEffectAdapter,
)
from revenue_bridge.email import EmailInboundNormalizer
from revenue_bridge.events import AppEvent
from revenue_bridge.github import GitHubInboundNormalizer
from revenue_bridge.inbox import RevenueInbox
from revenue_bridge.payment import (
    PaymentObservationMode,
    SimulatedPaymentEvidenceAdapter,
)
from revenue_bridge.proposals import (
    ActionProposal,
    ActionProposalState,
    compile_action_proposal,
)
from revenue_bridge.qualifier import (
    BoundedOffer,
    RevenueFitDecision,
    RevenueQualifier,
)


def run_creator_test_drive(verbose: bool = True) -> bool:
    """Execute the deterministic creator test drive covering all requirements A through J."""
    if verbose:
        print("\n" + "=" * 70)
        print("  MISSIONARYX REVENUE BRIDGE v0.1 — CREATOR TEST DRIVE")
        print("=" * 70 + "\n")

    boundary = CreatorApprovalBoundary()
    qualifier = RevenueQualifier()
    inbox = RevenueInbox(qualifier=qualifier, approval_boundary=boundary)

    # ───────────────────────────────────────────────────────────────────────────
    # STEP A: GitHub-like opportunity arrives
    # ───────────────────────────────────────────────────────────────────────────
    if verbose:
        print("[STEP A] Ingesting raw GitHub opportunity fixture...")
    raw_gh_issue = {
        "id": 88401,
        "number": 88401,
        "html_url": "https://github.com/ai-infra/agent-hub/issues/88401",
        "title": "Claude Code interrupted agent work on Linux",
        "body": (
            "We have severe automation failures when running Claude Code under Linux development tooling. "
            "The autonomous agent crashes during Python automation tasks and loses context state. "
            "We need reliable verification of AI-generated code."
        ),
        "user": {"login": "sarah_eng", "id": 55102},
        "created_at": "2026-08-31T16:00:00Z",
    }

    # ───────────────────────────────────────────────────────────────────────────
    # STEP B: Normalized to AppEvent
    # ───────────────────────────────────────────────────────────────────────────
    if verbose:
        print("[STEP B] Normalizing to common AppEvent contract...")
    event_gh = GitHubInboundNormalizer.normalize_issue(raw_gh_issue)
    assert isinstance(event_gh, AppEvent)
    assert event_gh.source_app == "github"
    assert event_gh.actor == "sarah_eng"
    assert "github.issue_comment.reply" in event_gh.capabilities
    assert event_gh.raw_evidence_ref.startswith("raw_gh_issue_")
    if verbose:
        print(f"  ✓ Normalized Event ID: {event_gh.event_id}")
        print(f"  ✓ Fingerprint:        {event_gh.fingerprint[:16]}...")
        print(f"  ✓ Preserved Raw Ref:  {event_gh.raw_evidence_ref}")

    # ───────────────────────────────────────────────────────────────────────────
    # STEP C: Revenue qualifier identifies exact evidence
    # ───────────────────────────────────────────────────────────────────────────
    if verbose:
        print("\n[STEP C] Running bounded revenue qualifier...")
    opp_gh = inbox.ingest(event_gh)
    q_gh = opp_gh.qualification
    assert q_gh.fit_decision == RevenueFitDecision.QUALIFIED
    assert len(q_gh.verified_evidence) > 0
    assert len(q_gh.unknown_information) > 0
    assert q_gh.proposed_offer is not None
    assert q_gh.proposed_offer.price_usd == 50.0
    if verbose:
        print(f"  ✓ Fit Decision:       {q_gh.fit_decision.value.upper()}")
        print(f"  ✓ Verified Pain:      {len(q_gh.verified_evidence)} verified excerpts")
        for p in q_gh.verified_evidence:
            print(f"      • {p}")
        print(f"  ✓ Unknowns (Honest):  {len(q_gh.unknown_information)} items explicitly marked unknown")
        print(f"  ✓ Proposed Offer:     {q_gh.proposed_offer.name} (${int(q_gh.proposed_offer.price_usd)})")

    # ───────────────────────────────────────────────────────────────────────────
    # STEP D: Proposal is prepared
    # ───────────────────────────────────────────────────────────────────────────
    if verbose:
        print("\n[STEP D] Preparing truthful ActionProposal...")
    proposal_gh = opp_gh.proposal
    assert proposal_gh is not None
    assert proposal_gh.requires_creator_approval is True
    assert proposal_gh.state == ActionProposalState.PROPOSED
    assert "sarah_eng" in proposal_gh.proposed_content
    assert "$50" in proposal_gh.proposed_content
    if verbose:
        print(f"  ✓ Proposal ID:        {proposal_gh.proposal_id}")
        print(f"  ✓ Target Resource:    {proposal_gh.target_resource}")
        print(f"  ✓ Required Cap:       {proposal_gh.required_capability}")
        print(f"  ✓ Approval Required:  {proposal_gh.requires_creator_approval}")
        print(f"  ✓ Proposal Fingerprint: {proposal_gh.proposal_fingerprint[:16]}...")

    # ───────────────────────────────────────────────────────────────────────────
    # STEP E: Dispatch before approval is refused
    # ───────────────────────────────────────────────────────────────────────────
    if verbose:
        print("\n[STEP E] Safety Check: Attempting dispatch BEFORE creator approval...")
    adapter_gh = SimulatedAppEffectAdapter(
        adapter_name="simulated_github_adapter",
        approval_boundary=boundary,
    )
    unapproved_caught = False
    try:
        inbox.dispatch(opp_gh.opportunity_id, adapter_gh)
    except ApprovalRequiredError as exc:
        unapproved_caught = True
        if verbose:
            print(f"  ✓ Dispatch successfully refused by safety boundary: {exc}")
    assert unapproved_caught, "Safety violation: Dispatch without approval was not refused!"

    # ───────────────────────────────────────────────────────────────────────────
    # STEP F: Creator approval is recorded
    # ───────────────────────────────────────────────────────────────────────────
    if verbose:
        print("\n[STEP F] Recording affirmative creator approval...")
    approval_record = inbox.approve(
        opportunity_id=opp_gh.opportunity_id,
        creator_identity="creator:matias",
        reason="Approved bounded MissionaryX Reliability Check proposal",
    )
    assert approval_record.is_approved
    assert approval_record.proposal_fingerprint == proposal_gh.proposal_fingerprint
    if verbose:
        print(f"  ✓ Approval ID:        {approval_record.approval_id}")
        print(f"  ✓ Creator:            {approval_record.creator_identity}")
        print(f"  ✓ Bound Fingerprint:  {approval_record.proposal_fingerprint[:16]}...")

    # Verification: If proposal content changes after approval, approval is invalidated
    tampered_proposal = proposal_gh.with_content("Tampered content promising free consulting")
    tamper_caught = False
    try:
        boundary.verify_approval(tampered_proposal, approval_record)
    except ApprovalInvalidatedError as exc:
        tamper_caught = True
        if verbose:
            print(f"  ✓ Tamper check passed: Material modification invalidated prior approval: {exc.__class__.__name__}")
    assert tamper_caught, "Safety violation: Tampered proposal did not invalidate approval!"

    # ───────────────────────────────────────────────────────────────────────────
    # STEP G: Simulated adapter executes
    # ───────────────────────────────────────────────────────────────────────────
    if verbose:
        print("\n[STEP G] Executing dispatch through simulated adapter...")
    effect_result = inbox.dispatch(opp_gh.opportunity_id, adapter_gh)
    assert effect_result.state == EffectState.SOMETHING_LANDED
    assert effect_result.is_landed
    if verbose:
        print(f"  ✓ Effect Intent ID:   {effect_result.effect_intent_id}")
        print(f"  ✓ Dispatched State:   {effect_result.state.value.upper()}")

    # ───────────────────────────────────────────────────────────────────────────
    # STEP H: Evidence records what actually happened
    # ───────────────────────────────────────────────────────────────────────────
    if verbose:
        print("\n[STEP H] Verifying cryptographic execution evidence...")
    assert effect_result.evidence_ref.startswith("ev_landed_")
    if verbose:
        print(f"  ✓ Evidence Ref:       {effect_result.evidence_ref}")
        print(f"  ✓ Dispatched At:      {effect_result.dispatched_at.isoformat()}")
        print(f"  ✓ Execution Details:  {dict(effect_result.details)}")

    # ───────────────────────────────────────────────────────────────────────────
    # STEP I: Indeterminate scenario does not blindly retry; reconciles cleanly
    # ───────────────────────────────────────────────────────────────────────────
    if verbose:
        print("\n[STEP I] Testing indeterminate connection-loss scenario & reconciliation...")
    # Create second event for indeterminate test
    raw_gh_issue_2 = {
        "id": 88402,
        "number": 88402,
        "html_url": "https://github.com/ai-infra/agent-hub/issues/88402",
        "title": "Gemini automation flakiness and agent crash",
        "body": "Python automation bot crashes repeatedly when invoking Gemini models.",
        "user": {"login": "marcus_dev", "id": 55103},
        "created_at": "2026-08-31T16:15:00Z",
    }
    event_gh_2 = GitHubInboundNormalizer.normalize_issue(raw_gh_issue_2)
    opp_2 = inbox.ingest(event_gh_2)
    inbox.approve(opp_2.opportunity_id, "creator:matias")

    # Set adapter to connection loss (indeterminate)
    adapter_indet = SimulatedAppEffectAdapter(
        adapter_name="simulated_flaky_adapter",
        default_mode=EffectOutcomeMode.CONNECTION_LOSS,
        approval_boundary=boundary,
    )
    effect_indet = inbox.dispatch(opp_2.opportunity_id, adapter_indet)
    assert effect_indet.state == EffectState.INDETERMINATE
    assert effect_indet.reconciliation_obligation_id is not None
    if verbose:
        print(f"  ✓ Dispatched with outcome: INDETERMINATE")
        print(f"  ✓ Obligation Created:     {effect_indet.reconciliation_obligation_id}")

    # Attempt blind retry -> MUST BE REFUSED
    blind_retry_caught = False
    try:
        inbox.dispatch(opp_2.opportunity_id, adapter_indet)
    except BlindRetryRefusedError as exc:
        blind_retry_caught = True
        if verbose:
            print(f"  ✓ Blind retry successfully blocked: {exc}")
    assert blind_retry_caught, "Safety violation: Blind retry on indeterminate effect was not blocked!"

    # Now reconcile authoritative ground truth
    adapter_indet.configure_reconciliation_resolution(
        effect_indet.effect_intent_id,
        EffectState.SOMETHING_LANDED,
    )
    recon_result = inbox.reconcile(opp_2.opportunity_id, adapter_indet)
    assert recon_result.resolved_state == EffectState.SOMETHING_LANDED
    if verbose:
        print(f"  ✓ Reconciled Result:      {recon_result.resolved_state.value.upper()}")
        print(f"  ✓ Reconciliation Evidence:{recon_result.evidence_ref}")

    # ───────────────────────────────────────────────────────────────────────────
    # STEP J: Email-shaped event follows the same common contract
    # ───────────────────────────────────────────────────────────────────────────
    if verbose:
        print("\n[STEP J] Testing Email application shape under same common contracts...")
    event_email = EmailInboundNormalizer.load_fixture("python_automation")
    assert isinstance(event_email, AppEvent)
    assert event_email.source_app == "email"
    assert "email.reply.send" in event_email.capabilities

    opp_email = inbox.ingest(event_email)
    assert opp_email.is_qualified
    assert opp_email.proposal is not None
    assert opp_email.proposal.required_capability == "email.reply.send"

    inbox.approve(opp_email.opportunity_id, "creator:matias")
    adapter_email = SimulatedAppEffectAdapter(
        adapter_name="simulated_email_adapter",
        supported_capabilities={"email.reply.send"},
        approval_boundary=boundary,
    )
    effect_email = inbox.dispatch(opp_email.opportunity_id, adapter_email)
    assert effect_email.is_landed
    if verbose:
        print(f"  ✓ Email Event Normalized: {event_email.event_id}")
        print(f"  ✓ Email Qualified:        {opp_email.qualification.fit_decision.value.upper()}")
        print(f"  ✓ Email Proposal:         {opp_email.proposal.proposal_id}")
        print(f"  ✓ Email Approved & Sent:  {effect_email.state.value.upper()} (Evidence: {effect_email.evidence_ref})")

    # ───────────────────────────────────────────────────────────────────────────
    # STEP K: Record explicit opportunity economics
    # ───────────────────────────────────────────────────────────────────────────
    if verbose:
        print("\n[STEP K] Recording explicit Profit Loop opportunity economics...")

    economics = OpportunityEconomics(
        opportunity_id=opp_gh.opportunity_id,
        sale_probability=0.25,
        offer_price_usd=50.0,
        proposed_acquisition_cost_usd=5.0,
        estimated_fulfillment_cost_usd=4.0,
        estimated_model_api_cost_usd=2.0,
        estimated_payment_platform_fees_usd=1.0,
        evidence_refs=(f"event:{event_gh.event_id}",),
        assumptions=(
            "25% sale probability is a deterministic demo estimate, not observed customer intent",
        ),
    )
    inbox.record_economics(opp_gh.opportunity_id, economics)

    assert economics.estimated_margin_if_sold_usd == 43.0
    assert economics.expected_contribution_usd == 5.75

    if verbose:
        print(f"  ✓ Offer Price:             ${economics.offer_price_usd:.2f}")
        print(f"  ✓ Expected Contribution:  ${economics.expected_contribution_usd:.2f}")
        print("  ✓ Sale probability explicitly labeled as an estimate")

    # ───────────────────────────────────────────────────────────────────────────
    # STEP L: Capital governor evaluates eligibility but does not spend
    # ───────────────────────────────────────────────────────────────────────────
    if verbose:
        print("\n[STEP L] Evaluating acquisition against capital governor...")

    spend_proposal, capital_evaluation = inbox.evaluate_capital(
        opp_gh.opportunity_id
    )

    assert spend_proposal.requested_spend_usd == 5.0
    assert spend_proposal.requires_creator_approval
    assert capital_evaluation.decision == CapitalDecision.PROPOSE_SPEND

    if verbose:
        print(f"  ✓ Proposed Acquisition:   ${spend_proposal.requested_spend_usd:.2f}")
        print(f"  ✓ Governor Decision:      {capital_evaluation.decision.value.upper()}")
        print("  ✓ No spending authority granted")

    # ───────────────────────────────────────────────────────────────────────────
    # STEP M: Record simulated realized costs, then observe payment evidence
    # ───────────────────────────────────────────────────────────────────────────
    if verbose:
        print("\n[STEP M] Recording SIMULATED realized costs and observing payment...")

    simulated_time = datetime(2026, 8, 31, 18, 0, tzinfo=timezone.utc)
    simulated_costs = (
        ("demo_acq_1", ContributionKind.ACQUISITION_COST, 5.0, "demo:simulated:acquisition"),
        ("demo_fulfill_1", ContributionKind.FULFILLMENT_COST, 4.0, "demo:simulated:fulfillment"),
        ("demo_api_1", ContributionKind.MODEL_API_COST, 2.0, "demo:simulated:model_api"),
        ("demo_fee_1", ContributionKind.PAYMENT_PLATFORM_FEE, 1.0, "demo:simulated:payment_fee"),
    )

    for entry_id, kind, amount, evidence_ref in simulated_costs:
        inbox.contribution_ledger.record_cost(
            entry_id=entry_id,
            opportunity_id=opp_gh.opportunity_id,
            kind=kind,
            amount_usd=amount,
            evidence_ref=evidence_ref,
            occurred_at=simulated_time,
        )

    before_payment = inbox.capital_snapshot()
    assert before_payment.total_cost_usd == 12.0
    assert before_payment.verified_customer_revenue_usd == 0.0
    assert before_payment.unrecovered_loss_usd == 12.0

    payment_adapter = SimulatedPaymentEvidenceAdapter(
        mode=PaymentObservationMode.VERIFIED,
        provider_name="simulated_profit_loop_provider",
        observed_amount_usd=50.0,
    )
    payment_observation = inbox.observe_payment(
        opportunity_id=opp_gh.opportunity_id,
        payment_id="demo_payment_1",
        adapter=payment_adapter,
    )

    # Observation alone MUST NOT alter accounting.
    assert payment_observation.amount_usd == 50.0
    assert inbox.capital_snapshot().verified_customer_revenue_usd == 0.0

    if verbose:
        print(f"  ✓ Simulated Costs:        ${before_payment.total_cost_usd:.2f}")
        print(f"  ✓ Unrecovered Before Pay:${before_payment.unrecovered_loss_usd:.2f}")
        print("  ✓ Payment observed, but accounting unchanged")

    # ───────────────────────────────────────────────────────────────────────────
    # STEP N: Verified payment becomes contribution evidence
    # ───────────────────────────────────────────────────────────────────────────
    if verbose:
        print("\n[STEP N] Recording verified simulated customer payment...")

    inbox.record_verified_payment(payment_observation)
    after_payment = inbox.capital_snapshot()

    assert after_payment.verified_customer_revenue_usd == 50.0
    assert after_payment.total_cost_usd == 12.0
    assert after_payment.net_contribution_usd == 38.0
    assert after_payment.unrecovered_loss_usd == 0.0

    if verbose:
        print(f"  ✓ Verified Revenue:       ${after_payment.verified_customer_revenue_usd:.2f}")
        print(f"  ✓ Total Simulated Costs:  ${after_payment.total_cost_usd:.2f}")
        print(f"  ✓ Net Contribution:      ${after_payment.net_contribution_usd:.2f}")
        print(f"  ✓ Unrecovered Loss:      ${after_payment.unrecovered_loss_usd:.2f}")

    # ───────────────────────────────────────────────────────────────────────────
    # Print Phone-Friendly Revenue Bridge Status Report
    # ───────────────────────────────────────────────────────────────────────────
    if verbose:
        print("\n" + "=" * 70)
        print("  CREATOR PHONE-FRIENDLY REVENUE STATUS REPORT")
        print("=" * 70)
        print(inbox.format_phone_status(opp_gh.opportunity_id))
        print("\n[TEST DRIVE RESULT] ALL 14 STEPS A-N COMPLETED AND VERIFIED DETERMINISTICALLY.\n")

    return True


def main() -> None:
    """CLI entrypoint."""
    success = run_creator_test_drive(verbose=True)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
