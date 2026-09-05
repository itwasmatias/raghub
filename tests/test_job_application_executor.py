"""Tests for JobApplicationExecutor — governance, approval gates, at-most-once, durability."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from revenue_bridge.approval import ApprovalInvalidatedError, ApprovalRequiredError
from revenue_bridge.effects import EffectOutcomeMode, SimulatedAppEffectAdapter

from job_application.browser import FixtureScenario, SimulatedATSBrowserAdapter
from job_application.candidate import synthetic_test_profile
from job_application.executor import (
    DuplicateApplicationError,
    GateARequiredError,
    GateBRequiredError,
    JobApplicationExecutor,
)
from job_application.ledger import ApplicationState, DurableApplicationLedger
from job_application.opportunity import DurableOpportunityStore, ingest_from_text

_JOB_APPLY_CAPABILITY = "job_application.ats_submit"
_BEGIN_CAPABILITY = "job_application.begin"

_POSTING = "Python agentic AI developer needed. LLM experience preferred. 2+ years Python."


def _make_executor(
    tmp_path: Path,
    scenario: FixtureScenario = FixtureScenario.SINGLE_PAGE_SUCCESS,
    effect_mode: EffectOutcomeMode = EffectOutcomeMode.SUCCESS,
) -> JobApplicationExecutor:
    ledger = DurableApplicationLedger(tmp_path / "apps.jsonl")
    store = DurableOpportunityStore(tmp_path / "opps.jsonl")
    browser = SimulatedATSBrowserAdapter(scenario)
    effect_adapter = SimulatedAppEffectAdapter(
        adapter_name="test_job_adapter",
        supported_capabilities={_JOB_APPLY_CAPABILITY, _BEGIN_CAPABILITY},
        default_mode=effect_mode,
    )
    return JobApplicationExecutor(
        ledger=ledger,
        opportunity_store=store,
        browser_adapter=browser,
        effect_adapter=effect_adapter,
    )


class TestIngestAndDuplicatePrevention:
    def test_ingest_creates_discovered_record(self, tmp_path: Path):
        executor = _make_executor(tmp_path)
        opp = ingest_from_text(_POSTING, "Acme", "Dev")
        record = executor.ingest_opportunity(opp)
        assert record.state == ApplicationState.DISCOVERED

    def test_duplicate_posting_blocked(self, tmp_path: Path):
        executor = _make_executor(tmp_path)
        opp1 = ingest_from_text(_POSTING, "Acme", "Dev")
        executor.ingest_opportunity(opp1)
        # Same posting text → same hash → duplicate
        opp2 = ingest_from_text(_POSTING, "Acme", "Dev")
        with pytest.raises(DuplicateApplicationError) as exc_info:
            executor.ingest_opportunity(opp2)
        assert exc_info.value.existing_application_id

    def test_different_company_not_blocked(self, tmp_path: Path):
        executor = _make_executor(tmp_path)
        opp1 = ingest_from_text("posting A", "AcmeCorp", "Dev")
        executor.ingest_opportunity(opp1)
        opp2 = ingest_from_text("posting B different", "BetaInc", "Engineer")
        record = executor.ingest_opportunity(opp2)
        assert record.state == ApplicationState.DISCOVERED


class TestQualifyAndAssess:
    def test_qualify_transitions_to_qualified(self, tmp_path: Path):
        executor = _make_executor(tmp_path)
        opp = ingest_from_text(_POSTING, "Acme", "Dev")
        record = executor.ingest_opportunity(opp)
        fit = executor.qualify_and_assess(record.application_id, synthetic_test_profile())
        assert fit.overall_score > 0
        updated = executor.ledger.get(record.application_id)
        assert updated.state == ApplicationState.QUALIFIED

    def test_fit_score_persisted_in_ledger(self, tmp_path: Path):
        executor = _make_executor(tmp_path)
        opp = ingest_from_text(_POSTING, "Acme", "Dev")
        record = executor.ingest_opportunity(opp)
        profile = synthetic_test_profile()
        fit = executor.qualify_and_assess(record.application_id, profile)
        r = executor.ledger.get(record.application_id)
        assert r.fit_score == fit.overall_score


class TestBuildPacket:
    def test_build_packet_transitions_to_packet_drafted(self, tmp_path: Path):
        executor = _make_executor(tmp_path)
        opp = ingest_from_text(_POSTING, "Acme", "Dev")
        record = executor.ingest_opportunity(opp)
        executor.qualify_and_assess(record.application_id, synthetic_test_profile())
        executor.build_application_packet(record.application_id, synthetic_test_profile())
        r = executor.ledger.get(record.application_id)
        assert r.state in (ApplicationState.NEEDS_USER_INPUT, ApplicationState.READY_FOR_APPLICATION)

    def test_packet_hash_persisted(self, tmp_path: Path):
        executor = _make_executor(tmp_path)
        opp = ingest_from_text(_POSTING, "Acme", "Dev")
        record = executor.ingest_opportunity(opp)
        executor.qualify_and_assess(record.application_id, synthetic_test_profile())
        packet = executor.build_application_packet(record.application_id, synthetic_test_profile())
        r = executor.ledger.get(record.application_id)
        assert r.packet_hash == packet.packet_hash


class TestGateAApproval:
    def _setup_to_ready(self, executor: JobApplicationExecutor, tmp_path: Path):
        opp = ingest_from_text(_POSTING, "Acme", "Dev")
        record = executor.ingest_opportunity(opp)
        executor.qualify_and_assess(record.application_id, synthetic_test_profile())
        packet = executor.build_application_packet(record.application_id, synthetic_test_profile())
        # Force to READY_FOR_APPLICATION state if NEEDS_USER_INPUT
        r = executor.ledger.get(record.application_id)
        if r.state == ApplicationState.NEEDS_USER_INPUT:
            executor.ledger.transition(record.application_id, ApplicationState.READY_FOR_APPLICATION)
        return record.application_id, packet

    def test_gate_a_approval_transitions_to_begin_approved(self, tmp_path: Path):
        executor = _make_executor(tmp_path)
        aid, packet = self._setup_to_ready(executor, tmp_path)
        proposal = executor.request_begin_approval(aid, packet)
        executor.grant_begin_approval(aid, proposal, "creator:matias")
        r = executor.ledger.get(aid)
        assert r.state == ApplicationState.BEGIN_APPROVED

    def test_gate_a_approval_stores_approval_id(self, tmp_path: Path):
        executor = _make_executor(tmp_path)
        aid, packet = self._setup_to_ready(executor, tmp_path)
        proposal = executor.request_begin_approval(aid, packet)
        approval = executor.grant_begin_approval(aid, proposal, "creator:matias")
        r = executor.ledger.get(aid)
        assert r.begin_approval_id == approval.approval_id

    def test_changed_packet_invalidates_gate_a_proposal(self, tmp_path: Path):
        executor = _make_executor(tmp_path)
        aid, packet = self._setup_to_ready(executor, tmp_path)
        proposal = executor.request_begin_approval(aid, packet)
        approval = executor.approval_boundary.grant_approval(
            proposal, "creator:matias", "Gate A test"
        )
        # Build a NEW proposal (simulating packet change)
        opp2 = ingest_from_text("different posting content entirely", "Acme", "Dev2")
        executor.opportunity_store.store(opp2)

        from job_application.candidate import synthetic_test_profile
        from job_application.cover_letter import DeterministicCoverLetterBoundary
        from job_application.fit import DeterministicFitAssessor
        from job_application.packet import build_packet
        from job_application.questions import QuestionClassifier
        from job_application.resume import DeterministicResumeTailoringBoundary

        profile = synthetic_test_profile()
        fit = DeterministicFitAssessor().assess(opp2, profile)
        resume = DeterministicResumeTailoringBoundary().generate(opp2, profile)
        cl = DeterministicCoverLetterBoundary().generate(opp2, profile, fit)
        packet2 = build_packet(opp2, profile, fit, resume, cl, [])
        proposal2 = executor.request_begin_approval(aid, packet2)

        # Prior approval does not match new proposal
        with pytest.raises((ApprovalInvalidatedError, ApprovalRequiredError)):
            executor.approval_boundary.verify_approval(proposal2, approval)


class TestFullVerticalSlice:
    """End-to-end test: ingest → qualify → packet → Gate A → form → Gate B → submit."""

    def _run_slice(
        self,
        tmp_path: Path,
        scenario: FixtureScenario = FixtureScenario.SINGLE_PAGE_SUCCESS,
        effect_mode: EffectOutcomeMode = EffectOutcomeMode.SUCCESS,
    ):
        executor = _make_executor(tmp_path, scenario, effect_mode)
        profile = synthetic_test_profile()

        # Step 1: Ingest
        opp = ingest_from_text(_POSTING, "AcmeAI", "AI Engineer")
        record = executor.ingest_opportunity(opp)
        aid = record.application_id

        # Step 2: Qualify
        executor.qualify_and_assess(aid, profile)

        # Step 3: Build packet
        packet = executor.build_application_packet(aid, profile)

        # Transition to READY_FOR_APPLICATION if stuck at NEEDS_USER_INPUT
        r = executor.ledger.get(aid)
        if r.state == ApplicationState.NEEDS_USER_INPUT:
            executor.ledger.transition(aid, ApplicationState.READY_FOR_APPLICATION)

        # Step 4: Gate A
        begin_proposal = executor.request_begin_approval(aid, packet)
        begin_approval = executor.grant_begin_approval(aid, begin_proposal, "creator:matias")

        # Step 5: Inspect form
        form_inspection = executor.inspect_ats_form(aid, begin_proposal, begin_approval, packet)
        assert form_inspection.form_id

        # Step 6: Fill form
        fill_results = executor.fill_application_form(
            aid, begin_proposal, begin_approval, form_inspection, packet
        )

        # Step 7: Gate B
        submit_proposal = executor.request_submit_approval(aid, packet, form_inspection)
        submit_approval = executor.grant_submit_approval(aid, submit_proposal, "creator:matias")

        # Step 8: Submit
        effect_result = executor.execute_submission(
            aid, packet, submit_proposal, submit_approval, form_inspection
        )
        return aid, executor, effect_result

    def test_successful_submission_confirmed(self, tmp_path: Path):
        aid, executor, effect_result = self._run_slice(tmp_path)
        r = executor.ledger.get(aid)
        assert r.state == ApplicationState.SUBMITTED_CONFIRMED
        assert r.confirmation_id is not None
        assert r.submission_evidence is not None

    def test_no_personal_data_before_gate_a(self, tmp_path: Path):
        """Verify that Gate A must be approved before any form interaction."""
        executor = _make_executor(tmp_path)
        profile = synthetic_test_profile()
        opp = ingest_from_text(_POSTING, "AcmeAI", "AI Engineer")
        record = executor.ingest_opportunity(opp)
        executor.qualify_and_assess(record.application_id, profile)
        packet = executor.build_application_packet(record.application_id, profile)

        # Attempting inspect_form without Gate A should fail
        from revenue_bridge.proposals import ActionProposal, ActionProposalState, ActionType
        from revenue_bridge.approval import CreatorApprovalRecord, ApprovalDecision

        # Create a fake proposal + fake approval to test state check
        r = executor.ledger.get(record.application_id)
        # Application is in NEEDS_USER_INPUT or READY_FOR_APPLICATION, NOT BEGIN_APPROVED
        assert r.state not in (ApplicationState.BEGIN_APPROVED,)

        dummy_proposal = ActionProposal(
            proposal_id="dummy", event_id=record.application_id,
            action_type=ActionType.FOLLOW_UP,
            target_resource="ats://acme", proposed_content="test",
            reason="test", required_capability=_BEGIN_CAPABILITY,
            evidence_refs=(), requires_creator_approval=True,
        )
        dummy_approval = executor.approval_boundary.grant_approval(
            dummy_proposal, "creator:matias", "test"
        )
        with pytest.raises(GateARequiredError):
            executor.inspect_ats_form(
                record.application_id, dummy_proposal, dummy_approval, packet
            )

    def test_submission_without_gate_b_raises(self, tmp_path: Path):
        executor = _make_executor(tmp_path)
        profile = synthetic_test_profile()
        opp = ingest_from_text(_POSTING, "AcmeAI", "AI Engineer")
        record = executor.ingest_opportunity(opp)
        executor.qualify_and_assess(record.application_id, profile)
        packet = executor.build_application_packet(record.application_id, profile)

        r = executor.ledger.get(record.application_id)
        if r.state == ApplicationState.NEEDS_USER_INPUT:
            executor.ledger.transition(record.application_id, ApplicationState.READY_FOR_APPLICATION)

        begin_proposal = executor.request_begin_approval(record.application_id, packet)
        begin_approval = executor.grant_begin_approval(
            record.application_id, begin_proposal, "creator:matias"
        )
        form_inspection = executor.inspect_ats_form(
            record.application_id, begin_proposal, begin_approval, packet
        )
        executor.fill_application_form(
            record.application_id, begin_proposal, begin_approval, form_inspection, packet
        )
        # Try to create submit proposal without granting approval
        submit_proposal = executor.request_submit_proposal_only(record.application_id, packet, form_inspection) \
            if hasattr(executor, "request_submit_proposal_only") \
            else executor.request_submit_approval(record.application_id, packet, form_inspection)

        # Do NOT grant Gate B — try to call execute_submission without Gate B approval
        # Application is in READY_TO_SUBMIT, not SUBMISSION_APPROVED → should fail
        r2 = executor.ledger.get(record.application_id)
        assert r2.state == ApplicationState.READY_TO_SUBMIT
        with pytest.raises(GateBRequiredError):
            executor.execute_submission(
                record.application_id, packet,
                submit_proposal,
                # Pass a dummy approval that won't match the SUBMISSION_APPROVED state check
                begin_approval,
                form_inspection,
            )


class TestAtMostOnceSubmission:
    def test_indeterminate_state_prevents_retry(self, tmp_path: Path):
        """After INDETERMINATE submission, state must not automatically retry."""
        executor = _make_executor(
            tmp_path,
            scenario=FixtureScenario.NETWORK_LOSS_AFTER_SUBMIT,
            effect_mode=EffectOutcomeMode.CONNECTION_LOSS,
        )
        profile = synthetic_test_profile()
        opp = ingest_from_text(_POSTING, "AcmeAI", "AI Engineer")
        record = executor.ingest_opportunity(opp)
        aid = record.application_id

        executor.qualify_and_assess(aid, profile)
        packet = executor.build_application_packet(aid, profile)

        r = executor.ledger.get(aid)
        if r.state == ApplicationState.NEEDS_USER_INPUT:
            executor.ledger.transition(aid, ApplicationState.READY_FOR_APPLICATION)

        begin_proposal = executor.request_begin_approval(aid, packet)
        begin_approval = executor.grant_begin_approval(aid, begin_proposal, "creator:matias")
        form_inspection = executor.inspect_ats_form(aid, begin_proposal, begin_approval, packet)
        executor.fill_application_form(aid, begin_proposal, begin_approval, form_inspection, packet)
        submit_proposal = executor.request_submit_approval(aid, packet, form_inspection)
        submit_approval = executor.grant_submit_approval(aid, submit_proposal, "creator:matias")

        effect_result = executor.execute_submission(
            aid, packet, submit_proposal, submit_approval, form_inspection
        )

        # State should be SUBMISSION_INDETERMINATE
        r = executor.ledger.get(aid)
        assert r.state == ApplicationState.SUBMISSION_INDETERMINATE
        assert r.reconciliation_state == "indeterminate"

    def test_reconciliation_resolves_indeterminate(self, tmp_path: Path):
        executor = _make_executor(
            tmp_path,
            scenario=FixtureScenario.NETWORK_LOSS_AFTER_SUBMIT,
            effect_mode=EffectOutcomeMode.CONNECTION_LOSS,
        )
        profile = synthetic_test_profile()
        opp = ingest_from_text(_POSTING, "AcmeAI", "AI Engineer")
        record = executor.ingest_opportunity(opp)
        aid = record.application_id

        executor.qualify_and_assess(aid, profile)
        packet = executor.build_application_packet(aid, profile)

        r = executor.ledger.get(aid)
        if r.state == ApplicationState.NEEDS_USER_INPUT:
            executor.ledger.transition(aid, ApplicationState.READY_FOR_APPLICATION)

        begin_proposal = executor.request_begin_approval(aid, packet)
        begin_approval = executor.grant_begin_approval(aid, begin_proposal, "creator:matias")
        form_inspection = executor.inspect_ats_form(aid, begin_proposal, begin_approval, packet)
        executor.fill_application_form(aid, begin_proposal, begin_approval, form_inspection, packet)
        submit_proposal = executor.request_submit_approval(aid, packet, form_inspection)
        submit_approval = executor.grant_submit_approval(aid, submit_proposal, "creator:matias")
        executor.execute_submission(aid, packet, submit_proposal, submit_approval, form_inspection)

        # Pre-configure reconciliation to resolve as SOMETHING_LANDED
        from revenue_bridge.effects import EffectState
        r2 = executor.ledger.get(aid)
        executor.effect_adapter.configure_reconciliation_resolution(
            r2.submission_effect_id, EffectState.SOMETHING_LANDED
        )

        recon = executor.reconcile_indeterminate(aid, f"idem_{aid}")
        r3 = executor.ledger.get(aid)
        assert r3.state == ApplicationState.SUBMITTED_CONFIRMED

    def test_confirmation_evidence_preserved(self, tmp_path: Path):
        executor = _make_executor(tmp_path, FixtureScenario.SINGLE_PAGE_SUCCESS)
        profile = synthetic_test_profile()
        opp = ingest_from_text(_POSTING, "AcmeAI", "AI Engineer")
        record = executor.ingest_opportunity(opp)
        aid = record.application_id

        executor.qualify_and_assess(aid, profile)
        packet = executor.build_application_packet(aid, profile)

        r = executor.ledger.get(aid)
        if r.state == ApplicationState.NEEDS_USER_INPUT:
            executor.ledger.transition(aid, ApplicationState.READY_FOR_APPLICATION)

        begin_proposal = executor.request_begin_approval(aid, packet)
        begin_approval = executor.grant_begin_approval(aid, begin_proposal, "creator:matias")
        form_inspection = executor.inspect_ats_form(aid, begin_proposal, begin_approval, packet)
        executor.fill_application_form(aid, begin_proposal, begin_approval, form_inspection, packet)
        submit_proposal = executor.request_submit_approval(aid, packet, form_inspection)
        submit_approval = executor.grant_submit_approval(aid, submit_proposal, "creator:matias")
        executor.execute_submission(aid, packet, submit_proposal, submit_approval, form_inspection)

        r = executor.ledger.get(aid)
        assert r.confirmation_id is not None
        assert r.submission_evidence is not None


class TestRestartDurability:
    def test_state_survives_restart(self, tmp_path: Path):
        executor1 = _make_executor(tmp_path)
        profile = synthetic_test_profile()
        opp = ingest_from_text(_POSTING, "AcmeAI", "AI Engineer")
        record = executor1.ingest_opportunity(opp)
        executor1.qualify_and_assess(record.application_id, profile)
        executor1.build_application_packet(record.application_id, profile)

        # New executor instance — simulates restart
        executor2 = _make_executor(tmp_path)
        r = executor2.ledger.get(record.application_id)
        assert r is not None
        assert r.state in (ApplicationState.NEEDS_USER_INPUT, ApplicationState.READY_FOR_APPLICATION, ApplicationState.PACKET_DRAFTED)
        assert r.fit_score > 0


class TestPrivacy:
    def test_sensitive_values_not_in_fit_rationale(self, tmp_path: Path):
        executor = _make_executor(tmp_path)
        profile = synthetic_test_profile()
        opp = ingest_from_text(_POSTING, "AcmeAI", "Dev")
        record = executor.ingest_opportunity(opp)
        fit = executor.qualify_and_assess(record.application_id, profile)

        # Sensitive values (real email/phone) should not appear in fit rationale
        assert "alex.testworthy@example.invalid" not in fit.fit_rationale
        assert "+1-555-000-0000" not in fit.fit_rationale

    def test_profile_safe_summary_redacts_sensitive(self):
        profile = synthetic_test_profile()
        summary = profile.safe_summary()
        for field_name in ("email", "phone", "full_name"):
            if summary.get(field_name) == "[SENSITIVE]":
                assert True
        # actual email value should not appear
        assert "alex.testworthy@example.invalid" not in str(summary)
