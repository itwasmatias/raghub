"""Bounded, revision-bound repair planning for durable mission state."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from tools.ai_controller.models import Task
from tools.ai_controller._locking import FileLock
from tools.ai_controller.queue import DurableQueue, atomic_json as queue_atomic_json
from tools.ai_controller.reports import report_lock_path

from .events import EventLogCorruptionError, MissionEventLog, make_event
from .graph import DependencyGraph
from .materializer import (
    TaskMaterializer,
    make_legacy_queue_task_id,
    reserved_metadata_collisions,
)
from .models import (
    ApprovalPolicy,
    MissionDefinition,
    MissionState,
    MissionStatus,
    MissionTaskDefinition,
    MissionTaskState,
    MissionTaskStatus,
)
from .scheduler import MissionScheduler, completion_report_error
from .store import MissionStateConflictError, MissionStore, _atomic_json

_QUEUE_STATES = ("pending", "running", "succeeded", "failed", "invalid")
_TERMINAL_MISSIONS = {
    MissionStatus.paused,
    MissionStatus.succeeded,
    MissionStatus.failed,
    MissionStatus.cancelled,
    MissionStatus.budget_exhausted,
}
_TERMINAL_TASKS = {
    MissionTaskStatus.succeeded,
    MissionTaskStatus.failed,
    MissionTaskStatus.blocked,
    MissionTaskStatus.cancelled,
}
_CONFLICTING_EVENTS = {
    "task_succeeded",
    "task_failed",
    "task_lost",
    "task_blocked",
    "task_cancelled",
}
_MISSION_TERMINAL_EVENTS = {
    "mission_completed",
    "mission_failed",
    "mission_cancelled",
    "mission_terminal",
}
_TASK_NONTERMINAL_EVENTS = {
    "task_ready",
    "task_enqueued",
    "task_running",
    "queue_created",
}


class RepairClassification(str, Enum):
    SAFE_REPAIR = "SAFE_REPAIR"
    ALREADY_CONSISTENT = "ALREADY_CONSISTENT"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    UNSUPPORTED = "UNSUPPORTED"


class RepairActionKind(str, Enum):
    REMATERIALIZE_MISSING_QUEUE_TASK = "REMATERIALIZE_MISSING_QUEUE_TASK"
    COMPLETE_FROM_DURABLE_SUCCESS = "COMPLETE_FROM_DURABLE_SUCCESS"
    RECORD_CONSISTENT_RECONCILIATION = "RECORD_CONSISTENT_RECONCILIATION"
    NONE = "NONE"


class RepairApplyStatus(str, Enum):
    APPLIED = "APPLIED"
    NO_LONGER_NEEDED = "NO_LONGER_NEEDED"
    STALE = "STALE"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class RepairAction:
    action_id: str
    mission_id: str
    task_id: str
    finding: str
    classification: RepairClassification
    proposed_action: RepairActionKind
    reason: str
    revision: str
    expected_queue_id: str
    evidence: tuple[str, ...]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "mission_id": self.mission_id,
            "task_id": self.task_id,
            "finding": self.finding,
            "classification": self.classification.value,
            "proposed_action": self.proposed_action.value,
            "reason": self.reason,
            "revision": self.revision,
            "expected_queue_id": self.expected_queue_id,
            "evidence": list(self.evidence),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class RepairApplicationResult:
    status: RepairApplyStatus
    action_id: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "status": self.status.value,
            "action_id": self.action_id,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class _QueueRecord:
    queue_id: str
    state: str
    path: Path
    payload: dict[str, Any] | None
    error: str | None = None


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _revision(envelope: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(envelope).encode("utf-8")).hexdigest()


def _load_snapshot(
    mission_id: str,
    store: MissionStore,
) -> tuple[MissionDefinition, MissionState, dict[str, Any], Path]:
    found = store.find_mission(mission_id)
    if found is None:
        raise FileNotFoundError(f"mission not found: {mission_id}")
    _, path = found
    envelope = store._load_envelope(path)
    definition = MissionDefinition.from_dict(envelope["definition"])
    state = MissionState.from_dict(envelope["state"])
    if definition.mission_id != mission_id or state.mission_id != mission_id:
        raise ValueError(f"malformed mission identity: {mission_id}")
    return definition, state, envelope, path


def _controller_task(
    mission_id: str,
    task_def: MissionTaskDefinition,
    queue_id: str,
) -> Task:
    return Task(
        id=queue_id,
        title=task_def.title,
        prompt=task_def.prompt,
        base_ref=task_def.base_ref,
        tests=list(task_def.tests),
        max_attempts=task_def.max_attempts,
        metadata={
            **task_def.metadata,
            "mission_id": mission_id,
            "mission_task_id": task_def.task_id,
            "depends_on": list(task_def.depends_on),
        },
    )


def _read_queue_records(queue: DurableQueue) -> list[_QueueRecord]:
    records: list[_QueueRecord] = []
    for state_name in _QUEUE_STATES:
        directory = getattr(queue, state_name)
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise TypeError("queue payload is not an object")
                records.append(
                    _QueueRecord(path.stem, state_name, path, payload)
                )
            except (OSError, ValueError, TypeError) as exc:
                records.append(
                    _QueueRecord(
                        path.stem,
                        state_name,
                        path,
                        None,
                        f"{type(exc).__name__}: {exc}",
                    )
                )
    return records


def _records_for_task(
    records: list[_QueueRecord],
    mission_id: str,
    task_id: str,
    expected_queue_id: str,
    legacy_queue_id: str,
) -> list[_QueueRecord]:
    selected: list[_QueueRecord] = []
    for record in records:
        if record.queue_id in {expected_queue_id, legacy_queue_id}:
            selected.append(record)
            continue
        if record.payload is None:
            continue
        metadata = record.payload.get("metadata")
        if (
            isinstance(metadata, dict)
            and metadata.get("mission_id") == mission_id
            and metadata.get("mission_task_id") == task_id
        ):
            selected.append(record)
    return selected


def _budget_violations(
    definition: MissionDefinition,
    state: MissionState,
) -> list[str]:
    usage = state.budget_usage
    limits = definition.budgets
    active_tasks = max(
        usage.active_tasks,
        sum(
            task.status == MissionTaskStatus.running
            for task in state.task_states.values()
        ),
    )
    queued_tasks = max(
        usage.queued_tasks,
        sum(
            task.status == MissionTaskStatus.queued
            for task in state.task_states.values()
        ),
    )
    total_attempts = max(
        usage.total_attempts,
        sum(task.attempt_count for task in state.task_states.values()),
    )
    violations: list[str] = []
    if active_tasks > limits.max_active_tasks:
        violations.append("max_active_tasks")
    if queued_tasks >= limits.max_queued_tasks:
        violations.append("max_queued_tasks")
    if total_attempts >= limits.max_total_attempts:
        violations.append("max_total_attempts")
    if usage.failed_tasks >= limits.max_failed_tasks:
        violations.append("max_failed_tasks")
    if usage.elapsed_seconds >= limits.max_runtime_seconds:
        violations.append("max_runtime_seconds")
    if usage.worktrees > limits.max_worktrees:
        violations.append("max_worktrees")
    return violations


def _runtime_budget_violations(
    definition: MissionDefinition,
    state: MissionState,
    task_def: MissionTaskDefinition,
) -> list[str]:
    violations = _budget_violations(definition, state)
    task_state = state.task_states.get(task_def.task_id)
    if task_state is None or not task_state.started_at:
        return violations
    try:
        started_at = datetime.fromisoformat(task_state.started_at)
    except (TypeError, ValueError):
        return violations
    if started_at.tzinfo is None:
        return violations
    elapsed = (
        datetime.now(timezone.utc) - started_at
    ).total_seconds()
    if elapsed >= task_def.timeout_seconds:
        violations.append("task_runtime_seconds")
    return violations


def _refresh_budget_usage(
    definition: MissionDefinition,
    state: MissionState,
    store: MissionStore,
    queue: DurableQueue,
    reports_root: Path,
) -> MissionState:
    scheduler = MissionScheduler(
        store=store,
        queue=queue,
        reports_root=Path(reports_root),
        missions_root=store._root,
        emit_materializer_events=False,
    )
    refreshed = scheduler._update_budget_usage(definition, state)
    persisted = state.budget_usage
    current = refreshed.budget_usage
    current.active_tasks = max(current.active_tasks, persisted.active_tasks)
    current.queued_tasks = max(current.queued_tasks, persisted.queued_tasks)
    current.total_attempts = max(
        current.total_attempts, persisted.total_attempts
    )
    current.failed_tasks = max(current.failed_tasks, persisted.failed_tasks)
    current.elapsed_seconds = max(
        current.elapsed_seconds, persisted.elapsed_seconds
    )
    current.worktrees = max(current.worktrees, persisted.worktrees)
    return refreshed


def _completion_report(
    reports_root: Path,
    mission_id: str,
    mission_task_id: str,
    queue_id: str,
    required_tests: list[list[str]],
) -> tuple[dict[str, Any] | None, tuple[str, ...], str | None]:
    path = Path(reports_root) / f"{queue_id}.json"
    if not path.exists():
        return None, (), "verified completion report is missing"
    try:
        with path.open("rb") as report_file:
            initial_stat = os.fstat(report_file.fileno())
            raw = report_file.read()
            final_stat = os.fstat(report_file.fileno())
        member_stat = path.stat()
        initial_identity = (
            initial_stat.st_dev,
            initial_stat.st_ino,
            initial_stat.st_size,
            initial_stat.st_mtime_ns,
        )
        final_identity = (
            final_stat.st_dev,
            final_stat.st_ino,
            final_stat.st_size,
            final_stat.st_mtime_ns,
        )
        member_identity = (
            member_stat.st_dev,
            member_stat.st_ino,
            member_stat.st_size,
            member_stat.st_mtime_ns,
        )
        if initial_identity != final_identity or final_identity != member_identity:
            return None, (), "completion report changed while being read"
        payload = json.loads(raw)
    except (OSError, ValueError) as exc:
        return None, (), f"completion report is malformed: {exc}"
    if not isinstance(payload, dict):
        return None, (), "completion report is not an object"
    report_error = completion_report_error(
        payload,
        mission_id=mission_id,
        mission_task_id=mission_task_id,
        queue_task_id=queue_id,
        required_tests=required_tests,
    )
    if report_error is not None:
        return None, (), report_error

    conflicts: list[str] = []
    for candidate in sorted(Path(reports_root).glob("*.json")):
        if candidate == path:
            continue
        try:
            other = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(other, dict):
            continue
        if (
            other.get("task_id") == queue_id
            or (
                other.get("mission_id") == mission_id
                and other.get("mission_task_id") == mission_task_id
            )
        ):
            conflicts.append(candidate.name)
    if conflicts:
        return (
            None,
            (),
            "multiple completion reports claim the same mission task: "
            + ",".join(conflicts),
        )

    semantic = {
        "status": payload["status"],
        "task_id": payload["task_id"],
        "mission_id": payload["mission_id"],
        "mission_task_id": payload["mission_task_id"],
        "attempts": payload["attempts"],
        "tests": payload["tests"],
        "files_changed": payload["files_changed"],
        "changes": payload["changes"],
        "started_at": payload["started_at"],
        "finished_at": payload["finished_at"],
    }
    evidence = (
        "report_sha256=" + hashlib.sha256(raw).hexdigest(),
        "report_canonical_sha256="
        + hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest(),
        "report_semantic_sha256="
        + hashlib.sha256(_canonical_json(semantic).encode("utf-8")).hexdigest(),
        "report_file_revision="
        + ":".join(str(value) for value in final_identity),
    )
    return payload, evidence, None


def _has_conflicting_event(
    events: list[Any],
    task_id: str,
    queue_ids: set[str],
    *,
    allow_task_succeeded: bool = False,
) -> bool:
    if any(event.event_type in _MISSION_TERMINAL_EVENTS for event in events):
        return True
    relevant = [
        (index, event)
        for index, event in enumerate(events)
        if event.task_id == task_id
        and (event.queue_task_id is None or event.queue_task_id in queue_ids)
    ]
    terminals = [
        (index, event)
        for index, event in relevant
        if event.event_type in _CONFLICTING_EVENTS
        and not (
            allow_task_succeeded and event.event_type == "task_succeeded"
        )
    ]
    if terminals:
        return True
    all_terminals = [
        (index, event)
        for index, event in relevant
        if event.event_type in _CONFLICTING_EVENTS
    ]
    if not all_terminals:
        return False
    last_terminal_index = max(index for index, _ in all_terminals)
    return any(
        index > last_terminal_index
        and event.event_type in _TASK_NONTERMINAL_EVENTS
        for index, event in relevant
    )


def _event_snapshot(
    events_dir: Path,
    mission_id: str,
) -> tuple[list[Any], str]:
    event_log = MissionEventLog(events_dir, mission_id)
    snapshot = event_log.read_snapshot()
    return list(snapshot.events), snapshot.revision


def _dependencies_satisfied(
    task_def: MissionTaskDefinition,
    state: MissionState,
) -> bool:
    return all(
        dependency in state.task_states
        and state.task_states[dependency].status == MissionTaskStatus.succeeded
        for dependency in task_def.depends_on
    )


def _make_action(
    *,
    mission_id: str,
    task_id: str,
    finding: str,
    classification: RepairClassification,
    proposed_action: RepairActionKind,
    reason: str,
    revision: str,
    expected_queue_id: str,
    evidence: tuple[str, ...],
    created_at: str,
) -> RepairAction:
    identity = {
        "version": "mission-safe-repair-v0.1",
        "mission_id": mission_id,
        "task_id": task_id,
        "finding": finding,
        "classification": classification.value,
        "proposed_action": proposed_action.value,
        "reason": reason,
        "revision": revision,
        "expected_queue_id": expected_queue_id,
        "evidence": list(evidence),
    }
    action_id = "repair-" + hashlib.sha256(
        _canonical_json(identity).encode("utf-8")
    ).hexdigest()[:24]
    return RepairAction(
        action_id=action_id,
        mission_id=mission_id,
        task_id=task_id,
        finding=finding,
        classification=classification,
        proposed_action=proposed_action,
        reason=reason,
        revision=revision,
        expected_queue_id=expected_queue_id,
        evidence=evidence,
        created_at=created_at,
    )


def _canonical_action_id(action: RepairAction) -> str:
    identity = {
        "version": "mission-safe-repair-v0.1",
        "mission_id": action.mission_id,
        "task_id": action.task_id,
        "finding": action.finding,
        "classification": action.classification.value,
        "proposed_action": action.proposed_action.value,
        "reason": action.reason,
        "revision": action.revision,
        "expected_queue_id": action.expected_queue_id,
        "evidence": list(action.evidence),
    }
    return "repair-" + hashlib.sha256(
        _canonical_json(identity).encode("utf-8")
    ).hexdigest()[:24]


def _classify_task(
    *,
    definition: MissionDefinition,
    state: MissionState,
    revision: str,
    task_def: MissionTaskDefinition,
    queue: DurableQueue,
    records: list[_QueueRecord],
    reports_root: Path,
    events: list[Any],
    event_revision: str,
) -> RepairAction:
    mission_id = definition.mission_id
    task_id = task_def.task_id
    task_state = state.task_states.get(task_id) or MissionTaskState(
        task_id=task_id,
        status=MissionTaskStatus.pending,
    )
    materializer = TaskMaterializer(queue, reports_root)
    expected_id = materializer.make_queue_task_id(mission_id, task_id)
    legacy_id = make_legacy_queue_task_id(mission_id, task_id)
    task_records = _records_for_task(
        records, mission_id, task_id, expected_id, legacy_id
    )
    evidence = (
        f"mission_status={state.status.value}",
        f"task_status={task_state.status.value}",
        f"task_attempts={task_state.attempt_count}",
        f"expected_queue_id={expected_id}",
        "accepted_queue_ids=" + ",".join(sorted((expected_id, legacy_id))),
        "queue_records="
        + ",".join(
            f"{record.state}:{record.queue_id}" for record in task_records
        ),
        f"event_revision={event_revision}",
    )
    created_at = definition.created_at or "1970-01-01T00:00:00+00:00"

    def action(
        finding: str,
        classification: RepairClassification,
        proposed_action: RepairActionKind,
        reason: str,
        extra_evidence: tuple[str, ...] = (),
    ) -> RepairAction:
        return _make_action(
            mission_id=mission_id,
            task_id=task_id,
            finding=finding,
            classification=classification,
            proposed_action=proposed_action,
            reason=reason,
            revision=revision,
            expected_queue_id=expected_id,
            evidence=evidence + extra_evidence,
            created_at=created_at,
        )

    if state.status in _TERMINAL_MISSIONS:
        return action(
            "MISSION_NOT_REPAIRABLE",
            RepairClassification.HUMAN_REVIEW_REQUIRED,
            RepairActionKind.NONE,
            f"mission status {state.status.value} forbids automatic repair",
        )

    metadata_collisions = sorted(reserved_metadata_collisions(task_def))
    if metadata_collisions:
        return action(
            "RESERVED_METADATA_COLLISION",
            RepairClassification.HUMAN_REVIEW_REQUIRED,
            RepairActionKind.NONE,
            "task metadata collides with controller-owned fields",
            ("reserved_metadata=" + ",".join(metadata_collisions),),
        )

    if (
        task_def.approval_policy != ApprovalPolicy.no_approval_required
        or task_state.status == MissionTaskStatus.approval_required
    ):
        return action(
            "APPROVAL_GATE_PENDING",
            RepairClassification.HUMAN_REVIEW_REQUIRED,
            RepairActionKind.NONE,
            "an approval gate forbids automatic repair",
        )

    violations = _runtime_budget_violations(
        definition, state, task_def
    )
    if violations or task_state.attempt_count >= task_def.max_attempts:
        return action(
            "EXECUTION_BUDGET_EXHAUSTED",
            RepairClassification.HUMAN_REVIEW_REQUIRED,
            RepairActionKind.NONE,
            "retry or mission budget does not permit execution",
            ("budget_violations=" + ",".join(violations),),
        )

    if any(record.error for record in task_records):
        return action(
            "MALFORMED_QUEUE_RECORD",
            RepairClassification.HUMAN_REVIEW_REQUIRED,
            RepairActionKind.NONE,
            "a relevant durable queue record is malformed",
            tuple(
                f"queue_error={record.queue_id}:{record.error}"
                for record in task_records
                if record.error
            ),
        )

    if len(task_records) > 1:
        return action(
            "CONFLICTING_QUEUE_IDENTITIES",
            RepairClassification.HUMAN_REVIEW_REQUIRED,
            RepairActionKind.NONE,
            "multiple current, legacy, or metadata-matched queue records exist",
        )

    if task_state.queue_task_id not in (None, expected_id, legacy_id):
        return action(
            "MISSION_QUEUE_IDENTITY_MISMATCH",
            RepairClassification.HUMAN_REVIEW_REQUIRED,
            RepairActionKind.NONE,
            "mission state references an unexpected queue identity",
        )

    record = task_records[0] if task_records else None
    matching_success = any(
        event.event_type == "task_succeeded"
        and event.task_id == task_id
        and event.queue_task_id in {expected_id, legacy_id}
        for event in events
    )
    matching_expiration = any(
        event.event_type == "repair_expired"
        and event.task_id == task_id
        and event.queue_task_id in {expected_id, legacy_id}
        for event in events
    )
    if matching_expiration:
        return action(
            "EXPIRED_REPAIR_REQUIRES_HUMAN_REVIEW",
            RepairClassification.HUMAN_REVIEW_REQUIRED,
            RepairActionKind.NONE,
            "durable repair evidence expired before mission-state persistence",
        )
    if matching_success and task_state.status != MissionTaskStatus.succeeded:
        return action(
            "PREEXISTING_SUCCESS_REQUIRES_RECONCILIATION",
            RepairClassification.HUMAN_REVIEW_REQUIRED,
            RepairActionKind.NONE,
            "durable success predates repair intent and requires restart reconciliation",
        )
    if _has_conflicting_event(
        events,
        task_id,
        {expected_id, legacy_id},
        allow_task_succeeded=(
            record is not None and record.state == "succeeded"
        ),
    ):
        return action(
            (
                "CONTRADICTORY_COMPLETION_EVIDENCE"
                if record is not None and record.state == "succeeded"
                else "CONTRADICTORY_QUEUE_EVENT"
            ),
            RepairClassification.HUMAN_REVIEW_REQUIRED,
            RepairActionKind.NONE,
            "terminal or contradictory mission event evidence forbids repair",
        )
    if record is not None:
        expected_payload = _controller_task(
            mission_id, task_def, record.queue_id
        ).to_dict()
        if record.payload != expected_payload:
            return action(
                "QUEUE_PAYLOAD_MISMATCH",
                RepairClassification.HUMAN_REVIEW_REQUIRED,
                RepairActionKind.NONE,
                "durable queue payload cannot be reconstructed exactly",
            )

        if record.queue_id == legacy_id:
            return action(
                "LEGACY_QUEUE_RECORD",
                RepairClassification.UNSUPPORTED,
                RepairActionKind.NONE,
                "legacy queue linkage remains under the existing migration workflow",
            )

        if record.state == "succeeded":
            if task_state.status == MissionTaskStatus.succeeded:
                return action(
                    "DURABLE_SUCCESS_ALREADY_RECORDED",
                    RepairClassification.ALREADY_CONSISTENT,
                    RepairActionKind.NONE,
                    "mission task and authoritative queue record already agree",
                )
            if task_state.status not in {
                MissionTaskStatus.queued,
                MissionTaskStatus.running,
            }:
                return action(
                    "DURABLE_SUCCESS_WITH_UNEXPECTED_TASK_STATE",
                    RepairClassification.HUMAN_REVIEW_REQUIRED,
                    RepairActionKind.NONE,
                    "terminal queue success conflicts with mission task state",
                )
            if not _dependencies_satisfied(task_def, state):
                return action(
                    "DEPENDENCY_AMBIGUITY",
                    RepairClassification.HUMAN_REVIEW_REQUIRED,
                    RepairActionKind.NONE,
                    "dependency state does not support automatic completion",
                )
            report, report_evidence, report_error = _completion_report(
                reports_root,
                mission_id,
                task_id,
                expected_id,
                task_def.tests,
            )
            if report_error:
                return action(
                    "UNVERIFIED_DURABLE_SUCCESS",
                    RepairClassification.HUMAN_REVIEW_REQUIRED,
                    RepairActionKind.NONE,
                    report_error,
                )
            return action(
                "DURABLE_SUCCESS_NOT_RECORDED",
                RepairClassification.SAFE_REPAIR,
                RepairActionKind.COMPLETE_FROM_DURABLE_SUCCESS,
                "authoritative terminal-success queue and report evidence can use the normal completion transition",
                (
                    f"report_status={report['status']}",
                    f"dependency_count={len(task_def.depends_on)}",
                    *report_evidence,
                ),
            )

        if record.state in {"failed", "invalid"}:
            return action(
                "TERMINAL_QUEUE_CONTRADICTION",
                RepairClassification.HUMAN_REVIEW_REQUIRED,
                RepairActionKind.NONE,
                f"terminal queue state {record.state} forbids automatic repair",
            )

        if (
            task_state.status in {
                MissionTaskStatus.queued,
                MissionTaskStatus.running,
            }
            and task_state.queue_task_id == expected_id
        ):
            return action(
                "MISSION_AND_QUEUE_AGREE",
                RepairClassification.ALREADY_CONSISTENT,
                RepairActionKind.NONE,
                "mission task and authoritative queue record already agree",
            )

        return action(
            "UNLINKED_EXISTING_QUEUE_RECORD",
            RepairClassification.UNSUPPORTED,
            RepairActionKind.NONE,
            "an exact queue record exists but is not linked; normal scheduler reconciliation remains authoritative",
        )

    if task_state.status in _TERMINAL_TASKS:
        return action(
            "TERMINAL_TASK_WITHOUT_QUEUE_EVIDENCE",
            RepairClassification.HUMAN_REVIEW_REQUIRED,
            RepairActionKind.NONE,
            f"terminal task status {task_state.status.value} forbids automatic repair",
        )

    if task_state.status == MissionTaskStatus.running:
        return action(
            "UNKNOWN_RUNNING_PROVIDER_STATE",
            RepairClassification.HUMAN_REVIEW_REQUIRED,
            RepairActionKind.NONE,
            "a running task with no queue record has unknown provider state",
        )

    if (Path(reports_root) / f"{expected_id}.json").exists():
        return action(
            "UNLINKED_TERMINAL_REPORT",
            RepairClassification.HUMAN_REVIEW_REQUIRED,
            RepairActionKind.NONE,
            "terminal report evidence exists without an authoritative queue record",
        )

    if (
        task_state.status == MissionTaskStatus.queued
        and task_state.queue_task_id == expected_id
    ):
        if not _dependencies_satisfied(task_def, state):
            return action(
                "DEPENDENCY_AMBIGUITY",
                RepairClassification.HUMAN_REVIEW_REQUIRED,
                RepairActionKind.NONE,
                "dependency state does not support rematerialization",
            )
        return action(
            "LINKED_QUEUE_TASK_MISSING",
            RepairClassification.SAFE_REPAIR,
            RepairActionKind.REMATERIALIZE_MISSING_QUEUE_TASK,
            "linked queued task has satisfied dependencies, available budgets, exact reconstructable payload, and no queue conflict",
            (f"dependencies_satisfied={len(task_def.depends_on)}",),
        )

    graph = DependencyGraph(definition, state)
    if task_id not in graph.ready_tasks():
        return action(
            "DEPENDENCIES_NOT_SATISFIED",
            RepairClassification.ALREADY_CONSISTENT,
            RepairActionKind.NONE,
            "task is correctly not materialized because dependencies are incomplete",
        )

    if task_state.queue_task_id is not None:
        return action(
            "MISSING_LINKED_QUEUE_RECORD",
            RepairClassification.UNSUPPORTED,
            RepairActionKind.NONE,
            "linked missing queue work requires normal restart reconciliation",
        )

    return action(
        "ELIGIBLE_TASK_MISSING_FROM_QUEUE",
        RepairClassification.SAFE_REPAIR,
        RepairActionKind.REMATERIALIZE_MISSING_QUEUE_TASK,
        "eligible task has satisfied dependencies, available budgets, exact reconstructable payload, and no queue conflict",
        (f"dependencies_satisfied={len(task_def.depends_on)}",),
    )


def plan_mission_repairs(
    mission_id: str,
    store: MissionStore,
    queue: DurableQueue,
    reports_root: Path,
) -> list[RepairAction]:
    """Return a deterministic, read-only repair finding for every mission task."""
    candidates = store.find_mission_candidates(mission_id)
    if len(candidates) > 1:
        valid_envelopes: list[dict[str, Any]] = []
        candidate_evidence: list[str] = []
        revision_input: list[dict[str, Any]] = []
        for status, path in candidates:
            try:
                raw = path.read_bytes()
                envelope = json.loads(raw)
                definition = MissionDefinition.from_dict(envelope["definition"])
                MissionState.from_dict(envelope["state"])
                if definition.mission_id == mission_id:
                    valid_envelopes.append(envelope)
                error = None
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raw = b""
                error = f"{type(exc).__name__}:{exc}"
            candidate_evidence.append(
                f"state_candidate={status.value}:{path.name}:"
                + (error or hashlib.sha256(raw).hexdigest())
            )
            revision_input.append(
                {
                    "status": status.value,
                    "path": path.as_posix(),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "error": error,
                }
            )
        if not valid_envelopes:
            raise MissionStateConflictError(mission_id, candidates)
        selected_definition = MissionDefinition.from_dict(
            valid_envelopes[0]["definition"]
        )
        revision = hashlib.sha256(
            _canonical_json(revision_input).encode("utf-8")
        ).hexdigest()
        materializer = TaskMaterializer(queue, Path(reports_root))
        return [
            _make_action(
                mission_id=mission_id,
                task_id=task.task_id,
                finding="SPLIT_MISSION_STATE",
                classification=RepairClassification.HUMAN_REVIEW_REQUIRED,
                proposed_action=RepairActionKind.NONE,
                reason="multiple mission-state envelopes require human review",
                revision=revision,
                expected_queue_id=materializer.make_queue_task_id(
                    mission_id, task.task_id
                ),
                evidence=tuple(candidate_evidence),
                created_at=(
                    selected_definition.created_at
                    or "1970-01-01T00:00:00+00:00"
                ),
            )
            for task in sorted(
                selected_definition.tasks, key=lambda item: item.task_id
            )
        ]
    definition, state, envelope, _ = _load_snapshot(mission_id, store)
    state = _refresh_budget_usage(
        definition,
        state,
        store,
        queue,
        Path(reports_root),
    )
    revision = _revision(envelope)
    records = _read_queue_records(queue)
    events_dir = store._root / "events"
    try:
        events, event_revision = _event_snapshot(events_dir, mission_id)
    except (EventLogCorruptionError, OSError) as exc:
        materializer = TaskMaterializer(queue, Path(reports_root))
        return [
            _make_action(
                mission_id=mission_id,
                task_id=task.task_id,
                finding="CORRUPT_EVENT_EVIDENCE",
                classification=RepairClassification.HUMAN_REVIEW_REQUIRED,
                proposed_action=RepairActionKind.NONE,
                reason=str(exc),
                revision=revision,
                expected_queue_id=materializer.make_queue_task_id(
                    mission_id, task.task_id
                ),
                evidence=("event_log_corrupt=true",),
                created_at=(
                    definition.created_at
                    or "1970-01-01T00:00:00+00:00"
                ),
            )
            for task in sorted(definition.tasks, key=lambda item: item.task_id)
        ]
    return [
        _classify_task(
            definition=definition,
            state=state,
            revision=revision,
            task_def=task_def,
            queue=queue,
            records=records,
            reports_root=Path(reports_root),
            events=events,
            event_revision=event_revision,
        )
        for task_def in sorted(definition.tasks, key=lambda task: task.task_id)
    ]


def _result(
    action: RepairAction,
    status: RepairApplyStatus,
    reason: str,
) -> RepairApplicationResult:
    return RepairApplicationResult(
        status=status,
        action_id=action.action_id,
        reason=reason,
    )


def _already_applied(action: RepairAction, event_log: MissionEventLog) -> bool:
    return any(
        event.event_type == "repair_applied"
        and event.metadata.get("action_id") == action.action_id
        for event in event_log.read_snapshot_locked().events
    )


def _evidence_fingerprint(action: RepairAction) -> str:
    return hashlib.sha256(
        _canonical_json(list(action.evidence)).encode("utf-8")
    ).hexdigest()


def _effect_fingerprint(
    action: RepairAction,
    state: MissionState,
    queue: DurableQueue,
) -> str | None:
    task_state = state.task_states.get(action.task_id)
    if task_state is None:
        return None
    queue_path: Path
    if (
        action.proposed_action
        == RepairActionKind.REMATERIALIZE_MISSING_QUEUE_TASK
    ):
        queue_state = TaskMaterializer(
            queue, Path(".")
        ).queue_task_exists_in(action.expected_queue_id)
        if (
            queue_state != "pending"
            or task_state.status != MissionTaskStatus.queued
            or task_state.queue_task_id != action.expected_queue_id
        ):
            return None
        queue_path = queue.pending / f"{action.expected_queue_id}.json"
    elif (
        action.proposed_action
        == RepairActionKind.COMPLETE_FROM_DURABLE_SUCCESS
    ):
        if (
            task_state.status != MissionTaskStatus.succeeded
            or task_state.queue_task_id != action.expected_queue_id
            or not task_state.report_path
        ):
            return None
        queue_state = "succeeded"
        queue_path = queue.succeeded / f"{action.expected_queue_id}.json"
    else:
        return None
    try:
        queue_sha256 = hashlib.sha256(queue_path.read_bytes()).hexdigest()
    except OSError:
        return None
    effect = {
        "mission_id": action.mission_id,
        "task_id": action.task_id,
        "queue_id": action.expected_queue_id,
        "queue_state": queue_state,
        "task_status": task_state.status.value,
        "report_path": task_state.report_path,
        "task_state": task_state.to_dict(),
        "queue_sha256": queue_sha256,
    }
    return hashlib.sha256(
        _canonical_json(effect).encode("utf-8")
    ).hexdigest()


def _applied_evidence_status(
    action: RepairAction,
    event_log: MissionEventLog,
    state: MissionState,
    queue: DurableQueue,
) -> tuple[bool, bool]:
    expected_effect = _effect_fingerprint(action, state, queue)
    invalid = False
    for event in event_log.read_snapshot_locked().events:
        if (
            event.event_type != "repair_applied"
            or event.metadata.get("action_id") != action.action_id
        ):
            continue
        metadata = event.metadata
        valid = (
            event.mission_id == action.mission_id
            and event.task_id == action.task_id
            and event.queue_task_id == action.expected_queue_id
            and metadata.get("mission_id") == action.mission_id
            and metadata.get("mission_task_id") == action.task_id
            and metadata.get("repair_action")
            == action.proposed_action.value
            and metadata.get("expected_queue_id")
            == action.expected_queue_id
            and metadata.get("revision") == action.revision
            and metadata.get("evidence_fingerprint")
            == _evidence_fingerprint(action)
            and expected_effect is not None
            and metadata.get("effect_fingerprint") == expected_effect
        )
        if valid:
            return True, False
        invalid = True
    return False, invalid


def _expected_queue_payload(
    action: RepairAction,
    definition: MissionDefinition,
) -> dict[str, Any] | None:
    task_def = next(
        (
            task
            for task in definition.tasks
            if task.task_id == action.task_id
        ),
        None,
    )
    if task_def is None:
        return None
    return _controller_task(
        action.mission_id,
        task_def,
        action.expected_queue_id,
    ).to_dict()


def _payload_fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()


def _repair_started_binding(
    action: RepairAction,
    event_log: MissionEventLog,
    definition: MissionDefinition,
    state: MissionState,
    queue: DurableQueue,
    *,
    require_durable_effect: bool,
) -> bool:
    expected_payload = _expected_queue_payload(action, definition)
    if (
        expected_payload is None
        or definition.mission_id != action.mission_id
        or state.mission_id != action.mission_id
        or action.task_id not in state.task_states
    ):
        return False
    task_state = state.task_states[action.task_id]
    if (
        task_state.queue_task_id is not None
        and task_state.queue_task_id != action.expected_queue_id
    ):
        return False

    expected_metadata = {
        "action_id": action.action_id,
        "mission_id": action.mission_id,
        "mission_task_id": action.task_id,
        "expected_queue_id": action.expected_queue_id,
        "finding": action.finding,
        "repair_action": action.proposed_action.value,
        "revision": action.revision,
        "evidence_fingerprint": _evidence_fingerprint(action),
        "expected_queue_payload_fingerprint": _payload_fingerprint(
            expected_payload
        ),
    }
    bound_event = any(
        event.event_type == "repair_started"
        and event.mission_id == action.mission_id
        and event.task_id == action.task_id
        and event.queue_task_id == action.expected_queue_id
        and all(
            event.metadata.get(key) == value
            for key, value in expected_metadata.items()
        )
        for event in event_log.read_snapshot_locked().events
    )
    if not bound_event:
        return False
    if not require_durable_effect:
        return True

    expected_state = (
        "pending"
        if action.proposed_action
        == RepairActionKind.REMATERIALIZE_MISSING_QUEUE_TASK
        else "succeeded"
    )
    matching_records = [
        record
        for record in _read_queue_records(queue)
        if record.queue_id == action.expected_queue_id
    ]
    return (
        len(matching_records) == 1
        and matching_records[0].state == expected_state
        and matching_records[0].payload == expected_payload
    )


def _can_resume_started_action(
    action: RepairAction,
    current: RepairAction,
    event_log: MissionEventLog,
    definition: MissionDefinition,
    state: MissionState,
    queue: DurableQueue,
) -> bool:
    if not _repair_started_binding(
        action,
        event_log,
        definition,
        state,
        queue,
        require_durable_effect=False,
    ):
        return False
    if (
        current.classification != RepairClassification.SAFE_REPAIR
        or current.proposed_action != action.proposed_action
        or current.expected_queue_id != action.expected_queue_id
        or current.revision != action.revision
        or current.finding != action.finding
        or current.reason != action.reason
    ):
        return False
    original_evidence = tuple(
        item for item in action.evidence if not item.startswith("event_revision=")
    )
    current_evidence = tuple(
        item for item in current.evidence if not item.startswith("event_revision=")
    )
    return original_evidence == current_evidence


def _ensure_repair_started(
    action: RepairAction,
    event_log: MissionEventLog,
    event_token: object,
    definition: MissionDefinition,
    state: MissionState,
    queue: DurableQueue,
) -> None:
    if _repair_started_binding(
        action,
        event_log,
        definition,
        state,
        queue,
        require_durable_effect=False,
    ):
        return
    if any(
        event.event_type == "repair_started"
        and event.metadata.get("action_id") == action.action_id
        for event in event_log.read_snapshot_locked().events
    ):
        raise ValueError("repair_started evidence is incomplete or conflicting")
    expected_payload = _expected_queue_payload(action, definition)
    if expected_payload is None:
        raise ValueError("repair action does not identify a mission task")
    event_log.append_locked(
        make_event(
            "repair_started",
            action.mission_id,
            task_id=action.task_id,
            queue_task_id=action.expected_queue_id,
            reason=action.reason,
            metadata={
                "action_id": action.action_id,
                "finding": action.finding,
                "repair_action": action.proposed_action.value,
                "revision": action.revision,
                "mission_id": action.mission_id,
                "mission_task_id": action.task_id,
                "expected_queue_id": action.expected_queue_id,
                "evidence_fingerprint": _evidence_fingerprint(action),
                "expected_queue_payload_fingerprint": _payload_fingerprint(
                    expected_payload
                ),
            },
        ),
        event_token,
    )


def _record_repair_expired(
    action: RepairAction,
    event_log: MissionEventLog,
    event_token: object,
    violations: list[str],
    *,
    success_evidence_durable: bool,
) -> None:
    if any(
        event.event_type == "repair_expired"
        and event.metadata.get("action_id") == action.action_id
        for event in event_log.read_snapshot_locked().events
    ):
        return
    event_log.append_locked(
        make_event(
            "repair_expired",
            action.mission_id,
            task_id=action.task_id,
            queue_task_id=action.expected_queue_id,
            reason="runtime budget expired after repair intent",
            metadata={
                "action_id": action.action_id,
                "budget_violations": list(violations),
                "success_evidence_durable": success_evidence_durable,
            },
        ),
        event_token,
    )


def _write_state_locked(
    path: Path,
    envelope: dict[str, Any],
    state: MissionState,
) -> None:
    updated = {
        "definition": envelope["definition"],
        "state": state.to_dict(),
    }
    _atomic_json(path, updated)


def _append_repair_event(
    action: RepairAction,
    event_log: MissionEventLog,
    event_token: object,
    state: MissionState,
    queue: DurableQueue,
) -> None:
    effect_fingerprint = _effect_fingerprint(action, state, queue)
    if effect_fingerprint is None:
        raise ValueError("repair effect is not durably present")
    event_log.append_locked(
        make_event(
            "repair_applied",
            action.mission_id,
            task_id=action.task_id,
            queue_task_id=action.expected_queue_id,
            reason=action.reason,
            metadata={
                "action_id": action.action_id,
                "finding": action.finding,
                "repair_action": action.proposed_action.value,
                "revision": action.revision,
                "mission_id": action.mission_id,
                "mission_task_id": action.task_id,
                "expected_queue_id": action.expected_queue_id,
                "evidence_fingerprint": _evidence_fingerprint(action),
                "effect_fingerprint": effect_fingerprint,
            },
        ),
        event_token,
    )


def _recover_missing_events(
    action: RepairAction,
    state: MissionState,
    event_log: MissionEventLog,
    event_token: object,
    queue: DurableQueue,
) -> None:
    events = list(event_log.read_snapshot_locked().events)
    if action.proposed_action == RepairActionKind.REMATERIALIZE_MISSING_QUEUE_TASK:
        if not any(
            event.event_type == "task_enqueued"
            and event.task_id == action.task_id
            and event.queue_task_id == action.expected_queue_id
            for event in events
        ):
            event_log.append_locked(
                make_event(
                    "task_enqueued",
                    action.mission_id,
                    task_id=action.task_id,
                    queue_task_id=action.expected_queue_id,
                    reason="safe recovery rematerialization",
                ),
                event_token,
            )
    elif action.proposed_action == RepairActionKind.COMPLETE_FROM_DURABLE_SUCCESS:
        task_state = state.task_states[action.task_id]
        if not any(
            event.event_type == "task_succeeded"
            and event.task_id == action.task_id
            and event.queue_task_id == action.expected_queue_id
            for event in events
        ):
            event_log.append_locked(
                make_event(
                    "task_succeeded",
                    action.mission_id,
                    task_id=action.task_id,
                    queue_task_id=action.expected_queue_id,
                    report_path=task_state.report_path,
                ),
                event_token,
            )
        if state.status in {
            MissionStatus.succeeded,
            MissionStatus.failed,
            MissionStatus.budget_exhausted,
        } and not any(
            event.event_type == "mission_terminal" for event in events
        ):
            event_log.append_locked(
                make_event(
                    "mission_terminal",
                    action.mission_id,
                    reason=f"status={state.status.value}",
                ),
                event_token,
            )
    _append_repair_event(action, event_log, event_token, state, queue)


def apply_mission_repair(
    action: RepairAction,
    store: MissionStore,
    queue: DurableQueue,
    reports_root: Path,
    missions_root: Path,
) -> RepairApplicationResult:
    """Revalidate and apply one safe repair while holding the mission store lock."""
    events_dir = Path(missions_root) / "events"
    if _canonical_action_id(action) != action.action_id:
        return _result(
            action,
            RepairApplyStatus.STALE,
            "repair action identifier does not match its immutable contents",
        )
    if action.classification != RepairClassification.SAFE_REPAIR:
        return _result(
            action,
            RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
            "only SAFE_REPAIR actions can be applied",
        )
    lock_stack = ExitStack()
    try:
        lock_stack.enter_context(store._lock)
    except OSError as exc:
        lock_stack.close()
        return _result(
            action,
            RepairApplyStatus.FAILED,
            f"could not acquire repair locks: {exc}",
        )

    with lock_stack:
        if len(store.find_mission_candidates(action.mission_id)) > 1:
            return _result(
                action,
                RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
                "multiple mission-state envelopes require human review",
            )
        try:
            # Global order: mission store -> queue -> report evidence -> event.
            lock_stack.enter_context(FileLock(queue.lock_path))
            lock_stack.enter_context(
                FileLock(report_lock_path(Path(reports_root)))
            )
            event_log = MissionEventLog(events_dir, action.mission_id)
            event_token = lock_stack.enter_context(event_log.locked())
        except OSError as exc:
            return _result(
                action,
                RepairApplyStatus.FAILED,
                f"could not acquire repair locks: {exc}",
            )
        try:
            definition, state, envelope, mission_path = _load_snapshot(
                action.mission_id, store
            )
            applied, invalid_applied = _applied_evidence_status(
                action, event_log, state, queue
            )
            if applied:
                return _result(
                    action,
                    RepairApplyStatus.NO_LONGER_NEEDED,
                    "the repair action and its durable effect already agree",
                )
            if invalid_applied:
                return _result(
                    action,
                    RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
                    "repair_applied evidence is incomplete or contradicts the current effect",
                )
            current_actions = plan_mission_repairs(
                action.mission_id, store, queue, Path(reports_root)
            )
        except EventLogCorruptionError as exc:
            return _result(
                action,
                RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
                f"event evidence is corrupt: {exc}",
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return _result(
                action,
                RepairApplyStatus.FAILED,
                f"could not revalidate durable state: {exc}",
            )

        current = next(
            (item for item in current_actions if item.task_id == action.task_id),
            None,
        )
        if current is None:
            return _result(
                action,
                RepairApplyStatus.STALE,
                "mission task no longer exists",
            )

        task_state = state.task_states.get(action.task_id)
        resuming_unlinked_started = False
        if (
            action.proposed_action
            == RepairActionKind.REMATERIALIZE_MISSING_QUEUE_TASK
            and current.finding
            in {"MISSION_AND_QUEUE_AGREE", "UNLINKED_EXISTING_QUEUE_RECORD"}
        ):
            if (
                current.finding == "MISSION_AND_QUEUE_AGREE"
                and task_state is not None
                and task_state.queue_task_id == action.expected_queue_id
                and _repair_started_binding(
                    action,
                    event_log,
                    definition,
                    state,
                    queue,
                    require_durable_effect=True,
                )
            ):
                try:
                    _recover_missing_events(
                        action, state, event_log, event_token, queue
                    )
                except OSError as exc:
                    return _result(
                        action,
                        RepairApplyStatus.FAILED,
                        f"could not recover repair audit evidence: {exc}",
                    )
                return _result(
                    action,
                    RepairApplyStatus.APPLIED,
                    "missing bound repair audit evidence was reconciled",
                )
            if (
                current.finding == "UNLINKED_EXISTING_QUEUE_RECORD"
                and _repair_started_binding(
                    action,
                    event_log,
                    definition,
                    state,
                    queue,
                    require_durable_effect=True,
                )
            ):
                resuming_unlinked_started = True
            else:
                return _result(
                    action,
                    (
                        RepairApplyStatus.HUMAN_REVIEW_REQUIRED
                        if current.finding
                        == "UNLINKED_EXISTING_QUEUE_RECORD"
                        else RepairApplyStatus.STALE
                    ),
                    "queue work appeared without a complete bound repair audit",
                )
        if (
            action.proposed_action
            == RepairActionKind.COMPLETE_FROM_DURABLE_SUCCESS
            and task_state is not None
            and task_state.status == MissionTaskStatus.succeeded
        ):
            if _repair_started_binding(
                action,
                event_log,
                definition,
                state,
                queue,
                require_durable_effect=True,
            ):
                try:
                    _recover_missing_events(
                        action, state, event_log, event_token, queue
                    )
                except OSError as exc:
                    return _result(
                        action,
                        RepairApplyStatus.FAILED,
                        f"could not recover repair audit evidence: {exc}",
                    )
                return _result(
                    action,
                    RepairApplyStatus.APPLIED,
                    "missing bound completion audit evidence was reconciled",
                )
        if (
            current.classification
            == RepairClassification.HUMAN_REVIEW_REQUIRED
            and not resuming_unlinked_started
        ):
            return _result(
                action,
                RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
                current.reason,
            )
        if (
            current.action_id != action.action_id
            and not resuming_unlinked_started
            and not _can_resume_started_action(
                action,
                current,
                event_log,
                definition,
                state,
                queue,
            )
        ):
            return _result(
                action,
                RepairApplyStatus.STALE,
                "repair action does not match the canonical current plan",
            )
        if current.revision != action.revision:
            return _result(
                action,
                RepairApplyStatus.STALE,
                "mission durable state changed after planning",
            )
        if (
            current.classification != RepairClassification.SAFE_REPAIR
            or current.proposed_action != action.proposed_action
            or current.expected_queue_id != action.expected_queue_id
        ) and not resuming_unlinked_started:
            return _result(
                action,
                RepairApplyStatus.STALE,
                "repair preconditions changed after planning",
            )

        task_def = next(
            task for task in definition.tasks if task.task_id == action.task_id
        )
        state = _refresh_budget_usage(
            definition,
            state,
            store,
            queue,
            Path(reports_root),
        )
        fresh_budget_violations = _runtime_budget_violations(
            definition, state, task_def
        )
        task_state = state.task_states.get(action.task_id)
        if (
            fresh_budget_violations
            or task_state is None
            or task_state.attempt_count >= task_def.max_attempts
        ):
            return _result(
                action,
                RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
                "current retry or mission budget does not permit repair",
            )
        try:
            _ensure_repair_started(
                action,
                event_log,
                event_token,
                definition,
                state,
                queue,
            )
        except (OSError, ValueError) as exc:
            return _result(
                action,
                RepairApplyStatus.FAILED,
                f"could not record repair intent: {exc}",
            )
        state = _refresh_budget_usage(
            definition, state, store, queue, Path(reports_root)
        )
        post_intent_violations = _runtime_budget_violations(
            definition, state, task_def
        )
        if post_intent_violations:
            try:
                _record_repair_expired(
                    action,
                    event_log,
                    event_token,
                    post_intent_violations,
                    success_evidence_durable=False,
                )
            except OSError as exc:
                return _result(
                    action,
                    RepairApplyStatus.FAILED,
                    f"could not record expired repair: {exc}",
                )
            return _result(
                action,
                RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
                "mission budget expired before durable mutation",
            )

        if (
            action.proposed_action
            == RepairActionKind.REMATERIALIZE_MISSING_QUEUE_TASK
        ):
            try:
                queue_id = action.expected_queue_id
                controller_task = _controller_task(
                    action.mission_id,
                    task_def,
                    queue_id,
                )
                if (
                    controller_task.id != queue_id
                    or controller_task.metadata.get("mission_id")
                    != action.mission_id
                    or controller_task.metadata.get("mission_task_id")
                    != action.task_id
                ):
                    return _result(
                        action,
                        RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
                        "canonical payload does not identify the selected mission task",
                    )
                state = _refresh_budget_usage(
                    definition,
                    state,
                    store,
                    queue,
                    Path(reports_root),
                )
                queue_budget_violations = _runtime_budget_violations(
                    definition, state, task_def
                )
                if queue_budget_violations:
                    _record_repair_expired(
                        action,
                        event_log,
                        event_token,
                        queue_budget_violations,
                        success_evidence_durable=False,
                    )
                    return _result(
                        action,
                        RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
                        "mission budget expired immediately before queue creation",
                    )
                queue_atomic_json(
                    queue.pending / f"{queue_id}.json",
                    controller_task.to_dict(),
                )
            except (OSError, ValueError, TypeError) as exc:
                return _result(
                    action,
                    RepairApplyStatus.FAILED,
                    f"queue write failed: {exc}",
                )
            if queue_id != action.expected_queue_id:
                return _result(
                    action,
                    RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
                    "materializer returned an unexpected queue identity",
                )

            post_records = _records_for_task(
                _read_queue_records(queue),
                action.mission_id,
                action.task_id,
                action.expected_queue_id,
                make_legacy_queue_task_id(action.mission_id, action.task_id),
            )
            expected_payload = _controller_task(
                action.mission_id,
                task_def,
                action.expected_queue_id,
            ).to_dict()
            if (
                len(post_records) != 1
                or post_records[0].queue_id != action.expected_queue_id
                or post_records[0].state != "pending"
                or post_records[0].payload != expected_payload
            ):
                return _result(
                    action,
                    RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
                    "queue evidence changed during materialization",
                )

            previous = state.task_states.get(action.task_id) or MissionTaskState(
                task_id=action.task_id,
                status=MissionTaskStatus.pending,
            )
            updated_task = replace(
                previous,
                status=MissionTaskStatus.queued,
                queue_task_id=queue_id,
                queued_at=(
                    previous.queued_at
                    or datetime.now(timezone.utc).isoformat()
                ),
            )
            state.task_states[action.task_id] = updated_task
            scheduler = MissionScheduler(
                store=store,
                queue=queue,
                reports_root=Path(reports_root),
                missions_root=Path(missions_root),
                emit_materializer_events=False,
            )
            state = scheduler._update_budget_usage(definition, state)
            try:
                _write_state_locked(mission_path, envelope, state)
            except OSError as exc:
                return _result(
                    action,
                    RepairApplyStatus.FAILED,
                    f"mission-store write failed after queue creation: {exc}",
                )
            try:
                event_log.append_locked(
                    make_event(
                        "task_enqueued",
                        action.mission_id,
                        task_id=action.task_id,
                        queue_task_id=queue_id,
                        reason="safe recovery rematerialization",
                    ),
                    event_token,
                )
                _append_repair_event(
                    action, event_log, event_token, state, queue
                )
            except (OSError, ValueError) as exc:
                return _result(
                    action,
                    RepairApplyStatus.FAILED,
                    f"event write failed after durable repair: {exc}",
                )
            return _result(
                action,
                RepairApplyStatus.APPLIED,
                "missing queue task was rematerialized exactly once",
            )

        if (
            action.proposed_action
            == RepairActionKind.COMPLETE_FROM_DURABLE_SUCCESS
        ):
            scheduler = MissionScheduler(
                store=store,
                queue=queue,
                reports_root=Path(reports_root),
                missions_root=Path(missions_root),
                emit_materializer_events=False,
            )
            selected_task_ids = {
                action.task_id,
                *task_def.depends_on,
            }
            selected_state = replace(
                state,
                task_states={
                    task_id: task_state
                    for task_id, task_state in state.task_states.items()
                    if task_id in selected_task_ids
                },
            )
            report, report_evidence, report_error = _completion_report(
                Path(reports_root),
                action.mission_id,
                action.task_id,
                action.expected_queue_id,
                task_def.tests,
            )
            if report_error or report is None:
                return _result(
                    action,
                    RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
                    report_error or "completion report is unavailable",
                )
            try:
                completed_state, completion_events = scheduler._reconcile_tasks(
                    definition,
                    selected_state,
                    report_overrides={action.task_id: report},
                )
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return _result(
                    action,
                    RepairApplyStatus.FAILED,
                    f"normal completion reconciliation failed: {exc}",
                )
            _, current_report_evidence, current_report_error = (
                _completion_report(
                    Path(reports_root),
                    action.mission_id,
                    action.task_id,
                    action.expected_queue_id,
                    task_def.tests,
                )
            )
            if (
                current_report_error is not None
                or current_report_evidence != report_evidence
            ):
                return _result(
                    action,
                    (
                        RepairApplyStatus.HUMAN_REVIEW_REQUIRED
                        if current_report_error
                        else RepairApplyStatus.STALE
                    ),
                    current_report_error
                    or "completion report changed during reconciliation",
                )
            completed_task = completed_state.task_states.get(action.task_id)
            if (
                completed_task is None
                or completed_task.status != MissionTaskStatus.succeeded
            ):
                return _result(
                    action,
                    RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
                    "normal completion reconciliation did not prove success",
                )
            completion_budget_state = _refresh_budget_usage(
                definition, state, store, queue, Path(reports_root)
            )
            completion_budget_violations = _runtime_budget_violations(
                definition, completion_budget_state, task_def
            )
            if completion_budget_violations:
                try:
                    _record_repair_expired(
                        action,
                        event_log,
                        event_token,
                        completion_budget_violations,
                        success_evidence_durable=False,
                    )
                except OSError as exc:
                    return _result(
                        action,
                        RepairApplyStatus.FAILED,
                        f"could not record expired completion: {exc}",
                    )
                return _result(
                    action,
                    RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
                    "runtime budget expired before durable success evidence",
                )
            state = replace(state, task_states=dict(state.task_states))
            state.task_states[action.task_id] = completed_task
            state = scheduler._update_budget_usage(definition, state)
            terminal_status = scheduler._check_mission_terminal(
                definition, state
            )
            destination = mission_path
            if terminal_status is not None:
                state = replace(
                    state,
                    status=terminal_status,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                destination = store._path(
                    terminal_status, action.mission_id
                )
            try:
                existing_events = list(event_log.read_snapshot_locked().events)
                for event in completion_events:
                    if event.event_type != "task_succeeded":
                        continue
                    if not any(
                        existing.event_type == "task_succeeded"
                        and existing.task_id == event.task_id
                        and existing.queue_task_id == event.queue_task_id
                        for existing in existing_events
                    ):
                        event_log.append_locked(event, event_token)
                        existing_events.append(event)
            except OSError as exc:
                return _result(
                    action,
                    RepairApplyStatus.FAILED,
                    f"task success evidence write failed before completion: {exc}",
                )
            post_success_budget_state = _refresh_budget_usage(
                definition,
                completion_budget_state,
                store,
                queue,
                Path(reports_root),
            )
            post_success_violations = _runtime_budget_violations(
                definition, post_success_budget_state, task_def
            )
            if post_success_violations:
                try:
                    _record_repair_expired(
                        action,
                        event_log,
                        event_token,
                        post_success_violations,
                        success_evidence_durable=True,
                    )
                except OSError as exc:
                    return _result(
                        action,
                        RepairApplyStatus.FAILED,
                        f"could not record expired completion: {exc}",
                    )
                return _result(
                    action,
                    RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
                    "runtime budget expired after durable success evidence",
                )
            final_budget_state = _refresh_budget_usage(
                definition,
                post_success_budget_state,
                store,
                queue,
                Path(reports_root),
            )
            final_budget_violations = _runtime_budget_violations(
                definition, final_budget_state, task_def
            )
            if final_budget_violations:
                try:
                    _record_repair_expired(
                        action,
                        event_log,
                        event_token,
                        final_budget_violations,
                        success_evidence_durable=True,
                    )
                except OSError as exc:
                    return _result(
                        action,
                        RepairApplyStatus.FAILED,
                        f"could not record expired completion: {exc}",
                    )
                return _result(
                    action,
                    RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
                    "runtime budget expired at mission-state persistence boundary",
                )
            try:
                _write_state_locked(destination, envelope, state)
                if destination != mission_path:
                    mission_path.unlink()
            except OSError as exc:
                return _result(
                    action,
                    RepairApplyStatus.FAILED,
                    f"mission-store completion write failed: {exc}",
                )
            try:
                for event in completion_events:
                    if event.event_type != "task_succeeded":
                        event_log.append_locked(event, event_token)
                if terminal_status is not None:
                    event_log.append_locked(
                        make_event(
                            "mission_terminal",
                            action.mission_id,
                            reason=f"status={terminal_status.value}",
                        ),
                        event_token,
                    )
                _append_repair_event(
                    action, event_log, event_token, state, queue
                )
            except (OSError, ValueError) as exc:
                return _result(
                    action,
                    RepairApplyStatus.FAILED,
                    f"event write failed after durable completion: {exc}",
                )
            return _result(
                action,
                RepairApplyStatus.APPLIED,
                "durable success was recorded through normal mission reconciliation",
            )

        return _result(
            action,
            RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
            "the proposed repair action is not supported",
        )
