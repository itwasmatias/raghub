"""
Mission task materializer.

Converts MissionTaskDefinition objects into controller queue Task objects,
with idempotency: re-running materialise() for the same mission+task always
returns the same queue_task_id and never double-enqueues.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from tools.ai_controller.models import Task
from tools.ai_controller.queue import DurableQueue
from .models import MissionState, MissionTaskDefinition

logger = logging.getLogger(__name__)

# Task-ID regex from the controller model.
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Queue state directories in the order we check them.
_QUEUE_STATES: list[str] = ["pending", "running", "succeeded", "failed", "invalid"]


def _sanitize_segment(value: str) -> str:
    """Replace every character that is not alphanumeric, '.', '_', or '-' with '-'."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", value)


class TaskMaterializer:
    """Idempotently converts mission task definitions into controller queue tasks."""

    def __init__(self, queue: DurableQueue, reports_root: Path) -> None:
        self._queue = queue
        self._reports_root = Path(reports_root)

    # ------------------------------------------------------------------
    # ID computation
    # ------------------------------------------------------------------

    def make_queue_task_id(self, mission_id: str, task_id: str) -> str:
        """Return a deterministic, path-safe queue task ID.

        Format: ``m-{mission_id[:40]}-{task_id[:60]}``

        Invalid characters (anything outside ``[A-Za-z0-9._-]``) are replaced
        with ``-``.  The resulting ID is truncated to 128 characters to satisfy
        the task-ID regex.
        """
        m_seg = _sanitize_segment(mission_id[:40])
        t_seg = _sanitize_segment(task_id[:60])
        raw = f"m-{m_seg}-{t_seg}"
        # Ensure the first char is alphanumeric (the prefix 'm' guarantees this).
        result = raw[:128]
        # Verify; if for some reason it doesn't match, fall back to a safe default.
        if not _TASK_ID_RE.fullmatch(result):
            result = "m-" + re.sub(r"[^A-Za-z0-9._-]", "-", result[2:])[:126]
        return result

    # ------------------------------------------------------------------
    # Materialise
    # ------------------------------------------------------------------

    def materialize(
        self,
        mission_id: str,
        task_def: MissionTaskDefinition,
        mission_state: MissionState,
    ) -> str:
        """Enqueue one mission task if not already present in the queue.

        The operation is idempotent: calling this method multiple times for
        the same (mission_id, task_def.task_id) pair is safe.

        Returns
        -------
        str
            The queue_task_id (deterministic regardless of whether the task
            was freshly enqueued or already existed).
        """
        queue_task_id = self.make_queue_task_id(mission_id, task_def.task_id)

        # Step 1 — check task_state for a previously recorded queue_task_id.
        task_state = mission_state.task_states.get(task_def.task_id)
        if task_state is not None and task_state.queue_task_id:
            logger.debug(
                "task already has queue_task_id mission=%s task=%s qid=%s",
                mission_id,
                task_def.task_id,
                task_state.queue_task_id,
            )
            return task_state.queue_task_id

        # Step 2 — check if task is already in any queue directory.
        existing_state = self.queue_task_exists_in(queue_task_id)
        if existing_state is not None:
            logger.debug(
                "task already in queue state=%s mission=%s task=%s",
                existing_state,
                mission_id,
                task_def.task_id,
            )
            return queue_task_id

        # Step 3 — build and enqueue the Task.
        metadata: dict = {
            "mission_id": mission_id,
            "mission_task_id": task_def.task_id,
            "depends_on": list(task_def.depends_on),
            **task_def.metadata,
        }

        controller_task = Task(
            id=queue_task_id,
            title=task_def.title,
            prompt=task_def.prompt,
            base_ref=task_def.base_ref,
            tests=list(task_def.tests),
            max_attempts=task_def.max_attempts,
            metadata=metadata,
        )

        try:
            self._queue.enqueue(controller_task)
            logger.info(
                "task enqueued mission=%s task=%s qid=%s",
                mission_id,
                task_def.task_id,
                queue_task_id,
            )
        except FileExistsError:
            # Another process beat us to it — idempotent; just return the ID.
            logger.debug(
                "task enqueue raced mission=%s task=%s qid=%s",
                mission_id,
                task_def.task_id,
                queue_task_id,
            )

        return queue_task_id

    # ------------------------------------------------------------------
    # Reconcile
    # ------------------------------------------------------------------

    def reconcile_task(
        self,
        mission_id: str,
        task_id: str,
        queue_task_id: str,
        reports_root: Path,
    ) -> dict | None:
        """Check the current queue state and report for a queued task.

        Returns
        -------
        dict
            ``{"queue_status": str, "report": dict | None}``
            where ``queue_status`` is one of
            ``"pending" | "running" | "succeeded" | "failed" | "missing"``.

        Returns ``None`` only if *queue_task_id* is falsy (should not happen
        in normal usage).
        """
        if not queue_task_id:
            return None

        queue_status = self.queue_task_exists_in(queue_task_id) or "missing"

        report: dict | None = None
        report_path = Path(reports_root) / f"{queue_task_id}.json"
        if report_path.exists():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "could not read report for %s: %s", queue_task_id, exc
                )

        return {"queue_status": queue_status, "report": report}

    # ------------------------------------------------------------------
    # Queue existence check
    # ------------------------------------------------------------------

    def queue_task_exists_in(self, queue_task_id: str) -> str | None:
        """Return the queue state directory name the task is in, or ``None``."""
        for state_name in _QUEUE_STATES:
            candidate: Path = getattr(self._queue, state_name, None) or (
                self._queue.root / state_name
            )
            if (candidate / f"{queue_task_id}.json").exists():
                return state_name
        return None
