"""
Dependency graph computation for mission orchestration.

Pure logic — no I/O, no filesystem access.  Every public method is
deterministic: wherever ordering is ambiguous the implementation sorts
by task_id before processing.
"""

from __future__ import annotations

from collections import deque

from .models import (
    FailurePolicy,
    MissionDefinition,
    MissionState,
    MissionTaskStatus,
)

# Statuses that mean "the task is no longer pending / will never run".
_TERMINAL_STATUSES = {
    MissionTaskStatus.succeeded,
    MissionTaskStatus.failed,
    MissionTaskStatus.cancelled,
    MissionTaskStatus.blocked,
}

# Statuses that mean the task is occupying queue / worker capacity.
_ACTIVE_STATUSES = {
    MissionTaskStatus.queued,
    MissionTaskStatus.running,
}


class DependencyGraph:
    """
    Computes derived graph properties from a MissionDefinition and the
    current MissionState.

    Parameters
    ----------
    definition:
        The static description of the mission (tasks, deps, policies …).
    state:
        The live execution state.  Task states that are absent from
        ``state.task_states`` are treated as *pending*.
    """

    def __init__(self, definition: MissionDefinition, state: MissionState) -> None:
        self._definition = definition
        self._state = state

        # Build indexed lookups from the definition.
        self._task_defs: dict[str, object] = {
            t.task_id: t for t in definition.tasks
        }

        # deps[task_id] = sorted list of direct dependencies (tasks it needs).
        self._deps: dict[str, list[str]] = {
            t.task_id: sorted(t.depends_on)
            for t in definition.tasks
        }

        # rdeps[task_id] = sorted list of direct dependents (tasks that need it).
        self._rdeps: dict[str, list[str]] = {t.task_id: [] for t in definition.tasks}
        for t in definition.tasks:
            for dep in t.depends_on:
                if dep in self._rdeps:
                    self._rdeps[dep].append(t.task_id)
        for tid in self._rdeps:
            self._rdeps[tid].sort()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _status(self, task_id: str) -> MissionTaskStatus:
        ts = self._state.task_states.get(task_id)
        if ts is None:
            return MissionTaskStatus.pending
        return ts.status

    def _failure_policy(self, task_id: str) -> FailurePolicy:
        td = self._task_defs.get(task_id)
        if td is None:
            return FailurePolicy.block_dependents
        return td.failure_policy  # type: ignore[attr-defined]

    def _all_deps_succeeded(self, task_id: str) -> bool:
        return all(
            self._status(dep) == MissionTaskStatus.succeeded
            for dep in self._deps.get(task_id, [])
        )

    def _has_failed_blocker(self, task_id: str) -> bool:
        """
        Return True if at least one dependency has failed with a policy that
        blocks dependents (anything except continue_independent).
        """
        for dep in self._deps.get(task_id, []):
            if self._status(dep) == MissionTaskStatus.failed:
                policy = self._failure_policy(dep)
                if policy != FailurePolicy.continue_independent:
                    return True
        return False

    # ------------------------------------------------------------------
    # Public query methods
    # ------------------------------------------------------------------

    def ready_tasks(self) -> list[str]:
        """
        Tasks whose status is pending or ready *and* whose every dependency
        has succeeded.
        """
        result: list[str] = []
        for tid in sorted(self._task_defs):
            status = self._status(tid)
            if status not in (MissionTaskStatus.pending, MissionTaskStatus.ready):
                continue
            if self._all_deps_succeeded(tid):
                result.append(tid)
        return result

    def blocked_tasks(self) -> list[str]:
        """
        Tasks that have at least one failed required dependency (i.e. one with
        a policy other than continue_independent).
        """
        result: list[str] = []
        for tid in sorted(self._task_defs):
            status = self._status(tid)
            if status in _TERMINAL_STATUSES or status in _ACTIVE_STATUSES:
                continue
            if self._has_failed_blocker(tid):
                result.append(tid)
        return result

    def running_tasks(self) -> list[str]:
        """Tasks that are currently queued or running."""
        return sorted(
            tid
            for tid in self._task_defs
            if self._status(tid) in _ACTIVE_STATUSES
        )

    def completed_tasks(self) -> list[str]:
        """Tasks that have succeeded."""
        return sorted(
            tid
            for tid in self._task_defs
            if self._status(tid) == MissionTaskStatus.succeeded
        )

    def failed_tasks(self) -> list[str]:
        """Tasks that have failed (regardless of failure policy)."""
        return sorted(
            tid
            for tid in self._task_defs
            if self._status(tid) == MissionTaskStatus.failed
        )

    def cancelled_tasks(self) -> list[str]:
        """Tasks that were cancelled."""
        return sorted(
            tid
            for tid in self._task_defs
            if self._status(tid) == MissionTaskStatus.cancelled
        )

    def topological_order(self) -> list[str]:
        """
        Return all tasks in a valid topological order via Kahn's algorithm.

        Ties (zero in-degree at the same time) are broken by sorting task_ids
        lexicographically, making the result deterministic.
        """
        all_ids = sorted(self._task_defs.keys())
        in_degree: dict[str, int] = {tid: 0 for tid in all_ids}
        for tid in all_ids:
            for dep in self._deps.get(tid, []):
                if dep in in_degree:
                    in_degree[tid] += 1

        queue: deque[str] = deque(
            sorted(tid for tid, deg in in_degree.items() if deg == 0)
        )
        order: list[str] = []

        while queue:
            node = queue.popleft()
            order.append(node)
            # Discover successors (tasks that depend on *node*).
            newly_zero: list[str] = []
            for successor in self._rdeps.get(node, []):
                in_degree[successor] -= 1
                if in_degree[successor] == 0:
                    newly_zero.append(successor)
            queue.extend(sorted(newly_zero))

        return order

    def upstream(self, task_id: str) -> list[str]:
        """
        All direct *and* transitive dependencies of *task_id* (i.e. tasks
        that must complete before *task_id* can run).

        The result is sorted by task_id and does **not** include *task_id*
        itself.
        """
        visited: set[str] = set()
        queue: deque[str] = deque(self._deps.get(task_id, []))
        while queue:
            dep = queue.popleft()
            if dep in visited:
                continue
            visited.add(dep)
            queue.extend(self._deps.get(dep, []))
        return sorted(visited)

    def downstream(self, task_id: str) -> list[str]:
        """
        All direct *and* transitive dependents of *task_id* (i.e. tasks that
        cannot run until *task_id* completes).

        The result is sorted by task_id and does **not** include *task_id*
        itself.
        """
        visited: set[str] = set()
        queue: deque[str] = deque(self._rdeps.get(task_id, []))
        while queue:
            dep = queue.popleft()
            if dep in visited:
                continue
            visited.add(dep)
            queue.extend(self._rdeps.get(dep, []))
        return sorted(visited)

    def is_mission_complete(self) -> bool:
        """
        Return True when every task has either succeeded or failed with the
        ``continue_independent`` policy (meaning its failure does not block
        the mission from being considered done).
        """
        for tid in self._task_defs:
            status = self._status(tid)
            if status == MissionTaskStatus.succeeded:
                continue
            if status == MissionTaskStatus.failed:
                if self._failure_policy(tid) == FailurePolicy.continue_independent:
                    continue
                return False
            # Any non-terminal status means we are not done.
            return False
        return True

    def is_mission_failed(self) -> bool:
        """
        Return True when at least one task has failed with a
        ``stop_mission`` failure policy.
        """
        return any(
            self._status(tid) == MissionTaskStatus.failed
            and self._failure_policy(tid) == FailurePolicy.stop_mission
            for tid in self._task_defs
        )

    def render_text(self) -> str:
        """
        Return a human-readable dependency tree.

        Root tasks (no dependencies) are listed first in sorted order; each
        task's direct dependents are indented beneath it.

        Example output::

            [done] task-a
              └── [running] task-b
                    ├── [blocked] task-c
                    └── [blocked] task-d
        """
        _STATUS_LABEL: dict[MissionTaskStatus, str] = {
            MissionTaskStatus.pending:           "pending",
            MissionTaskStatus.ready:             "ready",
            MissionTaskStatus.queued:            "queued",
            MissionTaskStatus.running:           "running",
            MissionTaskStatus.succeeded:         "done",
            MissionTaskStatus.failed:            "failed",
            MissionTaskStatus.blocked:           "blocked",
            MissionTaskStatus.cancelled:         "cancelled",
            MissionTaskStatus.approval_required: "approval",
        }

        def label(tid: str) -> str:
            return f"[{_STATUS_LABEL.get(self._status(tid), '?')}] {tid}"

        # Root tasks: those with no dependencies (or all deps are external).
        roots = sorted(
            tid for tid in self._task_defs if not self._deps.get(tid)
        )

        lines: list[str] = []

        def _render(tid: str, prefix: str, is_last: bool) -> None:
            connector = "└── " if is_last else "├── "
            if prefix:
                lines.append(f"{prefix}{connector}{label(tid)}")
            else:
                lines.append(label(tid))
            children = self._rdeps.get(tid, [])
            child_prefix = prefix + ("      " if is_last else "│     ")
            if not prefix:
                child_prefix = "  "
            for i, child in enumerate(children):
                _render(child, child_prefix, i == len(children) - 1)

        for i, root in enumerate(roots):
            _render(root, "", i == len(roots) - 1)

        return "\n".join(lines)

    def compute_critical_path(self) -> list[str]:
        """
        Return the longest chain of tasks by dependency count (simple v1).

        Uses dynamic programming over the topological order.  Ties between
        paths of the same length are broken by choosing the path whose
        task_ids are lexicographically smallest.
        """
        topo = self.topological_order()
        if not topo:
            return []

        # path_len[tid] = number of tasks on the longest path *ending* at tid.
        path_len: dict[str, int]  = {}
        # predecessor[tid] = previous task on that longest path.
        predecessor: dict[str, str | None] = {}

        for tid in topo:
            best_len  = 0
            best_pred: str | None = None
            for dep in sorted(self._deps.get(tid, [])):
                dep_len = path_len.get(dep, 0)
                if dep_len > best_len or (
                    dep_len == best_len and best_pred is not None and dep < best_pred
                ):
                    best_len  = dep_len
                    best_pred = dep
            path_len[tid]    = best_len + 1
            predecessor[tid] = best_pred

        # Find the end of the longest path (break ties by task_id).
        end_task = max(
            topo,
            key=lambda tid: (path_len[tid], [-ord(c) for c in tid]),
        )
        # Prefer lexicographically smallest task_id when lengths are equal.
        max_len = path_len[end_task]
        candidates = sorted(tid for tid in topo if path_len[tid] == max_len)
        end_task = candidates[0]

        # Reconstruct the path by following predecessors backwards.
        path: list[str] = []
        current: str | None = end_task
        while current is not None:
            path.append(current)
            current = predecessor[current]
        path.reverse()
        return path
