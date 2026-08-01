"""
Mission queue record migration scanner and planner.

Provides read-only discovery of legacy queue records and deterministic
migration planning without automatic destructive mutations.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tools.ai_controller.queue import DurableQueue
    from .store import MissionStore

from .materializer import TaskMaterializer, make_legacy_queue_task_id
from .models import MissionTaskState

logger = logging.getLogger(__name__)


def scan_mission_migration(
    mission_id: str,
    mission_store: "MissionStore",
    queue: "DurableQueue",
) -> list[dict]:
    """
    Scan a mission for legacy queue records requiring migration.

    This is a READ-ONLY operation that does not modify mission state or queue records.

    Returns a list of migration scan results, one per task, with classification:
    - legacy_only: Legacy queue record exists, no current record, no linkage
    - current_only: Current queue record exists, no legacy record
    - both_formats: Both legacy and current records exist
    - persisted_legacy_link: Mission state links to legacy record
    - persisted_current_link: Mission state links to current record
    - missing_linkage: Queue record exists but mission state has no linkage
    - metadata_missing: Legacy record has no metadata
    - metadata_conflict: Legacy record metadata doesn't match mission/task
    - no_queue_record: No queue record found (normal pending state)

    Parameters
    ----------
    mission_id : str
        Mission identifier
    mission_store : MissionStore
        Mission state and definition store
    queue : DurableQueue
        Controller durable queue

    Returns
    -------
    list[dict]
        List of scan results with keys:
        - mission_id
        - task_id
        - classification
        - legacy_queue_id (if exists)
        - current_queue_id (computed)
        - legacy_exists (bool)
        - current_exists (bool)
        - persisted_linkage (str | None)
        - metadata_valid (bool | None)
        - metadata_mismatch_reason (str | None)
    """
    try:
        definition = mission_store.load_definition(mission_id)
        state = mission_store.load_state(mission_id)
    except FileNotFoundError:
        logger.error("mission not found mission_id=%s", mission_id)
        return []

    results: list[dict] = []
    materializer = TaskMaterializer(queue, Path("/tmp/reports"))

    for task_def in definition.tasks:
        task_id = task_def.task_id

        # Compute IDs
        current_queue_id = materializer.make_queue_task_id(mission_id, task_id)
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)

        # Check persisted linkage
        task_state = state.task_states.get(task_id)
        persisted_linkage = task_state.queue_task_id if task_state else None

        # Check queue record existence
        legacy_exists = materializer.queue_task_exists_in(legacy_queue_id) is not None
        current_exists = materializer.queue_task_exists_in(current_queue_id) is not None

        # Validate legacy metadata if exists
        metadata_valid: bool | None = None
        metadata_mismatch_reason: str | None = None

        if legacy_exists:
            metadata_valid = materializer._validate_legacy_metadata(
                legacy_queue_id, mission_id, task_id
            )
            if not metadata_valid:
                metadata_mismatch_reason = "Metadata validation failed"

        # Classify
        classification = _classify_migration_status(
            legacy_exists=legacy_exists,
            current_exists=current_exists,
            persisted_linkage=persisted_linkage,
            legacy_queue_id=legacy_queue_id,
            current_queue_id=current_queue_id,
            metadata_valid=metadata_valid,
        )

        result = {
            "mission_id": mission_id,
            "task_id": task_id,
            "classification": classification,
            "legacy_queue_id": legacy_queue_id if legacy_exists else None,
            "current_queue_id": current_queue_id,
            "legacy_exists": legacy_exists,
            "current_exists": current_exists,
            "persisted_linkage": persisted_linkage,
            "metadata_valid": metadata_valid,
            "metadata_mismatch_reason": metadata_mismatch_reason,
        }

        results.append(result)

    return results


def _classify_migration_status(
    legacy_exists: bool,
    current_exists: bool,
    persisted_linkage: str | None,
    legacy_queue_id: str,
    current_queue_id: str,
    metadata_valid: bool | None,
) -> str:
    """
    Classify migration status for a single task.

    Returns one of:
    - no_queue_record
    - legacy_only
    - current_only
    - both_formats
    - persisted_legacy_link
    - persisted_current_link
    - missing_linkage
    - metadata_missing
    - metadata_conflict
    """
    # Check persisted linkage first
    if persisted_linkage:
        if persisted_linkage == legacy_queue_id:
            return "persisted_legacy_link"
        if persisted_linkage == current_queue_id:
            return "persisted_current_link"
        # Linkage to unknown ID
        return "missing_linkage"

    # No linkage - check queue records
    if not legacy_exists and not current_exists:
        return "no_queue_record"

    if legacy_exists and current_exists:
        return "both_formats"

    if current_exists:
        return "current_only"

    if legacy_exists:
        # Check metadata
        if metadata_valid is False:
            return "metadata_conflict"
        if metadata_valid is None:
            return "metadata_missing"
        return "legacy_only"

    return "no_queue_record"


def plan_mission_migration(scan_results: list[dict]) -> list[dict]:
    """
    Generate a deterministic migration plan from scan results.

    This is a READ-ONLY operation that does not modify mission state or queue records.

    Returns a list of proposed actions, each with:
    - action: "link_mission_state" | "delete_legacy_record" | "warn_metadata_conflict" | "no_action"
    - mission_id: str
    - task_id: str
    - reason: str (human-readable explanation)
    - queue_task_id: str | None (for link actions)
    - legacy_queue_id: str | None (for delete actions)

    Planning logic:
    - legacy_only (valid metadata): Propose linking mission state to legacy record
    - current_only: No action needed (already correct)
    - both_formats: Propose deleting legacy record (duplicate)
    - persisted_legacy_link: No action needed (already linked)
    - persisted_current_link: No action needed (already linked)
    - metadata_conflict: Warn about metadata mismatch, no action
    - metadata_missing: Warn about missing metadata, no action
    - no_queue_record: No action needed (normal pending state)
    - missing_linkage: Warn about orphaned linkage, no action

    Parameters
    ----------
    scan_results : list[dict]
        Results from scan_mission_migration()

    Returns
    -------
    list[dict]
        List of proposed migration actions
    """
    actions: list[dict] = []

    for result in scan_results:
        classification = result["classification"]
        mission_id = result["mission_id"]
        task_id = result["task_id"]

        if classification == "legacy_only":
            # Propose linking mission state to legacy record
            actions.append({
                "action": "link_mission_state",
                "mission_id": mission_id,
                "task_id": task_id,
                "queue_task_id": result["legacy_queue_id"],
                "reason": "Valid legacy queue record exists without mission linkage",
            })

        elif classification == "both_formats":
            # Propose deleting legacy record (duplicate)
            actions.append({
                "action": "delete_legacy_record",
                "mission_id": mission_id,
                "task_id": task_id,
                "legacy_queue_id": result["legacy_queue_id"],
                "reason": "Duplicate: both legacy and current queue records exist",
            })

        elif classification == "metadata_conflict":
            # Warn about metadata mismatch
            actions.append({
                "action": "warn_metadata_conflict",
                "mission_id": mission_id,
                "task_id": task_id,
                "legacy_queue_id": result["legacy_queue_id"],
                "reason": "Legacy record has mismatched metadata - manual review required",
            })

        elif classification == "metadata_missing":
            # Warn about missing metadata
            actions.append({
                "action": "warn_metadata_missing",
                "mission_id": mission_id,
                "task_id": task_id,
                "legacy_queue_id": result["legacy_queue_id"],
                "reason": "Legacy record has no metadata - manual review required",
            })

        elif classification == "missing_linkage":
            # Warn about orphaned linkage
            actions.append({
                "action": "warn_missing_linkage",
                "mission_id": mission_id,
                "task_id": task_id,
                "reason": f"Mission state links to unknown queue ID: {result['persisted_linkage']}",
            })

        # For current_only, persisted_legacy_link, persisted_current_link, no_queue_record:
        # No action needed

    return actions


def apply_mission_migration(
    mission_id: str,
    plan: list[dict],
    mission_store: "MissionStore",
    queue: "DurableQueue",
) -> dict:
    """
    Apply a migration plan with safe mutation guarantees.

    This function performs the actual state mutations proposed by the plan.
    It is idempotent and crash-safe.

    Actions applied:
    - link_mission_state: Update mission state to reference legacy queue record
    - delete_legacy_record: Remove legacy queue record file
    - warn_*: Log warnings but perform no mutations

    Parameters
    ----------
    mission_id : str
        Mission identifier
    plan : list[dict]
        Migration plan from plan_mission_migration()
    mission_store : MissionStore
        Mission state and definition store
    queue : DurableQueue
        Controller durable queue

    Returns
    -------
    dict
        Result with keys:
        - success: bool
        - actions_applied: int
        - errors: list[str]
        - warnings: list[str]
    """
    actions_applied = 0
    errors: list[str] = []
    warnings: list[str] = []

    try:
        state = mission_store.load_state(mission_id)
    except FileNotFoundError:
        return {
            "success": False,
            "actions_applied": 0,
            "errors": [f"Mission not found: {mission_id}"],
            "warnings": [],
        }

    for action_item in plan:
        action = action_item["action"]
        task_id = action_item["task_id"]

        if action == "link_mission_state":
            # Update mission state to link to legacy queue record
            queue_task_id = action_item["queue_task_id"]

            # Ensure task_state exists
            if task_id not in state.task_states:
                logger.warning(
                    "task_state not found for linkage mission=%s task=%s",
                    mission_id,
                    task_id,
                )
                errors.append(f"Task state not found for {task_id}")
                continue

            task_state = state.task_states[task_id]

            # Skip if already linked
            if task_state.queue_task_id == queue_task_id:
                logger.debug(
                    "task already linked mission=%s task=%s qid=%s",
                    mission_id,
                    task_id,
                    queue_task_id,
                )
                continue

            # Update and save
            task_state.queue_task_id = queue_task_id
            mission_store.update_state(mission_id, state)
            actions_applied += 1

            logger.info(
                "linked mission state to legacy record mission=%s task=%s qid=%s",
                mission_id,
                task_id,
                queue_task_id,
            )

        elif action == "delete_legacy_record":
            # Delete legacy queue record file
            legacy_queue_id = action_item["legacy_queue_id"]

            # Find and delete the file
            deleted = False
            for state_name in ["pending", "running", "succeeded", "failed", "invalid"]:
                state_dir = queue.root / state_name
                candidate_file = state_dir / f"{legacy_queue_id}.json"

                if candidate_file.exists():
                    try:
                        candidate_file.unlink()
                        deleted = True
                        logger.info(
                            "deleted legacy queue record mission=%s task=%s qid=%s state=%s",
                            mission_id,
                            task_id,
                            legacy_queue_id,
                            state_name,
                        )
                        break
                    except OSError as exc:
                        logger.error(
                            "failed to delete legacy record mission=%s task=%s qid=%s: %s",
                            mission_id,
                            task_id,
                            legacy_queue_id,
                            exc,
                        )
                        errors.append(f"Failed to delete {legacy_queue_id}: {exc}")
                        continue

            if deleted:
                actions_applied += 1
            else:
                logger.warning(
                    "legacy record not found for deletion mission=%s task=%s qid=%s",
                    mission_id,
                    task_id,
                    legacy_queue_id,
                )

        elif action.startswith("warn_"):
            # Warning actions - log but don't mutate
            reason = action_item.get("reason", "Unknown warning")
            logger.warning(
                "migration warning mission=%s task=%s action=%s: %s",
                mission_id,
                task_id,
                action,
                reason,
            )
            warnings.append(f"{task_id}: {reason}")

    success = len(errors) == 0

    return {
        "success": success,
        "actions_applied": actions_applied,
        "errors": errors,
        "warnings": warnings,
    }
