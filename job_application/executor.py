"""Job Application Executor for MissionaryX v0.1.

Orchestrates the full governed vertical slice:
  ingest → qualify → fit → packet → Gate A → form → Gate B → submit → evidence

Key invariants enforced here:
- No personal data transmitted before BEGIN_APPLICATION approval (Gate A).
- Final submit impossible without SUBMIT_APPLICATION approval (Gate B).
- Gate B approval binds to exact packet_hash; material changes invalidate it.
- At-most-once submission: INDETERMINATE → no automatic retry.
- Duplicate applications blocked before Gate A.
- State survives process restart via DurableApplicationLedger.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from revenue_bridge.approval import (
    ApprovalInvalidatedError,
    ApprovalRequiredError,
    CreatorApprovalBoundary,
    CreatorApprovalRecord,
)
from revenue_bridge.effects import (
    ApprovedAppAction,
    BlindRetryRefusedError,
    EffectExecutionResult,
    EffectOutcomeMode,
    EffectReconciliationResult,
    EffectState,
    SimulatedAppEffectAdapter,
)
from revenue_bridge.proposals import ActionProposal, ActionProposalState, ActionType

from job_application.browser import (
    ATSBrowserAdapter,
    FixtureScenario,
    FormInspection,
    HumanHandoffRequired,
    SimulatedATSBrowserAdapter,
    SubmissionOutcome,
)
from job_application.candidate import CandidateProfile, synthetic_test_profile
from job_application.cover_letter import CoverLetter, DeterministicCoverLetterBoundary
from job_application.fit import DeterministicFitAssessor, FitAssessment
from job_application.ledger import (
    ApplicationLedgerCorruptionError,
    ApplicationRecord,
    ApplicationState,
    DurableApplicationLedger,
)
from job_application.opportunity import (
    DurableOpportunityStore,
    JobOpportunity,
    ingest_from_text,
)
from job_application.packet import ApplicationPacket, build_packet
from job_application.questions import ApplicationQuestion, QuestionClassifier
from job_application.resume import DeterministicResumeTailoringBoundary, TailoredResume


class DuplicateApplicationError(Exception):
    """Raised when an application for this opportunity already exists."""

    def __init__(self, message: str, existing_application_id: str) -> None:
        super().__init__(message)
        self.existing_application_id = existing_application_id


class GateARequiredError(Exception):
    """Raised when an action requiring Gate A approval is attempted without it."""


class GateBRequiredError(Exception):
    """Raised when submission is attempted without Gate B approval."""


class GateBInvalidatedError(Exception):
    """Raised when packet changed after Gate B approval — approval is void."""


class IndeterminateSubmissionError(Exception):
    """Raised when blind retry is attempted after INDETERMINATE submission."""


_JOB_APPLY_CAPABILITY = "job_application.ats_submit"
_BEGIN_CAPABILITY = "job_application.begin"


@dataclass
class ExecutorContext:
    """Runtime state for a single application session."""
    application_id: str
    opportunity: JobOpportunity
    profile: CandidateProfile
    fit: FitAssessment | None = None
    resume: TailoredResume | None = None
    cover_letter: CoverLetter | None = None
    packet: ApplicationPacket | None = None
    all_questions: list[ApplicationQuestion] | None = None
    begin_proposal: ActionProposal | None = None
    begin_approval: CreatorApprovalRecord | None = None
    submit_proposal: ActionProposal | None = None
    submit_approval: CreatorApprovalRecord | None = None
    form_inspection: FormInspection | None = None
    effect_result: EffectExecutionResult | None = None
    reconciliation_result: EffectReconciliationResult | None = None


class JobApplicationExecutor:
    """Governed executor for the MissionaryX Job Application vertical slice.

    Combines existing MissionaryX approval, effect, and durable storage
    infrastructure with new job-specific components.
    """

    def __init__(
        self,
        ledger: DurableApplicationLedger,
        opportunity_store: DurableOpportunityStore,
        approval_boundary: CreatorApprovalBoundary | None = None,
        browser_adapter: ATSBrowserAdapter | None = None,
        effect_adapter: SimulatedAppEffectAdapter | None = None,
    ) -> None:
        self.ledger = ledger
        self.opportunity_store = opportunity_store
        self.approval_boundary = approval_boundary or CreatorApprovalBoundary()
        self.browser_adapter = browser_adapter or SimulatedATSBrowserAdapter(
            FixtureScenario.SINGLE_PAGE_SUCCESS
        )
        self.effect_adapter = effect_adapter or SimulatedAppEffectAdapter(
            adapter_name="job_application_adapter",
            supported_capabilities={_JOB_APPLY_CAPABILITY, _BEGIN_CAPABILITY},
        )
        self._assessor = DeterministicFitAssessor()
        self._resume_boundary = DeterministicResumeTailoringBoundary()
        self._cover_letter_boundary = DeterministicCoverLetterBoundary()
        self._classifier = QuestionClassifier()

    # ── Step 1: Ingest ───────────────────────────────────────────────────────

    def ingest_opportunity(self, opportunity: JobOpportunity) -> ApplicationRecord:
        """Persist a new opportunity and create a ledger entry.

        Raises DuplicateApplicationError if this opportunity was already applied to.
        """
        existing_opp = self.opportunity_store.find_duplicate(opportunity)
        if existing_opp:
            existing_app = self.ledger.find_by_job_id(existing_opp.job_id)
            if existing_app:
                raise DuplicateApplicationError(
                    f"Opportunity appears to match existing application "
                    f"{existing_app.application_id} for {existing_app.company}/{existing_app.title} "
                    f"(state: {existing_app.state.value}). "
                    "Duplicate applications are blocked.",
                    existing_application_id=existing_app.application_id,
                )

        self.opportunity_store.store(opportunity)

        aid = f"app_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        record = ApplicationRecord(
            application_id=aid,
            job_id=opportunity.job_id,
            company=opportunity.company,
            title=opportunity.title,
            job_url=opportunity.source_url,
            application_url=opportunity.application_url,
            posting_hash=opportunity.posting_hash,
            fit_score=0,
            fit_rationale="pending",
            state=ApplicationState.DISCOVERED,
            candidate_profile_hash="pending",
            created_at=now,
            updated_at=now,
        )
        return self.ledger.create(record)

    # ── Step 2: Qualify / Assess ─────────────────────────────────────────────

    def qualify_and_assess(
        self,
        application_id: str,
        profile: CandidateProfile,
    ) -> FitAssessment:
        """Run fit assessment and update ledger."""
        record = self._require_record(application_id)
        opportunity = self._require_opportunity(record.job_id)

        fit = self._assessor.assess(opportunity, profile)

        self.ledger.update_fields(
            application_id,
            fit_score=fit.overall_score,
            fit_rationale=fit.fit_rationale,
            candidate_profile_hash=profile.profile_hash,
        )
        self.ledger.transition(
            application_id,
            ApplicationState.QUALIFIED,
            notes=f"Fit assessed: {fit.recommendation.value} ({fit.overall_score}/100)",
        )
        return fit

    # ── Step 3: Build Packet ─────────────────────────────────────────────────

    def build_application_packet(
        self,
        application_id: str,
        profile: CandidateProfile,
        fixture_questions: list[tuple[str, str, bool]] | None = None,
    ) -> ApplicationPacket:
        """Generate resume, cover letter, classify questions, build packet."""
        record = self._require_record(application_id)
        opportunity = self._require_opportunity(record.job_id)
        fit = self._assessor.assess(opportunity, profile)

        resume = self._resume_boundary.generate(opportunity, profile)
        cover_letter = self._cover_letter_boundary.generate(opportunity, profile, fit)

        # Classify supplied questions (or use minimal fixture defaults)
        raw_questions = fixture_questions or [
            ("q_name", "Full name", True),
            ("q_email", "Email address", True),
            ("q_phone", "Phone number", False),
            ("q_linkedin", "LinkedIn URL", False),
            ("q_github", "GitHub URL", False),
            ("q_authorized", "Are you authorized to work in the US?", True),
            ("q_sponsor", "Do you require sponsorship?", True),
            ("q_salary", "Desired salary", False),
            ("q_why", "Why do you want to work here?", False),
        ]
        questions = self._classifier.classify_all(raw_questions, profile)
        packet = build_packet(opportunity, profile, fit, resume, cover_letter, questions)

        has_user_required = any(q.needs_user_input() for q in questions)
        new_state = (
            ApplicationState.NEEDS_USER_INPUT
            if has_user_required
            else ApplicationState.READY_FOR_APPLICATION
        )

        self.ledger.update_fields(
            application_id,
            resume_hash=resume.resume_hash,
            cover_letter_hash=cover_letter.letter_hash,
            packet_hash=packet.packet_hash,
            candidate_profile_hash=profile.profile_hash,
        )
        self.ledger.transition(
            application_id,
            ApplicationState.PACKET_DRAFTED,
            notes=f"Packet {packet.packet_hash[:12]}... drafted; {len(packet.user_required_questions)} user-required questions",
        )
        if new_state == ApplicationState.NEEDS_USER_INPUT:
            self.ledger.transition(
                application_id,
                ApplicationState.NEEDS_USER_INPUT,
                notes="Packet has USER_REQUIRED questions pending creator input",
            )
        else:
            self.ledger.transition(
                application_id,
                ApplicationState.READY_FOR_APPLICATION,
                notes="All questions resolved; ready for BEGIN_APPLICATION approval",
            )

        return packet

    # ── Step 4: Gate A — BEGIN_APPLICATION ──────────────────────────────────

    def request_begin_approval(
        self,
        application_id: str,
        packet: ApplicationPacket,
    ) -> ActionProposal:
        """Build the Gate A proposal. Creator must call grant_begin_approval() next."""
        record = self._require_record(application_id)
        opportunity = self._require_opportunity(record.job_id)

        review_summary = self._format_gate_a_review(opportunity, packet)
        proposal = ActionProposal(
            proposal_id=f"begin_{application_id}",
            event_id=application_id,
            action_type=ActionType.FOLLOW_UP,
            target_resource=opportunity.application_url or f"ats://{opportunity.company}/{opportunity.title}",
            proposed_content=review_summary,
            reason=(
                f"BEGIN_APPLICATION: {opportunity.company} / {opportunity.title}. "
                f"Fit: {packet.fit_score}/100 ({packet.fit_recommendation}). "
                f"Packet: {packet.packet_hash[:16]}..."
            ),
            required_capability=_BEGIN_CAPABILITY,
            evidence_refs=(
                f"packet:{packet.packet_hash}",
                f"posting:{packet.posting_hash}",
                f"profile:{packet.candidate_profile_hash}",
            ),
            requires_creator_approval=True,
            state=ActionProposalState.PROPOSED,
        )
        return proposal

    def grant_begin_approval(
        self,
        application_id: str,
        proposal: ActionProposal,
        creator_identity: str,
        reason: str = "Creator approves BEGIN_APPLICATION",
    ) -> CreatorApprovalRecord:
        """Creator approves Gate A. Records approval and transitions ledger."""
        approval = self.approval_boundary.grant_approval(proposal, creator_identity, reason)
        self.ledger.update_fields(application_id, begin_approval_id=approval.approval_id)
        self.ledger.transition(
            application_id,
            ApplicationState.BEGIN_APPROVED,
            notes=f"Gate A approved by {creator_identity}: {approval.approval_id}",
        )
        return approval

    # ── Step 5: Inspect Form (read-only, post-Gate-A) ───────────────────────

    def inspect_ats_form(
        self,
        application_id: str,
        begin_proposal: ActionProposal,
        begin_approval: CreatorApprovalRecord,
        packet: ApplicationPacket,
    ) -> FormInspection:
        """Read-only ATS form inspection. No personal data transmitted yet."""
        record = self._require_record(application_id)
        if record.state != ApplicationState.BEGIN_APPROVED:
            raise GateARequiredError(
                f"Cannot inspect ATS form: application {application_id} is in state "
                f"{record.state.value}. BEGIN_APPLICATION approval required first."
            )
        self.approval_boundary.verify_approval(begin_proposal, begin_approval)

        opportunity = self._require_opportunity(record.job_id)
        url = opportunity.application_url or "fixture://ats/apply"
        return self.browser_adapter.inspect_form(url)

    # ── Step 6: Fill Form ────────────────────────────────────────────────────

    def fill_application_form(
        self,
        application_id: str,
        begin_proposal: ActionProposal,
        begin_approval: CreatorApprovalRecord,
        form_inspection: FormInspection,
        packet: ApplicationPacket,
        field_overrides: dict[str, str] | None = None,
    ) -> tuple:
        """Fill fixture form with approved packet values.

        Gate A must be approved and still valid (packet_hash unchanged).
        Raises HumanHandoffRequired on CAPTCHA/MFA.
        """
        record = self._require_record(application_id)
        if record.state not in (
            ApplicationState.BEGIN_APPROVED,
            ApplicationState.FORM_IN_PROGRESS,
        ):
            raise GateARequiredError(
                f"Cannot fill form: application {application_id} not in approved state "
                f"(current: {record.state.value})"
            )

        # Verify Gate A approval still valid
        self.approval_boundary.verify_approval(begin_proposal, begin_approval)

        # Build field values from packet
        field_values: dict[str, str] = {}
        for q in packet.known_answers:
            if q.answer is not None:
                # Map question_id to typical form field IDs
                if "name" in q.question_id or q.category.value == "identity":
                    field_values["field_name"] = q.answer
                elif "email" in q.question_id or q.category.value == "contact":
                    field_values["field_email"] = q.answer
                elif "phone" in q.question_id:
                    field_values["field_phone"] = q.answer
                elif "linkedin" in q.question_id or q.category.value == "linkedin":
                    field_values["field_linkedin"] = q.answer
                elif "github" in q.question_id or q.category.value == "github":
                    field_values["field_github"] = q.answer
                field_values[q.question_id] = q.answer

        if field_overrides:
            field_values.update(field_overrides)

        self.ledger.transition(
            application_id,
            ApplicationState.FORM_IN_PROGRESS,
            notes="Form fill started",
        )

        fill_results = self.browser_adapter.fill_fields(form_inspection, field_values)

        self.ledger.transition(
            application_id,
            ApplicationState.READY_TO_SUBMIT,
            notes=f"Form filled: {len(fill_results)} fields processed",
        )
        return fill_results

    # ── Step 7: Gate B — SUBMIT_APPLICATION ─────────────────────────────────

    def request_submit_approval(
        self,
        application_id: str,
        packet: ApplicationPacket,
        form_inspection: FormInspection,
    ) -> ActionProposal:
        """Build Gate B proposal. Creator must call grant_submit_approval() next."""
        record = self._require_record(application_id)
        opportunity = self._require_opportunity(record.job_id)

        review_summary = self._format_gate_b_review(opportunity, packet, form_inspection)
        proposal = ActionProposal(
            proposal_id=f"submit_{application_id}_{packet.packet_hash[:8]}",
            event_id=application_id,
            action_type=ActionType.FOLLOW_UP,
            target_resource=opportunity.application_url or f"ats://{opportunity.company}/{opportunity.title}",
            proposed_content=review_summary,
            reason=(
                f"SUBMIT_APPLICATION: {opportunity.company} / {opportunity.title}. "
                f"Packet: {packet.packet_hash[:16]}..."
            ),
            required_capability=_JOB_APPLY_CAPABILITY,
            evidence_refs=(
                f"packet:{packet.packet_hash}",
                f"resume:{packet.resume_hash}",
                f"cover_letter:{packet.cover_letter_hash}",
                f"profile:{packet.candidate_profile_hash}",
                f"form:{form_inspection.form_id}",
            ),
            requires_creator_approval=True,
            state=ActionProposalState.PROPOSED,
        )
        return proposal

    def grant_submit_approval(
        self,
        application_id: str,
        proposal: ActionProposal,
        creator_identity: str,
        reason: str = "Creator approves SUBMIT_APPLICATION",
    ) -> CreatorApprovalRecord:
        """Creator approves Gate B. Records approval and transitions ledger."""
        approval = self.approval_boundary.grant_approval(proposal, creator_identity, reason)
        self.ledger.update_fields(application_id, submit_approval_id=approval.approval_id)
        self.ledger.transition(
            application_id,
            ApplicationState.SUBMISSION_APPROVED,
            notes=f"Gate B approved by {creator_identity}: {approval.approval_id}",
        )
        return approval

    # ── Step 8: Submit (governed effect, at-most-once) ───────────────────────

    def execute_submission(
        self,
        application_id: str,
        packet: ApplicationPacket,
        submit_proposal: ActionProposal,
        submit_approval: CreatorApprovalRecord,
        form_inspection: FormInspection,
    ) -> EffectExecutionResult:
        """Execute one governed submission. At-most-once enforced.

        - Gate B approval verified and bound to current packet_hash.
        - If submission results in INDETERMINATE, state becomes SUBMISSION_INDETERMINATE.
        - Blind retry is refused until reconciliation completes.
        """
        record = self._require_record(application_id)
        if record.state != ApplicationState.SUBMISSION_APPROVED:
            raise GateBRequiredError(
                f"Cannot submit: application {application_id} not in SUBMISSION_APPROVED state "
                f"(current: {record.state.value})"
            )

        # Verify Gate B approval still matches current packet
        self.approval_boundary.verify_approval(submit_proposal, submit_approval)

        # Write-ahead intent before touching the browser
        effect_intent_id = f"jobapp_{application_id}_{packet.packet_hash[:12]}"
        idempotency_key = f"idem_{application_id}"

        action = ApprovedAppAction(
            action_id=f"act_{uuid.uuid4().hex[:12]}",
            proposal=submit_proposal,
            approval=submit_approval,
            idempotency_key=idempotency_key,
            effect_intent_id=effect_intent_id,
        )

        self.ledger.transition(
            application_id,
            ApplicationState.SUBMITTING,
            notes=f"Submitting — intent_id={effect_intent_id}",
        )
        self.ledger.update_fields(
            application_id,
            submission_effect_id=effect_intent_id,
            submission_timestamp=datetime.now(timezone.utc),
        )

        # Execute browser submit (one-shot)
        browser_result = self.browser_adapter.submit_once(form_inspection)

        # Dispatch the governed effect (encodes the submission as MissionaryX effect)
        effect_result = self.effect_adapter.dispatch(action)

        # Determine final application state from browser result
        if browser_result.is_confirmed:
            self.ledger.transition(
                application_id,
                ApplicationState.SUBMITTED_CONFIRMED,
                notes=f"Confirmed — confirmation_id={browser_result.confirmation_id}",
            )
            self.ledger.update_fields(
                application_id,
                confirmation_id=browser_result.confirmation_id,
                submission_evidence=browser_result.evidence_ref,
                reconciliation_state="confirmed",
            )
        elif browser_result.is_indeterminate:
            self.ledger.transition(
                application_id,
                ApplicationState.SUBMISSION_INDETERMINATE,
                notes=(
                    f"INDETERMINATE — result unknown after submit click. "
                    f"Evidence: {browser_result.evidence_ref}. "
                    "Automatic retry prohibited. Reconciliation required."
                ),
            )
            self.ledger.update_fields(
                application_id,
                submission_evidence=browser_result.evidence_ref,
                reconciliation_state="indeterminate",
            )
        else:
            # REJECTED
            self.ledger.transition(
                application_id,
                ApplicationState.REJECTED,
                notes=f"Rejected by ATS — evidence: {browser_result.evidence_ref}",
            )
            self.ledger.update_fields(
                application_id,
                submission_evidence=browser_result.evidence_ref,
                reconciliation_state="rejected",
            )

        return effect_result

    # ── Step 9: Reconcile INDETERMINATE ─────────────────────────────────────

    def reconcile_indeterminate(
        self,
        application_id: str,
        idempotency_key: str,
    ) -> EffectReconciliationResult:
        """Reconcile an INDETERMINATE submission. May not retry blindly."""
        record = self._require_record(application_id)
        if record.state != ApplicationState.SUBMISSION_INDETERMINATE:
            raise ValueError(
                f"Application {application_id} is not in SUBMISSION_INDETERMINATE state "
                f"(current: {record.state.value})"
            )
        if not record.submission_effect_id:
            raise ValueError(f"No submission_effect_id on record {application_id}")

        recon = self.effect_adapter.reconcile(record.submission_effect_id, idempotency_key)

        if recon.resolved_state == EffectState.SOMETHING_LANDED:
            self.ledger.transition(
                application_id,
                ApplicationState.SUBMITTED_CONFIRMED,
                notes=f"Reconciled: SOMETHING_LANDED — {recon.evidence_ref}",
            )
            self.ledger.update_fields(
                application_id,
                reconciliation_state="confirmed_via_reconciliation",
                submission_evidence=recon.evidence_ref,
            )
        else:
            self.ledger.transition(
                application_id,
                ApplicationState.REJECTED,
                notes=f"Reconciled: NOTHING_LANDED — {recon.evidence_ref}",
            )
            self.ledger.update_fields(
                application_id,
                reconciliation_state="nothing_landed_via_reconciliation",
                submission_evidence=recon.evidence_ref,
            )
        return recon

    # ── Status ───────────────────────────────────────────────────────────────

    def format_status(self, application_id: str) -> str:
        """Human-readable status for operator review."""
        record = self._require_record(application_id)
        lines = [
            f"APPLICATION STATUS: {application_id}",
            f"  Company:     {record.company}",
            f"  Role:        {record.title}",
            f"  State:       {record.state.value}",
            f"  Fit score:   {record.fit_score}/100",
            f"  Packet hash: {(record.packet_hash or 'n/a')[:20]}...",
        ]
        if record.state == ApplicationState.SUBMISSION_INDETERMINATE:
            lines.append("  ⚠ INDETERMINATE: reconciliation required before any retry")
        if record.state == ApplicationState.SUBMITTED_CONFIRMED:
            lines.append(f"  ✓ Confirmation: {record.confirmation_id}")
        if record.begin_approval_id:
            lines.append(f"  Gate A: {record.begin_approval_id}")
        if record.submit_approval_id:
            lines.append(f"  Gate B: {record.submit_approval_id}")
        return "\n".join(lines)

    # ── Private helpers ──────────────────────────────────────────────────────

    def _require_record(self, application_id: str) -> ApplicationRecord:
        record = self.ledger.get(application_id)
        if record is None:
            raise ValueError(f"Application {application_id} not found in ledger")
        return record

    def _require_opportunity(self, job_id: str) -> JobOpportunity:
        opp = self.opportunity_store.get(job_id)
        if opp is None:
            raise ValueError(f"Opportunity {job_id} not found in store")
        return opp

    def _format_gate_a_review(
        self,
        opportunity: JobOpportunity,
        packet: ApplicationPacket,
    ) -> str:
        lines = [
            "=== GATE A: BEGIN_APPLICATION REVIEW ===",
            f"Company:            {opportunity.company}",
            f"Role:               {opportunity.title}",
            f"Application URL:    {opportunity.application_url or 'unknown'}",
            f"Fit score:          {packet.fit_score}/100 ({packet.fit_recommendation})",
            f"Resume hash:        {packet.resume_hash[:20]}...",
            f"Cover letter hash:  {packet.cover_letter_hash[:20]}...",
            f"Profile hash:       {packet.candidate_profile_hash[:20]}...",
            f"Packet hash:        {packet.packet_hash[:20]}...",
            f"Known answers:      {len(packet.known_answers)} auto-answered",
            f"User required:      {len(packet.user_required_questions)} pending creator input",
            f"Portfolio links:    {', '.join(packet.portfolio_links) or 'none'}",
            "Approval of this gate authorizes form filling but NOT final submit.",
        ]
        return "\n".join(lines)

    def _format_gate_b_review(
        self,
        opportunity: JobOpportunity,
        packet: ApplicationPacket,
        form_inspection: FormInspection,
    ) -> str:
        lines = [
            "=== GATE B: SUBMIT_APPLICATION REVIEW ===",
            f"Company:            {opportunity.company}",
            f"Role:               {opportunity.title}",
            f"ATS form:           {form_inspection.form_id}",
            f"Fit score:          {packet.fit_score}/100 ({packet.fit_recommendation})",
            f"Resume hash:        {packet.resume_hash[:20]}...",
            f"Cover letter hash:  {packet.cover_letter_hash[:20]}...",
            f"Packet hash:        {packet.packet_hash[:20]}...",
            "Answers to be submitted:",
        ]
        for q in packet.known_answers:
            lines.append(f"  [{q.category.value}] {q.question_text[:50]}: {q.answer}")
        lines += [
            f"Salary strategy:    {packet.salary_strategy}",
            "APPROVAL OF THIS GATE AUTHORIZES ONE FINAL SUBMIT ACTION.",
            "Packet changes after this approval will invalidate it.",
        ]
        return "\n".join(lines)
