"""
Mission definition validation.

Pure logic — no I/O, no filesystem access.  All errors are collected and
returned together so the caller can surface every problem in a single pass.
"""

from __future__ import annotations

import re
from collections import deque
from typing import TYPE_CHECKING

from .models import (
    ApprovalPolicy,
    MissionDefinition,
    ValidationError,
    ValidationResult,
)

# Regex shared with the controller queue task-ID contract.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _valid_id(value: str) -> bool:
    return bool(_ID_RE.match(value)) and ".." not in value


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_cycle(task_ids: list[str], deps: dict[str, list[str]]) -> list[list[str]]:
    """
    Return a list of cycle descriptions (each a list of task_ids that form a
    cycle) using Kahn's topological sort algorithm.

    A non-empty return value means at least one cycle exists.  The returned
    lists identify the nodes involved; they are *not* guaranteed to be minimal
    simple cycles but are sufficient for error reporting.
    """
    in_degree: dict[str, int] = {tid: 0 for tid in task_ids}
    for tid in task_ids:
        for dep in deps.get(tid, []):
            if dep in in_degree:
                in_degree[tid] += 1

    queue: deque[str] = deque(sorted(t for t, d in in_degree.items() if d == 0))
    visited_count = 0

    while queue:
        node = queue.popleft()
        visited_count += 1
        # Find tasks that depend *on* this node (i.e. node is in their deps).
        for tid in sorted(task_ids):
            if node in deps.get(tid, []):
                in_degree[tid] -= 1
                if in_degree[tid] == 0:
                    queue.append(tid)

    if visited_count < len(task_ids):
        # Nodes still with in_degree > 0 are part of a cycle.
        cycle_nodes = sorted(t for t, d in in_degree.items() if d > 0)
        return [cycle_nodes]
    return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_mission(definition: MissionDefinition) -> ValidationResult:
    """
    Validate a MissionDefinition exhaustively.

    All discovered problems are collected before returning; the caller
    receives the complete list rather than just the first error.
    """
    errors: list[ValidationError] = []
    mid = definition.mission_id or "<unknown>"

    def err(
        code: str,
        message: str,
        task_id: str | None = None,
        field: str | None = None,
    ) -> None:
        errors.append(
            ValidationError(
                code=code,
                message=message,
                mission_id=mid,
                task_id=task_id,
                field=field,
            )
        )

    # ------------------------------------------------------------------
    # Top-level mission fields
    # ------------------------------------------------------------------

    if not definition.mission_id:
        err("MISSION_ID_EMPTY", "mission_id must not be empty.", field="mission_id")
    elif not _valid_id(definition.mission_id):
        err(
            "MISSION_ID_INVALID",
            f"mission_id {definition.mission_id!r} does not match "
            r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ or contains '..'.",
            field="mission_id",
        )

    if definition.schema_version != "1":
        err(
            "SCHEMA_VERSION_UNSUPPORTED",
            f"schema_version must be '1', got {definition.schema_version!r}.",
            field="schema_version",
        )

    if not definition.title or not definition.title.strip():
        err("MISSION_TITLE_EMPTY", "Mission title must not be empty.", field="title")

    # ------------------------------------------------------------------
    # Task uniqueness
    # ------------------------------------------------------------------

    seen_task_ids: set[str] = set()
    duplicate_task_ids: set[str] = set()
    for task in definition.tasks:
        if task.task_id in seen_task_ids:
            duplicate_task_ids.add(task.task_id)
        seen_task_ids.add(task.task_id)

    for dup in sorted(duplicate_task_ids):
        err(
            "TASK_ID_DUPLICATE",
            f"task_id {dup!r} appears more than once in the mission.",
            task_id=dup,
            field="task_id",
        )

    all_task_ids = seen_task_ids  # alias for clarity

    # ------------------------------------------------------------------
    # Per-task validation
    # ------------------------------------------------------------------

    dep_map: dict[str, list[str]] = {}

    for task in definition.tasks:
        tid = task.task_id or "<unknown>"

        # task_id format
        if not task.task_id:
            err("TASK_ID_EMPTY", "task_id must not be empty.", task_id=tid, field="task_id")
        elif not _valid_id(task.task_id):
            err(
                "TASK_ID_INVALID",
                f"task_id {task.task_id!r} does not match the allowed pattern or contains '..'.",
                task_id=tid,
                field="task_id",
            )

        # title
        if not task.title or not task.title.strip():
            err("TASK_TITLE_EMPTY", "Task title must not be empty.", task_id=tid, field="title")

        # prompt
        if not task.prompt or not task.prompt.strip():
            err("TASK_PROMPT_EMPTY", "Task prompt must not be empty.", task_id=tid, field="prompt")

        # base_ref
        if not task.base_ref or not task.base_ref.strip():
            err("TASK_BASE_REF_EMPTY", "base_ref must not be empty.", task_id=tid, field="base_ref")

        # max_attempts
        if task.max_attempts < 1:
            err(
                "TASK_MAX_ATTEMPTS_INVALID",
                f"max_attempts must be >= 1, got {task.max_attempts}.",
                task_id=tid,
                field="max_attempts",
            )

        # timeout_seconds
        if task.timeout_seconds <= 0:
            err(
                "TASK_TIMEOUT_INVALID",
                f"timeout_seconds must be > 0, got {task.timeout_seconds}.",
                task_id=tid,
                field="timeout_seconds",
            )

        # tests — the top-level container must be a list of argv lists
        if not isinstance(task.tests, list):
            err(
                "TASK_TESTS_NOT_LIST",
                "tests must be a list of argument lists.",
                task_id=tid,
                field="tests",
            )
            continue

        # depends_on — reference validity
        valid_deps: list[str] = []
        for dep in task.depends_on:
            if dep == task.task_id:
                err(
                    "TASK_SELF_DEPENDENCY",
                    f"Task {tid!r} lists itself in depends_on.",
                    task_id=tid,
                    field="depends_on",
                )
            elif dep not in all_task_ids:
                err(
                    "TASK_DEPENDENCY_UNKNOWN",
                    f"Task {tid!r} depends on {dep!r} which is not defined in the mission.",
                    task_id=tid,
                    field="depends_on",
                )
            else:
                valid_deps.append(dep)
        dep_map[tid] = valid_deps

        # tests — each entry must be a list of non-empty strings
        for idx, test_cmd in enumerate(task.tests):
            if not isinstance(test_cmd, list):
                err(
                    "TASK_TEST_NOT_LIST",
                    f"tests[{idx}] must be a list of strings.",
                    task_id=tid,
                    field=f"tests[{idx}]",
                )
                continue
            for sidx, part in enumerate(test_cmd):
                if not isinstance(part, str) or not part:
                    err(
                        "TASK_TEST_ELEMENT_EMPTY",
                        f"tests[{idx}][{sidx}] must be a non-empty string.",
                        task_id=tid,
                        field=f"tests[{idx}][{sidx}]",
                    )

        # path conflict: required ∩ forbidden must be empty
        required_set  = set(task.required_changed_paths)
        forbidden_set = set(task.forbidden_changed_paths)
        conflict = sorted(required_set & forbidden_set)
        if conflict:
            err(
                "TASK_PATH_CONFLICT",
                f"Paths appear in both required_changed_paths and "
                f"forbidden_changed_paths: {conflict}.",
                task_id=tid,
                field="required_changed_paths",
            )

        # approval_policy "forbidden" requires metadata["approved_by"]
        if task.approval_policy == ApprovalPolicy.forbidden:
            approved_by = task.metadata.get("approved_by")
            if not approved_by:
                err(
                    "TASK_APPROVAL_FORBIDDEN_NO_ACK",
                    f"Task {tid!r} has approval_policy='forbidden' but "
                    "metadata['approved_by'] is missing or empty.",
                    task_id=tid,
                    field="approval_policy",
                )

    # ------------------------------------------------------------------
    # Cycle detection (only for tasks with valid IDs)
    # ------------------------------------------------------------------

    valid_task_ids = [t.task_id for t in definition.tasks if _valid_id(t.task_id)]
    cycles = _check_cycle(valid_task_ids, dep_map)
    for cycle_nodes in cycles:
        err(
            "TASK_DEPENDENCY_CYCLE",
            f"Dependency cycle detected among tasks: {cycle_nodes}.",
            field="depends_on",
        )

    # ------------------------------------------------------------------
    # Milestone validation
    # ------------------------------------------------------------------

    for milestone in definition.milestones:
        for tid in milestone.task_ids:
            if tid not in all_task_ids:
                errors.append(
                    ValidationError(
                        code="MILESTONE_TASK_UNKNOWN",
                        message=(
                            f"Milestone {milestone.milestone_id!r} references task "
                            f"{tid!r} which is not defined in the mission."
                        ),
                        mission_id=mid,
                        task_id=tid,
                        field="task_ids",
                    )
                )

    # ------------------------------------------------------------------
    # Budget validation
    # ------------------------------------------------------------------

    budgets = definition.budgets

    if budgets.max_active_tasks <= 0:
        err(
            "BUDGET_MAX_ACTIVE_TASKS_INVALID",
            f"budgets.max_active_tasks must be > 0, got {budgets.max_active_tasks}.",
            field="budgets.max_active_tasks",
        )
    if budgets.max_queued_tasks <= 0:
        err(
            "BUDGET_MAX_QUEUED_TASKS_INVALID",
            f"budgets.max_queued_tasks must be > 0, got {budgets.max_queued_tasks}.",
            field="budgets.max_queued_tasks",
        )
    if budgets.max_total_attempts <= 0:
        err(
            "BUDGET_MAX_TOTAL_ATTEMPTS_INVALID",
            f"budgets.max_total_attempts must be > 0, got {budgets.max_total_attempts}.",
            field="budgets.max_total_attempts",
        )
    if budgets.max_failed_tasks <= 0:
        err(
            "BUDGET_MAX_FAILED_TASKS_INVALID",
            f"budgets.max_failed_tasks must be > 0, got {budgets.max_failed_tasks}.",
            field="budgets.max_failed_tasks",
        )
    if budgets.max_runtime_seconds <= 0:
        err(
            "BUDGET_MAX_RUNTIME_INVALID",
            f"budgets.max_runtime_seconds must be > 0, got {budgets.max_runtime_seconds}.",
            field="budgets.max_runtime_seconds",
        )
    if budgets.max_worktrees <= 0:
        err(
            "BUDGET_MAX_WORKTREES_INVALID",
            f"budgets.max_worktrees must be > 0, got {budgets.max_worktrees}.",
            field="budgets.max_worktrees",
        )

    return ValidationResult(valid=len(errors) == 0, errors=errors)
