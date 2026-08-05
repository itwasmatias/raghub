"""
Mission scheduler.

Orchestrates mission execution by driving the task state machine on top of
the controller's DurableQueue.  One scheduler instance can manage a single
mission at a time; run multiple instances for multiple missions.

Scheduling cycle (``run_once``):
1.  Load definition + state from MissionStore.
2.  Reconcile in-flight task states against queue files and reports.
3.  Update task states based on queue outcomes.
4.  Update BudgetUsage counters.
5.  Check mission completion / failure.
6.  Identify ready tasks via DependencyGraph.
7.  Enqueue eligible ready tasks (respecting budget limits).
8.  Persist updated state (and transition status dir if needed).
9.  Append events for all state transitions.
10. Return current MissionStatus.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from tools.ai_controller._locking import FileLock
from tools.ai_controller.queue import DurableQueue, atomic_json as queue_atomic_json
from tools.ai_controller.models import Task
from tools.ai_controller.reports import report_lock_path
from .events import MissionEventLog, make_event
from .graph import DependencyGraph
from .materializer import TaskMaterializer, make_legacy_queue_task_id
from .models import (
    ApprovalPolicy,
    BudgetUsage,
    FailurePolicy,
    MissionDefinition,
    MissionEvent,
    MissionState,
    MissionStatus,
    MissionTaskState,
    MissionTaskStatus,
)
from .store import MissionStore

logger = logging.getLogger(__name__)


def completion_report_error(
    report: dict,
    *,
    mission_id: str,
    mission_task_id: str,
    queue_task_id: str,
    required_tests: list[list[str]] | None = None,
) -> str | None:
    """Return why a controller report cannot prove scheduler completion."""
    required = {
        "status",
        "task_id",
        "mission_id",
        "mission_task_id",
        "attempts",
        "tests",
        "files_changed",
        "changes",
        "started_at",
        "finished_at",
    }
    missing = sorted(required.difference(report))
    if missing:
        return "completion report is missing required fields: " + ",".join(missing)
    if report.get("status") != "succeeded":
        return "completion report does not record succeeded status"
    if report.get("task_id") != queue_task_id:
        return "completion report queue task identity does not match"
    if report.get("mission_id") != mission_id:
        return "completion report mission identity does not match"
    if report.get("mission_task_id") != mission_task_id:
        return "completion report mission task identity does not match"

    attempts = report.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return "completion report has no provider result"
    final_attempt = attempts[-1]
    if (
        not isinstance(final_attempt, dict)
        or not isinstance(final_attempt.get("provider"), str)
        or not final_attempt["provider"]
        or final_attempt.get("success") is not True
        or final_attempt.get("timed_out", False) is not False
    ):
        return "completion report does not prove provider success"

    tests = report.get("tests")
    if not isinstance(tests, list) or not tests:
        return "completion report does not prove verifier success"
    required_commands = required_tests or []
    executed_commands: list[list[str]] = []
    for test in tests:
        if (
            not isinstance(test, dict)
            or not isinstance(test.get("argv"), list)
            or not test["argv"]
            or any(not isinstance(arg, str) or not arg for arg in test["argv"])
            or not isinstance(test.get("exit_code"), int)
            or isinstance(test.get("exit_code"), bool)
            or test["exit_code"] != 0
            or test.get("timed_out", False) is not False
        ):
            return "completion report does not prove verifier success"
        executed_commands.append(test["argv"])
    if any(command not in executed_commands for command in required_commands):
        return "completion report lacks task-required verification evidence"

    files_changed = report.get("files_changed")
    changes = report.get("changes")
    if (
        not isinstance(files_changed, list)
        or not files_changed
        or any(not isinstance(path, str) or not path for path in files_changed)
        or not isinstance(changes, list)
        or not changes
    ):
        return "completion report does not contain a verified result"

    parsed_times: dict[str, datetime] = {}
    for field in ("started_at", "finished_at"):
        value = report.get(field)
        if not isinstance(value, str) or not value:
            return f"completion report {field} is invalid"
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return f"completion report {field} is invalid"
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return f"completion report {field} is not timezone-aware"
        parsed_times[field] = parsed
    if parsed_times["finished_at"] < parsed_times["started_at"]:
        return "completion report finished_at precedes started_at"
    return None

# Statuses treated as "task is no longer progressing forward".
_TERMINAL_TASK_STATUSES = {
    MissionTaskStatus.succeeded,
    MissionTaskStatus.failed,
    MissionTaskStatus.cancelled,
    MissionTaskStatus.blocked,
}

# Terminal mission statuses — the scheduler stops after reaching one.
_TERMINAL_MISSION_STATUSES = {
    MissionStatus.succeeded,
    MissionStatus.failed,
    MissionStatus.cancelled,
    MissionStatus.budget_exhausted,
}
_QUEUE_STATES = ("pending", "running", "succeeded", "failed", "invalid")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MissionScheduler:
    """Drive a single mission from start to terminal state."""

    def __init__(
        self,
        store: MissionStore,
        queue: DurableQueue,
        reports_root: Path,
        missions_root: Path,
        poll_interval_seconds: float = 10.0,
        emit_materializer_events: bool = True,
    ) -> None:
        self._store = store
        self._queue = queue
        self._reports_root = Path(reports_root)
        self._missions_root = Path(missions_root)
        self._poll_interval = poll_interval_seconds
        events_dir = (
            Path(missions_root) / "events"
            if emit_materializer_events
            else None
        )
        self._materializer = TaskMaterializer(queue, reports_root, events_dir=events_dir)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run_once(self, mission_id: str) -> MissionStatus:
        """Execute one full scheduling cycle and return the resulting status."""
        event_log = MissionEventLog(
            self._missions_root / "events", mission_id
        )
        # Global causal lock order shared with repair:
        # mission store -> durable queue -> report evidence -> mission event.
        with (
            self._store._lock,
            FileLock(self._queue.lock_path),
            FileLock(report_lock_path(self._reports_root)),
            event_log.locked() as event_token,
        ):
            return self._run_once_locked(
                mission_id, event_log, event_token
            )

    def _run_once_locked(
        self,
        mission_id: str,
        event_log: MissionEventLog,
        event_token: object,
    ) -> MissionStatus:
        """Run one cycle while all authoritative causal locks are held."""
        definition = self._store.load_definition(mission_id)
        state = self._store.load_state(mission_id)

        durable_events = list(event_log.read_snapshot_locked().events)
        new_events: list[MissionEvent] = []

        # Mark as running the first time we cycle.
        if state.status == MissionStatus.pending:
            state = MissionState(
                mission_id=state.mission_id,
                status=MissionStatus.running,
                task_states=state.task_states,
                budget_usage=state.budget_usage,
                started_at=state.started_at or _now(),
                finished_at=state.finished_at,
                paused_at=state.paused_at,
                failure_reason=state.failure_reason,
                root_cause_task_ids=state.root_cause_task_ids,
                events_path=state.events_path,
                report_path=state.report_path,
            )
            new_events.append(
                make_event("mission_started", mission_id)
            )

        # Skip scheduling work when paused; just persist and return.
        if state.status == MissionStatus.paused:
            self._store.update_state(mission_id, state)
            for ev in new_events:
                event_log.append_locked(ev, event_token)
            return state.status

        # --- Step 2-3: reconcile task states ---
        blocked_completion_tasks = self._incomplete_completion_repairs(
            durable_events
        )
        preserved_tasks = {
            task_id: state.task_states[task_id]
            for task_id in blocked_completion_tasks
            if task_id in state.task_states
        }
        reconcile_state = replace(
            state,
            task_states={
                task_id: task_state
                for task_id, task_state in state.task_states.items()
                if task_id not in blocked_completion_tasks
            },
        )
        state, reconcile_events = self._reconcile_tasks(
            definition, reconcile_state, durable_events=durable_events
        )
        state.task_states.update(preserved_tasks)
        new_events.extend(reconcile_events)
        success_events = [
            event
            for event in new_events
            if event.event_type == "task_succeeded"
        ]
        for event in success_events:
            if not any(
                existing.event_type == "task_succeeded"
                and existing.task_id == event.task_id
                and existing.queue_task_id == event.queue_task_id
                for existing in durable_events
            ):
                event_log.append_locked(event, event_token)
                durable_events.append(event)
        new_events = [
            event
            for event in new_events
            if event.event_type != "task_succeeded"
        ]
        if success_events:
            self._store.update_state(mission_id, state)
            persisted_state = self._store.load_state(mission_id)
            durable_events = list(
                event_log.read_snapshot_locked().events
            )
            for event in success_events:
                persisted_task = persisted_state.task_states.get(
                    event.task_id or ""
                )
                if (
                    persisted_task is None
                    or persisted_task.status
                    != MissionTaskStatus.succeeded
                    or persisted_task.queue_task_id
                    != event.queue_task_id
                    or not any(
                        durable.event_type == "task_succeeded"
                        and durable.task_id == event.task_id
                        and durable.queue_task_id == event.queue_task_id
                        for durable in durable_events
                    )
                ):
                    raise RuntimeError(
                        "durable task completion and success evidence disagree"
                    )
            state = persisted_state

        # --- Step 3b: mark blocked tasks ---
        state, block_events = self._mark_blocked_tasks(definition, state)
        new_events.extend(block_events)

        # --- Step 4: update budget usage ---
        state = self._update_budget_usage(definition, state)

        # --- Step 5: check mission completion/failure ---
        terminal_status = self._check_mission_terminal(definition, state)
        if terminal_status is not None:
            finished_at = _now()
            failure_reason: str | None = None
            root_cause_ids: list[str] = []

            graph = DependencyGraph(definition, state)
            if terminal_status == MissionStatus.failed:
                stop_tasks = [
                    tid
                    for tid in graph.failed_tasks()
                    if _task_def_by_id(definition, tid) is not None
                    and _task_def_by_id(definition, tid).failure_policy  # type: ignore[union-attr]
                    in (FailurePolicy.stop_mission, FailurePolicy.retry_then_stop)
                ]
                root_cause_ids = stop_tasks
                failure_reason = (
                    f"tasks failed with stop_mission policy: {stop_tasks}"
                    if stop_tasks
                    else "mission failed"
                )
            elif terminal_status == MissionStatus.budget_exhausted:
                exceeded = self._check_budgets(definition, state)
                failure_reason = f"budget exceeded: {exceeded}"

            new_state = MissionState(
                mission_id=state.mission_id,
                status=terminal_status,
                task_states=state.task_states,
                budget_usage=state.budget_usage,
                started_at=state.started_at,
                finished_at=finished_at,
                paused_at=state.paused_at,
                failure_reason=failure_reason or state.failure_reason,
                root_cause_task_ids=root_cause_ids or state.root_cause_task_ids,
                events_path=state.events_path,
                report_path=state.report_path,
            )
            self._store.transition(mission_id, terminal_status, new_state)
            new_events.append(
                make_event(
                    "mission_terminal",
                    mission_id,
                    reason=f"status={terminal_status.value}",
                )
            )
            for ev in new_events:
                event_log.append_locked(ev, event_token)
            logger.info(
                "mission reached terminal state mission_id=%s status=%s",
                mission_id,
                terminal_status.value,
            )
            return terminal_status

        # --- Step 6: find ready tasks ---
        ready_task_ids = self._compute_ready_tasks(
            definition, state, durable_events
        )

        # --- Step 7: enqueue eligible tasks ---
        budget_exceeded = self._check_budgets(definition, state)
        if budget_exceeded:
            logger.warning(
                "budget exceeded for mission %s: %s — suspending enqueue",
                mission_id,
                budget_exceeded,
            )

        if not budget_exceeded:
            for task_id in ready_task_ids:
                task_def = _task_def_by_id(definition, task_id)
                if task_def is None:
                    continue
                try:
                    qtid = self._materializer.materialize(
                        mission_id, task_def, state
                    )
                except Exception as exc:
                    logger.error(
                        "failed to materialise task mission=%s task=%s: %s",
                        mission_id,
                        task_id,
                        exc,
                        exc_info=True,
                    )
                    continue

                # Update task state to queued.
                ts = state.task_states.get(task_id) or MissionTaskState(
                    task_id=task_id,
                    status=MissionTaskStatus.pending,
                )
                updated_ts = MissionTaskState(
                    task_id=ts.task_id,
                    status=MissionTaskStatus.queued,
                    queue_task_id=qtid,
                    report_path=ts.report_path,
                    worktree_path=ts.worktree_path,
                    attempt_count=ts.attempt_count,
                    failure_reason=ts.failure_reason,
                    queued_at=ts.queued_at or _now(),
                    started_at=ts.started_at,
                    finished_at=ts.finished_at,
                    files_changed=ts.files_changed,
                    test_results=ts.test_results,
                )
                state.task_states[task_id] = updated_ts
                new_events.append(
                    make_event(
                        "task_enqueued",
                        mission_id,
                        task_id=task_id,
                        queue_task_id=qtid,
                    )
                )

        # --- Step 8: persist updated state ---
        self._store.update_state(mission_id, state)

        # --- Step 9: append events ---
        for ev in new_events:
            event_log.append_locked(ev, event_token)

        return state.status

    @staticmethod
    def _incomplete_completion_repairs(
        events: list[MissionEvent],
    ) -> set[str]:
        """Return tasks whose repair intent lacks durable success evidence."""
        applied_ids = {
            event.metadata.get("action_id")
            for event in events
            if event.event_type == "repair_applied"
        }
        expired_ids = {
            event.metadata.get("action_id")
            for event in events
            if event.event_type == "repair_expired"
        }
        blocked: set[str] = set()
        for index, event in enumerate(events):
            if (
                event.event_type != "repair_started"
                or event.task_id is None
                or event.metadata.get("repair_action")
                != "COMPLETE_FROM_DURABLE_SUCCESS"
                or event.metadata.get("action_id") in applied_ids
            ):
                continue
            if event.metadata.get("action_id") in expired_ids:
                blocked.add(event.task_id)
                continue
            has_success = any(
                later.event_type == "task_succeeded"
                and later.task_id == event.task_id
                and later.queue_task_id == event.queue_task_id
                for later in events[index + 1 :]
            )
            if not has_success:
                blocked.add(event.task_id)
        return blocked

    def run_continuous(self, mission_id: str) -> None:
        """Loop ``run_once`` until the mission reaches a terminal state."""
        logger.info("starting continuous scheduling for mission %s", mission_id)
        while True:
            try:
                status = self.run_once(mission_id)
            except Exception as exc:
                logger.error(
                    "unhandled error in scheduler cycle for mission %s: %s",
                    mission_id,
                    exc,
                    exc_info=True,
                )
                status = MissionStatus.running  # keep trying
            if status in _TERMINAL_MISSION_STATUSES:
                logger.info(
                    "mission %s reached terminal status %s — stopping",
                    mission_id,
                    status.value,
                )
                return
            time.sleep(self._poll_interval)

    # ------------------------------------------------------------------
    # Control operations
    # ------------------------------------------------------------------

    def pause(self, mission_id: str) -> None:
        """Transition the mission to *paused*.

        Active tasks continue to completion; new tasks will not be enqueued
        until :meth:`resume` is called.
        """
        state = self._store.load_state(mission_id)
        if state.status in _TERMINAL_MISSION_STATUSES:
            logger.warning(
                "cannot pause mission %s in terminal state %s",
                mission_id,
                state.status.value,
            )
            return
        new_state = MissionState(
            mission_id=state.mission_id,
            status=MissionStatus.paused,
            task_states=state.task_states,
            budget_usage=state.budget_usage,
            started_at=state.started_at,
            finished_at=state.finished_at,
            paused_at=_now(),
            failure_reason=state.failure_reason,
            root_cause_task_ids=state.root_cause_task_ids,
            events_path=state.events_path,
            report_path=state.report_path,
        )
        self._store.transition(mission_id, MissionStatus.paused, new_state)
        event_log = MissionEventLog(
            self._missions_root / "events", mission_id
        )
        event_log.append(make_event("mission_paused", mission_id))
        logger.info("mission paused mission_id=%s", mission_id)

    def resume(self, mission_id: str) -> None:
        """Transition the mission from *paused* back to *running*."""
        state = self._store.load_state(mission_id)
        if state.status != MissionStatus.paused:
            logger.warning(
                "cannot resume mission %s — current status is %s",
                mission_id,
                state.status.value,
            )
            return
        new_state = MissionState(
            mission_id=state.mission_id,
            status=MissionStatus.running,
            task_states=state.task_states,
            budget_usage=state.budget_usage,
            started_at=state.started_at,
            finished_at=state.finished_at,
            paused_at=None,
            failure_reason=state.failure_reason,
            root_cause_task_ids=state.root_cause_task_ids,
            events_path=state.events_path,
            report_path=state.report_path,
        )
        self._store.transition(mission_id, MissionStatus.running, new_state)
        event_log = MissionEventLog(
            self._missions_root / "events", mission_id
        )
        event_log.append(make_event("mission_resumed", mission_id))
        logger.info("mission resumed mission_id=%s", mission_id)

    def cancel(self, mission_id: str) -> None:
        """Transition the mission to *cancelled* and mark non-terminal tasks cancelled."""
        state = self._store.load_state(mission_id)
        if state.status in _TERMINAL_MISSION_STATUSES:
            logger.warning(
                "cannot cancel mission %s in terminal state %s",
                mission_id,
                state.status.value,
            )
            return

        updated_task_states: dict[str, MissionTaskState] = {}
        for tid, ts in state.task_states.items():
            if ts.status not in _TERMINAL_TASK_STATUSES:
                updated_task_states[tid] = MissionTaskState(
                    task_id=ts.task_id,
                    status=MissionTaskStatus.cancelled,
                    queue_task_id=ts.queue_task_id,
                    report_path=ts.report_path,
                    worktree_path=ts.worktree_path,
                    attempt_count=ts.attempt_count,
                    failure_reason="mission cancelled",
                    queued_at=ts.queued_at,
                    started_at=ts.started_at,
                    finished_at=ts.finished_at or _now(),
                    files_changed=ts.files_changed,
                    test_results=ts.test_results,
                )
            else:
                updated_task_states[tid] = ts

        new_state = MissionState(
            mission_id=state.mission_id,
            status=MissionStatus.cancelled,
            task_states=updated_task_states,
            budget_usage=state.budget_usage,
            started_at=state.started_at,
            finished_at=_now(),
            paused_at=state.paused_at,
            failure_reason="cancelled by operator",
            root_cause_task_ids=state.root_cause_task_ids,
            events_path=state.events_path,
            report_path=state.report_path,
        )
        self._store.transition(mission_id, MissionStatus.cancelled, new_state)
        event_log = MissionEventLog(
            self._missions_root / "events", mission_id
        )
        event_log.append(make_event("mission_cancelled", mission_id, reason="operator request"))
        logger.info("mission cancelled mission_id=%s", mission_id)

    @staticmethod
    def _retry_payload(
        mission_id: str,
        task_def: MissionTaskDefinition,
        queue_id: str,
        *,
        attempt_number: int,
    ) -> dict:
        metadata = {
            **task_def.metadata,
            "mission_id": mission_id,
            "mission_task_id": task_def.task_id,
            "depends_on": list(task_def.depends_on),
        }
        if attempt_number > 1:
            metadata["mission_attempt"] = attempt_number
        return Task(
            id=queue_id,
            title=task_def.title,
            prompt=task_def.prompt,
            base_ref=task_def.base_ref,
            tests=list(task_def.tests),
            max_attempts=task_def.max_attempts,
            metadata=metadata,
        ).to_dict()

    def _retry_queue_records(
        self,
        mission_id: str,
        task_def: MissionTaskDefinition,
        task_state: MissionTaskState,
        *,
        allow_pending_previous: bool = False,
    ) -> tuple[str, list[tuple[str, Path, dict]]]:
        """Return one owned retry record or fail closed on ambiguous evidence."""
        current_id = self._materializer.make_queue_task_id(
            mission_id, task_def.task_id
        )
        legacy_id = make_legacy_queue_task_id(mission_id, task_def.task_id)
        identities = {current_id, legacy_id}
        if (
            task_state.queue_task_id is not None
            and task_state.queue_task_id not in identities
        ):
            raise ValueError("mission task has a non-deterministic queue identity")

        records: list[tuple[str, Path, dict]] = []
        for state_name in _QUEUE_STATES:
            directory = getattr(self._queue, state_name)
            for path in sorted(directory.glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError) as exc:
                    if path.stem in identities:
                        raise ValueError(
                            "retry queue evidence is malformed"
                        ) from exc
                    continue
                if not isinstance(payload, dict):
                    if path.stem in identities:
                        raise ValueError("retry queue payload is not an object")
                    continue
                metadata = payload.get("metadata")
                owns_task = (
                    isinstance(metadata, dict)
                    and metadata.get("mission_id") == mission_id
                    and metadata.get("mission_task_id") == task_def.task_id
                )
                if path.stem in identities or owns_task:
                    if path.stem not in identities:
                        raise ValueError(
                            "retry queue record has a foreign identity"
                        )
                    records.append((state_name, path, payload))

        if len(records) > 1:
            raise ValueError(
                "retry queue identity occurs in multiple locations"
            )
        if not records:
            return current_id, records

        state_name, path, payload = records[0]
        if payload.get("id") != path.stem:
            raise ValueError("retry queue payload identity does not match location")
        expected = self._retry_payload(
            mission_id,
            task_def,
            path.stem,
            attempt_number=task_state.attempt_count,
        )
        retry_expected = self._retry_payload(
            mission_id,
            task_def,
            path.stem,
            attempt_number=task_state.attempt_count + 1,
        )
        if payload != expected and payload != retry_expected:
            raise ValueError(
                "retry queue payload is not the exact controller task"
            )
        if (
            state_name == "pending"
            and payload != retry_expected
            and not (allow_pending_previous and payload == expected)
        ):
            raise ValueError(
                "pending retry queue record lacks its bound retry attempt"
            )
        return path.stem, records

    def retry_queue_ownership_error(
        self,
        mission_id: str,
        task_def: MissionTaskDefinition,
        task_state: MissionTaskState,
    ) -> str | None:
        """Expose the scheduler's ownership check for proposal validation."""
        try:
            self._retry_queue_records(mission_id, task_def, task_state)
        except ValueError as exc:
            return str(exc)
        return None

    def _archive_retry_report(self, queue_id: str, attempt_number: int) -> None:
        current = self._reports_root / f"{queue_id}.json"
        if not current.exists():
            return
        history = self._reports_root / "history"
        destination = history / f"{queue_id}.attempt-{attempt_number}.json"
        if destination.exists():
            raise ValueError("retry report history already contains this attempt")
        history.mkdir(parents=True, exist_ok=True)
        os.replace(current, destination)

    def retry_task(
        self,
        mission_id: str,
        task_id: str,
        *,
        recovery_action_id: str | None = None,
    ) -> str:
        """Requeue one failed task through the existing queue identity authority."""
        event_log = MissionEventLog(
            self._missions_root / "events", mission_id
        )
        with (
            self._store._lock,
            FileLock(self._queue.lock_path),
            FileLock(report_lock_path(self._reports_root)),
            event_log.locked() as event_token,
        ):
            definition = self._store.load_definition(mission_id)
            state = self._store.load_state(mission_id)
            task_def = next(
                (item for item in definition.tasks if item.task_id == task_id),
                None,
            )
            task_state = state.task_states.get(task_id)
            if task_def is None or task_state is None:
                raise KeyError(f"mission task not found: {task_id}")
            if state.status != MissionStatus.running:
                raise ValueError(
                    f"mission does not permit task retry: {mission_id}"
                )
            queue_id, records = self._retry_queue_records(
                mission_id,
                task_def,
                task_state,
                allow_pending_previous=recovery_action_id is not None,
            )
            pending_record = (
                records[0]
                if len(records) == 1 and records[0][0] == "pending"
                else None
            )
            if (
                recovery_action_id
                and task_state.status == MissionTaskStatus.queued
                and pending_record is not None
                and task_state.queue_task_id == queue_id
            ):
                if not any(
                    event.event_type == "task_retry_queued"
                    and event.task_id == task_id
                    and event.queue_task_id == task_state.queue_task_id
                    and event.metadata.get("attempt_number")
                    == task_state.attempt_count + 1
                    and event.metadata.get("controller_action_id")
                    == recovery_action_id
                    for event in event_log.read_snapshot_locked().events
                ):
                    event_log.append_locked(
                        make_event(
                            "task_retry_queued",
                            mission_id,
                            task_id=task_id,
                            queue_task_id=task_state.queue_task_id,
                            metadata={
                                "attempt_number": task_state.attempt_count + 1,
                                "max_attempts": task_def.max_attempts,
                                "controller_action_id": recovery_action_id,
                                "recovered_interrupted_retry": True,
                            },
                        ),
                        event_token,
                    )
                return task_state.queue_task_id
            if task_state.status != MissionTaskStatus.failed:
                raise ValueError(f"mission task is not retryable: {task_id}")
            if (
                task_state.attempt_count >= task_def.max_attempts
                or state.budget_usage.total_attempts
                >= definition.budgets.max_total_attempts
            ):
                raise ValueError(f"mission task retry budget exhausted: {task_id}")
            durable_events = list(
                event_log.read_snapshot_locked().events
            )
            for dependency in task_def.depends_on:
                dependency_state = state.task_states.get(dependency)
                if dependency_state is None or not any(
                    self._is_exact_prerequisite_success(
                        event,
                        mission_id,
                        dependency,
                        dependency_state,
                    )
                    for event in durable_events
                ):
                    raise ValueError(
                        f"mission task prerequisite is not durably satisfied: "
                        f"{dependency}"
                    )

            if records and records[0][0] in {"pending", "running"} and not (
                recovery_action_id
                and records[0][0] == "pending"
                and records[0][2]
                in (
                    self._retry_payload(
                        mission_id,
                        task_def,
                        queue_id,
                        attempt_number=task_state.attempt_count,
                    ),
                    self._retry_payload(
                        mission_id,
                        task_def,
                        queue_id,
                        attempt_number=task_state.attempt_count + 1,
                    ),
                )
            ):
                raise ValueError("mission task already has active queue work")

            if records:
                state_name, source, _ = records[0]
                if state_name == "pending" and recovery_action_id:
                    queue_atomic_json(
                        source,
                        self._retry_payload(
                            mission_id,
                            task_def,
                            queue_id,
                            attempt_number=task_state.attempt_count + 1,
                        ),
                    )
                elif state_name not in {"failed", "invalid"}:
                    raise ValueError("retry queue record is not terminal")
                else:
                    self._archive_retry_report(
                        queue_id, task_state.attempt_count
                    )
                    destination = self._queue.pending / source.name
                    source.replace(destination)
                    queue_atomic_json(
                        destination,
                        self._retry_payload(
                            mission_id,
                            task_def,
                            queue_id,
                            attempt_number=task_state.attempt_count + 1,
                        ),
                    )
            else:
                self._archive_retry_report(
                    task_state.queue_task_id or queue_id,
                    task_state.attempt_count,
                )
                state.task_states[task_id] = replace(
                    task_state,
                    queue_task_id=None,
                )
                queue_id = self._materializer.materialize(
                    mission_id, task_def, state
                )
                queue_atomic_json(
                    self._queue.pending / f"{queue_id}.json",
                    self._retry_payload(
                        mission_id,
                        task_def,
                        queue_id,
                        attempt_number=task_state.attempt_count + 1,
                    ),
                )

            state.task_states[task_id] = MissionTaskState(
                task_id=task_state.task_id,
                status=MissionTaskStatus.queued,
                queue_task_id=queue_id,
                report_path=task_state.report_path,
                worktree_path=task_state.worktree_path,
                attempt_count=task_state.attempt_count,
                failure_reason=None,
                queued_at=_now(),
                started_at=None,
                finished_at=None,
                files_changed=task_state.files_changed,
                test_results=task_state.test_results,
            )
            self._store.update_state(mission_id, state)
            event_log.append_locked(
                make_event(
                    "task_retry_queued",
                    mission_id,
                    task_id=task_id,
                    queue_task_id=queue_id,
                    metadata={
                        "attempt_number": task_state.attempt_count + 1,
                        "max_attempts": task_def.max_attempts,
                        "controller_action_id": recovery_action_id,
                    },
                ),
                event_token,
            )
            return queue_id

    # ------------------------------------------------------------------
    # Internal scheduling helpers
    # ------------------------------------------------------------------

    def _reconcile_tasks(
        self,
        definition: MissionDefinition,
        state: MissionState,
        *,
        report_overrides: dict[str, dict] | None = None,
        durable_events: list[MissionEvent] | None = None,
    ) -> tuple[MissionState, list[MissionEvent]]:
        """Update task states based on current queue and report files.

        For each task that is in ``queued`` or ``running`` status we check:

        * If a report exists and ``status == "succeeded"`` → mark succeeded.
        * If a report exists and ``status == "failed"`` → mark failed.
        * If the task is in the queue's ``running`` dir → keep as running.
        * If the task is in the queue's ``pending`` dir → keep as queued.
        * If the task is missing from all queue dirs and no report → mark failed (lost).
        """
        new_events: list[MissionEvent] = []
        updated: dict[str, MissionTaskState] = dict(state.task_states)

        for tid, ts in list(state.task_states.items()):
            if ts.status not in (MissionTaskStatus.queued, MissionTaskStatus.running):
                continue
            if not ts.queue_task_id:
                continue

            try:
                if (
                    report_overrides is not None
                    and tid in report_overrides
                ):
                    queue_status = (
                        self._materializer.queue_task_exists_in(
                            ts.queue_task_id
                        )
                        or "missing"
                    )
                    info = {
                        "queue_status": queue_status,
                        "classification": "already_materialized",
                        "report": report_overrides[tid],
                    }
                else:
                    info = self._materializer.reconcile_task(
                        state.mission_id,
                        tid,
                        ts.queue_task_id,
                        self._reports_root,
                        mission_state=state,
                        task_state=ts,
                    )
            except Exception as exc:
                logger.error(
                    "reconcile_task error mission=%s task=%s: %s",
                    state.mission_id,
                    tid,
                    exc,
                    exc_info=True,
                )
                continue

            if info is None:
                continue

            queue_status: str = info["queue_status"]
            classification: str = info.get("classification", "eligible_missing")
            report: dict | None = info["report"]

            expected_retry_attempt = self._retry_attempt(
                durable_events or [], tid, ts.queue_task_id
            )
            if (
                report is not None
                and expected_retry_attempt is not None
                and (
                    report.get("task_id") != ts.queue_task_id
                    or report.get("mission_id") != state.mission_id
                    or report.get("mission_task_id") != tid
                    or report.get("mission_attempt")
                    != expected_retry_attempt
                )
            ):
                logger.warning(
                    "ignoring report from another retry attempt mission=%s task=%s",
                    state.mission_id,
                    tid,
                )
                report = None

            if classification in {"paused", "cancelled"}:
                logger.info(
                    "skip reconciliation for paused/cancelled task mission=%s task=%s classification=%s",
                    state.mission_id,
                    tid,
                    classification,
                )
                continue

            if report is not None:
                report_status = report.get("status", "")
                if report_status == "succeeded":
                    report_error = completion_report_error(
                        report,
                        mission_id=state.mission_id,
                        mission_task_id=tid,
                        queue_task_id=ts.queue_task_id,
                        required_tests=(
                            next(
                                (
                                    task.tests
                                    for task in definition.tasks
                                    if task.task_id == tid
                                ),
                                [],
                            )
                        ),
                    )
                    if report_error is not None:
                        logger.warning(
                            "completion report rejected mission=%s task=%s: %s",
                            state.mission_id,
                            tid,
                            report_error,
                        )
                        continue
                    updated[tid] = MissionTaskState(
                        task_id=ts.task_id,
                        status=MissionTaskStatus.succeeded,
                        queue_task_id=ts.queue_task_id,
                        report_path=str(self._reports_root / f"{ts.queue_task_id}.json"),
                        worktree_path=ts.worktree_path,
                        attempt_count=ts.attempt_count + 1,
                        failure_reason=None,
                        queued_at=ts.queued_at,
                        started_at=ts.started_at,
                        finished_at=_now(),
                        files_changed=list(report.get("files_changed", [])),
                        test_results=list(report.get("tests", [])),
                    )
                    new_events.append(
                        make_event(
                            "task_succeeded",
                            state.mission_id,
                            task_id=tid,
                            queue_task_id=ts.queue_task_id,
                            report_path=updated[tid].report_path,
                        )
                    )
                    logger.info(
                        "task succeeded mission=%s task=%s", state.mission_id, tid
                    )
                elif report_status == "failed":
                    updated[tid] = MissionTaskState(
                        task_id=ts.task_id,
                        status=MissionTaskStatus.failed,
                        queue_task_id=ts.queue_task_id,
                        report_path=str(self._reports_root / f"{ts.queue_task_id}.json"),
                        worktree_path=ts.worktree_path,
                        attempt_count=ts.attempt_count + 1,
                        failure_reason=report.get("failure_reason") or "task failed",
                        queued_at=ts.queued_at,
                        started_at=ts.started_at,
                        finished_at=_now(),
                        files_changed=list(report.get("files_changed", [])),
                        test_results=list(report.get("tests", [])),
                    )
                    new_events.append(
                        make_event(
                            "task_failed",
                            state.mission_id,
                            task_id=tid,
                            queue_task_id=ts.queue_task_id,
                            reason=updated[tid].failure_reason,
                        )
                    )
                    logger.info(
                        "task failed mission=%s task=%s reason=%s",
                        state.mission_id,
                        tid,
                        updated[tid].failure_reason,
                    )
                # If report has neither succeeded nor failed, treat as still running.

            elif queue_status == "running":
                # Task is actively being executed — update to running status.
                if ts.status != MissionTaskStatus.running:
                    updated[tid] = MissionTaskState(
                        task_id=ts.task_id,
                        status=MissionTaskStatus.running,
                        queue_task_id=ts.queue_task_id,
                        report_path=ts.report_path,
                        worktree_path=ts.worktree_path,
                        attempt_count=ts.attempt_count,
                        failure_reason=ts.failure_reason,
                        queued_at=ts.queued_at,
                        started_at=ts.started_at or _now(),
                        finished_at=ts.finished_at,
                        files_changed=ts.files_changed,
                        test_results=ts.test_results,
                    )
                    new_events.append(
                        make_event(
                            "task_running",
                            state.mission_id,
                            task_id=tid,
                            queue_task_id=ts.queue_task_id,
                        )
                    )

            elif queue_status == "pending":
                # Task is waiting in the queue — keep as queued.
                pass

            elif queue_status == "missing":
                if classification == "eligible_missing":
                    logger.warning(
                        "task lost from queue without report mission=%s task=%s qid=%s",
                        state.mission_id,
                        tid,
                        ts.queue_task_id,
                    )
                    updated[tid] = MissionTaskState(
                        task_id=ts.task_id,
                        status=MissionTaskStatus.failed,
                        queue_task_id=ts.queue_task_id,
                        report_path=ts.report_path,
                        worktree_path=ts.worktree_path,
                        attempt_count=ts.attempt_count,
                        failure_reason="task disappeared from queue without a report (lost worker?)",
                        queued_at=ts.queued_at,
                        started_at=ts.started_at,
                        finished_at=_now(),
                        files_changed=ts.files_changed,
                        test_results=ts.test_results,
                    )
                    new_events.append(
                        make_event(
                            "task_lost",
                            state.mission_id,
                            task_id=tid,
                            queue_task_id=ts.queue_task_id,
                            reason="missing from queue",
                        )
                    )
                else:
                    logger.info(
                        "reconcile preserved task mission=%s task=%s classification=%s",
                        state.mission_id,
                        tid,
                        classification,
                    )

        new_state = MissionState(
            mission_id=state.mission_id,
            status=state.status,
            task_states=updated,
            budget_usage=state.budget_usage,
            started_at=state.started_at,
            finished_at=state.finished_at,
            paused_at=state.paused_at,
            failure_reason=state.failure_reason,
            root_cause_task_ids=state.root_cause_task_ids,
            events_path=state.events_path,
            report_path=state.report_path,
        )
        return new_state, new_events

    @staticmethod
    def _retry_attempt(
        events: list[MissionEvent], task_id: str, queue_id: str
    ) -> int | None:
        attempts = [
            event.metadata.get("attempt_number")
            for event in events
            if event.event_type == "task_retry_queued"
            and event.task_id == task_id
            and event.queue_task_id == queue_id
            and isinstance(event.metadata.get("attempt_number"), int)
            and not isinstance(event.metadata.get("attempt_number"), bool)
            and event.metadata["attempt_number"] > 0
        ]
        if not attempts:
            return None
        return attempts[-1]

    def _compute_ready_tasks(
        self,
        definition: MissionDefinition,
        state: MissionState,
        durable_events: list[MissionEvent] | None = None,
    ) -> list[str]:
        """Return task IDs that are ready to be enqueued.

        Tasks requiring approval are left in the ``approval_required`` status
        and excluded from the returned list.
        """
        graph = DependencyGraph(definition, state)
        ready: list[str] = []
        for tid in graph.ready_tasks():
            task_def = _task_def_by_id(definition, tid)
            if task_def is None:
                continue
            if durable_events is not None and any(
                not any(
                    self._is_exact_prerequisite_success(
                        event,
                        definition.mission_id,
                        dependency,
                        state.task_states[dependency],
                    )
                    for event in durable_events
                )
                for dependency in task_def.depends_on
            ):
                continue
            if task_def.approval_policy in (
                ApprovalPolicy.approval_required_before_queue,
                ApprovalPolicy.approval_required_before_execution,
                ApprovalPolicy.forbidden,
            ):
                # Mark as approval_required if not already.
                ts = state.task_states.get(tid)
                if ts is None or ts.status not in (
                    MissionTaskStatus.approval_required,
                    *_TERMINAL_TASK_STATUSES,
                ):
                    state.task_states[tid] = MissionTaskState(
                        task_id=tid,
                        status=MissionTaskStatus.approval_required,
                        queue_task_id=ts.queue_task_id if ts else None,
                        report_path=ts.report_path if ts else None,
                        worktree_path=ts.worktree_path if ts else None,
                        attempt_count=ts.attempt_count if ts else 0,
                        failure_reason=ts.failure_reason if ts else None,
                        queued_at=ts.queued_at if ts else None,
                        started_at=ts.started_at if ts else None,
                        finished_at=ts.finished_at if ts else None,
                        files_changed=ts.files_changed if ts else [],
                        test_results=ts.test_results if ts else [],
                    )
                continue
            # Exclude tasks already queued/running/terminal.
            ts = state.task_states.get(tid)
            if ts is not None and ts.status not in (
                MissionTaskStatus.pending,
                MissionTaskStatus.ready,
            ):
                continue
            ready.append(tid)
        return ready

    def _is_exact_prerequisite_success(
        self,
        event: MissionEvent,
        mission_id: str,
        mission_task_id: str,
        task_state: MissionTaskState,
    ) -> bool:
        accepted_queue_ids = {
            self._materializer.make_queue_task_id(
                mission_id, mission_task_id
            ),
            make_legacy_queue_task_id(mission_id, mission_task_id),
        }
        metadata_identities = {
            "mission_id": mission_id,
            "mission_task_id": mission_task_id,
            "queue_task_id": event.queue_task_id,
        }
        return (
            event.event_type == "task_succeeded"
            and event.mission_id == mission_id
            and event.task_id == mission_task_id
            and isinstance(event.queue_task_id, str)
            and bool(event.queue_task_id)
            and event.queue_task_id in accepted_queue_ids
            and task_state.status == MissionTaskStatus.succeeded
            and task_state.queue_task_id == event.queue_task_id
            and all(
                key not in event.metadata
                or event.metadata.get(key) == expected
                for key, expected in metadata_identities.items()
            )
        )

    def _check_budgets(
        self,
        definition: MissionDefinition,
        state: MissionState,
    ) -> list[str]:
        """Return names of budget constraints that are exceeded."""
        bu = state.budget_usage
        limits = definition.budgets
        exceeded: list[str] = []
        if bu.active_tasks > limits.max_active_tasks:
            exceeded.append("max_active_tasks")
        if bu.queued_tasks > limits.max_queued_tasks:
            exceeded.append("max_queued_tasks")
        if bu.total_attempts >= limits.max_total_attempts:
            exceeded.append("max_total_attempts")
        if bu.failed_tasks >= limits.max_failed_tasks:
            exceeded.append("max_failed_tasks")
        if bu.elapsed_seconds >= limits.max_runtime_seconds:
            exceeded.append("max_runtime_seconds")
        if bu.worktrees > limits.max_worktrees:
            exceeded.append("max_worktrees")
        return exceeded

    def _mark_blocked_tasks(
        self,
        definition: MissionDefinition,
        state: MissionState,
    ) -> tuple[MissionState, list[MissionEvent]]:
        """Mark tasks as blocked when their dependencies failed with a blocking policy."""
        graph = DependencyGraph(definition, state)
        new_events: list[MissionEvent] = []
        updated = dict(state.task_states)

        for tid in graph.blocked_tasks():
            ts = state.task_states.get(tid)
            if ts is not None and ts.status == MissionTaskStatus.blocked:
                continue  # already marked
            updated[tid] = MissionTaskState(
                task_id=tid,
                status=MissionTaskStatus.blocked,
                queue_task_id=ts.queue_task_id if ts else None,
                report_path=ts.report_path if ts else None,
                worktree_path=ts.worktree_path if ts else None,
                attempt_count=ts.attempt_count if ts else 0,
                failure_reason="dependency failed",
                queued_at=ts.queued_at if ts else None,
                started_at=ts.started_at if ts else None,
                finished_at=ts.finished_at or _now() if ts else _now(),
                files_changed=ts.files_changed if ts else [],
                test_results=ts.test_results if ts else [],
            )
            new_events.append(
                make_event(
                    "task_blocked",
                    state.mission_id,
                    task_id=tid,
                    reason="dependency failed",
                )
            )
            logger.info(
                "task blocked mission=%s task=%s", state.mission_id, tid
            )

        new_state = MissionState(
            mission_id=state.mission_id,
            status=state.status,
            task_states=updated,
            budget_usage=state.budget_usage,
            started_at=state.started_at,
            finished_at=state.finished_at,
            paused_at=state.paused_at,
            failure_reason=state.failure_reason,
            root_cause_task_ids=state.root_cause_task_ids,
            events_path=state.events_path,
            report_path=state.report_path,
        )
        return new_state, new_events

    def _check_mission_terminal(
        self,
        definition: MissionDefinition,
        state: MissionState,
    ) -> MissionStatus | None:
        """Return a terminal ``MissionStatus`` if the mission should end, else ``None``.

        Priority order:
        1. Budget exhausted (checked first so we stop before making things worse).
        2. Hard failure (a task with stop_mission policy failed).
        3. Natural completion (all tasks in terminal states).
        """
        # 1. Budget
        exceeded = self._check_budgets(definition, state)
        if exceeded:
            logger.warning(
                "mission %s budget exhausted: %s", state.mission_id, exceeded
            )
            return MissionStatus.budget_exhausted

        graph = DependencyGraph(definition, state)

        # 2. Stop-mission failure.
        if graph.is_mission_failed():
            return MissionStatus.failed

        # 3. Natural completion — every task is in a terminal state and the
        #    graph considers the mission done.
        if graph.is_mission_complete():
            # Determine whether we succeeded or partially failed.
            failed = graph.failed_tasks()
            if failed:
                return MissionStatus.failed
            return MissionStatus.succeeded

        # Check if all tasks are in a terminal state even if graph doesn't
        # report complete (e.g. blocked + cancelled tasks remain).
        all_task_ids = {t.task_id for t in definition.tasks}
        all_terminal = all(
            state.task_states.get(tid, MissionTaskState(task_id=tid, status=MissionTaskStatus.pending)).status
            in _TERMINAL_TASK_STATUSES
            for tid in all_task_ids
        )
        if all_terminal and all_task_ids:
            failed = [
                tid
                for tid in all_task_ids
                if state.task_states.get(
                    tid, MissionTaskState(task_id=tid, status=MissionTaskStatus.pending)
                ).status == MissionTaskStatus.failed
            ]
            if failed:
                return MissionStatus.failed
            return MissionStatus.succeeded

        return None

    def _update_budget_usage(
        self,
        definition: MissionDefinition,
        state: MissionState,
    ) -> MissionState:
        """Recompute BudgetUsage from current task states."""
        active = sum(
            1
            for ts in state.task_states.values()
            if ts.status == MissionTaskStatus.running
        )
        queued = sum(
            1
            for ts in state.task_states.values()
            if ts.status == MissionTaskStatus.queued
        )
        failed = sum(
            1
            for ts in state.task_states.values()
            if ts.status == MissionTaskStatus.failed
        )
        total_attempts = sum(
            ts.attempt_count for ts in state.task_states.values()
        )
        elapsed: float = 0.0
        if state.started_at:
            try:
                start_dt = datetime.fromisoformat(state.started_at)
                elapsed = (datetime.now(timezone.utc) - start_dt).total_seconds()
            except (ValueError, TypeError):
                pass

        new_usage = BudgetUsage(
            active_tasks=active,
            queued_tasks=queued,
            total_attempts=total_attempts,
            failed_tasks=failed,
            elapsed_seconds=elapsed,
            worktrees=state.budget_usage.worktrees,
        )
        return MissionState(
            mission_id=state.mission_id,
            status=state.status,
            task_states=state.task_states,
            budget_usage=new_usage,
            started_at=state.started_at,
            finished_at=state.finished_at,
            paused_at=state.paused_at,
            failure_reason=state.failure_reason,
            root_cause_task_ids=state.root_cause_task_ids,
            events_path=state.events_path,
            report_path=state.report_path,
        )


# ---------------------------------------------------------------------------
# Module-level utility
# ---------------------------------------------------------------------------

def _task_def_by_id(
    definition: MissionDefinition, task_id: str
) -> object | None:
    """Return the MissionTaskDefinition for *task_id*, or None."""
    for t in definition.tasks:
        if t.task_id == task_id:
            return t
    return None
