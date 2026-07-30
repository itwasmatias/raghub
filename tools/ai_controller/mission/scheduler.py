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

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from tools.ai_controller.queue import DurableQueue
from .events import MissionEventLog, make_event
from .graph import DependencyGraph
from .materializer import TaskMaterializer
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
    ) -> None:
        self._store = store
        self._queue = queue
        self._reports_root = Path(reports_root)
        self._missions_root = Path(missions_root)
        self._poll_interval = poll_interval_seconds
        self._materializer = TaskMaterializer(queue, reports_root)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run_once(self, mission_id: str) -> MissionStatus:
        """Execute one full scheduling cycle and return the resulting status."""
        definition = self._store.load_definition(mission_id)
        state = self._store.load_state(mission_id)

        event_log = MissionEventLog(
            self._missions_root / "events", mission_id
        )
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
                event_log.append(ev)
            return state.status

        # --- Step 2-3: reconcile task states ---
        state, reconcile_events = self._reconcile_tasks(definition, state)
        new_events.extend(reconcile_events)

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
                event_log.append(ev)
            logger.info(
                "mission reached terminal state mission_id=%s status=%s",
                mission_id,
                terminal_status.value,
            )
            return terminal_status

        # --- Step 6: find ready tasks ---
        ready_task_ids = self._compute_ready_tasks(definition, state)

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
            event_log.append(ev)

        return state.status

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

    # ------------------------------------------------------------------
    # Internal scheduling helpers
    # ------------------------------------------------------------------

    def _reconcile_tasks(
        self,
        definition: MissionDefinition,
        state: MissionState,
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
                info = self._materializer.reconcile_task(
                    state.mission_id, tid, ts.queue_task_id, self._reports_root
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
            report: dict | None = info["report"]

            if report is not None:
                report_status = report.get("status", "")
                if report_status == "succeeded":
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
                # Task vanished from queue without a report — mark as lost/failed.
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

    def _compute_ready_tasks(
        self,
        definition: MissionDefinition,
        state: MissionState,
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
