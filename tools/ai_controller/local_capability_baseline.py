"""
Local Capability Baseline v0.1

Provider-neutral measurement foundation for RAGHub's Local Equivalence work.

This module provides immutable, deterministic, provenance-preserving measurement
records for comparing different execution strategies:
- Frontier cloud models
- Single local models
- Local models with tools
- Multiple independent local attempts
- Local with critique/judge
- Local with memory
- Hybrid escalation approaches

DO NOT:
- Make unsupported benchmark claims
- Call paid model APIs
- Require external provider APIs
- Hardcode specific providers (OpenAI, Anthropic, etc.) into core contracts

Measurement records are suitable for comparison but are not performance claims.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class LocalityType(str, Enum):
    """Execution locality classification."""
    LOCAL = "local"           # Fully local execution
    CLOUD = "cloud"           # Fully cloud-based execution
    HYBRID = "hybrid"         # Combination of local and cloud
    UNKNOWN = "unknown"       # Locality cannot be determined


class CostClass(str, Enum):
    """Cost classification for execution."""
    ZERO = "zero"                    # No marginal cost (e.g., local inference)
    METERED = "metered"              # Pay-per-use (e.g., cloud API)
    SUBSCRIPTION = "subscription"    # Fixed subscription cost
    UNKNOWN = "unknown"              # Cost model unknown


class TaskOutcome(str, Enum):
    """Task execution outcome."""
    SUCCESS = "success"                      # Task completed successfully
    FAILURE = "failure"                      # Task failed
    PARTIAL_SUCCESS = "partial_success"      # Partial completion
    TIMEOUT = "timeout"                      # Exceeded time limit
    RESOURCE_EXHAUSTED = "resource_exhausted"  # Ran out of resources
    HUMAN_INTERVENTION = "human_intervention"  # Required human assistance
    UNKNOWN = "unknown"                      # Outcome undetermined


def _require_identifier(value: Any, field_name: str) -> str:
    """Require a valid non-empty identifier."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(
            f"{field_name} must be a canonical non-empty identifier"
        )
    return value


def _canonical_json(value: Any) -> bytes:
    """Return canonical JSON encoding of value."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "metadata must be finite JSON values"
        ) from exc


def _require_timestamp(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _require_finite_number(value: Any, field_name: str) -> int | float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be numeric")
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    return value


def _require_optional_text(value: Any, field_name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string or None")
    return value


def _freeze_json(value: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    """Detach caller state and recursively freeze finite JSON metadata."""
    try:
        snapshot = json.loads(_canonical_json(_public_json(value)))
    except ValueError as exc:
        raise ValueError(f"{field_name} must contain finite JSON values") from exc

    def freeze(item):
        if isinstance(item, dict):
            return MappingProxyType({key: freeze(child) for key, child in item.items()})
        if isinstance(item, list):
            return tuple(freeze(child) for child in item)
        return item

    return freeze(snapshot)


def _public_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _public_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_public_json(child) for child in value]
    return value


@dataclass(frozen=True, slots=True)
class WorkerCapabilityProfile:
    """
    Immutable capability profile for a local worker/node.

    Describes what a local worker can do without making performance claims.
    """

    # Identity
    worker_id: str  # Unique worker identity
    profile_id: str  # Unique profile identity
    created_at: str  # ISO-8601 timestamp

    # Locality and provider (provider-neutral)
    locality: LocalityType
    provider_identifier: str | None  # Optional provider tag (not hardcoded)
    model_identifier: str | None  # Optional model/runtime identifier
    cost_class: CostClass

    # Capabilities (what can this worker do?)
    capabilities: frozenset[str]  # E.g., {"code", "analysis", "tools"}

    # Availability and capacity (observational, not guaranteed)
    typical_availability_hours: float | None  # Typical hours available per day
    max_concurrent_tasks: int | None  # Maximum concurrent task capacity

    # Hardware/runtime facts (provider-neutral)
    hardware_facts: Mapping[str, Any]  # E.g., {"ram_gb": 16, "cpu_cores": 8}
    runtime_facts: Mapping[str, Any]  # E.g., {"python_version": "3.14"}

    def __post_init__(self):
        """Validate profile fields."""
        _require_identifier(self.worker_id, "worker_id")
        _require_identifier(self.profile_id, "profile_id")

        _require_timestamp(self.created_at, "created_at")
        _require_optional_text(self.provider_identifier, "provider_identifier")
        _require_optional_text(self.model_identifier, "model_identifier")

        # Validate locality
        if not isinstance(self.locality, LocalityType):
            try:
                object.__setattr__(self, "locality", LocalityType(self.locality))
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid locality") from exc

        # Validate cost_class
        if not isinstance(self.cost_class, CostClass):
            try:
                object.__setattr__(self, "cost_class", CostClass(self.cost_class))
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid cost_class") from exc

        # Validate capabilities
        if not isinstance(self.capabilities, (set, frozenset)):
            raise TypeError("capabilities must be a set or frozenset")
        if not all(isinstance(cap, str) for cap in self.capabilities):
            raise TypeError("capabilities must contain only strings")
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))

        # Validate optional numeric fields
        if self.typical_availability_hours is not None:
            _require_finite_number(self.typical_availability_hours,
                "typical_availability_hours")
            if self.typical_availability_hours < 0:
                raise ValueError("typical_availability_hours cannot be negative")

        if self.max_concurrent_tasks is not None:
            if not isinstance(self.max_concurrent_tasks, int):
                raise TypeError("max_concurrent_tasks must be an integer or None")
            if self.max_concurrent_tasks < 1:
                raise ValueError("max_concurrent_tasks must be positive")

        # Detach and recursively freeze caller-owned facts.
        if not isinstance(self.hardware_facts, Mapping):
            raise TypeError("hardware_facts must be a mapping")
        if not isinstance(self.runtime_facts, Mapping):
            raise TypeError("runtime_facts must be a mapping")
        object.__setattr__(self, "hardware_facts",
            _freeze_json(self.hardware_facts, "hardware_facts"))
        object.__setattr__(self, "runtime_facts",
            _freeze_json(self.runtime_facts, "runtime_facts"))

    def profile_fingerprint(self) -> str:
        """Return deterministic fingerprint of this profile."""
        payload = {
            "worker_id": self.worker_id,
            "profile_id": self.profile_id,
            "created_at": self.created_at,
            "locality": self.locality.value,
            "provider_identifier": self.provider_identifier,
            "model_identifier": self.model_identifier,
            "cost_class": self.cost_class.value,
            "capabilities": sorted(self.capabilities),
            "typical_availability_hours": self.typical_availability_hours,
            "max_concurrent_tasks": self.max_concurrent_tasks,
            "hardware_facts": _public_json(self.hardware_facts),
            "runtime_facts": _public_json(self.runtime_facts),
        }
        return hashlib.sha256(_canonical_json(payload)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Safe public serialization."""
        return {
            "worker_id": self.worker_id,
            "profile_id": self.profile_id,
            "created_at": self.created_at,
            "locality": self.locality.value,
            "provider_identifier": self.provider_identifier,
            "model_identifier": self.model_identifier,
            "cost_class": self.cost_class.value,
            "capabilities": sorted(self.capabilities),
            "typical_availability_hours": self.typical_availability_hours,
            "max_concurrent_tasks": self.max_concurrent_tasks,
            "hardware_facts": _public_json(self.hardware_facts),
            "runtime_facts": _public_json(self.runtime_facts),
            "profile_fingerprint": self.profile_fingerprint(),
        }


@dataclass(frozen=True, slots=True)
class TaskMeasurement:
    """
    Immutable measurement record for a single task execution.

    Records what happened during task execution without making performance claims.
    Suitable for later comparison between different execution strategies.
    """

    # Identity
    measurement_id: str  # Unique measurement identity
    task_id: str  # Task identity
    worker_id: str  # Worker that executed the task
    profile_fingerprint: str | None  # Worker profile at execution time

    # Temporal
    started_at: str  # ISO-8601 timestamp
    completed_at: str | None  # ISO-8601 timestamp (None if not completed)
    elapsed_seconds: float | None  # Elapsed time in seconds

    # Outcome
    outcome: TaskOutcome
    success: bool  # True if task succeeded
    verification_result: str | None  # Optional verification outcome

    # Effort and intervention
    attempt_number: int  # Which attempt (1 for first, 2 for retry, etc.)
    human_intervention_count: int  # Number of human interventions required
    retry_count: int  # Number of retries (0 for first attempt)

    # Cost and escalation (externally supplied, may be None)
    zero_cloud_cost: bool  # True if this execution had zero marginal cloud cost
    cloud_escalation_count: int  # Number of cloud escalations (0 if none)
    estimated_cloud_cost_usd: float | None  # Estimated cloud cost in USD

    # Provenance and context
    execution_strategy: str  # E.g., "local-single", "cloud-frontier", "local-tools"
    locality: LocalityType
    provider_identifier: str | None
    model_identifier: str | None

    # Evidence (optional metadata for later analysis)
    evidence_metadata: Mapping[str, Any]  # Additional context

    def __post_init__(self):
        """Validate measurement fields."""
        _require_identifier(self.measurement_id, "measurement_id")
        _require_identifier(self.task_id, "task_id")
        _require_identifier(self.worker_id, "worker_id")
        _require_identifier(self.execution_strategy, "execution_strategy")
        _require_optional_text(self.profile_fingerprint, "profile_fingerprint")
        _require_optional_text(self.verification_result, "verification_result")
        _require_optional_text(self.provider_identifier, "provider_identifier")
        _require_optional_text(self.model_identifier, "model_identifier")

        # Validate timestamps
        for field in ["started_at", "completed_at"]:
            value = getattr(self, field)
            if value is None and field == "completed_at":
                continue  # completed_at can be None
            _require_timestamp(value, field)

        # Validate elapsed_seconds
        if self.elapsed_seconds is not None:
            _require_finite_number(self.elapsed_seconds, "elapsed_seconds")
            if self.elapsed_seconds < 0:
                raise ValueError("elapsed_seconds cannot be negative")

        # Validate outcome
        if not isinstance(self.outcome, TaskOutcome):
            try:
                object.__setattr__(self, "outcome", TaskOutcome(self.outcome))
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid outcome") from exc

        # Validate success
        if not isinstance(self.success, bool):
            raise TypeError("success must be a boolean")
        if self.success is not (self.outcome is TaskOutcome.SUCCESS):
            raise ValueError("success must match outcome")

        # Validate counts
        for field in ["attempt_number", "human_intervention_count", "retry_count", "cloud_escalation_count"]:
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{field} must be an integer")
            if value < 0:
                raise ValueError(f"{field} cannot be negative")

        # Validate zero_cloud_cost
        if not isinstance(self.zero_cloud_cost, bool):
            raise TypeError("zero_cloud_cost must be a boolean")

        # Validate estimated_cloud_cost_usd
        if self.estimated_cloud_cost_usd is not None:
            _require_finite_number(self.estimated_cloud_cost_usd,
                "estimated_cloud_cost_usd")
            if self.estimated_cloud_cost_usd < 0:
                raise ValueError("estimated_cloud_cost_usd cannot be negative")

        # Validate locality
        if not isinstance(self.locality, LocalityType):
            try:
                object.__setattr__(self, "locality", LocalityType(self.locality))
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid locality") from exc

        # Validate evidence_metadata
        if not isinstance(self.evidence_metadata, Mapping):
            raise TypeError("evidence_metadata must be a mapping")
        object.__setattr__(self, "evidence_metadata",
            _freeze_json(self.evidence_metadata, "evidence_metadata"))

    def measurement_fingerprint(self) -> str:
        """Return deterministic fingerprint of this measurement."""
        payload = {
            "measurement_id": self.measurement_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "profile_fingerprint": self.profile_fingerprint,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_seconds": self.elapsed_seconds,
            "outcome": self.outcome.value,
            "success": self.success,
            "verification_result": self.verification_result,
            "attempt_number": self.attempt_number,
            "human_intervention_count": self.human_intervention_count,
            "retry_count": self.retry_count,
            "zero_cloud_cost": self.zero_cloud_cost,
            "cloud_escalation_count": self.cloud_escalation_count,
            "estimated_cloud_cost_usd": self.estimated_cloud_cost_usd,
            "execution_strategy": self.execution_strategy,
            "locality": self.locality.value,
            "provider_identifier": self.provider_identifier,
            "model_identifier": self.model_identifier,
            "evidence_metadata": _public_json(self.evidence_metadata),
        }
        return hashlib.sha256(_canonical_json(payload)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Safe public serialization."""
        return {
            "measurement_id": self.measurement_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "profile_fingerprint": self.profile_fingerprint,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_seconds": self.elapsed_seconds,
            "outcome": self.outcome.value,
            "success": self.success,
            "verification_result": self.verification_result,
            "attempt_number": self.attempt_number,
            "human_intervention_count": self.human_intervention_count,
            "retry_count": self.retry_count,
            "zero_cloud_cost": self.zero_cloud_cost,
            "cloud_escalation_count": self.cloud_escalation_count,
            "estimated_cloud_cost_usd": self.estimated_cloud_cost_usd,
            "execution_strategy": self.execution_strategy,
            "locality": self.locality.value,
            "provider_identifier": self.provider_identifier,
            "model_identifier": self.model_identifier,
            "evidence_metadata": _public_json(self.evidence_metadata),
            "measurement_fingerprint": self.measurement_fingerprint(),
        }


@dataclass(frozen=True, slots=True)
class ComparisonBaseline:
    """
    Immutable baseline for comparing different execution strategies.

    Aggregates measurements from multiple task executions to enable
    comparison without making unsupported performance claims.
    """

    # Identity
    baseline_id: str  # Unique baseline identity
    created_at: str  # ISO-8601 timestamp

    # Context
    execution_strategy: str  # Strategy being measured
    task_count: int  # Number of tasks in this baseline

    # Aggregate measurements (NO unsupported claims)
    success_count: int  # Number of successful tasks
    failure_count: int  # Number of failed tasks
    total_elapsed_seconds: float  # Total elapsed time
    mean_elapsed_seconds: float | None  # Mean elapsed time
    median_elapsed_seconds: float | None  # Median elapsed time

    # Intervention and effort
    total_human_interventions: int  # Total human interventions
    total_retries: int  # Total retries across all tasks

    # Cost (externally supplied)
    total_cloud_cost_usd: float | None  # Total estimated cloud cost
    zero_cost_task_count: int  # Tasks with zero marginal cloud cost

    # Provenance
    measurements: tuple[str, ...]  # Measurement IDs included in baseline

    def __post_init__(self):
        """Validate baseline fields."""
        _require_identifier(self.baseline_id, "baseline_id")
        _require_identifier(self.execution_strategy, "execution_strategy")

        _require_timestamp(self.created_at, "created_at")

        # Validate counts
        for field in ["task_count", "success_count", "failure_count",
                     "total_human_interventions", "total_retries", "zero_cost_task_count"]:
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{field} must be an integer")
            if value < 0:
                raise ValueError(f"{field} cannot be negative")
        if self.success_count + self.failure_count > self.task_count:
            raise ValueError("success and failure counts cannot exceed task_count")

        # Validate elapsed times
        _require_finite_number(self.total_elapsed_seconds, "total_elapsed_seconds")
        if self.total_elapsed_seconds < 0:
            raise ValueError("total_elapsed_seconds cannot be negative")

        for field in ["mean_elapsed_seconds", "median_elapsed_seconds"]:
            value = getattr(self, field)
            if value is not None:
                _require_finite_number(value, field)
                if value < 0:
                    raise ValueError(f"{field} cannot be negative")

        # Validate total_cloud_cost_usd
        if self.total_cloud_cost_usd is not None:
            _require_finite_number(self.total_cloud_cost_usd,
                "total_cloud_cost_usd")
            if self.total_cloud_cost_usd < 0:
                raise ValueError("total_cloud_cost_usd cannot be negative")

        # Validate measurements
        if not isinstance(self.measurements, tuple):
            raise TypeError("measurements must be a tuple")
        if not all(isinstance(m, str) for m in self.measurements):
            raise TypeError("measurements must contain only strings")

    def baseline_fingerprint(self) -> str:
        """Return deterministic fingerprint of this baseline."""
        payload = {
            "baseline_id": self.baseline_id,
            "created_at": self.created_at,
            "execution_strategy": self.execution_strategy,
            "task_count": self.task_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "total_elapsed_seconds": self.total_elapsed_seconds,
            "mean_elapsed_seconds": self.mean_elapsed_seconds,
            "median_elapsed_seconds": self.median_elapsed_seconds,
            "total_human_interventions": self.total_human_interventions,
            "total_retries": self.total_retries,
            "total_cloud_cost_usd": self.total_cloud_cost_usd,
            "zero_cost_task_count": self.zero_cost_task_count,
            "measurements": list(self.measurements),
        }
        return hashlib.sha256(_canonical_json(payload)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Safe public serialization."""
        return {
            "baseline_id": self.baseline_id,
            "created_at": self.created_at,
            "execution_strategy": self.execution_strategy,
            "task_count": self.task_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "success_rate": self.success_count / self.task_count if self.task_count > 0 else None,
            "total_elapsed_seconds": self.total_elapsed_seconds,
            "mean_elapsed_seconds": self.mean_elapsed_seconds,
            "median_elapsed_seconds": self.median_elapsed_seconds,
            "total_human_interventions": self.total_human_interventions,
            "total_retries": self.total_retries,
            "total_cloud_cost_usd": self.total_cloud_cost_usd,
            "zero_cost_task_count": self.zero_cost_task_count,
            "measurements": list(self.measurements),
            "baseline_fingerprint": self.baseline_fingerprint(),
        }
