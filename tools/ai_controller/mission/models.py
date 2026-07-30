"""
Mission orchestrator data models.

All models use only the Python standard library and are JSON-serializable
via their to_dict / from_dict helpers.  Enum values are stored as plain
strings so that JSON files remain human-readable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


SCHEMA_VERSION = "1"


def _list_or_raw(value: Any) -> Any:
    """Preserve list-like values without flattening strings or mappings."""
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return value


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class MissionTaskStatus(str, Enum):
    pending          = "pending"
    ready            = "ready"
    queued           = "queued"
    running          = "running"
    succeeded        = "succeeded"
    failed           = "failed"
    blocked          = "blocked"
    cancelled        = "cancelled"
    approval_required = "approval_required"


class MissionStatus(str, Enum):
    pending          = "pending"
    running          = "running"
    paused           = "paused"
    succeeded        = "succeeded"
    failed           = "failed"
    cancelled        = "cancelled"
    budget_exhausted = "budget_exhausted"


class FailurePolicy(str, Enum):
    stop_mission         = "stop_mission"
    block_dependents     = "block_dependents"
    continue_independent = "continue_independent"
    retry_then_block     = "retry_then_block"
    retry_then_stop      = "retry_then_stop"
    manual_gate          = "manual_gate"


class ApprovalPolicy(str, Enum):
    no_approval_required              = "no_approval_required"
    approval_required_before_queue    = "approval_required_before_queue"
    approval_required_before_execution = "approval_required_before_execution"
    forbidden                         = "forbidden"


class ProviderPolicy(str, Enum):
    local_first  = "local_first"
    cloud_first  = "cloud_first"
    local_only   = "local_only"
    cloud_only   = "cloud_only"
    any_order    = "any_order"


# ---------------------------------------------------------------------------
# Budget primitives
# ---------------------------------------------------------------------------

@dataclass
class BudgetField:
    """Represents a single budget metric that may be fully or partially known."""
    reported:  float | None = None
    estimated: float | None = None
    unknown:   float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reported":  self.reported,
            "estimated": self.estimated,
            "unknown":   self.unknown,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BudgetField:
        return cls(
            reported=data.get("reported"),
            estimated=data.get("estimated"),
            unknown=data.get("unknown"),
        )


@dataclass
class MissionBudget:
    """Hard limits that the mission orchestrator will not exceed."""
    max_active_tasks:     int   = 5
    max_queued_tasks:     int   = 20
    max_total_attempts:   int   = 100
    max_failed_tasks:     int   = 10
    max_runtime_seconds:  float = 86400.0
    max_worktrees:        int   = 10

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_active_tasks":    self.max_active_tasks,
            "max_queued_tasks":    self.max_queued_tasks,
            "max_total_attempts":  self.max_total_attempts,
            "max_failed_tasks":    self.max_failed_tasks,
            "max_runtime_seconds": self.max_runtime_seconds,
            "max_worktrees":       self.max_worktrees,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MissionBudget:
        return cls(
            max_active_tasks=int(data.get("max_active_tasks", 5)),
            max_queued_tasks=int(data.get("max_queued_tasks", 20)),
            max_total_attempts=int(data.get("max_total_attempts", 100)),
            max_failed_tasks=int(data.get("max_failed_tasks", 10)),
            max_runtime_seconds=float(data.get("max_runtime_seconds", 86400.0)),
            max_worktrees=int(data.get("max_worktrees", 10)),
        )


# ---------------------------------------------------------------------------
# Mission definition models
# ---------------------------------------------------------------------------

@dataclass
class MissionTaskDefinition:
    """Full specification for a single task inside a mission."""

    task_id:                  str
    title:                    str
    prompt:                   str
    depends_on:               list[str]    = field(default_factory=list)
    base_ref:                 str          = "HEAD"
    tests:                    list[list[str]] = field(default_factory=list)
    provider_preference:      str | None   = None
    provider_fallback:        list[str]    = field(default_factory=list)
    provider_policy:          ProviderPolicy = field(default=ProviderPolicy.any_order)
    max_attempts:             int          = 3
    timeout_seconds:          float        = 1800.0
    failure_policy:           FailurePolicy  = field(default=FailurePolicy.block_dependents)
    required_changed_paths:   list[str]    = field(default_factory=list)
    forbidden_changed_paths:  list[str]    = field(default_factory=list)
    approval_policy:          ApprovalPolicy = field(default=ApprovalPolicy.no_approval_required)
    cleanup_policy:           str          = "preserve"  # "preserve" | "cleanup"
    metadata:                 dict         = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id":                 self.task_id,
            "title":                   self.title,
            "prompt":                  self.prompt,
            "depends_on":              list(self.depends_on),
            "base_ref":                self.base_ref,
            "tests":                   [list(t) for t in self.tests],
            "provider_preference":     self.provider_preference,
            "provider_fallback":       list(self.provider_fallback),
            "provider_policy":         self.provider_policy.value,
            "max_attempts":            self.max_attempts,
            "timeout_seconds":         self.timeout_seconds,
            "failure_policy":          self.failure_policy.value,
            "required_changed_paths":  list(self.required_changed_paths),
            "forbidden_changed_paths": list(self.forbidden_changed_paths),
            "approval_policy":         self.approval_policy.value,
            "cleanup_policy":          self.cleanup_policy,
            "metadata":                dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MissionTaskDefinition:
        return cls(
            task_id=data["task_id"],
            title=data["title"],
            prompt=data["prompt"],
            depends_on=_list_or_raw(data.get("depends_on", [])),
            base_ref=data.get("base_ref", "HEAD"),
            tests=_list_or_raw(data.get("tests", [])),
            provider_preference=data.get("provider_preference"),
            provider_fallback=_list_or_raw(data.get("provider_fallback", [])),
            provider_policy=ProviderPolicy(data.get("provider_policy", ProviderPolicy.any_order.value)),
            max_attempts=int(data.get("max_attempts", 3)),
            timeout_seconds=float(data.get("timeout_seconds", 1800.0)),
            failure_policy=FailurePolicy(data.get("failure_policy", FailurePolicy.block_dependents.value)),
            required_changed_paths=_list_or_raw(data.get("required_changed_paths", [])),
            forbidden_changed_paths=_list_or_raw(data.get("forbidden_changed_paths", [])),
            approval_policy=ApprovalPolicy(data.get("approval_policy", ApprovalPolicy.no_approval_required.value)),
            cleanup_policy=data.get("cleanup_policy", "preserve"),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class MilestoneDefinition:
    """A named checkpoint grouping a set of task IDs."""

    milestone_id: str
    title:        str
    description:  str       = ""
    task_ids:     list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "milestone_id": self.milestone_id,
            "title":        self.title,
            "description":  self.description,
            "task_ids":     list(self.task_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MilestoneDefinition:
        return cls(
            milestone_id=data["milestone_id"],
            title=data["title"],
            description=data.get("description", ""),
            task_ids=_list_or_raw(data.get("task_ids", [])),
        )


@dataclass
class MissionDefinition:
    """Top-level mission descriptor read from / written to a JSON file."""

    mission_id:      str
    title:           str
    description:     str                        = ""
    schema_version:  str                        = SCHEMA_VERSION
    version:         str                        = "0"
    base_ref:        str                        = "HEAD"
    repository_path: str                        = ""
    tasks:           list[MissionTaskDefinition] = field(default_factory=list)
    milestones:      list[MilestoneDefinition]  = field(default_factory=list)
    budgets:         MissionBudget              = field(default_factory=MissionBudget)
    metadata:        dict                       = field(default_factory=dict)
    created_at:      str                        = ""  # ISO-8601; set by validate() if empty

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id":      self.mission_id,
            "title":           self.title,
            "description":     self.description,
            "schema_version":  self.schema_version,
            "version":         self.version,
            "base_ref":        self.base_ref,
            "repository_path": self.repository_path,
            "tasks":           [t.to_dict() for t in self.tasks],
            "milestones":      [m.to_dict() for m in self.milestones],
            "budgets":         self.budgets.to_dict(),
            "metadata":        dict(self.metadata),
            "created_at":      self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MissionDefinition:
        return cls(
            mission_id=data["mission_id"],
            title=data["title"],
            description=data.get("description", ""),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            version=str(data.get("version", "0")),
            base_ref=data.get("base_ref", "HEAD"),
            repository_path=data.get("repository_path", ""),
            tasks=[MissionTaskDefinition.from_dict(t) for t in data.get("tasks", [])],
            milestones=[MilestoneDefinition.from_dict(m) for m in data.get("milestones", [])],
            budgets=MissionBudget.from_dict(data["budgets"]) if "budgets" in data else MissionBudget(),
            metadata=dict(data.get("metadata", {})),
            created_at=data.get("created_at", ""),
        )

    @classmethod
    def from_file(cls, path: str) -> MissionDefinition:
        """Load a MissionDefinition from a JSON file at *path*."""
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls.from_dict(data)


# ---------------------------------------------------------------------------
# Runtime state models
# ---------------------------------------------------------------------------

@dataclass
class MissionTaskState:
    """Live execution state for one task within a running mission."""

    task_id:        str
    status:         MissionTaskStatus
    queue_task_id:  str | None   = None   # ID in the controller task queue
    report_path:    str | None   = None
    worktree_path:  str | None   = None
    attempt_count:  int          = 0
    failure_reason: str | None   = None
    queued_at:      str | None   = None
    started_at:     str | None   = None
    finished_at:    str | None   = None
    files_changed:  list[str]    = field(default_factory=list)
    test_results:   list[dict]   = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id":        self.task_id,
            "status":         self.status.value,
            "queue_task_id":  self.queue_task_id,
            "report_path":    self.report_path,
            "worktree_path":  self.worktree_path,
            "attempt_count":  self.attempt_count,
            "failure_reason": self.failure_reason,
            "queued_at":      self.queued_at,
            "started_at":     self.started_at,
            "finished_at":    self.finished_at,
            "files_changed":  list(self.files_changed),
            "test_results":   [dict(r) for r in self.test_results],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MissionTaskState:
        return cls(
            task_id=data["task_id"],
            status=MissionTaskStatus(data["status"]),
            queue_task_id=data.get("queue_task_id"),
            report_path=data.get("report_path"),
            worktree_path=data.get("worktree_path"),
            attempt_count=int(data.get("attempt_count", 0)),
            failure_reason=data.get("failure_reason"),
            queued_at=data.get("queued_at"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            files_changed=list(data.get("files_changed", [])),
            test_results=[dict(r) for r in data.get("test_results", [])],
        )


@dataclass
class BudgetUsage:
    """Snapshot of resource consumption at a point in time."""
    active_tasks:   int   = 0
    queued_tasks:   int   = 0
    total_attempts: int   = 0
    failed_tasks:   int   = 0
    elapsed_seconds: float = 0.0
    worktrees:      int   = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_tasks":    self.active_tasks,
            "queued_tasks":    self.queued_tasks,
            "total_attempts":  self.total_attempts,
            "failed_tasks":    self.failed_tasks,
            "elapsed_seconds": self.elapsed_seconds,
            "worktrees":       self.worktrees,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BudgetUsage:
        return cls(
            active_tasks=int(data.get("active_tasks", 0)),
            queued_tasks=int(data.get("queued_tasks", 0)),
            total_attempts=int(data.get("total_attempts", 0)),
            failed_tasks=int(data.get("failed_tasks", 0)),
            elapsed_seconds=float(data.get("elapsed_seconds", 0.0)),
            worktrees=int(data.get("worktrees", 0)),
        )


@dataclass
class MissionState:
    """Full runtime state for a mission; persisted as JSON."""

    mission_id:           str
    status:               MissionStatus
    task_states:          dict[str, MissionTaskState]  # key = task_id
    budget_usage:         BudgetUsage  = field(default_factory=BudgetUsage)
    started_at:           str | None   = None
    finished_at:          str | None   = None
    paused_at:            str | None   = None
    failure_reason:       str | None   = None
    root_cause_task_ids:  list[str]    = field(default_factory=list)
    events_path:          str | None   = None
    report_path:          str | None   = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id":          self.mission_id,
            "status":              self.status.value,
            "task_states":         {tid: ts.to_dict() for tid, ts in self.task_states.items()},
            "budget_usage":        self.budget_usage.to_dict(),
            "started_at":          self.started_at,
            "finished_at":         self.finished_at,
            "paused_at":           self.paused_at,
            "failure_reason":      self.failure_reason,
            "root_cause_task_ids": list(self.root_cause_task_ids),
            "events_path":         self.events_path,
            "report_path":         self.report_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MissionState:
        task_states = {
            tid: MissionTaskState.from_dict(ts)
            for tid, ts in data.get("task_states", {}).items()
        }
        return cls(
            mission_id=data["mission_id"],
            status=MissionStatus(data["status"]),
            task_states=task_states,
            budget_usage=BudgetUsage.from_dict(data["budget_usage"]) if "budget_usage" in data else BudgetUsage(),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            paused_at=data.get("paused_at"),
            failure_reason=data.get("failure_reason"),
            root_cause_task_ids=list(data.get("root_cause_task_ids", [])),
            events_path=data.get("events_path"),
            report_path=data.get("report_path"),
        )


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------

@dataclass
class MissionEvent:
    """Append-only audit event emitted by the orchestrator."""

    event_type:    str             # mission_created, task_became_ready, task_enqueued, …
    timestamp:     str             # ISO-8601
    mission_id:    str
    task_id:       str | None = None
    reason:        str | None = None
    queue_task_id: str | None = None
    report_path:   str | None = None
    provider:      str | None = None
    metadata:      dict       = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type":    self.event_type,
            "timestamp":     self.timestamp,
            "mission_id":    self.mission_id,
            "task_id":       self.task_id,
            "reason":        self.reason,
            "queue_task_id": self.queue_task_id,
            "report_path":   self.report_path,
            "provider":      self.provider,
            "metadata":      dict(self.metadata),
        }


# ---------------------------------------------------------------------------
# Validation result types
# ---------------------------------------------------------------------------

@dataclass
class ValidationError:
    """A single structural or semantic validation problem."""
    code:       str
    message:    str
    mission_id: str | None = None
    task_id:    str | None = None
    field:      str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code":       self.code,
            "message":    self.message,
            "mission_id": self.mission_id,
            "task_id":    self.task_id,
            "field":      self.field,
        }


@dataclass
class ValidationResult:
    """Aggregate outcome of validating a MissionDefinition."""
    valid:  bool
    errors: list[ValidationError] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid":  self.valid,
            "errors": [e.to_dict() for e in self.errors],
        }
