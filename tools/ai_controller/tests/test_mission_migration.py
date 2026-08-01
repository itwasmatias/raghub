"""
Tests for mission queue record migration.

Tests cover:
1. Scan-only mode (read-only discovery)
2. Plan mode (deterministic action proposal)
3. Apply mode (safe mutation)
4. Classification scenarios
5. Failure injection and crash recovery
6. Migration idempotency
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.ai_controller.mission import make_legacy_queue_task_id
from tools.ai_controller.mission.models import (
    MissionDefinition,
    MissionState,
    MissionStatus,
    MissionTaskDefinition,
    MissionTaskState,
    MissionTaskStatus,
)
from tools.ai_controller.mission.store import MissionStore
from tools.ai_controller.models import Task
from tools.ai_controller.queue import DurableQueue


@pytest.fixture
def temp_missions_root(tmp_path: Path) -> Path:
    return tmp_path / "missions"


@pytest.fixture
def temp_queue_root(tmp_path: Path) -> Path:
    return tmp_path / "queue"


@pytest.fixture
def mission_store(temp_missions_root: Path) -> MissionStore:
    return MissionStore(temp_missions_root)


@pytest.fixture
def queue(temp_queue_root: Path) -> DurableQueue:
    return DurableQueue(temp_queue_root)


class TestMigrationScan:
    """Test migration scan mode (read-only discovery)."""

    def test_scan_detects_legacy_only_record(
        self,
        mission_store: MissionStore,
        queue: DurableQueue,
        temp_missions_root: Path,
    ):
        """Scan should detect legacy-only queue record with no mission linkage."""
        from tools.ai_controller.mission.migration import scan_mission_migration

        # Create a simple mission
        mission_id = "mission-scan-1"
        task_id = "task-legacy"

        definition = MissionDefinition(
            mission_id=mission_id,
            title="Test Mission",
            tasks=[
                MissionTaskDefinition(
                    task_id=task_id,
                    title="Task",
                    prompt="Work",
                )
            ],
        )

        initial_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.pending,
            task_states={
                task_id: MissionTaskState(
                    task_id=task_id,
                    status=MissionTaskStatus.pending,
                    queue_task_id=None,  # No linkage!
                )
            },
        )

        mission_store.create(definition, initial_state)

        # Create legacy queue record
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": mission_id,
                "mission_task_id": task_id,
            },
        )
        queue.enqueue(legacy_task)

        # Scan
        results = scan_mission_migration(
            mission_id=mission_id,
            mission_store=mission_store,
            queue=queue,
        )

        # Should detect legacy-only record
        assert len(results) == 1
        result = results[0]

        assert result["mission_id"] == mission_id
        assert result["task_id"] == task_id
        assert result["classification"] == "legacy_only"
        assert result["legacy_queue_id"] == legacy_queue_id
        assert result["current_queue_id"] is not None
        assert result["persisted_linkage"] is None
        assert result["metadata_valid"] is True

    def test_scan_detects_current_only_record(
        self,
        mission_store: MissionStore,
        queue: DurableQueue,
    ):
        """Scan should detect current-format-only queue record."""
        from tools.ai_controller.mission.migration import scan_mission_migration
        from tools.ai_controller.mission.materializer import TaskMaterializer

        mission_id = "mission-scan-2"
        task_id = "task-current"

        definition = MissionDefinition(
            mission_id=mission_id,
            title="Test Mission",
            tasks=[
                MissionTaskDefinition(
                    task_id=task_id,
                    title="Task",
                    prompt="Work",
                )
            ],
        )

        initial_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.pending,
            task_states={},
        )

        mission_store.create(definition, initial_state)

        # Create current-format queue record
        materializer = TaskMaterializer(queue, Path("/tmp/reports"))
        state = mission_store.load_state(mission_id)
        task_def = definition.tasks[0]

        current_queue_id = materializer.materialize(mission_id, task_def, state)

        # Scan
        results = scan_mission_migration(
            mission_id=mission_id,
            mission_store=mission_store,
            queue=queue,
        )

        # Should detect current-only record
        assert len(results) == 1
        result = results[0]

        assert result["classification"] == "current_only"
        assert result["current_queue_id"] == current_queue_id
        assert result["legacy_exists"] is False

    def test_scan_is_read_only(
        self,
        mission_store: MissionStore,
        queue: DurableQueue,
    ):
        """Scan must not modify mission state or queue records."""
        from tools.ai_controller.mission.migration import scan_mission_migration

        mission_id = "mission-scan-3"
        task_id = "task-readonly"

        definition = MissionDefinition(
            mission_id=mission_id,
            title="Test Mission",
            tasks=[
                MissionTaskDefinition(
                    task_id=task_id,
                    title="Task",
                    prompt="Work",
                )
            ],
        )

        initial_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.pending,
            task_states={
                task_id: MissionTaskState(
                    task_id=task_id,
                    status=MissionTaskStatus.pending,
                    queue_task_id=None,
                )
            },
        )

        mission_store.create(definition, initial_state)

        # Create legacy record
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": mission_id,
                "mission_task_id": task_id,
            },
        )
        queue.enqueue(legacy_task)

        # Scan
        results_1 = scan_mission_migration(
            mission_id=mission_id,
            mission_store=mission_store,
            queue=queue,
        )

        # Verify state unchanged
        state_after = mission_store.load_state(mission_id)
        assert state_after.task_states[task_id].queue_task_id is None

        # Scan again - should be identical
        results_2 = scan_mission_migration(
            mission_id=mission_id,
            mission_store=mission_store,
            queue=queue,
        )

        assert results_1 == results_2

    def test_scan_detects_both_formats(
        self,
        mission_store: MissionStore,
        queue: DurableQueue,
    ):
        """Scan should detect when both legacy and current records exist."""
        from tools.ai_controller.mission.migration import scan_mission_migration
        from tools.ai_controller.mission.materializer import TaskMaterializer

        mission_id = "mission-scan-4"
        task_id = "task-both"

        definition = MissionDefinition(
            mission_id=mission_id,
            title="Test Mission",
            tasks=[
                MissionTaskDefinition(
                    task_id=task_id,
                    title="Task",
                    prompt="Work",
                )
            ],
        )

        initial_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.pending,
            task_states={},
        )

        mission_store.create(definition, initial_state)

        # Create both legacy and current records DIRECTLY (bypass materializer)
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": mission_id,
                "mission_task_id": task_id,
            },
        )
        queue.enqueue(legacy_task)

        # Create current-format record directly
        materializer = TaskMaterializer(queue, Path("/tmp/reports"))
        current_queue_id = materializer.make_queue_task_id(mission_id, task_id)
        current_task = Task(
            id=current_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": mission_id,
                "mission_task_id": task_id,
            },
        )
        queue.enqueue(current_task)

        # Scan
        results = scan_mission_migration(
            mission_id=mission_id,
            mission_store=mission_store,
            queue=queue,
        )

        assert len(results) == 1
        result = results[0]

        assert result["classification"] == "both_formats"
        assert result["legacy_exists"] is True
        assert result["current_exists"] is True
        assert result["legacy_queue_id"] == legacy_queue_id
        assert result["current_queue_id"] == current_queue_id

    def test_scan_detects_metadata_conflict(
        self,
        mission_store: MissionStore,
        queue: DurableQueue,
    ):
        """Scan should detect legacy record with mismatched metadata."""
        from tools.ai_controller.mission.migration import scan_mission_migration

        mission_id = "mission-scan-5"
        task_id = "task-conflict"

        definition = MissionDefinition(
            mission_id=mission_id,
            title="Test Mission",
            tasks=[
                MissionTaskDefinition(
                    task_id=task_id,
                    title="Task",
                    prompt="Work",
                )
            ],
        )

        initial_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.pending,
            task_states={},
        )

        mission_store.create(definition, initial_state)

        # Create legacy record with WRONG metadata
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": "different-mission",  # Wrong!
                "mission_task_id": task_id,
            },
        )
        queue.enqueue(legacy_task)

        # Scan
        results = scan_mission_migration(
            mission_id=mission_id,
            mission_store=mission_store,
            queue=queue,
        )

        assert len(results) == 1
        result = results[0]

        assert result["classification"] == "metadata_conflict"
        assert result["metadata_valid"] is False
        assert result["legacy_exists"] is True

    def test_scan_detects_persisted_legacy_link(
        self,
        mission_store: MissionStore,
        queue: DurableQueue,
    ):
        """Scan should detect when mission state already links to legacy record."""
        from tools.ai_controller.mission.migration import scan_mission_migration

        mission_id = "mission-scan-6"
        task_id = "task-linked"

        definition = MissionDefinition(
            mission_id=mission_id,
            title="Test Mission",
            tasks=[
                MissionTaskDefinition(
                    task_id=task_id,
                    title="Task",
                    prompt="Work",
                )
            ],
        )

        # Create legacy queue record
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": mission_id,
                "mission_task_id": task_id,
            },
        )
        queue.enqueue(legacy_task)

        # State already has linkage
        initial_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.running,
            task_states={
                task_id: MissionTaskState(
                    task_id=task_id,
                    status=MissionTaskStatus.running,
                    queue_task_id=legacy_queue_id,  # Already linked!
                )
            },
        )

        mission_store.create(definition, initial_state)

        # Scan
        results = scan_mission_migration(
            mission_id=mission_id,
            mission_store=mission_store,
            queue=queue,
        )

        assert len(results) == 1
        result = results[0]

        assert result["classification"] == "persisted_legacy_link"
        assert result["persisted_linkage"] == legacy_queue_id


class TestMigrationPlan:
    """Test migration plan mode (deterministic action proposal)."""

    def test_plan_proposes_linkage_for_legacy_only(
        self,
        mission_store: MissionStore,
        queue: DurableQueue,
    ):
        """Plan should propose linking mission state to valid legacy record."""
        from tools.ai_controller.mission.migration import (
            scan_mission_migration,
            plan_mission_migration,
        )

        mission_id = "mission-plan-1"
        task_id = "task-legacy"

        definition = MissionDefinition(
            mission_id=mission_id,
            title="Test Mission",
            tasks=[
                MissionTaskDefinition(
                    task_id=task_id,
                    title="Task",
                    prompt="Work",
                )
            ],
        )

        initial_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.pending,
            task_states={
                task_id: MissionTaskState(
                    task_id=task_id,
                    status=MissionTaskStatus.pending,
                    queue_task_id=None,  # No linkage
                )
            },
        )

        mission_store.create(definition, initial_state)

        # Create legacy queue record
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": mission_id,
                "mission_task_id": task_id,
            },
        )
        queue.enqueue(legacy_task)

        # Scan
        scan_results = scan_mission_migration(
            mission_id=mission_id,
            mission_store=mission_store,
            queue=queue,
        )

        # Plan
        plan = plan_mission_migration(scan_results)

        assert len(plan) == 1
        action = plan[0]

        assert action["action"] == "link_mission_state"
        assert action["task_id"] == task_id
        assert action["queue_task_id"] == legacy_queue_id
        assert action["reason"] == "Valid legacy queue record exists without mission linkage"

    def test_plan_proposes_no_action_for_current_only(
        self,
        mission_store: MissionStore,
        queue: DurableQueue,
    ):
        """Plan should propose no action for current-format records (already correct)."""
        from tools.ai_controller.mission.migration import (
            scan_mission_migration,
            plan_mission_migration,
        )
        from tools.ai_controller.mission.materializer import TaskMaterializer

        mission_id = "mission-plan-2"
        task_id = "task-current"

        definition = MissionDefinition(
            mission_id=mission_id,
            title="Test Mission",
            tasks=[
                MissionTaskDefinition(
                    task_id=task_id,
                    title="Task",
                    prompt="Work",
                )
            ],
        )

        initial_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.pending,
            task_states={},
        )

        mission_store.create(definition, initial_state)

        # Create current-format record
        materializer = TaskMaterializer(queue, Path("/tmp/reports"))
        materializer.materialize(mission_id, definition.tasks[0], initial_state)

        # Scan
        scan_results = scan_mission_migration(
            mission_id=mission_id,
            mission_store=mission_store,
            queue=queue,
        )

        # Plan
        plan = plan_mission_migration(scan_results)

        # No action needed for current-format records
        assert len(plan) == 0

    def test_plan_proposes_deletion_for_both_formats(
        self,
        mission_store: MissionStore,
        queue: DurableQueue,
    ):
        """Plan should propose deleting legacy record when both formats exist."""
        from tools.ai_controller.mission.migration import (
            scan_mission_migration,
            plan_mission_migration,
        )
        from tools.ai_controller.mission.materializer import TaskMaterializer

        mission_id = "mission-plan-3"
        task_id = "task-both"

        definition = MissionDefinition(
            mission_id=mission_id,
            title="Test Mission",
            tasks=[
                MissionTaskDefinition(
                    task_id=task_id,
                    title="Task",
                    prompt="Work",
                )
            ],
        )

        initial_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.pending,
            task_states={},
        )

        mission_store.create(definition, initial_state)

        # Create both records
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": mission_id,
                "mission_task_id": task_id,
            },
        )
        queue.enqueue(legacy_task)

        materializer = TaskMaterializer(queue, Path("/tmp/reports"))
        current_queue_id = materializer.make_queue_task_id(mission_id, task_id)
        current_task = Task(
            id=current_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": mission_id,
                "mission_task_id": task_id,
            },
        )
        queue.enqueue(current_task)

        # Scan
        scan_results = scan_mission_migration(
            mission_id=mission_id,
            mission_store=mission_store,
            queue=queue,
        )

        # Plan
        plan = plan_mission_migration(scan_results)

        assert len(plan) == 1
        action = plan[0]

        assert action["action"] == "delete_legacy_record"
        assert action["task_id"] == task_id
        assert action["legacy_queue_id"] == legacy_queue_id
        assert "duplicate" in action["reason"].lower()

    def test_plan_is_deterministic(
        self,
        mission_store: MissionStore,
        queue: DurableQueue,
    ):
        """Plan should return identical results for identical scans."""
        from tools.ai_controller.mission.migration import (
            scan_mission_migration,
            plan_mission_migration,
        )

        mission_id = "mission-plan-4"
        task_id = "task-deterministic"

        definition = MissionDefinition(
            mission_id=mission_id,
            title="Test Mission",
            tasks=[
                MissionTaskDefinition(
                    task_id=task_id,
                    title="Task",
                    prompt="Work",
                )
            ],
        )

        initial_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.pending,
            task_states={},
        )

        mission_store.create(definition, initial_state)

        # Create legacy record
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": mission_id,
                "mission_task_id": task_id,
            },
        )
        queue.enqueue(legacy_task)

        # Scan twice
        scan_1 = scan_mission_migration(mission_id, mission_store, queue)
        scan_2 = scan_mission_migration(mission_id, mission_store, queue)

        # Plan twice
        plan_1 = plan_mission_migration(scan_1)
        plan_2 = plan_mission_migration(scan_2)

        # Plans should be identical
        assert plan_1 == plan_2


class TestMigrationApply:
    """Test migration apply mode (safe mutation)."""

    def test_apply_links_mission_state_for_legacy_only(
        self,
        mission_store: MissionStore,
        queue: DurableQueue,
    ):
        """Apply should link mission state to valid legacy record."""
        from tools.ai_controller.mission.migration import (
            scan_mission_migration,
            plan_mission_migration,
            apply_mission_migration,
        )

        mission_id = "mission-apply-1"
        task_id = "task-legacy"

        definition = MissionDefinition(
            mission_id=mission_id,
            title="Test Mission",
            tasks=[
                MissionTaskDefinition(
                    task_id=task_id,
                    title="Task",
                    prompt="Work",
                )
            ],
        )

        initial_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.pending,
            task_states={
                task_id: MissionTaskState(
                    task_id=task_id,
                    status=MissionTaskStatus.pending,
                    queue_task_id=None,  # No linkage
                )
            },
        )

        mission_store.create(definition, initial_state)

        # Create legacy queue record
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": mission_id,
                "mission_task_id": task_id,
            },
        )
        queue.enqueue(legacy_task)

        # Scan and plan
        scan_results = scan_mission_migration(mission_id, mission_store, queue)
        plan = plan_mission_migration(scan_results)

        # Apply
        apply_result = apply_mission_migration(
            mission_id=mission_id,
            plan=plan,
            mission_store=mission_store,
            queue=queue,
        )

        assert apply_result["success"] is True
        assert apply_result["actions_applied"] == 1

        # Verify mission state was updated
        updated_state = mission_store.load_state(mission_id)
        assert updated_state.task_states[task_id].queue_task_id == legacy_queue_id

    def test_apply_deletes_legacy_record_for_both_formats(
        self,
        mission_store: MissionStore,
        queue: DurableQueue,
    ):
        """Apply should delete legacy record when both formats exist."""
        from tools.ai_controller.mission.migration import (
            scan_mission_migration,
            plan_mission_migration,
            apply_mission_migration,
        )
        from tools.ai_controller.mission.materializer import TaskMaterializer

        mission_id = "mission-apply-2"
        task_id = "task-both"

        definition = MissionDefinition(
            mission_id=mission_id,
            title="Test Mission",
            tasks=[
                MissionTaskDefinition(
                    task_id=task_id,
                    title="Task",
                    prompt="Work",
                )
            ],
        )

        initial_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.pending,
            task_states={},
        )

        mission_store.create(definition, initial_state)

        # Create both records
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": mission_id,
                "mission_task_id": task_id,
            },
        )
        queue.enqueue(legacy_task)

        materializer = TaskMaterializer(queue, Path("/tmp/reports"))
        current_queue_id = materializer.make_queue_task_id(mission_id, task_id)
        current_task = Task(
            id=current_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": mission_id,
                "mission_task_id": task_id,
            },
        )
        queue.enqueue(current_task)

        # Verify both exist before apply
        assert materializer.queue_task_exists_in(legacy_queue_id) == "pending"
        assert materializer.queue_task_exists_in(current_queue_id) == "pending"

        # Scan and plan
        scan_results = scan_mission_migration(mission_id, mission_store, queue)
        plan = plan_mission_migration(scan_results)

        # Apply
        apply_result = apply_mission_migration(
            mission_id=mission_id,
            plan=plan,
            mission_store=mission_store,
            queue=queue,
        )

        assert apply_result["success"] is True
        assert apply_result["actions_applied"] == 1

        # Verify legacy record was deleted
        assert materializer.queue_task_exists_in(legacy_queue_id) is None
        # Current record should still exist
        assert materializer.queue_task_exists_in(current_queue_id) == "pending"

    def test_apply_is_idempotent(
        self,
        mission_store: MissionStore,
        queue: DurableQueue,
    ):
        """Apply should be safe to run multiple times."""
        from tools.ai_controller.mission.migration import (
            scan_mission_migration,
            plan_mission_migration,
            apply_mission_migration,
        )

        mission_id = "mission-apply-3"
        task_id = "task-idempotent"

        definition = MissionDefinition(
            mission_id=mission_id,
            title="Test Mission",
            tasks=[
                MissionTaskDefinition(
                    task_id=task_id,
                    title="Task",
                    prompt="Work",
                )
            ],
        )

        initial_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.pending,
            task_states={
                task_id: MissionTaskState(
                    task_id=task_id,
                    status=MissionTaskStatus.pending,
                    queue_task_id=None,
                )
            },
        )

        mission_store.create(definition, initial_state)

        # Create legacy record
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": mission_id,
                "mission_task_id": task_id,
            },
        )
        queue.enqueue(legacy_task)

        # First apply
        scan_1 = scan_mission_migration(mission_id, mission_store, queue)
        plan_1 = plan_mission_migration(scan_1)
        result_1 = apply_mission_migration(mission_id, plan_1, mission_store, queue)

        assert result_1["success"] is True
        assert result_1["actions_applied"] == 1

        # Second apply - should be no-op
        scan_2 = scan_mission_migration(mission_id, mission_store, queue)
        plan_2 = plan_mission_migration(scan_2)
        result_2 = apply_mission_migration(mission_id, plan_2, mission_store, queue)

        # No actions to apply
        assert result_2["success"] is True
        assert result_2["actions_applied"] == 0

    def test_apply_skips_warnings(
        self,
        mission_store: MissionStore,
        queue: DurableQueue,
    ):
        """Apply should not perform destructive actions for warnings."""
        from tools.ai_controller.mission.migration import (
            scan_mission_migration,
            plan_mission_migration,
            apply_mission_migration,
        )

        mission_id = "mission-apply-4"
        task_id = "task-conflict"

        definition = MissionDefinition(
            mission_id=mission_id,
            title="Test Mission",
            tasks=[
                MissionTaskDefinition(
                    task_id=task_id,
                    title="Task",
                    prompt="Work",
                )
            ],
        )

        initial_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.pending,
            task_states={},
        )

        mission_store.create(definition, initial_state)

        # Create legacy record with WRONG metadata
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": "different-mission",  # Wrong!
                "mission_task_id": task_id,
            },
        )
        queue.enqueue(legacy_task)

        # Scan and plan
        scan_results = scan_mission_migration(mission_id, mission_store, queue)
        plan = plan_mission_migration(scan_results)

        # Apply should not perform any destructive actions
        apply_result = apply_mission_migration(
            mission_id=mission_id,
            plan=plan,
            mission_store=mission_store,
            queue=queue,
        )

        assert apply_result["success"] is True
        # Warnings don't count as applied actions
        assert apply_result["actions_applied"] == 0
        assert len(apply_result["warnings"]) > 0


class TestMigrationFailureInjection:
    """Test migration failure injection and crash recovery."""

    def test_apply_handles_missing_mission(
        self,
        mission_store: MissionStore,
        queue: DurableQueue,
    ):
        """Apply should handle missing mission gracefully."""
        from tools.ai_controller.mission.migration import apply_mission_migration

        # Attempt to apply to non-existent mission
        result = apply_mission_migration(
            mission_id="nonexistent-mission",
            plan=[],
            mission_store=mission_store,
            queue=queue,
        )

        assert result["success"] is False
        assert len(result["errors"]) > 0
        assert "not found" in result["errors"][0].lower()

    def test_apply_handles_missing_task_state(
        self,
        mission_store: MissionStore,
        queue: DurableQueue,
    ):
        """Apply should handle missing task_state gracefully."""
        from tools.ai_controller.mission.migration import apply_mission_migration

        mission_id = "mission-fail-1"

        # Create mission with NO task_states
        definition = MissionDefinition(
            mission_id=mission_id,
            title="Test Mission",
            tasks=[
                MissionTaskDefinition(
                    task_id="task-missing",
                    title="Task",
                    prompt="Work",
                )
            ],
        )

        initial_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.pending,
            task_states={},  # Empty!
        )

        mission_store.create(definition, initial_state)

        # Try to apply link action for missing task_state
        plan = [
            {
                "action": "link_mission_state",
                "mission_id": mission_id,
                "task_id": "task-missing",
                "queue_task_id": "some-queue-id",
                "reason": "Test",
            }
        ]

        result = apply_mission_migration(mission_id, plan, mission_store, queue)

        # Should fail gracefully
        assert result["success"] is False
        assert len(result["errors"]) > 0
        assert "task state not found" in result["errors"][0].lower()

    def test_apply_handles_filesystem_errors_during_delete(
        self,
        mission_store: MissionStore,
        queue: DurableQueue,
    ):
        """Apply should handle filesystem errors during deletion."""
        from tools.ai_controller.mission.migration import apply_mission_migration

        mission_id = "mission-fail-2"
        task_id = "task-delete-fail"

        definition = MissionDefinition(
            mission_id=mission_id,
            title="Test Mission",
            tasks=[
                MissionTaskDefinition(
                    task_id=task_id,
                    title="Task",
                    prompt="Work",
                )
            ],
        )

        initial_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.pending,
            task_states={},
        )

        mission_store.create(definition, initial_state)

        # Plan to delete non-existent legacy record
        plan = [
            {
                "action": "delete_legacy_record",
                "mission_id": mission_id,
                "task_id": task_id,
                "legacy_queue_id": "nonexistent-queue-id",
                "reason": "Test deletion of missing file",
            }
        ]

        result = apply_mission_migration(mission_id, plan, mission_store, queue)

        # Should succeed (deletion is idempotent - already deleted)
        assert result["success"] is True
        assert result["actions_applied"] == 0  # Nothing to delete

    def test_scan_handles_corrupted_queue_metadata(
        self,
        mission_store: MissionStore,
        queue: DurableQueue,
    ):
        """Scan should handle corrupted queue record metadata gracefully."""
        from tools.ai_controller.mission.migration import scan_mission_migration

        mission_id = "mission-fail-3"
        task_id = "task-corrupt"

        definition = MissionDefinition(
            mission_id=mission_id,
            title="Test Mission",
            tasks=[
                MissionTaskDefinition(
                    task_id=task_id,
                    title="Task",
                    prompt="Work",
                )
            ],
        )

        initial_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.pending,
            task_states={},
        )

        mission_store.create(definition, initial_state)

        # Create legacy record with corrupted JSON (directly write invalid file)
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        queue.pending.mkdir(parents=True, exist_ok=True)
        legacy_file = queue.pending / f"{legacy_queue_id}.json"
        legacy_file.write_text("{INVALID JSON", encoding="utf-8")

        # Scan should not crash
        results = scan_mission_migration(mission_id, mission_store, queue)

        # Should detect the file but mark metadata as invalid
        assert len(results) == 1
        result = results[0]
        assert result["classification"] == "metadata_conflict"
        assert result["metadata_valid"] is False

    def test_scan_handles_missing_mission(
        self,
        mission_store: MissionStore,
        queue: DurableQueue,
    ):
        """Scan should return empty list for missing mission."""
        from tools.ai_controller.mission.migration import scan_mission_migration

        results = scan_mission_migration("nonexistent-mission", mission_store, queue)

        assert results == []

    def test_plan_handles_empty_scan_results(
        self,
    ):
        """Plan should return empty list for empty scan results."""
        from tools.ai_controller.mission.migration import plan_mission_migration

        plan = plan_mission_migration([])

        assert plan == []

    def test_apply_handles_empty_plan(
        self,
        mission_store: MissionStore,
        queue: DurableQueue,
    ):
        """Apply should handle empty plan gracefully."""
        from tools.ai_controller.mission.migration import apply_mission_migration

        mission_id = "mission-fail-4"

        definition = MissionDefinition(
            mission_id=mission_id,
            title="Test Mission",
            tasks=[],
        )

        initial_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.pending,
            task_states={},
        )

        mission_store.create(definition, initial_state)

        # Apply empty plan
        result = apply_mission_migration(mission_id, [], mission_store, queue)

        assert result["success"] is True
        assert result["actions_applied"] == 0
        assert len(result["errors"]) == 0

    def test_apply_continues_after_partial_failure(
        self,
        mission_store: MissionStore,
        queue: DurableQueue,
    ):
        """Apply should continue processing actions after encountering an error."""
        from tools.ai_controller.mission.migration import apply_mission_migration

        mission_id = "mission-fail-5"
        task_id_1 = "task-fail"
        task_id_2 = "task-success"

        definition = MissionDefinition(
            mission_id=mission_id,
            title="Test Mission",
            tasks=[
                MissionTaskDefinition(task_id=task_id_1, title="T1", prompt="W1"),
                MissionTaskDefinition(task_id=task_id_2, title="T2", prompt="W2"),
            ],
        )

        initial_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.pending,
            task_states={
                task_id_2: MissionTaskState(
                    task_id=task_id_2,
                    status=MissionTaskStatus.pending,
                    queue_task_id=None,
                ),
            },
        )

        mission_store.create(definition, initial_state)

        # Create legacy record for task_id_2
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id_2)
        legacy_task = Task(
            id=legacy_queue_id,
            title="T2",
            prompt="W2",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": mission_id,
                "mission_task_id": task_id_2,
            },
        )
        queue.enqueue(legacy_task)

        # Plan with one failing action and one succeeding action
        plan = [
            {
                "action": "link_mission_state",
                "mission_id": mission_id,
                "task_id": task_id_1,  # This will fail (no task_state)
                "queue_task_id": "fail-id",
                "reason": "Will fail",
            },
            {
                "action": "link_mission_state",
                "mission_id": mission_id,
                "task_id": task_id_2,  # This will succeed
                "queue_task_id": legacy_queue_id,
                "reason": "Will succeed",
            },
        ]

        result = apply_mission_migration(mission_id, plan, mission_store, queue)

        # Should have errors but also successes
        assert result["success"] is False  # At least one error
        assert result["actions_applied"] == 1  # Second action succeeded
        assert len(result["errors"]) == 1  # First action failed

        # Verify second action was applied
        updated_state = mission_store.load_state(mission_id)
        assert updated_state.task_states[task_id_2].queue_task_id == legacy_queue_id


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
