"""Browser/ATS adapter for Job Application Executor v0.1.

Abstract interface for form inspection, filling, and submission.
The SimulatedATSBrowserAdapter provides deterministic behavior for testing
without any live network, browser, or credentials.

Key invariants:
- Read-only inspection is separate from data transmission actions.
- CAPTCHA, MFA, identity verification → HumanHandoffRequired.
- Submit is a one-shot action; indeterminate outcomes must not auto-retry.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class HumanHandoffRequired(Exception):
    """Raised when the system encounters a condition requiring human intervention."""

    def __init__(self, reason: str, handoff_type: str = "unknown") -> None:
        super().__init__(reason)
        self.reason = reason
        self.handoff_type = handoff_type


class FormFieldType(str, Enum):
    TEXT = "text"
    EMAIL = "email"
    PHONE = "phone"
    TEXTAREA = "textarea"
    SELECT = "select"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    FILE = "file"
    HIDDEN = "hidden"
    SUBMIT = "submit"


class FillOutcome(str, Enum):
    SUCCESS = "success"
    VALIDATION_ERROR = "validation_error"
    FIELD_NOT_FOUND = "field_not_found"
    HUMAN_HANDOFF = "human_handoff"


class SubmissionOutcome(str, Enum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    INDETERMINATE = "indeterminate"
    CAPTCHA_REQUIRED = "captcha_required"
    MFA_REQUIRED = "mfa_required"


@dataclass(frozen=True, slots=True)
class FormField:
    field_id: str
    label: str
    field_type: FormFieldType
    required: bool
    options: tuple[str, ...] = field(default_factory=tuple)
    placeholder: str = ""
    current_value: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_id": self.field_id,
            "label": self.label,
            "field_type": self.field_type.value,
            "required": self.required,
            "options": list(self.options),
            "placeholder": self.placeholder,
            "current_value": self.current_value,
        }


@dataclass(frozen=True, slots=True)
class FormInspection:
    """Read-only snapshot of an ATS form structure."""
    form_id: str
    page_title: str
    fields: tuple[FormField, ...]
    has_captcha: bool
    has_mfa: bool
    is_multi_step: bool
    current_step: int
    total_steps: int
    evidence_ref: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "form_id": self.form_id,
            "page_title": self.page_title,
            "fields": [f.to_dict() for f in self.fields],
            "has_captcha": self.has_captcha,
            "has_mfa": self.has_mfa,
            "is_multi_step": self.is_multi_step,
            "current_step": self.current_step,
            "total_steps": self.total_steps,
            "evidence_ref": self.evidence_ref,
        }


@dataclass(frozen=True, slots=True)
class FillResult:
    field_id: str
    outcome: FillOutcome
    value_used: str | None
    validation_message: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_id": self.field_id,
            "outcome": self.outcome.value,
            "value_used": self.value_used,
            "validation_message": self.validation_message,
        }


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    outcome: SubmissionOutcome
    confirmation_id: str | None
    evidence_ref: str
    submitted_at: datetime
    details: tuple[tuple[str, Any], ...] = field(default_factory=tuple)

    @property
    def is_confirmed(self) -> bool:
        return self.outcome == SubmissionOutcome.CONFIRMED

    @property
    def is_indeterminate(self) -> bool:
        return self.outcome == SubmissionOutcome.INDETERMINATE

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "confirmation_id": self.confirmation_id,
            "evidence_ref": self.evidence_ref,
            "submitted_at": self.submitted_at.isoformat(),
            "details": dict(self.details),
        }


class ATSBrowserAdapter(ABC):
    """Abstract interface for ATS browser interaction."""

    @abstractmethod
    def inspect_form(self, url: str) -> FormInspection:
        """Read-only inspection of the ATS form. Must not transmit personal data."""

    @abstractmethod
    def fill_fields(
        self,
        form_inspection: FormInspection,
        field_values: dict[str, str],
    ) -> tuple[FillResult, ...]:
        """Fill form fields with approved values. Raises HumanHandoffRequired for CAPTCHA/MFA."""

    @abstractmethod
    def submit_once(self, form_inspection: FormInspection) -> SubmissionResult:
        """Execute one Submit action. Never called twice without reconciliation."""

    @abstractmethod
    def capture_evidence(self, label: str) -> str:
        """Capture screenshot or page evidence. Returns evidence reference string."""


class FixtureScenario(str, Enum):
    SINGLE_PAGE_SUCCESS = "single_page_success"
    MULTI_STEP_SUCCESS = "multi_step_success"
    VALIDATION_ERROR = "validation_error"
    CAPTCHA_INTERRUPTION = "captcha_interruption"
    MFA_INTERRUPTION = "mfa_interruption"
    NETWORK_LOSS_AFTER_SUBMIT = "network_loss_after_submit"
    DUPLICATE_APPLICATION = "duplicate_application"
    CONFIRMED_SUBMISSION = "confirmed_submission"
    DEFINITE_REJECTION = "definite_rejection"


class SimulatedATSBrowserAdapter(ATSBrowserAdapter):
    """Deterministic fixture-based ATS adapter. No network, browser, or credentials.

    Used for all v0.1 testing. Scenario drives outcome deterministically.
    """

    def __init__(
        self,
        scenario: FixtureScenario = FixtureScenario.SINGLE_PAGE_SUCCESS,
    ) -> None:
        self.scenario = scenario
        self._submitted = False
        self._fill_results: list[FillResult] = []
        self._evidence_counter = 0

    def set_scenario(self, scenario: FixtureScenario) -> None:
        self.scenario = scenario

    def inspect_form(self, url: str) -> FormInspection:
        has_captcha = self.scenario == FixtureScenario.CAPTCHA_INTERRUPTION
        has_mfa = self.scenario == FixtureScenario.MFA_INTERRUPTION
        is_multi = self.scenario == FixtureScenario.MULTI_STEP_SUCCESS

        fields = (
            FormField("field_name", "Full Name", FormFieldType.TEXT, required=True),
            FormField("field_email", "Email Address", FormFieldType.EMAIL, required=True),
            FormField("field_phone", "Phone Number", FormFieldType.PHONE, required=False),
            FormField("field_resume", "Resume Upload", FormFieldType.FILE, required=True),
            FormField("field_cover_letter", "Cover Letter", FormFieldType.TEXTAREA, required=False),
            FormField("field_linkedin", "LinkedIn URL", FormFieldType.TEXT, required=False),
            FormField("field_github", "GitHub URL", FormFieldType.TEXT, required=False),
            FormField(
                "field_authorized",
                "Are you authorized to work in the US?",
                FormFieldType.SELECT,
                required=True,
                options=("Yes", "No"),
            ),
        )

        self._evidence_counter += 1
        return FormInspection(
            form_id=f"fixture_form_{self.scenario.value}",
            page_title=f"Fixture ATS — {self.scenario.value}",
            fields=fields,
            has_captcha=has_captcha,
            has_mfa=has_mfa,
            is_multi_step=is_multi,
            current_step=1,
            total_steps=2 if is_multi else 1,
            evidence_ref=f"fixture_inspect_{self._evidence_counter:04d}",
        )

    def fill_fields(
        self,
        form_inspection: FormInspection,
        field_values: dict[str, str],
    ) -> tuple[FillResult, ...]:
        if form_inspection.has_captcha:
            raise HumanHandoffRequired(
                "CAPTCHA detected on fixture ATS form. Human intervention required.",
                handoff_type="captcha",
            )
        if form_inspection.has_mfa:
            raise HumanHandoffRequired(
                "MFA/login challenge detected. Human intervention required.",
                handoff_type="mfa",
            )

        results: list[FillResult] = []
        for field in form_inspection.fields:
            if field.field_type == FormFieldType.SUBMIT:
                continue
            value = field_values.get(field.field_id)
            if field.required and not value:
                results.append(FillResult(
                    field_id=field.field_id,
                    outcome=FillOutcome.VALIDATION_ERROR,
                    value_used=None,
                    validation_message=f"Required field '{field.label}' has no value",
                ))
            elif value is not None:
                results.append(FillResult(
                    field_id=field.field_id,
                    outcome=FillOutcome.SUCCESS,
                    value_used=value,
                    validation_message=None,
                ))

        if self.scenario == FixtureScenario.VALIDATION_ERROR:
            results.append(FillResult(
                field_id="field_email",
                outcome=FillOutcome.VALIDATION_ERROR,
                value_used=field_values.get("field_email"),
                validation_message="Email format validation failed (fixture)",
            ))

        self._fill_results = results
        return tuple(results)

    def submit_once(self, form_inspection: FormInspection) -> SubmissionResult:
        now = datetime.now(timezone.utc)
        self._evidence_counter += 1
        evidence_ref = f"fixture_submit_{self._evidence_counter:04d}"

        if self.scenario == FixtureScenario.NETWORK_LOSS_AFTER_SUBMIT:
            self._submitted = True
            return SubmissionResult(
                outcome=SubmissionOutcome.INDETERMINATE,
                confirmation_id=None,
                evidence_ref=evidence_ref,
                submitted_at=now,
                details=(
                    ("scenario", self.scenario.value),
                    ("reason", "Network connection lost after submit click; result unknown"),
                ),
            )

        if self.scenario == FixtureScenario.DUPLICATE_APPLICATION:
            return SubmissionResult(
                outcome=SubmissionOutcome.REJECTED,
                confirmation_id=None,
                evidence_ref=evidence_ref,
                submitted_at=now,
                details=(
                    ("scenario", self.scenario.value),
                    ("reason", "Duplicate application detected by ATS (fixture)"),
                ),
            )

        if self.scenario == FixtureScenario.DEFINITE_REJECTION:
            return SubmissionResult(
                outcome=SubmissionOutcome.REJECTED,
                confirmation_id=None,
                evidence_ref=evidence_ref,
                submitted_at=now,
                details=(("scenario", self.scenario.value),),
            )

        if self.scenario == FixtureScenario.CAPTCHA_INTERRUPTION:
            raise HumanHandoffRequired(
                "CAPTCHA required before submission. Human intervention required.",
                handoff_type="captcha",
            )

        # Default: successful submission
        self._submitted = True
        confirmation_id = f"fixture_conf_{now.strftime('%Y%m%d%H%M%S')}"
        return SubmissionResult(
            outcome=SubmissionOutcome.CONFIRMED,
            confirmation_id=confirmation_id,
            evidence_ref=evidence_ref,
            submitted_at=now,
            details=(
                ("scenario", self.scenario.value),
                ("confirmation_id", confirmation_id),
                ("page_title", "Application Submitted — Fixture ATS"),
            ),
        )

    def capture_evidence(self, label: str) -> str:
        self._evidence_counter += 1
        return f"fixture_evidence_{label}_{self._evidence_counter:04d}"
