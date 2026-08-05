"""
Tests for reconciliation event emission.

Verifies that:
1. Legacy record adoption emits an event
2. Legacy record rejection (metadata mismatch) emits an event
3. Both-formats conflict detection emits an event
4. New queue record creation emits an event
5. Reconciliation classifications emit appropriate events
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from tools.ai_controller.mission.materializer import TaskMaterializer
from tools.ai_controller.mission.events import (
    EventLogCorruptionError,
    MissionEventLog,
    make_event,
)
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


@pytest.fixture
def event_log(tmp_path: Path) -> MissionEventLog:
    def _log(mission_id: str) -> MissionEventLog:
        return MissionEventLog(tmp_path / "events", mission_id)
    return _log


class TestMaterializationEvents:
    """Test that materialize() emits appropriate events."""

    def test_legacy_adoption_emits_event(
        self,
        materializer: TaskMaterializer,
        temp_queue: DurableQueue,
        event_log,
    ):
        """Adopting a legacy record should emit a legacy_adopted event."""
        mission_id = "mission-alpha"
        task_id = "task-one"

        # Create valid legacy record
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
        temp_queue.enqueue(legacy_task)

        # Materialize (should adopt)
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
        assert result_id == legacy_queue_id

        # Check event was emitted
        log = event_log(mission_id)
        events = log.read_all()

        adoption_events = [e for e in events if e.event_type == "legacy_adopted"]
        assert len(adoption_events) == 1

        event = adoption_events[0]
        assert event.mission_id == mission_id
        assert event.task_id == task_id
        assert event.queue_task_id == legacy_queue_id

    def test_legacy_rejection_emits_event(
        self,
        materializer: TaskMaterializer,
        temp_queue: DurableQueue,
        event_log,
    ):
        """Rejecting a legacy record (metadata mismatch) should emit legacy_rejected event."""
        mission_id = "mission-beta"
        task_id = "task-two"

        # Create INVALID legacy record (wrong metadata)
        legacy_queue_id = make_legacy_queue_task_id(mission_id, task_id)
        legacy_task = Task(
            id=legacy_queue_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": "wrong-mission",  # WRONG
                "mission_task_id": task_id,
            },
        )
        temp_queue.enqueue(legacy_task)

        # Materialize (should reject and create new)
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
        current_id = materializer.make_queue_task_id(mission_id, task_id)
        assert result_id == current_id  # New record, not legacy

        # Check rejection event was emitted
        log = event_log(mission_id)
        events = log.read_all()

        rejection_events = [e for e in events if e.event_type == "legacy_rejected"]
        assert len(rejection_events) == 1

        event = rejection_events[0]
        assert event.mission_id == mission_id
        assert event.task_id == task_id
        assert event.metadata.get("legacy_queue_id") == legacy_queue_id
        assert "metadata" in event.reason.lower() or "mismatch" in event.reason.lower()

    def test_both_formats_conflict_emits_event(
        self,
        materializer: TaskMaterializer,
        temp_queue: DurableQueue,
        event_log,
    ):
        """When both legacy and current records exist, emit conflict event."""
        mission_id = "mission-gamma"
        task_id = "task-conflict"

        # Create both legacy and current records
        legacy_id = make_legacy_queue_task_id(mission_id, task_id)
        current_id = materializer.make_queue_task_id(mission_id, task_id)

        legacy_task = Task(
            id=legacy_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": mission_id,
                "mission_task_id": task_id,
            },
        )
        current_task = Task(
            id=current_id,
            title="Task",
            prompt="Work",
            base_ref="HEAD",
            tests=[],
            metadata={
                "mission_id": mission_id,
                "mission_task_id": task_id,
            },
        )

        temp_queue.enqueue(legacy_task)
        temp_queue.enqueue(current_task)

        # Materialize
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

        materializer.materialize(mission_id, task_def, mission_state)

        # Check conflict event was emitted
        log = event_log(mission_id)
        events = log.read_all()

        conflict_events = [e for e in events if e.event_type == "both_formats_found"]
        assert len(conflict_events) == 1

        event = conflict_events[0]
        assert event.mission_id == mission_id
        assert event.task_id == task_id
        assert event.metadata.get("current_queue_id") == current_id
        assert event.metadata.get("legacy_queue_id") == legacy_id

    def test_new_record_creation_emits_event(
        self,
        materializer: TaskMaterializer,
        event_log,
    ):
        """Creating a new queue record should emit queue_created event."""
        mission_id = "mission-delta"
        task_id = "task-new"

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

        # Check creation event was emitted
        log = event_log(mission_id)
        events = log.read_all()

        creation_events = [e for e in events if e.event_type == "queue_created"]
        assert len(creation_events) == 1

        event = creation_events[0]
        assert event.mission_id == mission_id
        assert event.task_id == task_id
        assert event.queue_task_id == queue_id


class TestReconciliationEvents:
    """Test that reconcile_task() emits appropriate events."""

    def test_eligible_missing_classification_emits_event(
        self,
        materializer: TaskMaterializer,
        event_log,
    ):
        """Missing queue record eligible for recreation should emit event."""
        mission_id = "mission-epsilon"
        task_id = "task-missing"
        queue_id = materializer.make_queue_task_id(mission_id, task_id)

        # Reconcile a missing task
        info = materializer.reconcile_task(
            mission_id,
            task_id,
            queue_id,
            Path("/tmp/reports"),
        )

        assert info["classification"] == "eligible_missing"

        # Check event was emitted
        log = event_log(mission_id)
        events = log.read_all()

        reconcile_events = [e for e in events if e.event_type == "reconcile_eligible_missing"]
        assert len(reconcile_events) == 1

        event = reconcile_events[0]
        assert event.mission_id == mission_id
        assert event.task_id == task_id
        assert event.queue_task_id == queue_id
        assert event.metadata.get("classification") == "eligible_missing"
    def test_paused_classification_emits_event(
        self,
        materializer: TaskMaterializer,
        event_log,
    ):
        """Paused mission classification should emit event."""
        mission_id = "mission-zeta"
        task_id = "task-paused"
        queue_id = materializer.make_queue_task_id(mission_id, task_id)

        mission_state = MissionState(
            mission_id=mission_id,
            status=MissionStatus.paused,
            task_states={},
        )
        task_state = MissionTaskState(
            task_id=task_id,
            status=MissionTaskStatus.queued,
            queue_task_id=queue_id,
        )

        info = materializer.reconcile_task(
            mission_id,
            task_id,
            queue_id,
            Path("/tmp/reports"),
            mission_state=mission_state,
            task_state=task_state,
        )

        assert info["classification"] == "paused"

        # Check event was emitted
        log = event_log(mission_id)
        events = log.read_all()

        reconcile_events = [e for e in events if e.event_type == "reconcile_paused"]
        assert len(reconcile_events) == 1


def test_append_rejects_partial_tail_after_restart(tmp_path: Path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    path = events_dir / "restart.jsonl"
    path.write_bytes(b'{"event_type":"task_ready"}\n{"event_type":')
    log = MissionEventLog(events_dir, "restart")

    with pytest.raises(EventLogCorruptionError):
        log.append(
            make_event("task_enqueued", "restart", task_id="task-a")
        )

    assert path.read_bytes().endswith(b'{"event_type":')


def test_append_locked_is_bound_to_instance_thread_and_active_scope(
    tmp_path: Path,
):
    first = MissionEventLog(tmp_path / "events", "first")
    second = MissionEventLog(tmp_path / "events", "second")
    foreign_errors: list[BaseException] = []
    foreign_done = threading.Event()
    first_event = make_event(
        "task_enqueued",
        "first",
        task_id="task-a",
        queue_task_id="queue-task-a",
    )

    with first.locked() as token:
        def append_from_foreign_thread() -> None:
            try:
                first.append_locked(first_event, token)
            except BaseException as exc:
                foreign_errors.append(exc)
            finally:
                foreign_done.set()

        thread = threading.Thread(target=append_from_foreign_thread)
        thread.start()
        thread.join(timeout=1)
        assert foreign_done.is_set()
        assert len(foreign_errors) == 1
        assert isinstance(foreign_errors[0], RuntimeError)
        assert not first._log_path.exists()

        with pytest.raises(RuntimeError):
            second.append_locked(
                make_event(
                    "task_enqueued",
                    "second",
                    task_id="task-a",
                    queue_task_id="queue-task-a",
                ),
                token,
            )

        first.append_locked(first_event, token)

    with pytest.raises(RuntimeError):
        first.append_locked(first_event, token)

    with pytest.raises(RuntimeError):
        first.append_locked(first_event, object())

    assert [event.event_type for event in first.read_all()] == ["task_enqueued"]
    assert not second._log_path.exists()


def test_nested_event_scopes_keep_exact_tokens_active_until_each_exit(
    tmp_path: Path,
):
    log = MissionEventLog(tmp_path / "events", "nested")

    with log.locked() as outer:
        with log.locked() as inner:
            log.append_locked(make_event("inner", "nested"), inner)
            log.append_locked(make_event("outer-during-inner", "nested"), outer)
        with pytest.raises(RuntimeError):
            log.append_locked(make_event("expired-inner", "nested"), inner)
        log.append_locked(make_event("outer-after-inner", "nested"), outer)

    assert [event.event_type for event in log.read_all()] == [
        "inner",
        "outer-during-inner",
        "outer-after-inner",
    ]


def test_lock_held_append_validates_tail_flushes_and_fsyncs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    log = MissionEventLog(tmp_path / "events", "durable")
    fsync_calls = 0
    original_fsync = os.fsync

    def track_fsync(fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", track_fsync)
    with log.locked() as token:
        log.append_locked(make_event("first", "durable"), token)
        assert fsync_calls == 1
        log._log_path.write_bytes(
            log._log_path.read_bytes() + b'{"event_type":'
        )
        with pytest.raises(EventLogCorruptionError):
            log.append_locked(make_event("second", "durable"), token)

    assert fsync_calls == 1


def test_foreign_mission_append_writes_zero_bytes(tmp_path: Path):
    log = MissionEventLog(tmp_path / "events", "mission-alpha")

    with pytest.raises(EventLogCorruptionError):
        log.append(make_event("external", "mission-beta"))

    assert not log._log_path.exists()


@pytest.mark.parametrize(
    "mission_ids",
    [
        ["mission-beta"],
        ["mission-alpha", "mission-beta"],
        ["mission-beta", "mission-alpha"],
    ],
)
def test_foreign_historical_event_makes_owned_snapshot_corrupt(
    tmp_path: Path,
    mission_ids: list[str],
):
    log = MissionEventLog(tmp_path / "events", "mission-alpha")
    log._events_dir.mkdir(parents=True)
    log._log_path.write_text(
        "".join(
            json.dumps(make_event("external", mission_id).to_dict()) + "\n"
            for mission_id in mission_ids
        ),
        encoding="utf-8",
    )

    with pytest.raises(EventLogCorruptionError):
        log.read_snapshot()


def test_event_log_owner_cannot_differ_from_path_basename(tmp_path: Path):
    events_dir = tmp_path / "events"
    log = MissionEventLog(events_dir, "mission-alpha")
    log._log_path = events_dir / "mission-beta.jsonl"

    with pytest.raises(EventLogCorruptionError):
        log.read_snapshot()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
