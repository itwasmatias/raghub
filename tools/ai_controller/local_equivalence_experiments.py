"""Immutable, provider-neutral contracts for Local Equivalence experiments.

These records describe measurements produced by governed systems. They do not
execute tasks, call models, select providers, or establish capability claims.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from types import MappingProxyType
from typing import Any, Mapping

from tools.ai_controller.local_capability_baseline import (
    LocalityType,
    TaskMeasurement,
    TaskOutcome,
)


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ExperimentError(ValueError):
    """Base experiment contract error."""


class ExperimentContractError(ExperimentError):
    """Experiment evidence does not compose exactly."""


class ExperimentConflictError(ExperimentError):
    """An authoritative identity was reused with different content."""


class ExperimentArmType(str, Enum):
    CLOUD_FRONTIER_BASELINE = "cloud_frontier_baseline"
    SINGLE_LOCAL = "single_local"
    LOCAL_WITH_TOOLS = "local_with_tools"
    INDEPENDENT_LOCAL_ATTEMPTS = "independent_local_attempts"
    LOCAL_WITH_VERIFIER = "local_with_verifier"
    LOCAL_WITH_PERSISTENT_MEMORY = "local_with_persistent_memory"
    HYBRID_RARE_CLOUD_ESCALATION = "hybrid_rare_cloud_escalation"


class ExperimentRunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ExperimentContractError(f"{name} must be a canonical identifier")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ExperimentContractError(f"{name} must be canonical non-empty text")
    return value


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ExperimentContractError(f"{name} must be lowercase SHA-256 hex")
    return value


def _timestamp(value: Any, name: str) -> str:
    from datetime import datetime
    if not isinstance(value, str):
        raise ExperimentContractError(f"{name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExperimentContractError(f"{name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExperimentContractError(f"{name} must be timezone-aware")
    return value


def _identifiers(values: Any, name: str, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list, set, frozenset)):
        raise ExperimentContractError(f"{name} must be a collection")
    result = tuple(sorted(_identifier(item, f"{name} entry") for item in values))
    if len(set(result)) != len(result):
        raise ExperimentContractError(f"{name} contains duplicate identity")
    if required and not result:
        raise ExperimentContractError(f"{name} must not be empty")
    return result


def _public(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _public(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_public(child) for child in value]
    if isinstance(value, Enum):
        return value.value
    return value


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(_public(value), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise ExperimentContractError("record must contain finite JSON values") from exc


def _hash(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value)).hexdigest()


def _freeze_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExperimentContractError(f"{name} must be a mapping")
    try:
        snapshot = json.loads(_canonical(value))
    except ExperimentContractError as exc:
        raise ExperimentContractError(f"{name} must contain finite JSON values") from exc

    def freeze(item):
        if isinstance(item, dict):
            return MappingProxyType({key: freeze(child) for key, child in item.items()})
        if isinstance(item, list):
            return tuple(freeze(child) for child in item)
        return item

    return freeze(snapshot)


def _finite_nonnegative(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ExperimentContractError(f"{name} must be numeric")
    if not math.isfinite(value):
        raise ExperimentContractError(f"{name} must be finite")
    if value < 0:
        raise ExperimentContractError(f"{name} cannot be negative")
    return float(value)


def _validate_resource_tree(value: Any, name: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            _identifier(key, f"{name} name")
            _validate_resource_tree(child, f"{name}.{key}")
        return
    if isinstance(value, tuple):
        for index, child in enumerate(value):
            _validate_resource_tree(child, f"{name}[{index}]")
        return
    _finite_nonnegative(value, name)


@dataclass(frozen=True, slots=True)
class ExperimentArm:
    arm_id: str
    arm_type: ExperimentArmType
    execution_strategy: str
    locality: LocalityType
    description: str
    required_capabilities: frozenset[str]

    def __post_init__(self):
        _identifier(self.arm_id, "arm_id")
        _identifier(self.execution_strategy, "execution_strategy")
        _text(self.description, "description")
        try:
            object.__setattr__(self, "arm_type", ExperimentArmType(self.arm_type))
            object.__setattr__(self, "locality", LocalityType(self.locality))
        except (TypeError, ValueError) as exc:
            raise ExperimentContractError("unsupported experiment arm or locality") from exc
        capabilities = frozenset(_identifiers(self.required_capabilities,
            "required_capabilities"))
        object.__setattr__(self, "required_capabilities", capabilities)
        expected = (LocalityType.CLOUD if self.arm_type is ExperimentArmType.CLOUD_FRONTIER_BASELINE
            else LocalityType.HYBRID if self.arm_type is ExperimentArmType.HYBRID_RARE_CLOUD_ESCALATION
            else LocalityType.LOCAL)
        if self.locality is not expected:
            raise ExperimentContractError("experiment arm locality is contradictory")

    def to_dict(self):
        return {"arm_id": self.arm_id, "arm_type": self.arm_type.value,
            "execution_strategy": self.execution_strategy, "locality": self.locality.value,
            "description": self.description,
            "required_capabilities": sorted(self.required_capabilities)}


@dataclass(frozen=True, slots=True)
class ExperimentTask:
    experiment_id: str
    task_id: str
    description: str
    expected_result: str
    required_capabilities: frozenset[str]
    source_evidence_ids: tuple[str, ...]

    def __post_init__(self):
        _identifier(self.experiment_id, "experiment_id")
        _identifier(self.task_id, "task_id")
        _text(self.description, "description")
        _text(self.expected_result, "expected_result")
        object.__setattr__(self, "required_capabilities", frozenset(
            _identifiers(self.required_capabilities, "required_capabilities")))
        object.__setattr__(self, "source_evidence_ids",
            _identifiers(self.source_evidence_ids, "source_evidence_ids", required=True))

    def to_dict(self):
        return {"experiment_id": self.experiment_id, "task_id": self.task_id,
            "description": self.description, "expected_result": self.expected_result,
            "required_capabilities": sorted(self.required_capabilities),
            "source_evidence_ids": list(self.source_evidence_ids)}


@dataclass(frozen=True, slots=True)
class ExperimentDefinition:
    experiment_id: str
    created_at: str
    purpose: str
    arms: tuple[ExperimentArm, ...]
    tasks: tuple[ExperimentTask, ...]

    def __post_init__(self):
        _identifier(self.experiment_id, "experiment_id")
        _timestamp(self.created_at, "created_at")
        _text(self.purpose, "purpose")
        if not all(type(item) is ExperimentArm for item in self.arms):
            raise ExperimentContractError("arms must contain ExperimentArm values")
        if not all(type(item) is ExperimentTask for item in self.tasks):
            raise ExperimentContractError("tasks must contain ExperimentTask values")
        arms = tuple(sorted(self.arms, key=lambda item: item.arm_id))
        tasks = tuple(sorted(self.tasks, key=lambda item: item.task_id))
        if not arms or len({item.arm_id for item in arms}) != len(arms):
            raise ExperimentContractError("arms must have unique identities")
        if not tasks or len({item.task_id for item in tasks}) != len(tasks):
            raise ExperimentContractError("tasks must have unique identities")
        if any(item.experiment_id != self.experiment_id for item in tasks):
            raise ExperimentContractError("foreign task in experiment definition")
        object.__setattr__(self, "arms", arms)
        object.__setattr__(self, "tasks", tasks)

    def definition_fingerprint(self) -> str:
        return hashlib.sha256(_canonical({"experiment_id": self.experiment_id,
            "created_at": self.created_at, "purpose": self.purpose,
            "arms": [item.to_dict() for item in self.arms],
            "tasks": [item.to_dict() for item in self.tasks]})).hexdigest()

    def to_dict(self):
        return {"experiment_id": self.experiment_id, "created_at": self.created_at,
            "purpose": self.purpose, "arms": [item.to_dict() for item in self.arms],
            "tasks": [item.to_dict() for item in self.tasks],
            "definition_fingerprint": self.definition_fingerprint()}


@dataclass(frozen=True, slots=True)
class ExperimentEvidence:
    experiment_id: str
    arm_id: str
    task_id: str
    task_result_id: str
    evidence_ids: tuple[str, ...]
    evaluation_id: str | None = None
    confidence_id: str | None = None
    checkpoint_id: str | None = None
    recovery_id: str | None = None

    def __post_init__(self):
        for name in ("experiment_id", "arm_id", "task_id", "task_result_id"):
            _identifier(getattr(self, name), name)
        object.__setattr__(self, "evidence_ids",
            _identifiers(self.evidence_ids, "evidence_ids", required=True))
        for name in ("evaluation_id", "confidence_id", "checkpoint_id", "recovery_id"):
            value = getattr(self, name)
            if value is not None:
                _identifier(value, name)

    def to_dict(self):
        return {"experiment_id": self.experiment_id, "arm_id": self.arm_id,
            "task_id": self.task_id, "task_result_id": self.task_result_id,
            "evidence_ids": list(self.evidence_ids), "evaluation_id": self.evaluation_id,
            "confidence_id": self.confidence_id, "checkpoint_id": self.checkpoint_id,
            "recovery_id": self.recovery_id}


@dataclass(frozen=True, slots=True)
class VerificationAssessment:
    assessment_id: str
    experiment_id: str
    arm_id: str
    task_id: str
    run_id: str
    verifier_id: str
    correct: bool | None
    confidence: float
    evidence_ids: tuple[str, ...]
    rationale: str
    is_proof: bool = False

    def __post_init__(self):
        for name in ("assessment_id", "experiment_id", "arm_id", "task_id",
                     "run_id", "verifier_id"):
            _identifier(getattr(self, name), name)
        if self.correct is not None and not isinstance(self.correct, bool):
            raise ExperimentContractError("correct must be boolean or None")
        confidence = _finite_nonnegative(self.confidence, "confidence")
        if confidence > 1:
            raise ExperimentContractError("confidence must be within [0, 1]")
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "evidence_ids",
            _identifiers(self.evidence_ids, "verification evidence_ids", required=True))
        _text(self.rationale, "rationale")
        if not isinstance(self.is_proof, bool):
            raise ExperimentContractError("is_proof must be boolean")
        if self.is_proof:
            raise ExperimentContractError("verification or agreement cannot be represented as proof")

    def to_dict(self):
        return {"assessment_id": self.assessment_id, "experiment_id": self.experiment_id,
            "arm_id": self.arm_id, "task_id": self.task_id, "run_id": self.run_id,
            "verifier_id": self.verifier_id, "correct": self.correct,
            "confidence": self.confidence, "evidence_ids": list(self.evidence_ids),
            "rationale": self.rationale, "is_proof": self.is_proof}


@dataclass(frozen=True, slots=True)
class ExperimentAttempt:
    experiment_id: str
    arm_id: str
    task_id: str
    run_id: str
    attempt_id: str
    worker_node_id: str
    capability_profile_fingerprint: str
    execution_strategy: str
    measurement: TaskMeasurement
    evidence: ExperimentEvidence
    verification: VerificationAssessment | None
    resource_measurements: Mapping[str, Any]

    def __post_init__(self):
        for name in ("experiment_id", "arm_id", "task_id", "run_id", "attempt_id",
                     "worker_node_id", "execution_strategy"):
            _identifier(getattr(self, name), name)
        _digest(self.capability_profile_fingerprint, "capability_profile_fingerprint")
        if type(self.measurement) is not TaskMeasurement:
            raise ExperimentContractError("measurement must be TaskMeasurement")
        if type(self.evidence) is not ExperimentEvidence:
            raise ExperimentContractError("evidence must be ExperimentEvidence")
        if self.verification is not None and type(self.verification) is not VerificationAssessment:
            raise ExperimentContractError("verification must be VerificationAssessment or None")
        expected = (self.experiment_id, self.arm_id, self.task_id)
        if (self.evidence.experiment_id, self.evidence.arm_id, self.evidence.task_id) != expected:
            raise ExperimentContractError("foreign experiment evidence")
        measurement = self.measurement
        if (measurement.task_id != self.task_id or measurement.worker_id != self.worker_node_id
                or measurement.profile_fingerprint != self.capability_profile_fingerprint
                or measurement.execution_strategy != self.execution_strategy):
            raise ExperimentContractError("measurement provenance substitution")
        if self.verification is not None and (
            self.verification.experiment_id, self.verification.arm_id,
            self.verification.task_id, self.verification.run_id
        ) != (self.experiment_id, self.arm_id, self.task_id, self.run_id):
            raise ExperimentContractError("foreign verification assessment")
        resources = _freeze_mapping(self.resource_measurements, "resource_measurements")
        _validate_resource_tree(resources, "resource_measurements")
        object.__setattr__(self, "resource_measurements", resources)

    def attempt_fingerprint(self) -> str:
        return hashlib.sha256(_canonical(self.to_dict(include_fingerprint=False))).hexdigest()

    def to_dict(self, *, include_fingerprint=True):
        payload = {"experiment_id": self.experiment_id, "arm_id": self.arm_id,
            "task_id": self.task_id, "run_id": self.run_id, "attempt_id": self.attempt_id,
            "worker_node_id": self.worker_node_id,
            "capability_profile_fingerprint": self.capability_profile_fingerprint,
            "execution_strategy": self.execution_strategy,
            "measurement": self.measurement.to_dict(), "evidence": self.evidence.to_dict(),
            "verification": None if self.verification is None else self.verification.to_dict(),
            "resource_measurements": _public(self.resource_measurements)}
        if include_fingerprint:
            payload["attempt_fingerprint"] = self.attempt_fingerprint()
        return payload


@dataclass(frozen=True, slots=True)
class ExperimentMetrics:
    task_count: int
    successful_task_count: int
    correct_task_count: int
    assessed_task_count: int
    elapsed_seconds: float
    attempt_count: int
    retry_count: int
    human_intervention_count: int
    mean_verification_confidence: float | None
    verification_disagreement_task_count: int
    evidence_reference_count: int
    resource_totals: Mapping[str, float]
    marginal_cloud_cost_usd: float | None
    cloud_escalation_count: int
    zero_cloud_cost_attempt_count: int

    def __post_init__(self):
        object.__setattr__(self, "resource_totals",
            _freeze_mapping(self.resource_totals, "resource_totals"))

    def to_dict(self):
        return {"task_count": self.task_count,
            "successful_task_count": self.successful_task_count,
            "correct_task_count": self.correct_task_count,
            "assessed_task_count": self.assessed_task_count,
            "elapsed_seconds": self.elapsed_seconds, "attempt_count": self.attempt_count,
            "retry_count": self.retry_count,
            "human_intervention_count": self.human_intervention_count,
            "mean_verification_confidence": self.mean_verification_confidence,
            "verification_disagreement_task_count": self.verification_disagreement_task_count,
            "evidence_reference_count": self.evidence_reference_count,
            "resource_totals": _public(self.resource_totals),
            "marginal_cloud_cost_usd": self.marginal_cloud_cost_usd,
            "cloud_escalation_count": self.cloud_escalation_count,
            "zero_cloud_cost_attempt_count": self.zero_cloud_cost_attempt_count}


def _metrics(task_ids: tuple[str, ...], attempts: tuple[ExperimentAttempt, ...]) -> ExperimentMetrics:
    successful = {item.task_id for item in attempts if item.measurement.success}
    assessments: dict[str, list[VerificationAssessment]] = {}
    for item in attempts:
        if item.verification is not None:
            assessments.setdefault(item.task_id, []).append(item.verification)
    correct = {task_id for task_id, values in assessments.items()
        if values and all(item.correct is True for item in values)}
    disagreements = sum(1 for values in assessments.values()
        if len({item.correct for item in values}) > 1)
    confidences = [item.confidence for values in assessments.values() for item in values]
    evidence = {identity for item in attempts for identity in (
        *item.evidence.evidence_ids,
        *(item.verification.evidence_ids if item.verification else ()),
    )}
    resource_totals: dict[str, float] = {}
    for item in attempts:
        for key, value in item.resource_measurements.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                resource_totals[key] = resource_totals.get(key, 0.0) + float(value)
    costs = [item.measurement.estimated_cloud_cost_usd for item in attempts
        if item.measurement.estimated_cloud_cost_usd is not None]
    return ExperimentMetrics(len(task_ids), len(successful), len(correct), len(assessments),
        sum(item.measurement.elapsed_seconds or 0.0 for item in attempts), len(attempts),
        sum(item.measurement.retry_count for item in attempts),
        sum(item.measurement.human_intervention_count for item in attempts),
        None if not confidences else sum(confidences) / len(confidences), disagreements,
        len(evidence),
        resource_totals, None if not costs else sum(costs),
        sum(item.measurement.cloud_escalation_count for item in attempts),
        sum(1 for item in attempts if item.measurement.zero_cloud_cost))


@dataclass(frozen=True, slots=True)
class ExperimentRun:
    experiment_id: str
    arm_id: str
    run_id: str
    definition_fingerprint: str
    status: ExperimentRunStatus
    task_ids: tuple[str, ...]
    attempts: tuple[ExperimentAttempt, ...]
    observed_at: str
    metrics: ExperimentMetrics = field(init=False)

    def __post_init__(self):
        for name in ("experiment_id", "arm_id", "run_id"):
            _identifier(getattr(self, name), name)
        _digest(self.definition_fingerprint, "definition_fingerprint")
        try:
            object.__setattr__(self, "status", ExperimentRunStatus(self.status))
        except (TypeError, ValueError) as exc:
            raise ExperimentContractError("unsupported run status") from exc
        task_ids = _identifiers(self.task_ids, "task_ids", required=True)
        if not all(type(item) is ExperimentAttempt for item in self.attempts):
            raise ExperimentContractError("attempts must contain ExperimentAttempt values")
        attempts = tuple(sorted(self.attempts, key=lambda item: item.attempt_id))
        if len({item.attempt_id for item in attempts}) != len(attempts):
            raise ExperimentContractError("duplicate experiment attempt identity")
        for field_name, identities in (
            ("measurement", [item.measurement.measurement_id for item in attempts]),
            ("task result", [item.evidence.task_result_id for item in attempts]),
            ("verification", [item.verification.assessment_id for item in attempts
                              if item.verification is not None]),
        ):
            if len(set(identities)) != len(identities):
                raise ExperimentContractError(f"duplicate {field_name} identity")
        if any((item.experiment_id, item.arm_id, item.run_id) !=
               (self.experiment_id, self.arm_id, self.run_id) for item in attempts):
            raise ExperimentContractError("foreign attempt in experiment run")
        if any(item.task_id not in task_ids for item in attempts):
            raise ExperimentContractError("foreign task attempt in experiment run")
        if self.status is ExperimentRunStatus.COMPLETED and (
            {item.task_id for item in attempts} != set(task_ids)):
            raise ExperimentContractError("completed run lacks task attempt provenance")
        _timestamp(self.observed_at, "observed_at")
        object.__setattr__(self, "task_ids", task_ids)
        object.__setattr__(self, "attempts", attempts)
        object.__setattr__(self, "metrics", _metrics(task_ids, attempts))

    def run_fingerprint(self) -> str:
        return hashlib.sha256(_canonical(self.to_dict(include_fingerprint=False))).hexdigest()

    def to_dict(self, *, include_fingerprint=True):
        payload = {"experiment_id": self.experiment_id, "arm_id": self.arm_id,
            "run_id": self.run_id, "definition_fingerprint": self.definition_fingerprint,
            "status": self.status.value, "task_ids": list(self.task_ids),
            "attempts": [item.to_dict() for item in self.attempts],
            "observed_at": self.observed_at, "metrics": self.metrics.to_dict()}
        if include_fingerprint:
            payload["run_fingerprint"] = self.run_fingerprint()
        return payload


@dataclass(frozen=True, slots=True)
class ExperimentComparison:
    comparison_id: str
    experiment_id: str
    definition_fingerprint: str
    run_ids: tuple[str, ...]
    metrics_by_arm: tuple[tuple[str, ExperimentMetrics], ...]

    def to_dict(self):
        return {"comparison_id": self.comparison_id, "experiment_id": self.experiment_id,
            "definition_fingerprint": self.definition_fingerprint,
            "run_ids": list(self.run_ids),
            "metrics_by_arm": [{"arm_id": arm_id, "metrics": metrics.to_dict()}
                for arm_id, metrics in self.metrics_by_arm]}


class ExperimentCoordinator:
    """Thread-safe in-memory identity authority; no execution or persistence."""

    def __init__(self, definition: ExperimentDefinition):
        if type(definition) is not ExperimentDefinition:
            raise TypeError("definition must be ExperimentDefinition")
        self._definition = definition
        self._definition_fingerprint = definition.definition_fingerprint()
        self._runs: dict[str, ExperimentRun] = {}
        self._lock = RLock()

    def _require_definition(self):
        if (self._definition.experiment_id == "" or
                self._definition.definition_fingerprint() != self._definition_fingerprint):
            raise ExperimentContractError("authoritative experiment definition mutated")

    def register_run(self, run: ExperimentRun) -> ExperimentRun:
        if type(run) is not ExperimentRun:
            raise TypeError("run must be ExperimentRun")
        with self._lock:
            self._require_definition()
            if run.experiment_id != self._definition.experiment_id:
                raise ExperimentContractError("foreign experiment run")
            if run.definition_fingerprint != self._definition_fingerprint:
                raise ExperimentContractError("experiment definition fingerprint substitution")
            arms = {item.arm_id: item for item in self._definition.arms}
            tasks = {item.task_id: item for item in self._definition.tasks}
            selected = arms.get(run.arm_id)
            if selected is None or set(run.task_ids) - set(tasks):
                raise ExperimentContractError("foreign arm or task in run")
            for item in run.attempts:
                task = tasks[item.task_id]
                measurement = item.measurement
                if (item.execution_strategy != selected.execution_strategy
                        or measurement.locality is not selected.locality
                        or not task.required_capabilities.issubset(selected.required_capabilities)):
                    raise ExperimentContractError("arm, capability, or locality provenance mismatch")
                if measurement.zero_cloud_cost and (measurement.estimated_cloud_cost_usd or 0) != 0:
                    raise ExperimentContractError("zero-cloud-cost evidence contradicts measured cost")
                if selected.locality is LocalityType.LOCAL and (
                        measurement.cloud_escalation_count != 0
                        or not measurement.zero_cloud_cost
                        or (measurement.estimated_cloud_cost_usd or 0) != 0):
                    raise ExperimentContractError("local arm contains cloud execution evidence")
            if selected.arm_type is ExperimentArmType.INDEPENDENT_LOCAL_ATTEMPTS:
                for task_id in run.task_ids:
                    workers = {item.worker_node_id for item in run.attempts
                        if item.task_id == task_id}
                    if len(workers) < 2:
                        raise ExperimentContractError(
                            "independent local attempts require distinct workers")
            if selected.arm_type is ExperimentArmType.LOCAL_WITH_VERIFIER and any(
                    item.verification is None for item in run.attempts):
                raise ExperimentContractError("verifier arm requires verification evidence")
            prior = self._runs.get(run.run_id)
            if prior is not None:
                if prior.run_fingerprint() != run.run_fingerprint():
                    raise ExperimentConflictError("run identity conflicts with authoritative record")
                return prior
            new_provenance = {
                identity for item in run.attempts for identity in (
                    item.measurement.measurement_id, item.evidence.task_result_id,
                    *item.evidence.evidence_ids,
                    *((item.verification.assessment_id, *item.verification.evidence_ids)
                      if item.verification is not None else ()),
                )
            }
            for existing in self._runs.values():
                existing_provenance = {
                    identity for item in existing.attempts for identity in (
                        item.measurement.measurement_id, item.evidence.task_result_id,
                        *item.evidence.evidence_ids,
                        *((item.verification.assessment_id, *item.verification.evidence_ids)
                          if item.verification is not None else ()),
                    )
                }
                if new_provenance & existing_provenance:
                    raise ExperimentContractError("experiment run reuses provenance identity")
            self._runs[run.run_id] = run
            return run

    def inspect_run(self, run_id: str) -> ExperimentRun:
        _identifier(run_id, "run_id")
        with self._lock:
            self._require_definition()
            try:
                return self._runs[run_id]
            except KeyError as exc:
                raise ExperimentContractError("unknown experiment run") from exc

    def compare(self, runs: tuple[ExperimentRun, ...]) -> ExperimentComparison:
        items = tuple(runs)
        with self._lock:
            self._require_definition()
            if not items or not all(type(item) is ExperimentRun for item in items):
                raise ExperimentContractError("comparison requires experiment runs")
            if len({item.run_id for item in items}) != len(items):
                raise ExperimentContractError("comparison contains duplicate run identity")
            for item in items:
                authoritative = self._runs.get(item.run_id)
                if authoritative is None or authoritative.run_fingerprint() != item.run_fingerprint():
                    raise ExperimentContractError("comparison contains foreign run evidence")
            ordered = tuple(sorted(items, key=lambda item: (item.arm_id, item.run_id)))
            if len({item.arm_id for item in ordered}) != len(ordered):
                raise ExperimentContractError("comparison requires at most one run per arm")
            payload = {"experiment_id": self._definition.experiment_id,
                "definition_fingerprint": self._definition_fingerprint,
                "runs": [(item.arm_id, item.run_id, item.run_fingerprint()) for item in ordered]}
            return ExperimentComparison(_hash("comparison-", payload),
                self._definition.experiment_id, self._definition_fingerprint,
                tuple(item.run_id for item in ordered),
                tuple((item.arm_id, item.metrics) for item in ordered))
