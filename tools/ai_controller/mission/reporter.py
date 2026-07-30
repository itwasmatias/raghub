"""
Mission report generator.

Pure logic — no I/O, no filesystem access.  Consumes a MissionDefinition,
MissionState, and MissionEventLog to produce a fully self-contained dict
that can be serialised to JSON and stored or displayed.
"""

from __future__ import annotations

import datetime
from typing import Any

from .models import (
    MissionDefinition,
    MissionState,
    MissionStatus,
    MissionTaskStatus,
)
from .graph import DependencyGraph
from .events import MissionEventLog


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _count_tasks_by_status(
    state: MissionState,
    definition: MissionDefinition,
) -> dict[str, int]:
    """Return a counter dict keyed by status name for all defined tasks."""
    counts: dict[str, int] = {
        "total": 0,
        "succeeded": 0,
        "failed": 0,
        "running": 0,
        "queued": 0,
        "blocked": 0,
        "cancelled": 0,
        "pending": 0,
        "approval_required": 0,
    }
    for task_def in definition.tasks:
        counts["total"] += 1
        ts = state.task_states.get(task_def.task_id)
        if ts is None:
            counts["pending"] += 1
        elif ts.status == MissionTaskStatus.succeeded:
            counts["succeeded"] += 1
        elif ts.status == MissionTaskStatus.failed:
            counts["failed"] += 1
        elif ts.status == MissionTaskStatus.running:
            counts["running"] += 1
        elif ts.status == MissionTaskStatus.queued:
            counts["queued"] += 1
        elif ts.status == MissionTaskStatus.blocked:
            counts["blocked"] += 1
        elif ts.status == MissionTaskStatus.cancelled:
            counts["cancelled"] += 1
        elif ts.status == MissionTaskStatus.pending:
            counts["pending"] += 1
        elif ts.status == MissionTaskStatus.approval_required:
            counts["approval_required"] += 1
        else:
            # ready and any future statuses default to pending bucket
            counts["pending"] += 1
    return counts


def _elapsed_seconds(
    started_at: str | None,
    finished_at: str | None,
) -> float | None:
    """Return wall-clock elapsed seconds between two ISO-8601 strings, or None."""
    if started_at is None:
        return None
    try:
        start = datetime.datetime.fromisoformat(started_at.rstrip("Z"))
    except ValueError:
        return None
    if finished_at is not None:
        try:
            end = datetime.datetime.fromisoformat(finished_at.rstrip("Z"))
        except ValueError:
            end = datetime.datetime.utcnow()
    else:
        end = datetime.datetime.utcnow()
    delta = (end - start).total_seconds()
    return max(delta, 0.0)


def _determine_outcome(
    definition: MissionDefinition,
    state: MissionState,
    counts: dict[str, int],
    root_cause_task_ids: list[str],
) -> str:
    """
    Derive a high-level outcome string from the mission state and task counts.

    Priority order (first match wins):
    1. All tasks succeeded → "succeeded"
    2. Mission status is budget_exhausted → "budget_exhausted"
    3. Mission status is cancelled → "cancelled"
    4. Mission status is paused → "paused"
    5. Mission status is failed and root_cause exists → "failed"
    6. Any approval_required tasks → "blocked_by_approval"
    7. Any blocked tasks → "blocked_by_dependency"
    8. Any failed tasks but others succeeded/continuing → "completed_with_failures"
    9. Fallback → mission_status.value
    """
    total = counts["total"]
    if total > 0 and counts["succeeded"] == total:
        return "succeeded"

    if state.status == MissionStatus.budget_exhausted:
        return "budget_exhausted"

    if state.status == MissionStatus.cancelled:
        return "cancelled"

    if state.status == MissionStatus.paused:
        return "paused"

    if state.status == MissionStatus.failed and root_cause_task_ids:
        return "failed"

    if counts["approval_required"] > 0:
        return "blocked_by_approval"

    if counts["blocked"] > 0:
        return "blocked_by_dependency"

    if counts["failed"] > 0 and (counts["succeeded"] > 0 or counts["running"] > 0 or counts["queued"] > 0):
        return "completed_with_failures"

    return state.status.value


def _build_recommended_action(
    outcome: str,
    mission_id: str,
    root_cause_task_ids: list[str],
    approval_required_task_ids: list[str],
) -> str:
    """Return a human-readable next-step string for the given outcome."""
    if outcome == "succeeded":
        return "Mission complete. No action required."
    if outcome == "failed":
        ids = ", ".join(root_cause_task_ids) if root_cause_task_ids else "unknown"
        return f"Review failed tasks: {ids}. Fix root causes and restart."
    if outcome == "cancelled":
        return "Mission was cancelled."
    if outcome == "budget_exhausted":
        return "Budget limit reached. Review budget settings and restart."
    if outcome == "blocked_by_approval":
        ids = ", ".join(approval_required_task_ids) if approval_required_task_ids else "unknown"
        return f"Approval required for tasks: {ids}. Approve and resume."
    if outcome == "blocked_by_dependency":
        return "Some tasks are blocked. Review failed dependencies."
    if outcome == "paused":
        return f"Mission is paused. Run 'mission resume {mission_id}' to continue."
    if outcome == "running":
        return "Mission is running. Continue controller execution."
    return "Review mission status."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_mission_report(
    definition: MissionDefinition,
    state: MissionState,
    event_log: MissionEventLog,
) -> dict[str, Any]:
    """
    Generate a complete mission report dict.

    Parameters
    ----------
    definition:
        The static mission specification.
    state:
        The live execution state snapshot.
    event_log:
        The durable event log for this mission.

    Returns
    -------
    dict
        Fully self-contained report suitable for JSON serialisation.
    """
    graph = DependencyGraph(definition, state)

    # --- task counts ---------------------------------------------------------
    counts = _count_tasks_by_status(state, definition)

    # --- dependency statuses per task ----------------------------------------
    dependency_states: dict[str, list[str]] = {}
    for task_def in definition.tasks:
        dep_statuses: list[str] = []
        for dep_id in task_def.depends_on:
            dep_ts = state.task_states.get(dep_id)
            dep_status = dep_ts.status.value if dep_ts is not None else MissionTaskStatus.pending.value
            dep_statuses.append(dep_status)
        dependency_states[task_def.task_id] = dep_statuses

    # --- root cause and approval_required task IDs ---------------------------
    root_cause_task_ids: list[str] = list(state.root_cause_task_ids)

    approval_required_task_ids: list[str] = sorted(
        ts.task_id
        for ts in state.task_states.values()
        if ts.status == MissionTaskStatus.approval_required
    )

    # --- timing --------------------------------------------------------------
    elapsed = _elapsed_seconds(state.started_at, state.finished_at)

    # --- outcome and recommended action --------------------------------------
    outcome = _determine_outcome(definition, state, counts, root_cause_task_ids)
    recommended_action = _build_recommended_action(
        outcome,
        definition.mission_id,
        root_cause_task_ids,
        approval_required_task_ids,
    )

    # --- events (last 50) ----------------------------------------------------
    all_events = event_log.read_all()
    recent_events = all_events[-50:]

    # --- assemble report -----------------------------------------------------
    return {
        "mission_id": definition.mission_id,
        "title": definition.title,
        "description": definition.description,
        "mission_status": state.status.value,
        "outcome": outcome,
        "started_at": state.started_at,
        "finished_at": state.finished_at,
        "elapsed_seconds": elapsed,
        "base_ref": definition.base_ref,
        "repository_path": definition.repository_path,
        "task_graph": graph.render_text(),
        "task_summary": counts,
        "task_states": [ts.to_dict() for ts in state.task_states.values()],
        "dependency_states": dependency_states,
        "budget_limits": definition.budgets.to_dict(),
        "budget_usage": state.budget_usage.to_dict(),
        "root_cause_task_ids": root_cause_task_ids,
        "failure_reason": state.failure_reason,
        "events": [e.to_dict() for e in recent_events],
        "events_path": state.events_path,
        "report_path": state.report_path,
        "recommended_action": recommended_action,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
