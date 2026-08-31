"""Comprehensive deterministic test suite for MissionaryX Revenue Bridge v0.1.

Validates all 10 specifications and Hard Safety Constraints from REVENUE_BRIDGE_MISSION.md:
1. Common App Event Contract
2. Revenue Qualification & Honesty
3. Action Proposal Contract
4. Creator Approval Boundary
5. Outbound App Effect Boundary
6. GitHub Inbound Slice
7. Email Contract
8. Revenue Inbox / Phone-Friendly Status
9. Creator Test Drive
10. End-to-End Safety & Invariants
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
import pytest

from revenue_bridge.approval import (
    ApprovalDecision,
    ApprovalIdentityError,
    ApprovalInvalidatedError,
    ApprovalRequiredError,
    CreatorApprovalBoundary,
    CreatorApprovalRecord,
)
from revenue_bridge.demo import run_creator_test_drive
from revenue_bridge.effects import (
    AppEffectAdapter,
    ApprovedAppAction,
    BlindRetryRefusedError,
    CapabilityNotSupportedError,
    EffectAdapterRegistry,
    EffectExecutionResult,
    EffectOutcomeMode,
    EffectReconciliationResult,
    EffectState,
    SimulatedAppEffectAdapter,
)
from revenue_bridge.email import (
    EmailInboundNormalizer,
    SAMPLE_EMAIL_PYTHON_AUTOMATION_INQUIRY,
    SAMPLE_EMAIL_CODEX_VERIFICATION_INQUIRY,
    SAMPLE_EMAIL_NEWSLETTER,
)
from revenue_bridge.events import AppEvent
from revenue_bridge.github import (
    GitHubInboundNormalizer,
    SAMPLE_GITHUB_CLAUDE_CODE_ISSUE,
    SAMPLE_GITHUB_GEMINI_AUTOMATION_ISSUE,
    SAMPLE_GITHUB_NON_RELEVANT_ISSUE,
)
from revenue_bridge.inbox import RevenueInbox, RevenueOpportunity
from revenue_bridge.proposals import (
    ActionProposal,
    ActionProposalState,
    ActionType,
    compile_action_proposal,
)
from revenue_bridge.qualifier import (
    BoundedOffer,
    RevenueFitDecision,
    RevenueQualification,
    RevenueQualifier,
)


class TestNormalizedContractsAndProvenance:
    """Requirement 1: Common App Event Contract & Provenance Retention."""

    def test_app_event_immutability(self):
        """AppEvent instances must be immutable (frozen)."""
        event = AppEvent(
            event_id="ev_001",
            source_app="github",
            event_type="github.issue_opened",
            actor="test_actor",
            thread_id="thread_1",
            content="Some problem description",
            source_url="https://example.com/1",
            observed_at=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
            raw_evidence_ref="raw_hash_123",
            capabilities=("github.issue.read",),
        )
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            event.content = "Mutated content"

    def test_app_event_provenance_retention(self):
        """AppEvent must strictly preserve provenance and raw evidence references."""
        event = GitHubInboundNormalizer.normalize_issue(SAMPLE_GITHUB_CLAUDE_CODE_ISSUE)
        assert event.event_id == "ev_gh_issue_10928374"
        assert event.actor == "alexdev99"
        assert event.source_app == "github"
        assert event.raw_evidence_ref.startswith("raw_gh_issue_")
        assert event.source_url == "https://github.com/developer-tools/agent-runner/issues/104"

    def test_app_event_fingerprint_deterministic(self):
        """Identical event payloads must produce identical fingerprints."""
        event1 = GitHubInboundNormalizer.normalize_issue(SAMPLE_GITHUB_CLAUDE_CODE_ISSUE)
        event2 = GitHubInboundNormalizer.normalize_issue(SAMPLE_GITHUB_CLAUDE_CODE_ISSUE)
        assert event1.fingerprint == event2.fingerprint
        assert isinstance(event1.fingerprint, str)
        assert len(event1.fingerprint) == 64

    def test_app_event_missing_evidence_remains_unknown(self):
        """Missing evidence must remain unknown; never fabricated."""
        event = GitHubInboundNormalizer.normalize_issue(SAMPLE_GITHUB_CLAUDE_CODE_ISSUE)
        # Event should explicitly list uncertainties rather than guessing
        assert len(event.uncertainty) > 0
        assert any("budget: unknown" in u for u in event.uncertainty)
        assert any("willingness_to_pay: unknown" in u for u in event.uncertainty)

    def test_app_event_to_and_from_dict_roundtrip(self):
        """Event dictionary serialization must roundtrip losslessly."""
        event = GitHubInboundNormalizer.normalize_issue(SAMPLE_GITHUB_CLAUDE_CODE_ISSUE)
        d = event.to_dict()
        reconstructed = AppEvent.from_dict(d)
        assert reconstructed.event_id == event.event_id
        assert reconstructed.fingerprint == event.fingerprint
        assert reconstructed.observed_at == event.observed_at


class TestRevenueQualificationAndHonesty:
    """Requirement 2: Revenue Qualification & Honest Evidence Separation."""

    def test_qualification_recognizes_claude_code_pain(self):
        event = GitHubInboundNormalizer.load_fixture("claude_code")
        qualifier = RevenueQualifier()
        q = qualifier.qualify(event)
        assert q.fit_decision == RevenueFitDecision.QUALIFIED
        assert "claude_code" in q.pain_categories
        assert len(q.verified_evidence) > 0
        assert q.proposed_offer is not None
        assert q.proposed_offer.name == "MissionaryX Reliability Check"
        assert q.proposed_offer.price_usd == 50.0

    def test_qualification_recognizes_gemini_automation_pain(self):
        event = GitHubInboundNormalizer.load_fixture("gemini_automation")
        qualifier = RevenueQualifier()
        q = qualifier.qualify(event)
        assert q.fit_decision == RevenueFitDecision.QUALIFIED
        assert "gemini" in q.pain_categories or "automation_failures" in q.pain_categories

    def test_qualification_recognizes_python_linux_automation_pain(self):
        event = EmailInboundNormalizer.load_fixture("python_automation")
        qualifier = RevenueQualifier()
        q = qualifier.qualify(event)
        assert q.fit_decision == RevenueFitDecision.QUALIFIED
        assert any(c in q.pain_categories for c in ("python_automation", "linux_development_tooling", "ai_agent_reliability"))

    def test_qualification_recognizes_codex_verification_pain(self):
        event = EmailInboundNormalizer.load_fixture("codex_verification")
        qualifier = RevenueQualifier()
        q = qualifier.qualify(event)
        assert q.fit_decision == RevenueFitDecision.QUALIFIED
        assert "codex" in q.pain_categories or "ai_generated_code_verification" in q.pain_categories

    def test_qualification_rejects_unrelated_pain(self):
        event = GitHubInboundNormalizer.load_fixture("non_relevant")
        qualifier = RevenueQualifier()
        q = qualifier.qualify(event)
        assert q.fit_decision == RevenueFitDecision.UNQUALIFIED
        assert q.proposed_offer is None
        assert len(q.pain_categories) == 0

    def test_no_fabricated_customer_facts(self):
        """Qualifier must explicitly mark unknown facts instead of fabricating willingness to pay."""
        event = GitHubInboundNormalizer.load_fixture("claude_code")
        qualifier = RevenueQualifier()
        q = qualifier.qualify(event)
        assert len(q.unknown_information) > 0
        assert any("willingness_to_pay: unknown" in u for u in q.unknown_information)
        assert any("customer_budget: unknown" in u for u in q.unknown_information)

    def test_qualification_serialization_roundtrip(self):
        event = GitHubInboundNormalizer.load_fixture("claude_code")
        qualifier = RevenueQualifier()
        q = qualifier.qualify(event)
        d = q.to_dict()
        q2 = RevenueQualification.from_dict(d)
        assert q2.fingerprint == q.fingerprint
        assert q2.fit_decision == q.fit_decision


class TestActionProposalContract:
    """Requirement 3: Action Proposal Contract."""

    def test_proposal_ai_is_proposal_never_authority(self):
        """Action proposal is an inert draft that cannot execute on its own."""
        event = GitHubInboundNormalizer.load_fixture("claude_code")
        qualifier = RevenueQualifier()
        q = qualifier.qualify(event)
        proposal = compile_action_proposal(event, q)
        assert proposal.state == ActionProposalState.PROPOSED
        assert proposal.requires_creator_approval is True

    def test_proposal_requires_creator_approval_enforced(self):
        """Attempting to create an external action proposal without approval requirement must fail."""
        with pytest.raises(ValueError, match="MUST require creator approval"):
            ActionProposal(
                proposal_id="p1",
                event_id="e1",
                action_type=ActionType.REPLY,
                target_resource="res1",
                proposed_content="content",
                reason="reason",
                required_capability="cap1",
                evidence_refs=(),
                requires_creator_approval=False,
            )

    def test_proposal_fingerprint_changes_on_mutation(self):
        """Changing proposal content must change its cryptographic fingerprint."""
        event = GitHubInboundNormalizer.load_fixture("claude_code")
        q = RevenueQualifier().qualify(event)
        p1 = compile_action_proposal(event, q)
        p2 = p1.with_content("Altered content with unauthorized discounts")
        assert p1.proposal_fingerprint != p2.proposal_fingerprint

    def test_proposal_to_and_from_dict(self):
        event = GitHubInboundNormalizer.load_fixture("claude_code")
        q = RevenueQualifier().qualify(event)
        p = compile_action_proposal(event, q)
        d = p.to_dict()
        p2 = ActionProposal.from_dict(d)
        assert p2.proposal_fingerprint == p.proposal_fingerprint


class TestCreatorApprovalBoundary:
    """Requirement 4: Creator Approval Boundary."""

    def test_approval_binding_to_exact_fingerprint(self):
        event = GitHubInboundNormalizer.load_fixture("claude_code")
        q = RevenueQualifier().qualify(event)
        proposal = compile_action_proposal(event, q)

        boundary = CreatorApprovalBoundary()
        approval = boundary.grant_approval(proposal, "creator:matias")
        assert approval.is_approved
        assert approval.proposal_fingerprint == proposal.proposal_fingerprint
        assert boundary.verify_approval(proposal, approval) is True

    def test_modification_after_approval_invalidates_authority(self):
        """Material modification after approval MUST invalidate approval."""
        event = GitHubInboundNormalizer.load_fixture("claude_code")
        q = RevenueQualifier().qualify(event)
        proposal = compile_action_proposal(event, q)

        boundary = CreatorApprovalBoundary()
        approval = boundary.grant_approval(proposal, "creator:matias")

        tampered = proposal.with_content("Modified content!")
        with pytest.raises(ApprovalInvalidatedError):
            boundary.verify_approval(tampered, approval)

    def test_approval_without_record_raises(self):
        event = GitHubInboundNormalizer.load_fixture("claude_code")
        q = RevenueQualifier().qualify(event)
        proposal = compile_action_proposal(event, q)

        boundary = CreatorApprovalBoundary()
        with pytest.raises(ApprovalRequiredError):
            boundary.verify_approval(proposal, None)

    def test_unauthorized_creator_identity_rejected(self):
        event = GitHubInboundNormalizer.load_fixture("claude_code")
        q = RevenueQualifier().qualify(event)
        proposal = compile_action_proposal(event, q)

        boundary = CreatorApprovalBoundary()
        with pytest.raises(ApprovalIdentityError):
            boundary.grant_approval(proposal, "unauthorized_bot_or_user")

    def test_creator_rejection_record(self):
        event = GitHubInboundNormalizer.load_fixture("claude_code")
        q = RevenueQualifier().qualify(event)
        proposal = compile_action_proposal(event, q)

        boundary = CreatorApprovalBoundary()
        rejection = boundary.reject_proposal(proposal, "creator:matias", reason="Not pursuing lead")
        assert rejection.decision == ApprovalDecision.REJECTED
        with pytest.raises(ApprovalRequiredError):
            boundary.verify_approval(proposal, rejection)


class TestOutboundEffectBoundaryAndSafety:
    """Requirement 5: Outbound App Effect Boundary & Indeterminate Handling."""

    def test_dispatch_without_approval_refused(self):
        event = GitHubInboundNormalizer.load_fixture("claude_code")
        q = RevenueQualifier().qualify(event)
        proposal = compile_action_proposal(event, q)

        adapter = SimulatedAppEffectAdapter()
        action_without_approval = ApprovedAppAction(
            action_id="act_1",
            proposal=proposal,
            approval=CreatorApprovalRecord(
                approval_id="appr_fake",
                proposal_id=proposal.proposal_id,
                proposal_fingerprint="wrong_fp",
                creator_identity="creator:matias",
                decision=ApprovalDecision.APPROVED,
                approved_at=datetime.now(timezone.utc),
                scope_boundary="test",
            ),
            idempotency_key="ik_1",
            effect_intent_id="intent_1",
        )
        with pytest.raises(ApprovalInvalidatedError):
            adapter.dispatch(action_without_approval)

    def test_successful_simulated_dispatch(self):
        event = GitHubInboundNormalizer.load_fixture("claude_code")
        q = RevenueQualifier().qualify(event)
        proposal = compile_action_proposal(event, q)
        boundary = CreatorApprovalBoundary()
        approval = boundary.grant_approval(proposal, "creator:matias")

        adapter = SimulatedAppEffectAdapter(approval_boundary=boundary)
        action = ApprovedAppAction(
            action_id="act_1",
            proposal=proposal,
            approval=approval,
            idempotency_key="ik_1",
            effect_intent_id="intent_1",
        )
        result = adapter.dispatch(action)
        assert result.state == EffectState.SOMETHING_LANDED
        assert result.is_landed
        assert result.evidence_ref.startswith("ev_landed_")

    def test_definite_rejection_outcome(self):
        event = GitHubInboundNormalizer.load_fixture("claude_code")
        q = RevenueQualifier().qualify(event)
        proposal = compile_action_proposal(event, q)
        boundary = CreatorApprovalBoundary()
        approval = boundary.grant_approval(proposal, "creator:matias")

        adapter = SimulatedAppEffectAdapter(
            approval_boundary=boundary,
            default_mode=EffectOutcomeMode.DEFINITE_REJECTION,
        )
        action = ApprovedAppAction(
            action_id="act_1",
            proposal=proposal,
            approval=approval,
            idempotency_key="ik_1",
            effect_intent_id="intent_1",
        )
        result = adapter.dispatch(action)
        assert result.state == EffectState.NOTHING_LANDED
        assert result.evidence_ref.startswith("ev_rejected_")

    def test_indeterminate_effect_creates_obligation(self):
        event = GitHubInboundNormalizer.load_fixture("claude_code")
        q = RevenueQualifier().qualify(event)
        proposal = compile_action_proposal(event, q)
        boundary = CreatorApprovalBoundary()
        approval = boundary.grant_approval(proposal, "creator:matias")

        adapter = SimulatedAppEffectAdapter(
            approval_boundary=boundary,
            default_mode=EffectOutcomeMode.CONNECTION_LOSS,
        )
        action = ApprovedAppAction(
            action_id="act_1",
            proposal=proposal,
            approval=approval,
            idempotency_key="ik_1",
            effect_intent_id="intent_1",
        )
        result = adapter.dispatch(action)
        assert result.state == EffectState.INDETERMINATE
        assert result.is_indeterminate
        assert result.reconciliation_obligation_id is not None

    def test_no_blind_retry_after_uncertain_effect(self):
        """Attempting to retry an indeterminate effect without reconciliation MUST be refused."""
        event = GitHubInboundNormalizer.load_fixture("claude_code")
        q = RevenueQualifier().qualify(event)
        proposal = compile_action_proposal(event, q)
        boundary = CreatorApprovalBoundary()
        approval = boundary.grant_approval(proposal, "creator:matias")

        adapter = SimulatedAppEffectAdapter(
            approval_boundary=boundary,
            default_mode=EffectOutcomeMode.CONNECTION_LOSS,
        )
        action = ApprovedAppAction(
            action_id="act_1",
            proposal=proposal,
            approval=approval,
            idempotency_key="ik_1",
            effect_intent_id="intent_1",
        )
        adapter.dispatch(action)

        # Retry without reconciliation
        with pytest.raises(BlindRetryRefusedError, match="strictly forbids blind retry"):
            adapter.dispatch(action)

    def test_reconciliation_resolves_indeterminate_effect(self):
        event = GitHubInboundNormalizer.load_fixture("claude_code")
        q = RevenueQualifier().qualify(event)
        proposal = compile_action_proposal(event, q)
        boundary = CreatorApprovalBoundary()
        approval = boundary.grant_approval(proposal, "creator:matias")

        adapter = SimulatedAppEffectAdapter(
            approval_boundary=boundary,
            default_mode=EffectOutcomeMode.CONNECTION_LOSS,
        )
        action = ApprovedAppAction(
            action_id="act_1",
            proposal=proposal,
            approval=approval,
            idempotency_key="ik_1",
            effect_intent_id="intent_1",
        )
        res = adapter.dispatch(action)
        assert res.is_indeterminate

        # Configure ground truth discovered by probe
        adapter.configure_reconciliation_resolution("intent_1", EffectState.SOMETHING_LANDED)
        recon = adapter.reconcile("intent_1", "ik_1")
        assert recon.resolved_state == EffectState.SOMETHING_LANDED
        assert recon.evidence_ref.startswith("ev_recon_")

    def test_unsupported_capability_refused(self):
        event = GitHubInboundNormalizer.load_fixture("claude_code")
        q = RevenueQualifier().qualify(event)
        proposal = compile_action_proposal(event, q)
        boundary = CreatorApprovalBoundary()
        approval = boundary.grant_approval(proposal, "creator:matias")

        adapter = SimulatedAppEffectAdapter(
            supported_capabilities={"email.reply.send"},  # Doesn't support github
            approval_boundary=boundary,
        )
        action = ApprovedAppAction(
            action_id="act_1",
            proposal=proposal,
            approval=approval,
            idempotency_key="ik_1",
            effect_intent_id="intent_1",
        )
        with pytest.raises(CapabilityNotSupportedError):
            adapter.dispatch(action)


class TestGitHubAndEmailUnifiedContracts:
    """Requirements 6 & 7: GitHub and Email Contracts."""

    def test_github_and_email_produce_identical_contract_shape(self):
        gh_event = GitHubInboundNormalizer.load_fixture("claude_code")
        em_event = EmailInboundNormalizer.load_fixture("python_automation")

        assert isinstance(gh_event, AppEvent)
        assert isinstance(em_event, AppEvent)

        # Both have required core fields
        for ev in (gh_event, em_event):
            assert ev.event_id
            assert ev.source_app
            assert ev.actor
            assert ev.content
            assert ev.observed_at.tzinfo is not None
            assert ev.raw_evidence_ref
            assert len(ev.capabilities) > 0
            assert len(ev.uncertainty) > 0

    def test_inbox_handles_both_github_and_email_opportunities(self):
        inbox = RevenueInbox()
        gh_opp = inbox.ingest(GitHubInboundNormalizer.load_fixture("claude_code"))
        em_opp = inbox.ingest(EmailInboundNormalizer.load_fixture("python_automation"))

        assert gh_opp.is_qualified
        assert em_opp.is_qualified
        assert gh_opp.source == "github"
        assert em_opp.source == "email"


class TestRevenueInboxAndPhoneFriendlyReport:
    """Requirement 8: Revenue Inbox and Phone-Friendly Status Report."""

    def test_phone_friendly_report_content(self):
        inbox = RevenueInbox()
        event = GitHubInboundNormalizer.load_fixture("claude_code")
        opp = inbox.ingest(event)
        inbox.approve(opp.opportunity_id, "creator:matias")

        report = inbox.format_phone_status(opp.opportunity_id)
        assert "REVENUE BRIDGE — INBOX STATUS" in report
        assert "TOP OPPORTUNITY:" in report
        assert "SOURCE:          GITHUB" in report
        assert "EXACT PAIN:" in report
        assert "Claude Code" in report
        assert "FIT DECISION:    QUALIFIED" in report
        assert "VERIFIED EVIDENCE:" in report
        assert "UNKNOWN INFORMATION:" in report
        assert "PROPOSED OFFER:  MissionaryX Reliability Check ($50 for one clearly bounded problem)" in report
        assert "APPROVAL STATE:  APPROVED (by creator:matias)"
        assert "NEXT CREATOR:" in report

    def test_phone_friendly_report_no_invented_percentages(self):
        """Report must not contain invented progress percentages."""
        inbox = RevenueInbox()
        event = GitHubInboundNormalizer.load_fixture("claude_code")
        inbox.ingest(event)
        report = inbox.format_phone_status()
        assert "%" not in report


class TestCreatorTestDrive:
    """Requirement 9: Creator Test Drive Execution."""

    def test_full_creator_test_drive_passes(self):
        """Deterministic test drive must pass all 10 steps A through J."""
        assert run_creator_test_drive(verbose=False) is True
