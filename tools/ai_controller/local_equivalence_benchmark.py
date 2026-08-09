"""
Local Equivalence Benchmark Harness v0.1

Deterministic benchmark execution harness that consumes existing Local Equivalence
Experiment Contracts. Coordinates experiments, collects measurements, and generates
reproducible manifests.

DOES NOT:
- Run expensive benchmarks yet
- Call paid model APIs
- Hardcode model providers
- Make capability claims
- Duplicate routing/execution/research/persistence systems

This is a COORDINATION layer that uses existing experiment contracts for measurement.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from tools.ai_controller.local_capability_baseline import LocalityType, TaskMeasurement
from tools.ai_controller.local_equivalence_experiments import (
    ExperimentArm,
    ExperimentAttempt,
    ExperimentCoordinator,
    ExperimentDefinition,
    ExperimentEvidence,
    ExperimentRun,
    ExperimentRunStatus,
    ExperimentTask,
    VerificationAssessment,
)


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class BenchmarkError(ValueError):
    """Base benchmark harness error."""


class BenchmarkContractError(BenchmarkError):
    """Benchmark contract violation."""


class BenchmarkExecutionError(BenchmarkError):
    """Benchmark execution error."""


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise BenchmarkContractError(f"{name} must be a canonical identifier")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise BenchmarkContractError(f"{name} must be canonical non-empty text")
    return value


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise BenchmarkContractError(f"{name} must be lowercase SHA-256 hex")
    return value


def _timestamp(value: Any, name: str) -> str:
    from datetime import datetime
    if not isinstance(value, str):
        raise BenchmarkContractError(f"{name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BenchmarkContractError(f"{name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BenchmarkContractError(f"{name} must be timezone-aware")
    return value


def _identifiers(values: Any, name: str, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list, set, frozenset)):
        raise BenchmarkContractError(f"{name} must be a collection")
    result = tuple(sorted(_identifier(item, f"{name} entry") for item in values))
    if len(set(result)) != len(result):
        raise BenchmarkContractError(f"{name} contains duplicate identity")
    if required and not result:
        raise BenchmarkContractError(f"{name} must not be empty")
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
        raise BenchmarkContractError("record must contain finite JSON values") from exc


def _freeze_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BenchmarkContractError(f"{name} must be a mapping")
    try:
        snapshot = json.loads(_canonical(value))
    except BenchmarkContractError as exc:
        raise BenchmarkContractError(f"{name} must contain finite JSON values") from exc

    def freeze(item):
        if isinstance(item, dict):
            return MappingProxyType({key: freeze(child) for key, child in item.items()})
        if isinstance(item, list):
            return tuple(freeze(child) for child in item)
        return item

    return freeze(snapshot)


def _finite_nonnegative(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise BenchmarkContractError(f"{name} must be numeric")
    if not math.isfinite(value):
        raise BenchmarkContractError(f"{name} must be finite")
    if value < 0:
        raise BenchmarkContractError(f"{name} cannot be negative")
    return float(value)


class TaskDifficulty(str, Enum):
    """Task difficulty classification."""
    TRIVIAL = "trivial"
    SIMPLE = "simple"
    MODERATE = "moderate"
    CHALLENGING = "challenging"
    EXPERT = "expert"
    UNKNOWN = "unknown"


class CorrectnessType(str, Enum):
    """Type of correctness evaluation."""
    OBJECTIVE = "objective"              # Has definitive ground truth
    EVALUATOR_JUDGMENT = "evaluator_judgment"  # Requires evaluator judgment
    VERIFICATION_CONSENSUS = "verification_consensus"  # Multiple verifiers
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True, slots=True)
class BenchmarkTaskSpec:
    """
    Immutable task specification for benchmark execution.

    Defines what the task is, what capabilities it requires, and how it should
    be evaluated. Does not execute the task.
    """

    task_id: str
    category: str
    description: str
    expected_output: str
    difficulty: TaskDifficulty
    required_capabilities: frozenset[str]
    permitted_tools: frozenset[str]
    reference_evidence_ids: tuple[str, ...]
    evaluation_requirements: Mapping[str, Any]
    correctness_type: CorrectnessType

    def __post_init__(self):
        _identifier(self.task_id, "task_id")
        _identifier(self.category, "category")
        _text(self.description, "description")
        _text(self.expected_output, "expected_output")
        try:
            object.__setattr__(self, "difficulty", TaskDifficulty(self.difficulty))
            object.__setattr__(self, "correctness_type", CorrectnessType(self.correctness_type))
        except (TypeError, ValueError) as exc:
            raise BenchmarkContractError("unsupported difficulty or correctness type") from exc
        object.__setattr__(self, "required_capabilities", frozenset(
            _identifiers(self.required_capabilities, "required_capabilities")))
        object.__setattr__(self, "permitted_tools", frozenset(
            _identifiers(self.permitted_tools, "permitted_tools")))
        object.__setattr__(self, "reference_evidence_ids",
            _identifiers(self.reference_evidence_ids, "reference_evidence_ids"))
        object.__setattr__(self, "evaluation_requirements",
            _freeze_mapping(self.evaluation_requirements, "evaluation_requirements"))

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "category": self.category,
            "description": self.description,
            "expected_output": self.expected_output,
            "difficulty": self.difficulty.value,
            "required_capabilities": sorted(self.required_capabilities),
            "permitted_tools": sorted(self.permitted_tools),
            "reference_evidence_ids": list(self.reference_evidence_ids),
            "evaluation_requirements": _public(self.evaluation_requirements),
            "correctness_type": self.correctness_type.value,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkSuite:
    """
    Immutable benchmark suite definition.

    Defines a collection of tasks and metadata about the benchmark.
    """

    suite_id: str
    name: str
    version: str
    created_at: str
    description: str
    tasks: tuple[BenchmarkTaskSpec, ...]
    suite_metadata: Mapping[str, Any]

    def __post_init__(self):
        _identifier(self.suite_id, "suite_id")
        _text(self.name, "name")
        _text(self.version, "version")
        _timestamp(self.created_at, "created_at")
        _text(self.description, "description")
        if not all(type(item) is BenchmarkTaskSpec for item in self.tasks):
            raise BenchmarkContractError("tasks must contain BenchmarkTaskSpec values")
        tasks = tuple(sorted(self.tasks, key=lambda item: item.task_id))
        if not tasks or len({item.task_id for item in tasks}) != len(tasks):
            raise BenchmarkContractError("tasks must have unique identities")
        object.__setattr__(self, "tasks", tasks)
        object.__setattr__(self, "suite_metadata",
            _freeze_mapping(self.suite_metadata, "suite_metadata"))

    def suite_fingerprint(self) -> str:
        return hashlib.sha256(_canonical({
            "suite_id": self.suite_id,
            "name": self.name,
            "version": self.version,
            "created_at": self.created_at,
            "description": self.description,
            "tasks": [item.to_dict() for item in self.tasks],
            "suite_metadata": _public(self.suite_metadata),
        })).hexdigest()

    def to_dict(self):
        return {
            "suite_id": self.suite_id,
            "name": self.name,
            "version": self.version,
            "created_at": self.created_at,
            "description": self.description,
            "tasks": [item.to_dict() for item in self.tasks],
            "suite_metadata": _public(self.suite_metadata),
            "suite_fingerprint": self.suite_fingerprint(),
        }


@dataclass(frozen=True, slots=True)
class BenchmarkRunPlan:
    """
    Immutable, deterministic benchmark run plan.

    Binds all experimental conditions that affect execution and measurement.
    Equivalent inputs must generate equivalent fingerprints.
    """

    plan_id: str
    suite_fingerprint: str
    experiment_fingerprint: str
    arm_ids: tuple[str, ...]
    task_ids: tuple[str, ...]
    repetitions_per_task: int
    random_seed: int | None
    worker_requirements: Mapping[str, Any]
    evaluator_config: Mapping[str, Any]
    resource_measurement_config: Mapping[str, Any]
    created_at: str
    software_version: Mapping[str, Any]

    def __post_init__(self):
        _identifier(self.plan_id, "plan_id")
        _digest(self.suite_fingerprint, "suite_fingerprint")
        _digest(self.experiment_fingerprint, "experiment_fingerprint")
        object.__setattr__(self, "arm_ids", _identifiers(self.arm_ids, "arm_ids", required=True))
        object.__setattr__(self, "task_ids", _identifiers(self.task_ids, "task_ids", required=True))
        if not isinstance(self.repetitions_per_task, int) or self.repetitions_per_task < 1:
            raise BenchmarkContractError("repetitions_per_task must be positive integer")
        if self.random_seed is not None and not isinstance(self.random_seed, int):
            raise BenchmarkContractError("random_seed must be integer or None")
        _timestamp(self.created_at, "created_at")
        object.__setattr__(self, "worker_requirements",
            _freeze_mapping(self.worker_requirements, "worker_requirements"))
        object.__setattr__(self, "evaluator_config",
            _freeze_mapping(self.evaluator_config, "evaluator_config"))
        object.__setattr__(self, "resource_measurement_config",
            _freeze_mapping(self.resource_measurement_config, "resource_measurement_config"))
        object.__setattr__(self, "software_version",
            _freeze_mapping(self.software_version, "software_version"))

    def plan_fingerprint(self) -> str:
        return hashlib.sha256(_canonical({
            "suite_fingerprint": self.suite_fingerprint,
            "experiment_fingerprint": self.experiment_fingerprint,
            "arm_ids": list(self.arm_ids),
            "task_ids": list(self.task_ids),
            "repetitions_per_task": self.repetitions_per_task,
            "random_seed": self.random_seed,
            "worker_requirements": _public(self.worker_requirements),
            "evaluator_config": _public(self.evaluator_config),
            "resource_measurement_config": _public(self.resource_measurement_config),
            "software_version": _public(self.software_version),
        })).hexdigest()

    def to_dict(self):
        return {
            "plan_id": self.plan_id,
            "suite_fingerprint": self.suite_fingerprint,
            "experiment_fingerprint": self.experiment_fingerprint,
            "arm_ids": list(self.arm_ids),
            "task_ids": list(self.task_ids),
            "repetitions_per_task": self.repetitions_per_task,
            "random_seed": self.random_seed,
            "worker_requirements": _public(self.worker_requirements),
            "evaluator_config": _public(self.evaluator_config),
            "resource_measurement_config": _public(self.resource_measurement_config),
            "created_at": self.created_at,
            "software_version": _public(self.software_version),
            "plan_fingerprint": self.plan_fingerprint(),
        }


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """
    Result from evaluating a benchmark attempt.

    Distinguishes objective correctness from evaluator judgment.
    """

    evaluation_id: str
    task_id: str
    attempt_id: str
    evaluator_id: str
    correct: bool | None
    confidence: float
    correctness_type: CorrectnessType
    evidence_ids: tuple[str, ...]
    rationale: str
    is_ground_truth: bool = False

    def __post_init__(self):
        for name in ("evaluation_id", "task_id", "attempt_id", "evaluator_id"):
            _identifier(getattr(self, name), name)
        if self.correct is not None and not isinstance(self.correct, bool):
            raise BenchmarkContractError("correct must be boolean or None")
        confidence = _finite_nonnegative(self.confidence, "confidence")
        if confidence > 1:
            raise BenchmarkContractError("confidence must be within [0, 1]")
        object.__setattr__(self, "confidence", confidence)
        try:
            object.__setattr__(self, "correctness_type", CorrectnessType(self.correctness_type))
        except (TypeError, ValueError) as exc:
            raise BenchmarkContractError("unsupported correctness type") from exc
        object.__setattr__(self, "evidence_ids",
            _identifiers(self.evidence_ids, "evaluation evidence_ids", required=True))
        _text(self.rationale, "rationale")
        if not isinstance(self.is_ground_truth, bool):
            raise BenchmarkContractError("is_ground_truth must be boolean")
        if self.is_ground_truth and self.correctness_type is not CorrectnessType.OBJECTIVE:
            raise BenchmarkContractError("ground truth requires objective correctness type")

    def to_dict(self):
        return {
            "evaluation_id": self.evaluation_id,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "evaluator_id": self.evaluator_id,
            "correct": self.correct,
            "confidence": self.confidence,
            "correctness_type": self.correctness_type.value,
            "evidence_ids": list(self.evidence_ids),
            "rationale": self.rationale,
            "is_ground_truth": self.is_ground_truth,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkAttemptResult:
    """
    Complete result for a single benchmark attempt.

    Aggregates experiment attempt, measurement, and evaluation.
    """

    attempt: ExperimentAttempt
    evaluation: EvaluationResult

    def __post_init__(self):
        if type(self.attempt) is not ExperimentAttempt:
            raise BenchmarkContractError("attempt must be ExperimentAttempt")
        if type(self.evaluation) is not EvaluationResult:
            raise BenchmarkContractError("evaluation must be EvaluationResult")
        if self.attempt.task_id != self.evaluation.task_id:
            raise BenchmarkContractError("attempt and evaluation task_id mismatch")
        if self.attempt.attempt_id != self.evaluation.attempt_id:
            raise BenchmarkContractError("attempt and evaluation attempt_id mismatch")

    def to_dict(self):
        return {
            "attempt": self.attempt.to_dict(),
            "evaluation": self.evaluation.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class BenchmarkRunResult:
    """
    Complete result for a benchmark run.

    Aggregates experiment run and benchmark-specific evaluations.
    """

    run: ExperimentRun
    task_results: tuple[BenchmarkAttemptResult, ...]
    plan_fingerprint: str

    def __post_init__(self):
        if type(self.run) is not ExperimentRun:
            raise BenchmarkContractError("run must be ExperimentRun")
        _digest(self.plan_fingerprint, "plan_fingerprint")
        if not all(type(item) is BenchmarkAttemptResult for item in self.task_results):
            raise BenchmarkContractError("task_results must contain BenchmarkAttemptResult")
        task_results = tuple(sorted(self.task_results,
            key=lambda item: (item.attempt.task_id, item.attempt.attempt_id)))
        object.__setattr__(self, "task_results", task_results)

        # Validate all task results belong to this run
        for result in task_results:
            if (result.attempt.experiment_id != self.run.experiment_id or
                result.attempt.arm_id != self.run.arm_id or
                result.attempt.run_id != self.run.run_id):
                raise BenchmarkContractError("foreign task result in benchmark run")

    def to_dict(self):
        return {
            "run": self.run.to_dict(),
            "task_results": [item.to_dict() for item in self.task_results],
            "plan_fingerprint": self.plan_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkAggregation:
    """
    Aggregated metrics across benchmark attempts.

    Only aggregates compatible runs from the same plan.
    """

    aggregation_id: str
    plan_fingerprint: str
    arm_id: str
    task_count: int
    attempt_count: int
    successful_attempts: int
    correct_attempts: int
    objective_correct_count: int
    judged_correct_count: int
    mean_confidence: float | None
    elapsed_seconds_total: float
    elapsed_seconds_mean: float | None
    elapsed_seconds_median: float | None
    retry_count_total: int
    human_intervention_count_total: int
    cloud_escalation_count_total: int
    marginal_cloud_cost_total_usd: float | None
    resource_totals: Mapping[str, float]

    def __post_init__(self):
        _identifier(self.aggregation_id, "aggregation_id")
        _digest(self.plan_fingerprint, "plan_fingerprint")
        _identifier(self.arm_id, "arm_id")
        for name in ("task_count", "attempt_count", "successful_attempts", "correct_attempts",
                     "objective_correct_count", "judged_correct_count", "retry_count_total",
                     "human_intervention_count_total", "cloud_escalation_count_total"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise BenchmarkContractError(f"{name} must be non-negative integer")
        if self.mean_confidence is not None:
            confidence = _finite_nonnegative(self.mean_confidence, "mean_confidence")
            if confidence > 1:
                raise BenchmarkContractError("mean_confidence must be within [0, 1]")
            object.__setattr__(self, "mean_confidence", confidence)
        for name in ("elapsed_seconds_total", "elapsed_seconds_mean", "elapsed_seconds_median"):
            value = getattr(self, name)
            if value is not None:
                _finite_nonnegative(value, name)
        if self.marginal_cloud_cost_total_usd is not None:
            _finite_nonnegative(self.marginal_cloud_cost_total_usd,
                "marginal_cloud_cost_total_usd")
        object.__setattr__(self, "resource_totals",
            _freeze_mapping(self.resource_totals, "resource_totals"))

    def to_dict(self):
        return {
            "aggregation_id": self.aggregation_id,
            "plan_fingerprint": self.plan_fingerprint,
            "arm_id": self.arm_id,
            "task_count": self.task_count,
            "attempt_count": self.attempt_count,
            "successful_attempts": self.successful_attempts,
            "correct_attempts": self.correct_attempts,
            "objective_correct_count": self.objective_correct_count,
            "judged_correct_count": self.judged_correct_count,
            "success_rate": self.successful_attempts / self.attempt_count if self.attempt_count else None,
            "correctness_rate": self.correct_attempts / self.attempt_count if self.attempt_count else None,
            "mean_confidence": self.mean_confidence,
            "elapsed_seconds_total": self.elapsed_seconds_total,
            "elapsed_seconds_mean": self.elapsed_seconds_mean,
            "elapsed_seconds_median": self.elapsed_seconds_median,
            "retry_count_total": self.retry_count_total,
            "human_intervention_count_total": self.human_intervention_count_total,
            "cloud_escalation_count_total": self.cloud_escalation_count_total,
            "marginal_cloud_cost_total_usd": self.marginal_cloud_cost_total_usd,
            "resource_totals": _public(self.resource_totals),
        }


@dataclass(frozen=True, slots=True)
class BenchmarkComparisonReport:
    """
    Comparison report across multiple benchmark arms.

    Does not declare winners or make unsupported claims.
    """

    comparison_id: str
    plan_fingerprint: str
    aggregations: tuple[BenchmarkAggregation, ...]
    comparison_metadata: Mapping[str, Any]

    def __post_init__(self):
        _identifier(self.comparison_id, "comparison_id")
        _digest(self.plan_fingerprint, "plan_fingerprint")
        if not all(type(item) is BenchmarkAggregation for item in self.aggregations):
            raise BenchmarkContractError("aggregations must contain BenchmarkAggregation")
        if not self.aggregations:
            raise BenchmarkContractError("comparison requires at least one aggregation")
        aggregations = tuple(sorted(self.aggregations, key=lambda item: item.arm_id))
        if len({item.arm_id for item in aggregations}) != len(aggregations):
            raise BenchmarkContractError("comparison contains duplicate arm")
        if len({item.plan_fingerprint for item in aggregations}) != 1:
            raise BenchmarkContractError("comparison requires compatible plan fingerprints")
        object.__setattr__(self, "aggregations", aggregations)
        object.__setattr__(self, "comparison_metadata",
            _freeze_mapping(self.comparison_metadata, "comparison_metadata"))

    def to_dict(self):
        return {
            "comparison_id": self.comparison_id,
            "plan_fingerprint": self.plan_fingerprint,
            "aggregations": [item.to_dict() for item in self.aggregations],
            "comparison_metadata": _public(self.comparison_metadata),
        }


@dataclass(frozen=True, slots=True)
class BenchmarkRunManifest:
    """
    Deterministic manifest for reproducing benchmark execution.

    Preserves references/fingerprints rather than copying mutable upstream authority.
    """

    manifest_id: str
    plan: BenchmarkRunPlan
    suite_fingerprint: str
    experiment_fingerprint: str
    created_at: str
    manifest_metadata: Mapping[str, Any]

    def __post_init__(self):
        _identifier(self.manifest_id, "manifest_id")
        if type(self.plan) is not BenchmarkRunPlan:
            raise BenchmarkContractError("plan must be BenchmarkRunPlan")
        _digest(self.suite_fingerprint, "suite_fingerprint")
        _digest(self.experiment_fingerprint, "experiment_fingerprint")
        if self.plan.suite_fingerprint != self.suite_fingerprint:
            raise BenchmarkContractError("plan suite_fingerprint mismatch")
        if self.plan.experiment_fingerprint != self.experiment_fingerprint:
            raise BenchmarkContractError("plan experiment_fingerprint mismatch")
        _timestamp(self.created_at, "created_at")
        object.__setattr__(self, "manifest_metadata",
            _freeze_mapping(self.manifest_metadata, "manifest_metadata"))

    def manifest_fingerprint(self) -> str:
        return hashlib.sha256(_canonical({
            "plan": self.plan.to_dict(),
            "suite_fingerprint": self.suite_fingerprint,
            "experiment_fingerprint": self.experiment_fingerprint,
            "manifest_metadata": _public(self.manifest_metadata),
        })).hexdigest()

    def to_dict(self):
        return {
            "manifest_id": self.manifest_id,
            "plan": self.plan.to_dict(),
            "suite_fingerprint": self.suite_fingerprint,
            "experiment_fingerprint": self.experiment_fingerprint,
            "created_at": self.created_at,
            "manifest_metadata": _public(self.manifest_metadata),
            "manifest_fingerprint": self.manifest_fingerprint(),
        }


class BenchmarkEvaluator(Protocol):
    """
    Protocol for benchmark evaluators.

    Evaluators assess whether an attempt met task requirements.
    """

    def evaluate(
        self,
        task_spec: BenchmarkTaskSpec,
        attempt: ExperimentAttempt,
    ) -> EvaluationResult:
        """
        Evaluate a benchmark attempt.

        Returns evaluation result distinguishing objective correctness from judgment.
        """
        ...


class BenchmarkExecutor(Protocol):
    """
    Protocol for benchmark executors.

    Executors perform the actual task execution through governed capabilities.
    Must not perform direct subprocess/API calls - should delegate to governed systems.
    """

    def execute(
        self,
        task_spec: BenchmarkTaskSpec,
        experiment_task: ExperimentTask,
        arm: ExperimentArm,
        run_id: str,
        attempt_id: str,
    ) -> ExperimentAttempt:
        """
        Execute a benchmark task through governed capabilities.

        Returns experiment attempt with measurement and evidence.
        Must delegate to existing governed execution systems.
        """
        ...


class BenchmarkCoordinator:
    """
    Thread-safe benchmark coordinator.

    Coordinates benchmark execution using existing experiment infrastructure.
    Does not duplicate routing/execution/persistence systems.
    """

    def __init__(
        self,
        suite: BenchmarkSuite,
        definition: ExperimentDefinition,
        executor: BenchmarkExecutor,
        evaluator: BenchmarkEvaluator,
    ):
        if type(suite) is not BenchmarkSuite:
            raise TypeError("suite must be BenchmarkSuite")
        if type(definition) is not ExperimentDefinition:
            raise TypeError("definition must be ExperimentDefinition")

        self._suite = suite
        self._definition = definition
        self._executor = executor
        self._evaluator = evaluator
        self._coordinator = ExperimentCoordinator(definition)
        self._manifests: dict[str, BenchmarkRunManifest] = {}
        self._results: dict[str, BenchmarkRunResult] = {}
        self._lock = RLock()

    def create_plan(
        self,
        plan_id: str,
        arm_ids: tuple[str, ...],
        task_ids: tuple[str, ...],
        repetitions_per_task: int = 1,
        random_seed: int | None = None,
        worker_requirements: Mapping[str, Any] | None = None,
        evaluator_config: Mapping[str, Any] | None = None,
        resource_measurement_config: Mapping[str, Any] | None = None,
        software_version: Mapping[str, Any] | None = None,
    ) -> BenchmarkRunPlan:
        """Create a deterministic benchmark run plan."""
        from datetime import datetime, timezone

        with self._lock:
            return BenchmarkRunPlan(
                plan_id=plan_id,
                suite_fingerprint=self._suite.suite_fingerprint(),
                experiment_fingerprint=self._definition.definition_fingerprint(),
                arm_ids=tuple(arm_ids),
                task_ids=tuple(task_ids),
                repetitions_per_task=repetitions_per_task,
                random_seed=random_seed,
                worker_requirements=worker_requirements or {},
                evaluator_config=evaluator_config or {},
                resource_measurement_config=resource_measurement_config or {},
                created_at=datetime.now(timezone.utc).isoformat(),
                software_version=software_version or {},
            )

    def create_manifest(
        self,
        manifest_id: str,
        plan: BenchmarkRunPlan,
        manifest_metadata: Mapping[str, Any] | None = None,
    ) -> BenchmarkRunManifest:
        """Create a reproducible benchmark manifest."""
        from datetime import datetime, timezone

        with self._lock:
            manifest = BenchmarkRunManifest(
                manifest_id=manifest_id,
                plan=plan,
                suite_fingerprint=self._suite.suite_fingerprint(),
                experiment_fingerprint=self._definition.definition_fingerprint(),
                created_at=datetime.now(timezone.utc).isoformat(),
                manifest_metadata=manifest_metadata or {},
            )
            self._manifests[manifest_id] = manifest
            return manifest

    def execute_plan(
        self,
        plan: BenchmarkRunPlan,
        arm_id: str,
    ) -> BenchmarkRunResult:
        """
        Execute a benchmark plan for a specific arm.

        Uses injected executor and evaluator. Does not perform direct execution.
        """
        if type(plan) is not BenchmarkRunPlan:
            raise TypeError("plan must be BenchmarkRunPlan")

        with self._lock:
            # Validate plan matches this coordinator
            if (plan.suite_fingerprint != self._suite.suite_fingerprint() or
                plan.experiment_fingerprint != self._definition.definition_fingerprint()):
                raise BenchmarkContractError("plan fingerprints do not match coordinator")

            if arm_id not in plan.arm_ids:
                raise BenchmarkContractError("arm_id not in plan")

            # Find arm and tasks
            arm = next((a for a in self._definition.arms if a.arm_id == arm_id), None)
            if arm is None:
                raise BenchmarkContractError("arm not found in experiment definition")

            suite_tasks = {t.task_id: t for t in self._suite.tasks}
            exp_tasks = {t.task_id: t for t in self._definition.tasks}

            # This is a fake implementation that doesn't actually execute
            # Real implementation would use executor and evaluator
            raise BenchmarkExecutionError(
                "execute_plan is not yet implemented - this is v0.1 harness only"
            )

    def aggregate(
        self,
        plan_fingerprint: str,
        arm_id: str,
        results: tuple[BenchmarkRunResult, ...],
    ) -> BenchmarkAggregation:
        """Aggregate compatible benchmark results."""
        if not all(type(r) is BenchmarkRunResult for r in results):
            raise TypeError("results must contain BenchmarkRunResult")

        with self._lock:
            if not results:
                raise BenchmarkContractError("aggregation requires at least one result")

            # Validate all results are compatible
            for result in results:
                if result.plan_fingerprint != plan_fingerprint:
                    raise BenchmarkContractError("incompatible plan fingerprints")
                if result.run.arm_id != arm_id:
                    raise BenchmarkContractError("incompatible arm_ids")

            # Aggregate metrics
            task_ids = set()
            attempts = []
            evaluations = []
            for result in results:
                for task_result in result.task_results:
                    task_ids.add(task_result.attempt.task_id)
                    attempts.append(task_result.attempt)
                    evaluations.append(task_result.evaluation)

            successful = sum(1 for a in attempts if a.measurement.success)
            correct = sum(1 for e in evaluations if e.correct is True)
            objective = sum(1 for e in evaluations
                if e.correctness_type is CorrectnessType.OBJECTIVE and e.correct is True)
            judged = sum(1 for e in evaluations
                if e.correctness_type is CorrectnessType.EVALUATOR_JUDGMENT and e.correct is True)

            confidences = [e.confidence for e in evaluations]
            elapsed = [a.measurement.elapsed_seconds for a in attempts
                if a.measurement.elapsed_seconds is not None]

            resource_totals: dict[str, float] = {}
            for a in attempts:
                for key, value in a.resource_measurements.items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        resource_totals[key] = resource_totals.get(key, 0.0) + float(value)

            costs = [a.measurement.estimated_cloud_cost_usd for a in attempts
                if a.measurement.estimated_cloud_cost_usd is not None]

            return BenchmarkAggregation(
                aggregation_id=f"agg-{plan_fingerprint[:8]}-{arm_id}",
                plan_fingerprint=plan_fingerprint,
                arm_id=arm_id,
                task_count=len(task_ids),
                attempt_count=len(attempts),
                successful_attempts=successful,
                correct_attempts=correct,
                objective_correct_count=objective,
                judged_correct_count=judged,
                mean_confidence=sum(confidences) / len(confidences) if confidences else None,
                elapsed_seconds_total=sum(elapsed),
                elapsed_seconds_mean=sum(elapsed) / len(elapsed) if elapsed else None,
                elapsed_seconds_median=sorted(elapsed)[len(elapsed) // 2] if elapsed else None,
                retry_count_total=sum(a.measurement.retry_count for a in attempts),
                human_intervention_count_total=sum(
                    a.measurement.human_intervention_count for a in attempts),
                cloud_escalation_count_total=sum(
                    a.measurement.cloud_escalation_count for a in attempts),
                marginal_cloud_cost_total_usd=sum(costs) if costs else None,
                resource_totals=resource_totals,
            )

    def compare(
        self,
        comparison_id: str,
        plan_fingerprint: str,
        aggregations: tuple[BenchmarkAggregation, ...],
        comparison_metadata: Mapping[str, Any] | None = None,
    ) -> BenchmarkComparisonReport:
        """Create comparison report across arms."""
        return BenchmarkComparisonReport(
            comparison_id=comparison_id,
            plan_fingerprint=plan_fingerprint,
            aggregations=aggregations,
            comparison_metadata=comparison_metadata or {},
        )
