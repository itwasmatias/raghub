"""
Mission task materializer.

Converts MissionTaskDefinition objects into controller queue Task objects,
with idempotency: re-running materialise() for the same mission+task always
returns the same queue_task_id and never double-enqueues.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from tools.ai_controller.models import Task
from tools.ai_controller.queue import DurableQueue
from .models import (
    MissionState,
    MissionStatus,
    MissionTaskDefinition,
    MissionTaskState,
    MissionTaskStatus,
)

logger = logging.getLogger(__name__)

# Task-ID regex from the controller model.
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Queue state directories in the order we check them.
_QUEUE_STATES: list[str] = ["pending", "running", "succeeded", "failed", "invalid"]

# Versioned canonical input for mission-task queue identity generation.
_IDENTITY_VERSION = "mission-task-v1"
RESERVED_MISSION_METADATA_FIELDS = frozenset(
    {
        "mission_id",
        "mission_task_id",
        "depends_on",
        "queue_task_id",
        "id",
        "provider",
        "provider_preference",
        "provider_fallback",
        "provider_policy",
        "controller_context",
    }
)


def reserved_metadata_collisions(task_def: MissionTaskDefinition) -> set[str]:
    """Return task metadata keys owned by mission/controller authority."""
    return RESERVED_MISSION_METADATA_FIELDS.intersection(task_def.metadata)


def _sanitize_segment(value: str) -> str:
    """Replace every character that is not alphanumeric, '.', '_', or '-' with '-'.

    Also replaces '..' sequences to prevent path traversal attacks.
    """
    # First pass: replace non-alphanumeric characters (except ._-)
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "-", value)

    # Second pass: replace '..' to prevent path traversal
    # Replace all occurrences of '..' with a single '.'
    while ".." in sanitized:
        sanitized = sanitized.replace("..", ".")

    return sanitized


def make_legacy_queue_task_id(mission_id: str, task_id: str) -> str:
    """
    Generate a queue task ID using the legacy algorithm.

    This function exists solely for legacy record lookup and compatibility.
    New materializations must use the current collision-resistant format.

    Legacy format: ``m-{mission_id[:40]}-{task_id[:60]}`` truncated to 128 chars

    Invalid characters (anything outside ``[A-Za-z0-9._-]``) are replaced
    with ``-``.
    """
    m_seg = _sanitize_segment(mission_id[:40])
    t_seg = _sanitize_segment(task_id[:60])
    raw = f"m-{m_seg}-{t_seg}"
    result = raw[:128]
    return result


class TaskMaterializer:
    """Idempotently converts mission task definitions into controller queue tasks."""

    def __init__(
        self,
        queue: DurableQueue,
        reports_root: Path,
        events_dir: Path | None = None,
    ) -> None:
        self._queue = queue
        self._reports_root = Path(reports_root)
        self._events_dir = Path(events_dir) if events_dir else None

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _emit_event(
        self,
        event_type: str,
        mission_id: str,
        task_id: str | None = None,
        queue_task_id: str | None = None,
        reason: str | None = None,
        **metadata_extra,
    ) -> None:
        """Emit a reconciliation event if events_dir is configured.

        Failures in event emission do not prevent materialization from succeeding.
        """
        if self._events_dir is None:
            return

        try:
            from .events import MissionEventLog, make_event

            event = make_event(
                event_type=event_type,
                mission_id=mission_id,
                task_id=task_id,
                queue_task_id=queue_task_id,
                reason=reason,
                metadata=metadata_extra,
            )

            log = MissionEventLog(self._events_dir, mission_id)
            log.append(event)
        except (OSError, PermissionError, ValueError) as exc:
            # Event emission failure should not break materialization
            logger.warning(
                "failed to emit event type=%s mission=%s: %s",
                event_type,
                mission_id,
                exc,
            )

    # ------------------------------------------------------------------
    # ID computation
    # ------------------------------------------------------------------

    def make_queue_task_id(self, mission_id: str, task_id: str) -> str:
        """Return a deterministic, collision-resistant, path-safe queue task ID.

        The canonical identity is versioned and uses SHA-256 over the tuple
        ``(identity-version, mission-id, task-id)`` so the same logical task
        always resolves to the same queue ID across restarts and materializer
        instances.  The readable prefix is kept short for diagnostics, while the
        digest suffix carries the collision-resistant component.
        """
        canonical_input = f"{_IDENTITY_VERSION}:{mission_id}:{task_id}".encode("utf-8")
        digest = hashlib.sha256(canonical_input).hexdigest()[:12]

        m_seg = _sanitize_segment(mission_id[:18])
        t_seg = _sanitize_segment(task_id[:18])
        raw = f"m-{m_seg}-{t_seg}-{digest}"
        result = raw[:128]

        if not _TASK_ID_RE.fullmatch(result):
            fallback = hashlib.sha256(canonical_input).hexdigest()[:120]
            result = f"m-{fallback}"

        return result

    # ------------------------------------------------------------------
    # Metadata validation
    # ------------------------------------------------------------------

    def _validate_legacy_metadata(
        self,
        queue_task_id: str,
        mission_id: str,
        task_id: str,
    ) -> bool:
        """Validate that a legacy queue record's metadata matches the mission/task.

        Returns True if metadata is valid and record can be safely adopted.
        Returns False if metadata is missing, incomplete, or mismatched.
        """
        # Find the queue record file
        queue_file: Path | None = None
        for state_name in _QUEUE_STATES:
            candidate: Path = getattr(self._queue, state_name, None) or (
                self._queue.root / state_name
            )
            candidate_file = candidate / f"{queue_task_id}.json"
            if candidate_file.exists():
                queue_file = candidate_file
                break

        if queue_file is None:
            return False

        # Read and parse metadata
        try:
            task_data = json.loads(queue_file.read_text(encoding="utf-8"))
            metadata = task_data.get("metadata", {})
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "could not read queue file for validation qid=%s: %s",
                queue_task_id,
                exc,
            )
            return False

        # Validate required fields
        recorded_mission_id = metadata.get("mission_id")
        recorded_task_id = metadata.get("mission_task_id")

        if not recorded_mission_id or not recorded_task_id:
            logger.warning(
                "legacy record has incomplete metadata qid=%s mission_id=%s task_id=%s",
                queue_task_id,
                recorded_mission_id,
                recorded_task_id,
            )
            return False

        # Validate values match
        if recorded_mission_id != mission_id:
            logger.warning(
                "legacy record mission_id mismatch qid=%s expected=%s actual=%s",
                queue_task_id,
                mission_id,
                recorded_mission_id,
            )
            return False

        if recorded_task_id != task_id:
            logger.warning(
                "legacy record task_id mismatch qid=%s expected=%s actual=%s",
                queue_task_id,
                task_id,
                recorded_task_id,
            )
            return False

        # Metadata is valid
        return True

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

        Supports legacy queue record adoption: if a queue record exists in the
        legacy ID format, it will be adopted rather than creating a duplicate.

        Returns
        -------
        str
            The queue_task_id (current or legacy format, depending on what
            was found or created).
        """
        current_queue_id = self.make_queue_task_id(mission_id, task_def.task_id)
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_def.task_id)

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

        # Step 2 — check if task is already in any queue directory (current or legacy).
        current_state = self.queue_task_exists_in(current_queue_id)
        legacy_state = self.queue_task_exists_in(legacy_queue_id)

        # Handle the various combinations
        if current_state is not None and legacy_state is not None:
            # Both records exist - this is a conflict situation
            # Validate they're for the same mission/task using metadata
            logger.warning(
                "both current and legacy queue records exist mission=%s task=%s current=%s legacy=%s",
                mission_id,
                task_def.task_id,
                current_queue_id,
                legacy_queue_id,
            )
            # Emit conflict detection event
            self._emit_event(
                event_type="both_formats_found",
                mission_id=mission_id,
                task_id=task_def.task_id,
                queue_task_id=current_queue_id,
                reason="Both current and legacy queue records exist for the same mission task",
                current_queue_id=current_queue_id,
                legacy_queue_id=legacy_queue_id,
                current_state=current_state,
                legacy_state=legacy_state,
            )
            # Deterministic choice: prefer current format
            # The legacy record should be handled by cleanup/migration separately
            return current_queue_id

        if current_state is not None:
            # Current-format record found
            logger.debug(
                "current-format queue record found state=%s mission=%s task=%s qid=%s",
                current_state,
                mission_id,
                task_def.task_id,
                current_queue_id,
            )
            return current_queue_id

        if legacy_state is not None:
            # Legacy record found - validate metadata before adopting
            if self._validate_legacy_metadata(legacy_queue_id, mission_id, task_def.task_id):
                logger.info(
                    "adopting legacy queue record state=%s mission=%s task=%s legacy_qid=%s",
                    legacy_state,
                    mission_id,
                    task_def.task_id,
                    legacy_queue_id,
                )
                # Emit adoption event
                self._emit_event(
                    event_type="legacy_adopted",
                    mission_id=mission_id,
                    task_id=task_def.task_id,
                    queue_task_id=legacy_queue_id,
                    reason=f"Legacy queue record adopted (state: {legacy_state})",
                    legacy_state=legacy_state,
                )
                return legacy_queue_id
            else:
                # Metadata validation failed - do NOT adopt
                # Log warning and fall through to create new current-format record
                logger.warning(
                    "legacy record metadata validation failed, creating new record mission=%s task=%s legacy_qid=%s",
                    mission_id,
                    task_def.task_id,
                    legacy_queue_id,
                )
                # Emit rejection event
                self._emit_event(
                    event_type="legacy_rejected",
                    mission_id=mission_id,
                    task_id=task_def.task_id,
                    queue_task_id=None,  # Will create new ID
                    reason="Legacy record metadata validation failed",
                    legacy_queue_id=legacy_queue_id,
                    legacy_state=legacy_state,
                )

        # Step 3 — no existing record found, build and enqueue using current format.
        queue_task_id = current_queue_id
        metadata: dict = {
            **task_def.metadata,
            "mission_id": mission_id,
            "mission_task_id": task_def.task_id,
            "depends_on": list(task_def.depends_on),
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
            # Emit queue creation event
            self._emit_event(
                event_type="queue_created",
                mission_id=mission_id,
                task_id=task_def.task_id,
                queue_task_id=queue_task_id,
                reason="New queue record created",
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
        *,
        mission_state: MissionState | None = None,
        task_state: MissionTaskState | None = None,
    ) -> dict | None:
        """Classify the current queue state for a mission task.

        The method returns a machine-readable classification so that scheduler
        and restart reconciliation can decide whether to rematerialize work,
        preserve terminal evidence, or defer without treating a missing queue
        record as a hard mission failure.
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

        if mission_state is not None and mission_state.status == MissionStatus.paused:
            classification = "paused"
        elif task_state is not None and task_state.status == MissionTaskStatus.cancelled:
            classification = "cancelled"
        elif queue_status in {"pending", "running", "succeeded", "failed", "invalid"}:
            classification = "already_materialized"
        elif report is not None:
            classification = "completed_with_evidence"
        else:
            classification = "eligible_missing"

        # Emit reconciliation event
        event_type = f"reconcile_{classification}"
        reason = f"Reconciliation classified as {classification} (queue_status: {queue_status})"
        self._emit_event(
            event_type=event_type,
            mission_id=mission_id,
            task_id=task_id,
            queue_task_id=queue_task_id,
            reason=reason,
            classification=classification,
            queue_status=queue_status,
        )

        return {
            "queue_status": queue_status,
            "classification": classification,
            "report": report,
            "queue_task_id": queue_task_id,
            "mission_id": mission_id,
            "task_id": task_id,
        }

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
