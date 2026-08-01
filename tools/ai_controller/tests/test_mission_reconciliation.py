"""
Tests for mission queue reconciliation and legacy ID compatibility.

Verifies that:
1. Legacy queue records are adopted without duplication
2. Missing queue records are classified correctly
3. Restart reconciliation is idempotent
4. Crash boundaries are handled safely
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools.ai_controller.mission.materializer import TaskMaterializer
from tools.ai_controller.mission import make_legacy_queue_task_id
from tools.ai_controller.mission.models import (
    MissionDefinition,
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
    return TaskMaterializer(temp_queue, tmp_path / "reports")


class TestLegacyIDCompatibility:
    """Test that legacy queue records are adopted correctly."""

    def test_exact_legacy_id_generation(self):
        """Verify exact legacy ID format for known inputs."""
        # Simple case
        legacy_id = make_legacy_queue_task_id("mission-alpha", "task-one")
        assert legacy_id == "m-mission-alpha-task-one"

        # Long names that truncate
        long_mission = "a" * 50
        long_task = "b" * 70
        legacy_id = make_legacy_queue_task_id(long_mission, long_task)

        # Legacy: m-{40 chars mission}-{60 chars task} = 2 + 40 + 1 + 60 = 103 chars
        expected_m = "a" * 40
        expected_t = "b" * 60
        expected = f"m-{expected_m}-{expected_t}"
        assert legacy_id == expected
        assert len(legacy_id) == 103

    def test_current_and_legacy_ids_differ(self, materializer: TaskMaterializer):
        """Current ID format differs from legacy for same inputs."""
        mission_id = "mission-alpha"
        task_id = "task-one"

        current_id = materializer.make_queue_task_id(mission_id, task_id)
        legacy_id = make_legacy_queue_task_id(mission_id, task_id)

        # They should differ (current has hash suffix)
        assert current_id != legacy_id
        assert "-" in current_id
        # Current should contain hash suffix
        parts = current_id.split("-")
        assert len(parts) >= 4  # m, mission, task, hash

    def test_legacy_queue_record_exists_but_no_linkage(
        self,
        materializer: TaskMaterializer,
        temp_queue: DurableQueue,
        tmp_path: Path,
    ):
        """When a legacy queue record exists but mission state has no linkage, adopt it."""
        mission_id = "mission-beta"
        task_id = "implement-feature"

        # Create a legacy queue record
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_queue_id,
            title="Implement Feature",
            prompt="Add the feature",
            base_ref="HEAD",
            tests=[],
            metadata={"mission_id": mission_id, "mission_task_id": task_id},
        )

        # Enqueue it in the queue as if it was created by legacy code
        temp_queue.enqueue(legacy_task)

        # Verify it exists in pending
        assert temp_queue.pending.joinpath(f"{legacy_queue_id}.json").exists()

        # Create mission state with NO queue_task_id linkage
        task_def = MissionTaskDefinition(
            task_id=task_id,
            title="Implement Feature",
            prompt="Add the feature",
        )
        mission_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.running,
            task_states={
                task_id: MissionTaskState(
                    task_id=task_id,
                    status=MissionTaskStatus.pending,
                    queue_task_id=None,  # No linkage!
                )
            },
        )

        # Call materialize - it should adopt the legacy record, NOT create a new one
        result_id = materializer.materialize(mission_id, task_def, mission_state)

        # EXPECTED: should return the legacy ID
        # ACTUAL: currently returns new ID and may try to create duplicate
        assert result_id == legacy_queue_id, \
            f"Expected legacy ID {legacy_queue_id}, got {result_id}"

        # Should NOT have created a new-format queue record
        current_id = materializer.make_queue_task_id(mission_id, task_id)
        assert not temp_queue.pending.joinpath(f"{current_id}.json").exists(), \
            f"Should not create new queue record when legacy exists"

        # Legacy record should still exist
        assert temp_queue.pending.joinpath(f"{legacy_queue_id}.json").exists()

    def test_both_legacy_and_current_records_exist(
        self,
        materializer: TaskMaterializer,
        temp_queue: DurableQueue,
    ):
        """When both legacy and current format records exist, detect conflict."""
        mission_id = "mission-conflict"
        task_id = "conflicting-task"

        # Create both legacy and current queue records
        legacy_id = make_legacy_queue_task_id(mission_id, task_id)
        current_id = materializer.make_queue_task_id(mission_id, task_id)

        legacy_task = Task(
            id=legacy_id,
            title="Task",
            prompt="Do work",
            base_ref="HEAD",
            tests=[],
            metadata={"mission_id": mission_id, "mission_task_id": task_id},
        )
        current_task = Task(
            id=current_id,
            title="Task",
            prompt="Do work",
            base_ref="HEAD",
            tests=[],
            metadata={"mission_id": mission_id, "mission_task_id": task_id},
        )

        temp_queue.enqueue(legacy_task)
        temp_queue.enqueue(current_task)

        task_def = MissionTaskDefinition(
            task_id=task_id,
            title="Task",
            prompt="Do work",
        )
        mission_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.running,
            task_states={},
        )

        # This should detect the conflict and handle it deterministically
        # For now, we expect an exception or clear conflict indication
        # Implementation will determine exact behavior
        result_id = materializer.materialize(mission_id, task_def, mission_state)

        # Should choose one deterministically (e.g., current over legacy)
        # or raise a clear error
        assert result_id in (legacy_id, current_id)

    def test_persisted_queue_task_id_is_respected(
        self,
        materializer: TaskMaterializer,
        temp_queue: DurableQueue,
    ):
        """When mission state has persisted queue_task_id, use it regardless of format."""
        mission_id = "mission-gamma"
        task_id = "persisted-task"

        # Create a legacy queue record
        legacy_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_id,
            title="Task",
            prompt="Do work",
            base_ref="HEAD",
            tests=[],
            metadata={"mission_id": mission_id, "mission_task_id": task_id},
        )
        temp_queue.enqueue(legacy_task)

        # Mission state has the legacy ID persisted
        task_def = MissionTaskDefinition(
            task_id=task_id,
            title="Task",
            prompt="Do work",
        )
        mission_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.running,
            task_states={
                task_id: MissionTaskState(
                    task_id=task_id,
                    status=MissionTaskStatus.queued,
                    queue_task_id=legacy_id,  # Persisted linkage
                )
            },
        )

        # Materialize should return the persisted ID
        result_id = materializer.materialize(mission_id, task_def, mission_state)
        assert result_id == legacy_id

        # Should NOT create a new-format record
        current_id = materializer.make_queue_task_id(mission_id, task_id)
        assert not temp_queue.pending.joinpath(f"{current_id}.json").exists()


class TestReconciliationClassifications:
    """Test reconciliation decision logic for missing queue records."""

    def test_identity_format_is_versioned_and_deterministic(self, materializer: TaskMaterializer):
        """The queue ID should include a versioned digest over the canonical identity."""
        queue_id = materializer.make_queue_task_id("mission-alpha", "task-one")
        expected = hashlib.sha256(b"mission-task-v1:mission-alpha:task-one").hexdigest()[:12]
        assert queue_id.startswith("m-")
        assert expected in queue_id
        assert queue_id == materializer.make_queue_task_id("mission-alpha", "task-one")

    def test_missing_queue_record_for_eligible_task(
        self,
        materializer: TaskMaterializer,
        tmp_path: Path,
    ):
        """A genuinely missing queue record for an eligible task should be detected."""
        mission_id = "mission-delta"
        task_id = "missing-task"
        queue_task_id = materializer.make_queue_task_id(mission_id, task_id)

        # No queue record exists
        info = materializer.reconcile_task(
            mission_id, task_id, queue_task_id, tmp_path / "reports"
        )

        assert info is not None
        assert info["queue_status"] == "missing"
        assert info["classification"] == "eligible_missing"
        assert info["report"] is None

    def test_cancelled_task_is_classified_without_recreation(
        self,
        materializer: TaskMaterializer,
        tmp_path: Path,
    ):
        """Cancelled tasks should be classified as cancelled and not rematerialized."""
        mission_id = "mission-cancelled"
        task_id = "cancelled-task"
        queue_task_id = materializer.make_queue_task_id(mission_id, task_id)
        task_state = MissionTaskState(
            task_id=task_id,
            status=MissionTaskStatus.cancelled,
            queue_task_id=queue_task_id,
        )
        mission_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.running,
            task_states={task_id: task_state},
        )

        info = materializer.reconcile_task(
            mission_id,
            task_id,
            queue_task_id,
            tmp_path / "reports",
            mission_state=mission_state,
            task_state=task_state,
        )

        assert info is not None
        assert info["classification"] == "cancelled"
        assert info["queue_status"] == "missing"

    def test_paused_mission_is_classified_before_recreation(
        self,
        materializer: TaskMaterializer,
        tmp_path: Path,
    ):
        """Paused missions should be classified as paused rather than treated as missing."""
        mission_id = "mission-paused"
        task_id = "paused-task"
        queue_task_id = materializer.make_queue_task_id(mission_id, task_id)
        task_state = MissionTaskState(
            task_id=task_id,
            status=MissionTaskStatus.queued,
            queue_task_id=queue_task_id,
        )
        mission_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.paused,
            task_states={task_id: task_state},
        )

        info = materializer.reconcile_task(
            mission_id,
            task_id,
            queue_task_id,
            tmp_path / "reports",
            mission_state=mission_state,
            task_state=task_state,
        )

        assert info is not None
        assert info["classification"] == "paused"
        assert info["queue_status"] == "missing"

    def test_reconcile_with_legacy_queue_record(
        self,
        materializer: TaskMaterializer,
        temp_queue: DurableQueue,
        tmp_path: Path,
    ):
        """Reconciliation should find legacy queue records."""
        mission_id = "mission-epsilon"
        task_id = "legacy-reconcile"

        # Create legacy queue record
        legacy_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={"mission_id": mission_id, "mission_task_id": task_id},
        )
        temp_queue.enqueue(legacy_task)

        # Reconcile should find it using legacy lookup
        # Currently reconcile_task expects the exact queue_task_id
        # We need to enhance it to check both current and legacy IDs
        info = materializer.reconcile_task(
            mission_id, task_id, legacy_id, tmp_path / "reports"
        )

        assert info is not None
        assert info["queue_status"] == "pending"


class TestRestartSafety:
    """Test that restart scenarios preserve correctness."""

    def test_crash_after_queue_enqueue_before_state_save(
        self,
        materializer: TaskMaterializer,
        temp_queue: DurableQueue,
        tmp_path: Path,
    ):
        """Crash after queue enqueue but before state save is recoverable."""
        mission_id = "mission-crash-test"
        task_id = "crash-task"

        task_def = MissionTaskDefinition(
            task_id=task_id,
            title="Task",
            prompt="Work",
        )

        # First call: creates queue record
        mission_state_before = MissionState(
            mission_id=mission_id,
            status=MissionStatus.running,
            task_states={},
        )
        queue_id_1 = materializer.materialize(mission_id, task_def, mission_state_before)

        # Verify queue record exists
        assert temp_queue.pending.joinpath(f"{queue_id_1}.json").exists()

        # Simulate crash: mission state was NOT saved with the queue_task_id
        # On restart, mission_state still has no linkage
        mission_state_after_crash = MissionState(
            mission_id=mission_id,
            status=MissionStatus.running,
            task_states={},  # No state saved!
        )

        # Second call after restart: should find existing record, not duplicate
        queue_id_2 = materializer.materialize(mission_id, task_def, mission_state_after_crash)

        # Should return same ID
        assert queue_id_2 == queue_id_1

        # Should not create duplicate
        queue_files = list(temp_queue.pending.glob("*.json"))
        assert len(queue_files) == 1

    def test_idempotent_materialize_calls(
        self,
        materializer: TaskMaterializer,
        temp_queue: DurableQueue,
    ):
        """Repeated materialize calls return same ID without duplication."""
        mission_id = "mission-idempotent"
        task_id = "idempotent-task"

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

        # Call multiple times
        id1 = materializer.materialize(mission_id, task_def, mission_state)
        id2 = materializer.materialize(mission_id, task_def, mission_state)
        id3 = materializer.materialize(mission_id, task_def, mission_state)

        assert id1 == id2 == id3

        # Only one queue file should exist
        queue_files = list(temp_queue.pending.glob("*.json"))
        assert len(queue_files) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
