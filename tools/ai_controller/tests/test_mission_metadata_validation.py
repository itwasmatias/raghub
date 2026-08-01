"""
Tests for metadata validation during legacy queue record adoption.

Verifies that:
1. Legacy records with mismatched mission_id are rejected
2. Legacy records with mismatched mission_task_id are rejected
3. Legacy records with missing metadata are handled with defined policy
4. Legacy records with correct metadata are adopted safely
5. Metadata validation prevents silent data corruption
"""

from __future__ import annotations

from pathlib import Path

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
    return TaskMaterializer(temp_queue, tmp_path / "reports")


class TestLegacyMetadataValidation:
    """Test that legacy queue records are validated before adoption."""

    def test_legacy_record_with_mismatched_mission_id_rejected(
        self,
        materializer: TaskMaterializer,
        temp_queue: DurableQueue,
    ):
        """Legacy record with wrong mission_id should be rejected, not adopted."""
        mission_id = "mission-correct"
        task_id = "task-one"
        wrong_mission_id = "mission-wrong"

        # Create legacy queue record with WRONG mission_id in metadata
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": wrong_mission_id,  # WRONG!
                "mission_task_id": task_id,
            },
        )
        temp_queue.enqueue(legacy_task)

        # Attempt to materialize with correct mission_id
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

        # Should NOT adopt the legacy record (metadata mismatch)
        # Should create NEW current-format record instead
        result_id = materializer.materialize(mission_id, task_def, mission_state)

        # Should be current format, not legacy
        current_id = materializer.make_queue_task_id(mission_id, task_id)
        assert result_id == current_id, \
            f"Should create new record, not adopt mismatched legacy record"

        # New record should exist
        assert temp_queue.pending.joinpath(f"{current_id}.json").exists()

        # Legacy record should still exist but NOT be linked
        assert temp_queue.pending.joinpath(f"{legacy_queue_id}.json").exists()

    def test_legacy_record_with_mismatched_task_id_rejected(
        self,
        materializer: TaskMaterializer,
        temp_queue: DurableQueue,
    ):
        """Legacy record with wrong mission_task_id should be rejected."""
        mission_id = "mission-alpha"
        task_id = "task-correct"
        wrong_task_id = "task-wrong"

        # Create legacy queue record with WRONG task_id in metadata
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": mission_id,
                "mission_task_id": wrong_task_id,  # WRONG!
            },
        )
        temp_queue.enqueue(legacy_task)

        # Attempt to materialize with correct task_id
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

        # Should create new record, not adopt mismatched legacy
        current_id = materializer.make_queue_task_id(mission_id, task_id)
        assert result_id == current_id

    def test_legacy_record_with_missing_mission_id_metadata_rejected(
        self,
        materializer: TaskMaterializer,
        temp_queue: DurableQueue,
    ):
        """Legacy record with missing mission_id metadata should be rejected."""
        mission_id = "mission-beta"
        task_id = "task-two"

        # Create legacy queue record with NO mission_id in metadata
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                # mission_id is MISSING
                "mission_task_id": task_id,
            },
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

        # Should NOT adopt record with incomplete metadata
        current_id = materializer.make_queue_task_id(mission_id, task_id)
        assert result_id == current_id

    def test_legacy_record_with_missing_task_id_metadata_rejected(
        self,
        materializer: TaskMaterializer,
        temp_queue: DurableQueue,
    ):
        """Legacy record with missing mission_task_id metadata should be rejected."""
        mission_id = "mission-gamma"
        task_id = "task-three"

        # Create legacy queue record with NO mission_task_id in metadata
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": mission_id,
                # mission_task_id is MISSING
            },
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

        # Should NOT adopt record with incomplete metadata
        current_id = materializer.make_queue_task_id(mission_id, task_id)
        assert result_id == current_id

    def test_legacy_record_with_correct_metadata_is_adopted(
        self,
        materializer: TaskMaterializer,
        temp_queue: DurableQueue,
    ):
        """Legacy record with correct metadata should be adopted safely."""
        mission_id = "mission-delta"
        task_id = "task-four"

        # Create legacy queue record with CORRECT metadata
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": mission_id,  # Correct
                "mission_task_id": task_id,  # Correct
            },
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

        # Should adopt the legacy record (metadata matches)
        assert result_id == legacy_queue_id

        # Should NOT create a new current-format record
        current_id = materializer.make_queue_task_id(mission_id, task_id)
        if current_id != legacy_queue_id:
            assert not temp_queue.pending.joinpath(f"{current_id}.json").exists()

    def test_legacy_record_with_empty_metadata_rejected(
        self,
        materializer: TaskMaterializer,
        temp_queue: DurableQueue,
    ):
        """Legacy record with empty metadata dict should be rejected."""
        mission_id = "mission-epsilon"
        task_id = "task-five"

        # Create legacy queue record with EMPTY metadata
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

        # Should NOT adopt empty-metadata record
        current_id = materializer.make_queue_task_id(mission_id, task_id)
        assert result_id == current_id

    def test_validation_prevents_silent_corruption(
        self,
        materializer: TaskMaterializer,
        temp_queue: DurableQueue,
    ):
        """Metadata validation prevents silent linkage to wrong mission's work."""
        # Scenario: two different missions happen to have same truncated prefix
        # in legacy format, but different full IDs
        mission_a = "mission-project-a-feature-x"
        mission_b = "mission-project-b-feature-y"
        task_id = "implement"

        # Both might truncate to same legacy ID if prefix is identical
        legacy_id_a = make_legacy_queue_task_id(mission_a, task_id)
        legacy_id_b = make_legacy_queue_task_id(mission_b, task_id)

        # Create legacy record for mission A
        legacy_task_a = Task(
            id=legacy_id_a,
            title="Task A",
            prompt="Work A",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": mission_a,
                "mission_task_id": task_id,
            },
        )
        temp_queue.enqueue(legacy_task_a)

        # Attempt to materialize for mission B
        task_def_b = MissionTaskDefinition(
            task_id=task_id,
            title="Task B",
            prompt="Work B",
        )
        mission_state_b = MissionState(
            mission_id=mission_b,
            status=MissionStatus.running,
            task_states={},
        )

        result_id = materializer.materialize(mission_b, task_def_b, mission_state_b)

        # If legacy IDs are different, this is not a collision test
        # If they're the same, validation MUST prevent adoption
        if legacy_id_a == legacy_id_b:
            # Metadata validation MUST reject this
            current_id_b = materializer.make_queue_task_id(mission_b, task_id)
            assert result_id == current_id_b, \
                "Must not adopt mission A's record for mission B"
        else:
            # Different legacy IDs - just verify no cross-contamination
            assert result_id != legacy_id_a


class TestCurrentFormatMetadataPreservation:
    """Test that current-format records preserve metadata correctly."""

    def test_new_record_includes_mission_metadata(
        self,
        materializer: TaskMaterializer,
        temp_queue: DurableQueue,
    ):
        """Newly created queue records must include mission identity metadata."""
        mission_id = "mission-zeta"
        task_id = "task-metadata"

        task_def = MissionTaskDefinition(
            task_id=task_id,
            title="Task",
            prompt="Work",
            metadata={"priority": "high"},
        )
        mission_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.running,
            task_states={},
        )

        queue_id = materializer.materialize(mission_id, task_def, mission_state)

        # Read the queue file
        queue_file = temp_queue.pending / f"{queue_id}.json"
        assert queue_file.exists()

        import json
        task_data = json.loads(queue_file.read_text())

        # Verify mission identity metadata
        assert task_data["metadata"]["mission_id"] == mission_id
        assert task_data["metadata"]["mission_task_id"] == task_id

        # Verify task-level metadata is also preserved
        assert task_data["metadata"]["priority"] == "high"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
