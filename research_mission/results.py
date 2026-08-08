"""Immutable result and evidence contracts for governed research missions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from types import MappingProxyType
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from research_mission.models import ResearchRole


_MAPPING_PROXY_TYPE = type(MappingProxyType({}))
_MAX_METADATA_DEPTH = 64


def _identifier(value: str, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _optional_identifier(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, field_name)


def _normalize_timestamp(value: datetime, field_name: str) -> datetime:
    if type(value) is not datetime:
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    if type(value.tzinfo) not in (timezone, ZoneInfo):
        raise TypeError(
            f"{field_name} timezone must be datetime.timezone or zoneinfo.ZoneInfo"
        )
    try:
        offset = value.utcoffset()
    except Exception as exc:
        raise ValueError(f"{field_name} must be timezone-aware") from exc
    if offset is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    try:
        return value.astimezone(timezone.utc)
    except Exception as exc:
        raise ValueError(f"{field_name} must be a valid timezone-aware datetime") from exc


def _string_tuple(values, field_name: str) -> tuple[str, ...]:
    if type(values) not in (list, tuple):
        raise TypeError(f"{field_name} must be a list or tuple of strings")
    result = tuple(values)
    if any(type(value) is not str for value in result):
        raise TypeError(f"{field_name} must contain only strings")
    if any(not value.strip() for value in result):
        raise ValueError(f"{field_name} must contain only non-empty strings")
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} entries must be unique")
    return result


def _freeze_metadata(metadata) -> Mapping[str, Any]:
    if type(metadata) not in (dict, _MAPPING_PROXY_TYPE):
        raise TypeError("metadata must be a dict")
    return _freeze_metadata_value(metadata, "metadata", set(), 0)


def _freeze_metadata_value(
    value,
    field_name: str,
    ancestors: set[int],
    depth: int,
):
    value_type = type(value)
    if value is None or value_type in (bool, int, str):
        return value
    if value_type is float:
        if not isfinite(value):
            raise ValueError(f"{field_name} float values must be finite")
        return value
    if value_type in (dict, _MAPPING_PROXY_TYPE):
        if depth >= _MAX_METADATA_DEPTH:
            raise ValueError(
                f"{field_name} nesting exceeds maximum depth {_MAX_METADATA_DEPTH}"
            )
        identity = id(value)
        if identity in ancestors:
            raise ValueError(f"{field_name} must not be cyclic")
        if any(type(key) is not str for key in value):
            raise ValueError(f"{field_name} keys must be strings")
        ancestors.add(identity)
        try:
            frozen = {
                key: _freeze_metadata_value(
                    value[key],
                    f"{field_name}.{key}",
                    ancestors,
                    depth + 1,
                )
                for key in sorted(value)
            }
        finally:
            ancestors.remove(identity)
        return MappingProxyType(frozen)
    if value_type in (list, tuple):
        if depth >= _MAX_METADATA_DEPTH:
            raise ValueError(
                f"{field_name} nesting exceeds maximum depth {_MAX_METADATA_DEPTH}"
            )
        identity = id(value)
        if identity in ancestors:
            raise ValueError(f"{field_name} must not be cyclic")
        ancestors.add(identity)
        try:
            return tuple(
                _freeze_metadata_value(
                    item,
                    f"{field_name}[]",
                    ancestors,
                    depth + 1,
                )
                for item in value
            )
        finally:
            ancestors.remove(identity)
    raise TypeError(
        f"{field_name} values must be JSON-compatible primitive, list, or dict values"
    )


def _metadata_identity(value):
    """Return a typed canonical identity that does not conflate bool and int."""

    value_type = type(value)
    if value is None:
        return ("null",)
    if value_type is bool:
        return ("bool", value)
    if value_type is int:
        return ("int", value)
    if value_type is float:
        return ("float", value.hex())
    if value_type is str:
        return ("string", value)
    if value_type is tuple:
        return ("array", tuple(_metadata_identity(item) for item in value))
    if value_type is _MAPPING_PROXY_TYPE:
        return (
            "object",
            tuple((key, _metadata_identity(value[key])) for key in value),
        )
    raise TypeError("metadata contains a non-canonical value")


class ResearchTaskResultStatus(str, Enum):
    """Terminal outcomes reported by a planned research task."""

    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(slots=True, frozen=True)
class ChallengerAssessment:
    """A structural challenge to supplied research findings."""

    challenged_findings: tuple[str, ...] = ()
    weaknesses: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    alternative_explanations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "challenged_findings",
            "weaknesses",
            "contradictions",
            "missing_evidence",
            "alternative_explanations",
        ):
            object.__setattr__(
                self,
                field_name,
                _string_tuple(getattr(self, field_name), field_name),
            )
        if not any(
            (
                self.challenged_findings,
                self.weaknesses,
                self.contradictions,
                self.missing_evidence,
                self.alternative_explanations,
            )
        ):
            raise ValueError("challenger assessment requires at least one structural entry")


class JudgeOutcome(str, Enum):
    """Governed outcome selected by a judge task, without runtime inference."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CONTESTED = "contested"
    INCONCLUSIVE = "inconclusive"


@dataclass(slots=True, frozen=True)
class JudgeDecision:
    """A supplied judge evaluation over earlier research task outputs."""

    outcome: JudgeOutcome
    rationale: str
    confidence: float | None = None
    accepted_findings: tuple[str, ...] = ()
    rejected_findings: tuple[str, ...] = ()
    contested_findings: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.outcome) is not JudgeOutcome:
            raise TypeError("outcome must be a JudgeOutcome")
        object.__setattr__(self, "rationale", _identifier(self.rationale, "rationale"))
        if self.confidence is not None:
            if type(self.confidence) not in (int, float):
                raise TypeError("confidence must be a number or None")
            confidence = float(self.confidence)
            if not isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                raise ValueError("confidence must be finite and between 0 and 1")
            object.__setattr__(self, "confidence", confidence)
        for field_name in (
            "accepted_findings",
            "rejected_findings",
            "contested_findings",
            "unresolved_questions",
        ):
            object.__setattr__(
                self,
                field_name,
                _string_tuple(getattr(self, field_name), field_name),
            )


def _copy_challenger_assessment(
    value: ChallengerAssessment | None,
) -> ChallengerAssessment | None:
    if value is None:
        return None
    if type(value) is not ChallengerAssessment:
        raise TypeError("challenger_assessment must be a ChallengerAssessment or None")
    return ChallengerAssessment(
        challenged_findings=value.challenged_findings,
        weaknesses=value.weaknesses,
        contradictions=value.contradictions,
        missing_evidence=value.missing_evidence,
        alternative_explanations=value.alternative_explanations,
    )


def _copy_judge_decision(value: JudgeDecision | None) -> JudgeDecision | None:
    if value is None:
        return None
    if type(value) is not JudgeDecision:
        raise TypeError("judge_decision must be a JudgeDecision or None")
    return JudgeDecision(
        outcome=value.outcome,
        rationale=value.rationale,
        confidence=value.confidence,
        accepted_findings=value.accepted_findings,
        rejected_findings=value.rejected_findings,
        contested_findings=value.contested_findings,
        unresolved_questions=value.unresolved_questions,
    )


@dataclass(slots=True, frozen=True, eq=False)
class ResearchTaskResult:
    """The immutable terminal result supplied for one planned research task."""

    mission_id: str
    task_id: str
    role: ResearchRole
    status: ResearchTaskResultStatus
    summary: str
    produced_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)
    challenger_assessment: ChallengerAssessment | None = None
    judge_decision: JudgeDecision | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "mission_id", _identifier(self.mission_id, "mission_id"))
        object.__setattr__(self, "task_id", _identifier(self.task_id, "task_id"))
        if type(self.role) is not ResearchRole:
            raise TypeError("role must be a ResearchRole")
        if type(self.status) is not ResearchTaskResultStatus:
            raise TypeError("status must be a ResearchTaskResultStatus")
        object.__setattr__(self, "summary", _identifier(self.summary, "summary"))
        object.__setattr__(
            self,
            "produced_at",
            _normalize_timestamp(self.produced_at, "produced_at"),
        )
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))
        object.__setattr__(
            self,
            "challenger_assessment",
            _copy_challenger_assessment(self.challenger_assessment),
        )
        object.__setattr__(
            self,
            "judge_decision",
            _copy_judge_decision(self.judge_decision),
        )

        if self.role is ResearchRole.RESEARCHER:
            if self.challenger_assessment is not None or self.judge_decision is not None:
                raise ValueError("researcher result must not contain role-specific payloads")
        elif self.role is ResearchRole.CHALLENGER:
            if self.judge_decision is not None:
                raise ValueError("challenger result must not contain a judge decision")
            if (
                self.status is ResearchTaskResultStatus.COMPLETED
                and self.challenger_assessment is None
            ):
                raise ValueError(
                    "completed challenger result requires challenger_assessment"
                )
        elif self.role is ResearchRole.JUDGE:
            if self.challenger_assessment is not None:
                raise ValueError("judge result must not contain a challenger assessment")
            if (
                self.status is ResearchTaskResultStatus.COMPLETED
                and self.judge_decision is None
            ):
                raise ValueError("completed judge result requires judge_decision")

    @property
    def finding(self) -> str:
        """Backward-readable alias for the normalized result summary."""

        return self.summary

    def __eq__(self, other) -> bool:
        if type(other) is not ResearchTaskResult:
            return NotImplemented
        return _result_identity(self) == _result_identity(other)


@dataclass(slots=True, frozen=True, eq=False)
class ResearchEvidence:
    """Supplied evidence attributable to exactly one planned task result."""

    evidence_id: str
    mission_id: str
    task_id: str
    evidence_type: str
    source: str
    summary: str
    observed_at: datetime
    reference: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "evidence_id",
            "mission_id",
            "task_id",
            "evidence_type",
            "source",
            "summary",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "reference",
            _optional_identifier(self.reference, "reference"),
        )
        object.__setattr__(
            self,
            "observed_at",
            _normalize_timestamp(self.observed_at, "observed_at"),
        )
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))

    @property
    def category(self) -> str:
        return self.evidence_type

    def __eq__(self, other) -> bool:
        if type(other) is not ResearchEvidence:
            return NotImplemented
        return _evidence_identity(self) == _evidence_identity(other)


def _result_identity(result: ResearchTaskResult):
    challenge = result.challenger_assessment
    decision = result.judge_decision
    return (
        result.mission_id,
        result.task_id,
        result.role.value,
        result.status.value,
        result.summary,
        result.produced_at.isoformat(),
        _metadata_identity(result.metadata),
        None
        if challenge is None
        else (
            challenge.challenged_findings,
            challenge.weaknesses,
            challenge.contradictions,
            challenge.missing_evidence,
            challenge.alternative_explanations,
        ),
        None
        if decision is None
        else (
            decision.outcome.value,
            decision.rationale,
            decision.confidence,
            decision.accepted_findings,
            decision.rejected_findings,
            decision.contested_findings,
            decision.unresolved_questions,
        ),
    )


def _evidence_identity(evidence: ResearchEvidence):
    return (
        evidence.evidence_id,
        evidence.mission_id,
        evidence.task_id,
        evidence.evidence_type,
        evidence.source,
        evidence.summary,
        evidence.observed_at.isoformat(),
        evidence.reference,
        _metadata_identity(evidence.metadata),
    )


def _copy_result(result: ResearchTaskResult) -> ResearchTaskResult:
    if type(result) is not ResearchTaskResult:
        raise TypeError("result must be a ResearchTaskResult")
    return ResearchTaskResult(
        mission_id=result.mission_id,
        task_id=result.task_id,
        role=result.role,
        status=result.status,
        summary=result.summary,
        produced_at=result.produced_at,
        metadata=result.metadata,
        challenger_assessment=result.challenger_assessment,
        judge_decision=result.judge_decision,
    )


def _copy_evidence(evidence: ResearchEvidence) -> ResearchEvidence:
    if type(evidence) is not ResearchEvidence:
        raise TypeError("evidence must contain only ResearchEvidence values")
    return ResearchEvidence(
        evidence_id=evidence.evidence_id,
        mission_id=evidence.mission_id,
        task_id=evidence.task_id,
        evidence_type=evidence.evidence_type,
        source=evidence.source,
        summary=evidence.summary,
        observed_at=evidence.observed_at,
        reference=evidence.reference,
        metadata=evidence.metadata,
    )
