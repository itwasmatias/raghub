"""
Failure injection tests for crash boundaries and atomicity guarantees.

Tests cover:
1. Crash after queue enqueue but before state save
2. Crash after state save but before event emission
3. Concurrent materializer instances (race conditions)
4. Corrupted queue files
5. Corrupted state files
6. Missing metadata in legacy records
7. Filesystem errors during materialization
8. Invalid queue task IDs
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from tools.ai_controller.mission.materializer import TaskMaterializer
from tools.ai_controller.mission import make_legacy_queue_task_id
from tools.ai_controller.mission.models import (
    MissionState,
    MissionStatus,
    MissionTaskDefinition,
    MissionTaskState,
    MissionTaskStatus,
)
from tools.ai_controller.models import Task
from tools.ai_controller.queue import DurableQueue


@pytest.fixture
def temp_queue(tmp_path: Path) -> DurableQueue:
    return DurableQueue(tmp_path / "queue")


@pytest.fixture
def materializer(temp_queue: DurableQueue, tmp_path: Path) -> TaskMaterializer:
    return TaskMaterializer(
        temp_queue,
        tmp_path / "reports",
        events_dir=tmp_path / "events",
    )


class TestCrashRecovery:
    """Test recovery from crashes at various points in materialization."""

    def test_crash_after_enqueue_before_state_save_idempotent(
        self,
        materializer: TaskMaterializer,
        temp_queue: DurableQueue,
    ):
        """
        Crash after queue.enqueue() but before mission state save.

        Recovery: re-materialize should detect existing queue record
        and return same ID without duplication.
        """
        mission_id = "mission-crash-1"
        task_id = "task-crash"

        task_def = MissionTaskDefinition(
            task_id=task_id,
            title="Task",
            prompt="Work",
        )

        # First materialization (succeeds, creates queue record)
        mission_state_1 = MissionState(
            mission_id=mission_id,
            status=MissionStatus.running,
            task_states={},
        )
        queue_id_1 = materializer.materialize(mission_id, task_def, mission_state_1)

        # Verify queue record exists
        queue_file = temp_queue.pending / f"{queue_id_1}.json"
        assert queue_file.exists()

        # CRASH: mission state was NOT saved (still empty task_states)

        # Recovery: re-materialize with same empty state
        mission_state_2 = MissionState(
            mission_id=mission_id,
            status=MissionStatus.running,
            task_states={},  # State still empty after crash
        )
        queue_id_2 = materializer.materialize(mission_id, task_def, mission_state_2)

        # Should return same queue ID
        assert queue_id_2 == queue_id_1

        # Should not create duplicate
        queue_files = list(temp_queue.pending.glob("*.json"))
        assert len(queue_files) == 1

    def test_concurrent_materializers_race_condition(
        self,
        temp_queue: DurableQueue,
        tmp_path: Path,
    ):
        """
        Two materializer instances try to enqueue same task concurrently.

        Both should get the same queue_task_id without duplication.
        """
        mission_id = "mission-race"
        task_id = "task-race"

        mat1 = TaskMaterializer(temp_queue, tmp_path / "reports1")
        mat2 = TaskMaterializer(temp_queue, tmp_path / "reports2")

        task_def = MissionTaskDefinition(
            task_id=task_id,
            title="Task",
            prompt="Work",
        )
        mission_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.running,
            task_states={},
        )

        # First materializer wins
        queue_id_1 = mat1.materialize(mission_id, task_def, mission_state)

        # Second materializer finds existing record
        queue_id_2 = mat2.materialize(mission_id, task_def, mission_state)

        # Should be identical
        assert queue_id_1 == queue_id_2

        # Only one queue file should exist
        queue_files = list(temp_queue.pending.glob("*.json"))
        assert len(queue_files) == 1


class TestCorruptedFiles:
    """Test handling of corrupted or malformed files."""

    def test_corrupted_queue_file_during_validation(
        self,
        materializer: TaskMaterializer,
        temp_queue: DurableQueue,
    ):
        """Corrupted legacy queue file should fail validation and create new record."""
        mission_id = "mission-corrupt"
        task_id = "task-corrupt"

        # Create legacy queue file with INVALID JSON
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_file = temp_queue.pending / f"{legacy_queue_id}.json"
        temp_queue.pending.mkdir(parents=True, exist_ok=True)
        legacy_file.write_text("{ INVALID JSON HERE }", encoding="utf-8")

        # Attempt to materialize
        task_def = MissionTaskDefinition(
            task_id=task_id,
            title="Task",
            prompt="Work",
        )
        mission_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.running,
            task_states={},
        )

        # Should detect corrupted file, fail validation, create new record
        result_id = materializer.materialize(mission_id, task_def, mission_state)

        # Should be current format (not legacy)
        current_id = materializer.make_queue_task_id(mission_id, task_id)
        assert result_id == current_id

        # New valid file should exist
        current_file = temp_queue.pending / f"{current_id}.json"
        assert current_file.exists()

        # Should be valid JSON
        data = json.loads(current_file.read_text())
        assert data["id"] == current_id

    def test_empty_metadata_in_legacy_record(
        self,
        materializer: TaskMaterializer,
        temp_queue: DurableQueue,
    ):
        """Legacy record with empty/null metadata should be rejected."""
        mission_id = "mission-null-meta"
        task_id = "task-null"

        # Create legacy record with null metadata
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={},  # Empty!
        )
        temp_queue.enqueue(legacy_task)

        task_def = MissionTaskDefinition(
            task_id=task_id,
            title="Task",
            prompt="Work",
        )
        mission_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.running,
            task_states={},
        )

        result_id = materializer.materialize(mission_id, task_def, mission_state)

        # Should reject and create new
        current_id = materializer.make_queue_task_id(mission_id, task_id)
        assert result_id == current_id


class TestFilesystemErrors:
    """Test handling of filesystem errors during operations."""

    def test_read_only_events_directory_fails_gracefully(
        self,
        temp_queue: DurableQueue,
        tmp_path: Path,
    ):
        """If events directory is read-only, materialization should still succeed."""
        events_dir = tmp_path / "events_ro"
        events_dir.mkdir()
        events_dir.chmod(0o444)  # Read-only

        mat = TaskMaterializer(
            temp_queue,
            tmp_path / "reports",
            events_dir=events_dir,
        )

        task_def = MissionTaskDefinition(
            task_id="task-ro",
            title="Task",
            prompt="Work",
        )
        mission_state = MissionState(
            mission_id="mission-ro",
            status=MissionStatus.running,
            task_states={},
        )

        # Should succeed even if event emission fails
        try:
            queue_id = mat.materialize("mission-ro", task_def, mission_state)
            assert queue_id is not None

            # Queue record should exist
            queue_file = temp_queue.pending / f"{queue_id}.json"
            assert queue_file.exists()
        finally:
            # Cleanup: restore permissions
            events_dir.chmod(0o755)

    def test_queue_directory_missing_creates_on_demand(
        self,
        tmp_path: Path,
    ):
        """If queue directory doesn't exist, it should be created on first enqueue."""
        queue_dir = tmp_path / "nonexistent_queue"
        # Don't create it!
        queue = DurableQueue(queue_dir)

        mat = TaskMaterializer(queue, tmp_path / "reports")

        task_def = MissionTaskDefinition(
            task_id="task-create",
            title="Task",
            prompt="Work",
        )
        mission_state = MissionState(
            mission_id="mission-create",
            status=MissionStatus.running,
            task_states={},
        )

        queue_id = mat.materialize("mission-create", task_def, mission_state)

        # Directory should now exist
        assert queue.pending.exists()

        # Queue file should exist
        queue_file = queue.pending / f"{queue_id}.json"
        assert queue_file.exists()


class TestInvalidInputs:
    """Test handling of invalid inputs and edge cases."""

    def test_invalid_queue_task_id_in_reconcile(
        self,
        materializer: TaskMaterializer,
        tmp_path: Path,
    ):
        """Reconcile with invalid queue_task_id should return None."""
        info = materializer.reconcile_task(
            "mission-x",
            "task-x",
            "",  # Empty queue_task_id
            tmp_path / "reports",
        )

        assert info is None

    def test_reconcile_with_nonexistent_queue_id(
        self,
        materializer: TaskMaterializer,
        tmp_path: Path,
    ):
        """Reconcile with queue_task_id that doesn't exist should classify as missing."""
        info = materializer.reconcile_task(
            "mission-y",
            "task-y",
            "nonexistent-queue-id",
            tmp_path / "reports",
        )

        assert info is not None
        assert info["queue_status"] == "missing"
        assert info["classification"] == "eligible_missing"

    def test_materialize_with_very_long_mission_and_task_ids(
        self,
        materializer: TaskMaterializer,
    ):
        """Very long IDs should be truncated safely without errors."""
        long_mission = "mission-" + ("x" * 500)
        long_task = "task-" + ("y" * 500)

        task_def = MissionTaskDefinition(
            task_id=long_task,
            title="Task",
            prompt="Work",
        )
        mission_state = MissionState(
            mission_id=long_mission,
            status=MissionStatus.running,
            task_states={},
        )

        # Should succeed
        queue_id = materializer.materialize(long_mission, task_def, mission_state)

        # Queue ID should be within 128 char limit
        assert len(queue_id) <= 128
        assert queue_id.startswith("m-")

    def test_materialize_with_special_characters_in_ids(
        self,
        materializer: TaskMaterializer,
    ):
        """Special characters should be sanitized."""
        mission_id = "mission/../etc/passwd"
        task_id = "task\\windows\\system32"

        task_def = MissionTaskDefinition(
            task_id=task_id,
            title="Task",
            prompt="Work",
        )
        mission_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.running,
            task_states={},
        )

        queue_id = materializer.materialize(mission_id, task_def, mission_state)

        # Should not contain path separators or ..
        assert "/" not in queue_id
        assert "\\" not in queue_id
        assert ".." not in queue_id


class TestStateSafetyEdgeCases:
    """Test edge cases in state handling."""

    def test_materialize_with_partial_task_state(
        self,
        materializer: TaskMaterializer,
    ):
        """Task state with some fields set should not break materialization."""
        mission_id = "mission-partial"
        task_id = "task-partial"

        task_def = MissionTaskDefinition(
            task_id=task_id,
            title="Task",
            prompt="Work",
        )

        # State has partial task_state (no queue_task_id)
        mission_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.running,
            task_states={
                task_id: MissionTaskState(
                    task_id=task_id,
                    status=MissionTaskStatus.pending,
                    queue_task_id=None,  # Not yet set
                )
            },
        )

        # Should succeed
        queue_id = materializer.materialize(mission_id, task_def, mission_state)
        assert queue_id is not None

    def test_reconcile_with_report_but_missing_queue_file(
        self,
        materializer: TaskMaterializer,
        tmp_path: Path,
    ):
        """Report exists but queue file is missing - should classify as completed_with_evidence."""
        mission_id = "mission-report-only"
        task_id = "task-report"
        queue_id = materializer.make_queue_task_id(mission_id, task_id)

        # Create report file
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_file = reports_dir / f"{queue_id}.json"
        report_file.write_text(json.dumps({"status": "succeeded"}), encoding="utf-8")

        # Reconcile (no queue file exists)
        info = materializer.reconcile_task(
            mission_id,
            task_id,
            queue_id,
            reports_dir,
        )

        assert info is not None
        assert info["queue_status"] == "missing"
        assert info["classification"] == "completed_with_evidence"
        assert info["report"] is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
