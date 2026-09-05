"""Tests for SimulatedATSBrowserAdapter fixture behavior."""
import pytest

from job_application.browser import (
    FixtureScenario,
    FillOutcome,
    FormInspection,
    HumanHandoffRequired,
    SimulatedATSBrowserAdapter,
    SubmissionOutcome,
)


def _inspect(adapter: SimulatedATSBrowserAdapter) -> FormInspection:
    return adapter.inspect_form("fixture://ats/apply")


class TestFormInspection:
    def test_single_page_inspection_returns_fields(self):
        adapter = SimulatedATSBrowserAdapter(FixtureScenario.SINGLE_PAGE_SUCCESS)
        inspection = _inspect(adapter)
        assert len(inspection.fields) > 0
        assert not inspection.has_captcha
        assert not inspection.has_mfa

    def test_multi_step_inspection_shows_steps(self):
        adapter = SimulatedATSBrowserAdapter(FixtureScenario.MULTI_STEP_SUCCESS)
        inspection = _inspect(adapter)
        assert inspection.is_multi_step
        assert inspection.total_steps > 1

    def test_captcha_inspection_flags_captcha(self):
        adapter = SimulatedATSBrowserAdapter(FixtureScenario.CAPTCHA_INTERRUPTION)
        inspection = _inspect(adapter)
        assert inspection.has_captcha

    def test_mfa_inspection_flags_mfa(self):
        adapter = SimulatedATSBrowserAdapter(FixtureScenario.MFA_INTERRUPTION)
        inspection = _inspect(adapter)
        assert inspection.has_mfa

    def test_inspection_returns_evidence_ref(self):
        adapter = SimulatedATSBrowserAdapter(FixtureScenario.SINGLE_PAGE_SUCCESS)
        inspection = _inspect(adapter)
        assert inspection.evidence_ref


class TestFormFilling:
    def test_fill_with_valid_values_succeeds(self):
        adapter = SimulatedATSBrowserAdapter(FixtureScenario.SINGLE_PAGE_SUCCESS)
        inspection = _inspect(adapter)
        results = adapter.fill_fields(
            inspection,
            {
                "field_name": "Alex Testworthy",
                "field_email": "alex.testworthy@example.invalid",
                "field_resume": "resume.pdf",
                "field_authorized": "Yes",
            },
        )
        success_count = sum(1 for r in results if r.outcome == FillOutcome.SUCCESS)
        assert success_count >= 3

    def test_captcha_scenario_raises_human_handoff(self):
        adapter = SimulatedATSBrowserAdapter(FixtureScenario.CAPTCHA_INTERRUPTION)
        inspection = _inspect(adapter)
        with pytest.raises(HumanHandoffRequired) as exc_info:
            adapter.fill_fields(inspection, {"field_email": "test@example.invalid"})
        assert exc_info.value.handoff_type == "captcha"

    def test_mfa_scenario_raises_human_handoff(self):
        adapter = SimulatedATSBrowserAdapter(FixtureScenario.MFA_INTERRUPTION)
        inspection = _inspect(adapter)
        with pytest.raises(HumanHandoffRequired) as exc_info:
            adapter.fill_fields(inspection, {})
        assert exc_info.value.handoff_type == "mfa"

    def test_validation_error_scenario_reports_error(self):
        adapter = SimulatedATSBrowserAdapter(FixtureScenario.VALIDATION_ERROR)
        inspection = adapter.inspect_form("fixture://ats/apply")
        results = adapter.fill_fields(inspection, {"field_email": "bad-email"})
        error_results = [r for r in results if r.outcome == FillOutcome.VALIDATION_ERROR]
        assert len(error_results) >= 1


class TestSubmission:
    def test_single_page_success_confirms_submission(self):
        adapter = SimulatedATSBrowserAdapter(FixtureScenario.SINGLE_PAGE_SUCCESS)
        inspection = _inspect(adapter)
        result = adapter.submit_once(inspection)
        assert result.outcome == SubmissionOutcome.CONFIRMED
        assert result.confirmation_id is not None
        assert result.evidence_ref

    def test_network_loss_produces_indeterminate(self):
        adapter = SimulatedATSBrowserAdapter(FixtureScenario.NETWORK_LOSS_AFTER_SUBMIT)
        inspection = _inspect(adapter)
        result = adapter.submit_once(inspection)
        assert result.outcome == SubmissionOutcome.INDETERMINATE
        assert result.confirmation_id is None
        assert result.is_indeterminate

    def test_duplicate_application_produces_rejection(self):
        adapter = SimulatedATSBrowserAdapter(FixtureScenario.DUPLICATE_APPLICATION)
        inspection = _inspect(adapter)
        result = adapter.submit_once(inspection)
        assert result.outcome == SubmissionOutcome.REJECTED

    def test_captcha_on_submit_raises_human_handoff(self):
        adapter = SimulatedATSBrowserAdapter(FixtureScenario.CAPTCHA_INTERRUPTION)
        # Get inspection ignoring captcha flag (inspect does not raise)
        inspection = adapter.inspect_form("fixture://ats/apply")
        with pytest.raises(HumanHandoffRequired):
            adapter.submit_once(inspection)

    def test_confirmed_submission_is_not_indeterminate(self):
        adapter = SimulatedATSBrowserAdapter(FixtureScenario.CONFIRMED_SUBMISSION)
        inspection = _inspect(adapter)
        result = adapter.submit_once(inspection)
        assert result.is_confirmed
        assert not result.is_indeterminate

    def test_submission_result_has_evidence_ref(self):
        adapter = SimulatedATSBrowserAdapter(FixtureScenario.SINGLE_PAGE_SUCCESS)
        inspection = _inspect(adapter)
        result = adapter.submit_once(inspection)
        assert result.evidence_ref

    def test_submission_result_serializable(self):
        adapter = SimulatedATSBrowserAdapter(FixtureScenario.SINGLE_PAGE_SUCCESS)
        inspection = _inspect(adapter)
        result = adapter.submit_once(inspection)
        d = result.to_dict()
        assert d["outcome"] == "confirmed"
        assert d["confirmation_id"] is not None
