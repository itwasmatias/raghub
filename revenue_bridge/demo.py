"""Creator Test Drive for MissionaryX Revenue Bridge v0.1.

Demonstrates the complete end-to-end governed revenue workflow (Steps A through J):
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
    # Print Phone-Friendly Revenue Bridge Status Report
    # ───────────────────────────────────────────────────────────────────────────
    if verbose:
        print("\n" + "=" * 70)
        print("  CREATOR PHONE-FRIENDLY REVENUE STATUS REPORT")
        print("=" * 70)
        print(inbox.format_phone_status(opp_gh.opportunity_id))
        print("\n[TEST DRIVE RESULT] ALL 10 STEPS A-J COMPLETED AND VERIFIED DETERMINISTICALLY.\n")

    return True


def main() -> None:
    """CLI entrypoint."""
    success = run_creator_test_drive(verbose=True)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
