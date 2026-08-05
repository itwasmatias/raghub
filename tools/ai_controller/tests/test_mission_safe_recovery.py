from __future__ import annotations

import json
import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.ai_controller._locking import FileLock
from tools.ai_controller.mission.events import (
    EventLogCorruptionError,
    MissionEventLog,
    make_event,
)
from tools.ai_controller.mission.materializer import (
    TaskMaterializer,
    make_legacy_queue_task_id,
)
from tools.ai_controller.mission.models import (
    ApprovalPolicy,
    BudgetUsage,
    MissionBudget,
    MissionDefinition,
    MissionState,
    MissionStatus,
    MissionTaskDefinition,
    MissionTaskState,
    MissionTaskStatus,
)
from tools.ai_controller.mission.store import MissionStore
from tools.ai_controller.models import Task
from tools.ai_controller.queue import DurableQueue, atomic_json as queue_atomic_json


def _lock_state(lock: FileLock) -> tuple[int | None, int]:
    owner = FileLock._owners.get(lock._key)
    return (owner[0], owner[1]) if owner is not None else (None, 0)


def test_file_lock_same_instance_releases_only_after_outer_exit(
    tmp_path: Path,
):
    lock = FileLock(tmp_path / "nested.lock")
    contender_acquired = threading.Event()

    with lock:
        owner, depth = _lock_state(lock)
        assert owner == threading.get_ident()
        assert depth == 1
        with lock:
            assert _lock_state(lock) == (threading.get_ident(), 2)
        assert _lock_state(lock) == (threading.get_ident(), 1)

    assert _lock_state(lock) == (None, 0)

    def contend() -> None:
        with FileLock(lock.path):
            contender_acquired.set()

    thread = threading.Thread(target=contend)
    thread.start()
    thread.join(timeout=1)
    assert contender_acquired.is_set()


def test_file_lock_nested_exceptions_preserve_depth_and_release(
    tmp_path: Path,
):
    lock = FileLock(tmp_path / "exceptions.lock")

    with pytest.raises(RuntimeError, match="outer"):
        with lock:
            with pytest.raises(ValueError, match="inner"):
                with lock:
                    raise ValueError("inner")
            assert _lock_state(lock) == (threading.get_ident(), 1)
            raise RuntimeError("outer")

    assert _lock_state(lock) == (None, 0)


def test_file_lock_foreign_thread_cannot_release_owner_lock(
    tmp_path: Path,
):
    lock = FileLock(tmp_path / "foreign.lock")
    attempted = threading.Event()
    errors: list[BaseException] = []

    with lock:
        def release_from_foreign_thread() -> None:
            try:
                lock.__exit__(None, None, None)
            except BaseException as exc:
                errors.append(exc)
            finally:
                attempted.set()

        thread = threading.Thread(target=release_from_foreign_thread)
        thread.start()
        thread.join(timeout=1)
        assert attempted.is_set()
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        assert "non-owner" in str(errors[0])
        assert _lock_state(lock) == (threading.get_ident(), 1)

    assert _lock_state(lock) == (None, 0)


def test_nested_authoritative_store_queue_report_and_event_calls_release(
    tmp_path: Path,
):
    from tools.ai_controller.reports import ReportStore, report_lock_path

    store, queue, reports, missions, definition = _create_mission(tmp_path)
    state = store.load_state(definition.mission_id)
    task = _expected_task(queue, reports, definition, "task-a")
    report_store = ReportStore(reports)
    event_log = MissionEventLog(
        missions / "events", definition.mission_id
    )

    with store._lock:
        store.update_state(definition.mission_id, state)
    with FileLock(queue.lock_path):
        queue.enqueue(task)
    with FileLock(report_lock_path(reports)):
        report_store.write(task.id, {"status": "pending"})
    with event_log.locked():
        event_log.append(make_event("nested", definition.mission_id))

    assert FileLock._owners == {}


def _create_mission(
    tmp_path: Path,
    *,
    mission_id: str = "repair-mission",
    task_ids: tuple[str, ...] = ("task-a",),
    task_status: MissionTaskStatus = MissionTaskStatus.pending,
    approval_policy: ApprovalPolicy = ApprovalPolicy.no_approval_required,
    budget_usage: BudgetUsage | None = None,
    budgets: MissionBudget | None = None,
    prompt: str = "Repair this task",
    task_metadata: dict | None = None,
    task_tests: list[list[str]] | None = None,
    started_at: str | None = None,
) -> tuple[MissionStore, DurableQueue, Path, Path, MissionDefinition]:
    missions_root = tmp_path / "controller" / "missions"
    reports_root = tmp_path / "controller" / "reports"
    reports_root.mkdir(parents=True)
    store = MissionStore(missions_root)
    queue = DurableQueue(tmp_path / "controller" / "queue")
    tasks = [
        MissionTaskDefinition(
            task_id=task_id,
            title=f"Task {task_id}",
            prompt=prompt,
            depends_on=list(task_ids[:index]),
            approval_policy=approval_policy,
            metadata=dict(task_metadata or {}),
            tests=[list(command) for command in (task_tests or [])],
        )
        for index, task_id in enumerate(task_ids)
    ]
    definition = MissionDefinition(
        mission_id=mission_id,
        title="Repair mission",
        tasks=tasks,
        budgets=budgets or MissionBudget(),
        created_at="2026-08-04T12:00:00+00:00",
    )
    states = {
        task_id: MissionTaskState(
            task_id=task_id,
            status=(
                MissionTaskStatus.succeeded
                if index < len(task_ids) - 1
                else task_status
            ),
        )
        for index, task_id in enumerate(task_ids)
    }
    state = MissionState(
        mission_id=mission_id,
        status=MissionStatus.running,
        task_states=states,
        budget_usage=budget_usage or BudgetUsage(),
        started_at=started_at,
    )
    store.create(definition, state)
    return store, queue, reports_root, missions_root, definition


def _expected_task(
    queue: DurableQueue,
    reports_root: Path,
    definition: MissionDefinition,
    task_id: str,
) -> Task:
    task_def = next(task for task in definition.tasks if task.task_id == task_id)
    materializer = TaskMaterializer(queue, reports_root)
    queue_id = materializer.make_queue_task_id(definition.mission_id, task_id)
    return Task(
        id=queue_id,
        title=task_def.title,
        prompt=task_def.prompt,
        base_ref=task_def.base_ref,
        tests=list(task_def.tests),
        max_attempts=task_def.max_attempts,
        metadata={
            **task_def.metadata,
            "mission_id": definition.mission_id,
            "mission_task_id": task_id,
            "depends_on": list(task_def.depends_on),
        },
    )


def _write_succeeded_evidence(
    queue: DurableQueue,
    reports_root: Path,
    task: Task,
) -> None:
    queue.enqueue(task)
    os.replace(
        queue.pending / f"{task.id}.json",
        queue.succeeded / f"{task.id}.json",
    )
    (reports_root / f"{task.id}.json").write_text(
        json.dumps(
            {
                "controller_version": "fixture",
                "task_id": task.id,
                "mission_id": task.metadata["mission_id"],
                "mission_task_id": task.metadata["mission_task_id"],
                "title": task.title,
                "original_task_prompt": task.prompt,
                "status": "succeeded",
                "attempts": [
                    {
                        "provider": "fixture-provider",
                        "success": True,
                        "timed_out": False,
                    }
                ],
                "files_changed": ["fixture.txt"],
                "changes": [{"path": "fixture.txt"}],
                "tests": [
                    {
                        "argv": ["python3", "-m", "pytest"],
                        "exit_code": 0,
                        "timed_out": False,
                    }
                ],
                "started_at": "2026-08-04T12:00:00+00:00",
                "finished_at": "2026-08-04T12:01:00+00:00",
            }
        ),
        encoding="utf-8",
    )


def _plan(tmp_path: Path, **kwargs):
    from tools.ai_controller.mission.repair import plan_mission_repairs

    store, queue, reports, missions, definition = _create_mission(tmp_path, **kwargs)
    actions = plan_mission_repairs(definition.mission_id, store, queue, reports)
    return store, queue, reports, missions, definition, actions


def test_plan_is_read_only_stably_ordered_and_has_stable_action_ids(tmp_path: Path):
    from tools.ai_controller.mission.repair import (
        RepairActionKind,
        RepairClassification,
        plan_mission_repairs,
    )

    store, queue, reports, _, definition = _create_mission(
        tmp_path,
        task_ids=("task-z", "task-a"),
    )
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    first = plan_mission_repairs(definition.mission_id, store, queue, reports)
    second = plan_mission_repairs(definition.mission_id, store, queue, reports)

    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert before == after
    assert [action.task_id for action in first] == ["task-a", "task-z"]
    assert [action.action_id for action in first] == [
        action.action_id for action in second
    ]
    assert first[0].classification is RepairClassification.SAFE_REPAIR
    assert first[0].proposed_action is RepairActionKind.REMATERIALIZE_MISSING_QUEUE_TASK
    assert first[1].classification is RepairClassification.HUMAN_REVIEW_REQUIRED
    assert first[0].revision
    assert first[0].expected_queue_id
    assert first[0].evidence
    assert first[0].created_at == "2026-08-04T12:00:00+00:00"


def test_rematerializes_exact_payload_and_repeated_apply_is_idempotent(tmp_path: Path):
    from tools.ai_controller.mission.repair import (
        RepairApplyStatus,
        apply_mission_repair,
    )

    store, queue, reports, missions, definition, actions = _plan(tmp_path)
    action = actions[0]

    first = apply_mission_repair(action, store, queue, reports, missions)
    second = apply_mission_repair(action, store, queue, reports, missions)

    assert first.status is RepairApplyStatus.APPLIED
    assert second.status is RepairApplyStatus.NO_LONGER_NEEDED
    queue_files = list(queue.pending.glob("*.json"))
    assert [path.stem for path in queue_files] == [action.expected_queue_id]
    assert json.loads(queue_files[0].read_text(encoding="utf-8")) == _expected_task(
        queue, reports, definition, "task-a"
    ).to_dict()
    state = store.load_state(definition.mission_id)
    assert state.task_states["task-a"].status is MissionTaskStatus.queued
    assert state.task_states["task-a"].queue_task_id == action.expected_queue_id
    events = MissionEventLog(missions / "events", definition.mission_id).read_all()
    assert sum(event.event_type == "repair_applied" for event in events) == 1


def test_concurrent_applicants_mutate_and_audit_once(tmp_path: Path):
    from tools.ai_controller.mission.repair import (
        RepairApplyStatus,
        apply_mission_repair,
    )

    store, queue, reports, missions, definition, actions = _plan(tmp_path)
    action = actions[0]
    barrier = threading.Barrier(2)

    def apply_from_independent_process_view():
        independent_store = MissionStore(missions)
        independent_queue = DurableQueue(queue.root)
        barrier.wait()
        return apply_mission_repair(
            action,
            independent_store,
            independent_queue,
            reports,
            missions,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: apply_from_independent_process_view(),
                range(2),
            )
        )

    assert sorted(result.status.value for result in results) == [
        RepairApplyStatus.APPLIED.value,
        RepairApplyStatus.NO_LONGER_NEEDED.value,
    ]
    assert len(list(queue.pending.glob("*.json"))) == 1
    events = MissionEventLog(
        missions / "events", definition.mission_id
    ).read_all()
    assert sum(event.event_type == "repair_started" for event in events) == 1
    assert sum(event.event_type == "repair_applied" for event in events) == 1
    assert sum(event.event_type == "task_enqueued" for event in events) == 1


def test_queue_loss_after_applied_repair_can_be_repaired_again(tmp_path: Path):
    from tools.ai_controller.mission.repair import (
        RepairApplyStatus,
        apply_mission_repair,
        plan_mission_repairs,
    )

    store, queue, reports, missions, definition, actions = _plan(tmp_path)
    first_action = actions[0]
    first = apply_mission_repair(
        first_action, store, queue, reports, missions
    )
    queue_file = queue.pending / f"{first_action.expected_queue_id}.json"
    assert queue_file.exists()
    queue_file.unlink()

    second_action = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]
    second = apply_mission_repair(
        second_action, store, queue, reports, missions
    )

    assert first.status is RepairApplyStatus.APPLIED
    assert second_action.action_id != first_action.action_id
    assert second.status is RepairApplyStatus.APPLIED
    assert queue_file.exists()
    events = MissionEventLog(
        missions / "events", definition.mission_id
    ).read_all()
    assert sum(event.event_type == "repair_applied" for event in events) == 2


def test_completion_uses_terminal_queue_and_verified_report(tmp_path: Path):
    from tools.ai_controller.mission.repair import (
        RepairActionKind,
        RepairApplyStatus,
        RepairClassification,
        apply_mission_repair,
        plan_mission_repairs,
    )

    store, queue, reports, missions, definition = _create_mission(
        tmp_path,
        task_status=MissionTaskStatus.queued,
    )
    expected = _expected_task(queue, reports, definition, "task-a")
    _write_succeeded_evidence(queue, reports, expected)
    state = store.load_state(definition.mission_id)
    state.task_states["task-a"].queue_task_id = expected.id
    store.update_state(definition.mission_id, state)

    action = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]
    result = apply_mission_repair(action, store, queue, reports, missions)

    assert action.classification is RepairClassification.SAFE_REPAIR
    assert action.proposed_action is RepairActionKind.COMPLETE_FROM_DURABLE_SUCCESS
    assert result.status is RepairApplyStatus.APPLIED
    completed = store.load_state(definition.mission_id).task_states["task-a"]
    assert completed.status is MissionTaskStatus.succeeded
    assert completed.report_path == str(reports / f"{expected.id}.json")
    assert store.load_state(definition.mission_id).status is MissionStatus.succeeded
    assert (
        store.find_mission(definition.mission_id)[0]
        is MissionStatus.succeeded
    )
    events = MissionEventLog(missions / "events", definition.mission_id).read_all()
    assert sum(event.event_type == "task_succeeded" for event in events) == 1
    assert sum(event.event_type == "repair_applied" for event in events) == 1


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("tests.0.exit_code", 1),
        ("attempts.0.success", False),
        ("mission_id", "other-mission"),
        ("mission_task_id", "other-task"),
        ("tests.0.argv", ["python3", "-m", "unittest"]),
        ("files_changed", ["different.txt"]),
    ],
)
def test_completion_report_changes_after_planning_are_rejected(
    tmp_path: Path,
    mutation: str,
    value,
):
    from tools.ai_controller.mission.repair import (
        RepairApplyStatus,
        apply_mission_repair,
        plan_mission_repairs,
    )

    store, queue, reports, missions, definition = _create_mission(
        tmp_path,
        task_status=MissionTaskStatus.queued,
    )
    expected = _expected_task(queue, reports, definition, "task-a")
    _write_succeeded_evidence(queue, reports, expected)
    state = store.load_state(definition.mission_id)
    state.task_states["task-a"].queue_task_id = expected.id
    store.update_state(definition.mission_id, state)
    action = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]
    report_path = reports / f"{expected.id}.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    target = report
    parts = mutation.split(".")
    for part in parts[:-1]:
        target = target[int(part)] if part.isdigit() else target[part]
    target[parts[-1]] = value
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = apply_mission_repair(action, store, queue, reports, missions)

    assert result.status in {
        RepairApplyStatus.STALE,
        RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
    }
    assert store.load_state(definition.mission_id).task_states[
        "task-a"
    ].status is MissionTaskStatus.queued


@pytest.mark.parametrize(
    "missing_field",
    [
        "attempts",
        "tests",
        "files_changed",
        "task_id",
        "mission_id",
        "mission_task_id",
        "started_at",
        "finished_at",
    ],
)
def test_incomplete_completion_reports_require_human_review(
    tmp_path: Path,
    missing_field: str,
):
    from tools.ai_controller.mission.repair import (
        RepairClassification,
        plan_mission_repairs,
    )

    store, queue, reports, _, definition = _create_mission(
        tmp_path,
        task_status=MissionTaskStatus.queued,
    )
    expected = _expected_task(queue, reports, definition, "task-a")
    _write_succeeded_evidence(queue, reports, expected)
    state = store.load_state(definition.mission_id)
    state.task_states["task-a"].queue_task_id = expected.id
    store.update_state(definition.mission_id, state)
    report_path = reports / f"{expected.id}.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    del report[missing_field]
    report_path.write_text(json.dumps(report), encoding="utf-8")

    action = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]

    assert action.classification is RepairClassification.HUMAN_REVIEW_REQUIRED


def test_status_only_success_report_requires_human_review(tmp_path: Path):
    from tools.ai_controller.mission.repair import (
        RepairClassification,
        plan_mission_repairs,
    )

    store, queue, reports, _, definition = _create_mission(
        tmp_path,
        task_status=MissionTaskStatus.queued,
    )
    expected = _expected_task(queue, reports, definition, "task-a")
    _write_succeeded_evidence(queue, reports, expected)
    state = store.load_state(definition.mission_id)
    state.task_states["task-a"].queue_task_id = expected.id
    store.update_state(definition.mission_id, state)
    (reports / f"{expected.id}.json").write_text(
        json.dumps({"status": "succeeded"}),
        encoding="utf-8",
    )

    action = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]

    assert action.classification is RepairClassification.HUMAN_REVIEW_REQUIRED


def test_replaced_completion_report_is_rejected_after_planning(tmp_path: Path):
    from tools.ai_controller.mission.repair import (
        RepairApplyStatus,
        apply_mission_repair,
        plan_mission_repairs,
    )

    store, queue, reports, missions, definition = _create_mission(
        tmp_path,
        task_status=MissionTaskStatus.queued,
    )
    expected = _expected_task(queue, reports, definition, "task-a")
    _write_succeeded_evidence(queue, reports, expected)
    state = store.load_state(definition.mission_id)
    state.task_states["task-a"].queue_task_id = expected.id
    store.update_state(definition.mission_id, state)
    action = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]
    report_path = reports / f"{expected.id}.json"
    replacement = reports / "replacement.json"
    replacement.write_bytes(report_path.read_bytes())
    os.replace(replacement, report_path)

    result = apply_mission_repair(action, store, queue, reports, missions)

    assert result.status in {
        RepairApplyStatus.STALE,
        RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
    }
    assert store.load_state(definition.mission_id).task_states[
        "task-a"
    ].status is MissionTaskStatus.queued


def test_conflicting_completion_reports_require_human_review(tmp_path: Path):
    from tools.ai_controller.mission.repair import (
        RepairClassification,
        plan_mission_repairs,
    )

    store, queue, reports, _, definition = _create_mission(
        tmp_path,
        task_status=MissionTaskStatus.queued,
    )
    expected = _expected_task(queue, reports, definition, "task-a")
    _write_succeeded_evidence(queue, reports, expected)
    state = store.load_state(definition.mission_id)
    state.task_states["task-a"].queue_task_id = expected.id
    store.update_state(definition.mission_id, state)
    conflict = json.loads(
        (reports / f"{expected.id}.json").read_text(encoding="utf-8")
    )
    conflict["files_changed"] = ["conflicting.txt"]
    (reports / "conflicting-report.json").write_text(
        json.dumps(conflict),
        encoding="utf-8",
    )

    action = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]

    assert action.classification is RepairClassification.HUMAN_REVIEW_REQUIRED


@pytest.mark.parametrize(
    ("mutation", "expected_status"),
    [
        ("generic_revision", "STALE"),
        ("approval_gate", "HUMAN_REVIEW_REQUIRED"),
        ("budget_exhausted", "HUMAN_REVIEW_REQUIRED"),
    ],
)
def test_apply_revalidates_revision_approval_and_budget(
    tmp_path: Path,
    mutation: str,
    expected_status: str,
):
    from tools.ai_controller.mission.repair import apply_mission_repair

    store, queue, reports, missions, definition, actions = _plan(tmp_path)
    action = actions[0]
    if mutation == "generic_revision":
        state = store.load_state(definition.mission_id)
        state.started_at = "2026-08-04T12:01:00+00:00"
        store.update_state(definition.mission_id, state)
    elif mutation == "approval_gate":
        state = store.load_state(definition.mission_id)
        state.task_states["task-a"].status = MissionTaskStatus.approval_required
        store.update_state(definition.mission_id, state)
    else:
        state = store.load_state(definition.mission_id)
        state.budget_usage.total_attempts = definition.budgets.max_total_attempts
        store.update_state(definition.mission_id, state)

    result = apply_mission_repair(action, store, queue, reports, missions)

    assert result.status.value == expected_status
    assert not list(queue.pending.glob("*.json"))
    assert store.load_state(definition.mission_id).task_states[
        "task-a"
    ].status is not MissionTaskStatus.queued


def test_queue_record_appearing_after_plan_is_not_duplicated(tmp_path: Path):
    from tools.ai_controller.mission.repair import (
        RepairApplyStatus,
        apply_mission_repair,
    )

    store, queue, reports, missions, definition, actions = _plan(tmp_path)
    queue.enqueue(_expected_task(queue, reports, definition, "task-a"))

    result = apply_mission_repair(actions[0], store, queue, reports, missions)

    assert result.status is RepairApplyStatus.HUMAN_REVIEW_REQUIRED
    assert len(list(queue.pending.glob("*.json"))) == 1
    assert store.load_state(definition.mission_id).task_states[
        "task-a"
    ].status is MissionTaskStatus.pending


@pytest.mark.parametrize("conflict_kind", ["legacy", "payload", "metadata", "malformed"])
def test_conflicts_and_malformed_records_require_human_review(
    tmp_path: Path,
    conflict_kind: str,
):
    from tools.ai_controller.mission.repair import (
        RepairApplyStatus,
        RepairClassification,
        apply_mission_repair,
        plan_mission_repairs,
    )

    store, queue, reports, missions, definition, actions = _plan(tmp_path)
    expected = _expected_task(queue, reports, definition, "task-a")
    if conflict_kind == "legacy":
        queue.enqueue(expected)
        legacy = replace(
            expected,
            id=make_legacy_queue_task_id(definition.mission_id, "task-a"),
        )
        queue.enqueue(legacy)
    elif conflict_kind == "payload":
        expected = replace(expected, prompt="different prompt")
        queue.enqueue(expected)
    elif conflict_kind == "metadata":
        expected.metadata["mission_task_id"] = "other-task"
        queue.enqueue(expected)
    else:
        (queue.pending / f"{expected.id}.json").write_text("{", encoding="utf-8")

    replanned = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]
    applied = apply_mission_repair(actions[0], store, queue, reports, missions)

    assert replanned.classification is RepairClassification.HUMAN_REVIEW_REQUIRED
    assert applied.status is RepairApplyStatus.HUMAN_REVIEW_REQUIRED
    assert store.load_state(definition.mission_id).task_states[
        "task-a"
    ].status is MissionTaskStatus.pending


@pytest.mark.parametrize(
    "reserved_field",
    [
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
    ],
)
def test_reserved_task_metadata_collisions_require_human_review(
    tmp_path: Path,
    reserved_field: str,
):
    from tools.ai_controller.mission.repair import (
        RepairClassification,
        plan_mission_repairs,
    )

    store, queue, reports, _, definition = _create_mission(
        tmp_path,
        task_metadata={reserved_field: "forged"},
    )

    action = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]

    assert action.classification is RepairClassification.HUMAN_REVIEW_REQUIRED
    assert action.finding == "RESERVED_METADATA_COLLISION"
    assert not list(queue.pending.glob("*.json"))


def test_nonreserved_task_metadata_is_preserved_during_repair(tmp_path: Path):
    from tools.ai_controller.mission.repair import (
        RepairApplyStatus,
        apply_mission_repair,
    )

    store, queue, reports, missions, _, actions = _plan(
        tmp_path,
        task_metadata={"fixture_label": "preserved"},
    )

    result = apply_mission_repair(
        actions[0], store, queue, reports, missions
    )
    payload = json.loads(
        (queue.pending / f"{actions[0].expected_queue_id}.json").read_text(
            encoding="utf-8"
        )
    )

    assert result.status is RepairApplyStatus.APPLIED
    assert payload["metadata"]["fixture_label"] == "preserved"
    assert payload["metadata"]["mission_id"] == actions[0].mission_id
    assert payload["metadata"]["mission_task_id"] == actions[0].task_id


def test_unknown_or_contradictory_completion_evidence_requires_human_review(
    tmp_path: Path,
):
    from tools.ai_controller.mission.repair import (
        RepairClassification,
        plan_mission_repairs,
    )

    store, queue, reports, missions, definition = _create_mission(
        tmp_path,
        task_status=MissionTaskStatus.queued,
    )
    expected = _expected_task(queue, reports, definition, "task-a")
    queue.enqueue(expected)
    os.replace(
        queue.pending / f"{expected.id}.json",
        queue.succeeded / f"{expected.id}.json",
    )
    state = store.load_state(definition.mission_id)
    state.task_states["task-a"].queue_task_id = expected.id
    store.update_state(definition.mission_id, state)

    no_report = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]
    (reports / f"{expected.id}.json").write_text(
        json.dumps({"status": "succeeded"}),
        encoding="utf-8",
    )
    MissionEventLog(missions / "events", definition.mission_id).append(
        make_event(
            "task_failed",
            definition.mission_id,
            task_id="task-a",
            queue_task_id=expected.id,
        )
    )
    contradictory = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]

    assert no_report.classification is RepairClassification.HUMAN_REVIEW_REQUIRED
    assert contradictory.classification is RepairClassification.HUMAN_REVIEW_REQUIRED


def test_queue_write_failure_never_advances_mission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from tools.ai_controller.mission import repair

    store, queue, reports, missions, definition, actions = _plan(tmp_path)

    def fail_enqueue(path: Path, payload: dict):
        raise OSError("injected queue write failure")

    monkeypatch.setattr(repair, "queue_atomic_json", fail_enqueue)
    result = repair.apply_mission_repair(
        actions[0], store, queue, reports, missions
    )

    assert result.status is repair.RepairApplyStatus.FAILED
    assert store.load_state(definition.mission_id).task_states[
        "task-a"
    ].status is MissionTaskStatus.pending
    assert not list(queue.pending.glob("*.json"))


def test_store_write_failure_after_enqueue_is_restart_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import tools.ai_controller.mission.repair as repair

    store, queue, reports, missions, definition, actions = _plan(tmp_path)
    original = repair._atomic_json

    def fail_store(path: Path, payload: dict):
        raise OSError("injected mission-store write failure")

    monkeypatch.setattr(repair, "_atomic_json", fail_store)
    first = repair.apply_mission_repair(
        actions[0], store, queue, reports, missions
    )
    monkeypatch.setattr(repair, "_atomic_json", original)
    second = repair.apply_mission_repair(
        actions[0], store, queue, reports, missions
    )

    assert first.status is repair.RepairApplyStatus.FAILED
    assert second.status is repair.RepairApplyStatus.APPLIED
    assert len(list(queue.pending.glob("*.json"))) == 1
    assert store.load_state(definition.mission_id).task_states[
        "task-a"
    ].status is MissionTaskStatus.queued


def test_event_write_failure_is_explicit_and_retry_does_not_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from tools.ai_controller.mission import repair

    store, queue, reports, missions, definition, actions = _plan(tmp_path)
    original = MissionEventLog.append_locked

    def fail_event(self, event, token):
        raise OSError("injected event write failure")

    monkeypatch.setattr(MissionEventLog, "append_locked", fail_event)
    first = repair.apply_mission_repair(
        actions[0], store, queue, reports, missions
    )
    monkeypatch.setattr(MissionEventLog, "append_locked", original)
    second = repair.apply_mission_repair(
        actions[0], store, queue, reports, missions
    )

    assert first.status is repair.RepairApplyStatus.FAILED
    assert second.status is repair.RepairApplyStatus.APPLIED
    assert len(list(queue.pending.glob("*.json"))) == 1
    assert store.load_state(definition.mission_id).task_states[
        "task-a"
    ].status is MissionTaskStatus.queued
    events = MissionEventLog(missions / "events", definition.mission_id).read_all()
    assert sum(event.event_type == "task_enqueued" for event in events) == 1
    assert sum(event.event_type == "repair_applied" for event in events) == 1


def test_windows_and_posix_path_strings_round_trip_in_reconstructed_payload(
    tmp_path: Path,
):
    from tools.ai_controller.mission.repair import (
        RepairApplyStatus,
        apply_mission_repair,
    )

    prompt = r"Edit C:\work\repo\src\app.py and tests/unit/test_app.py"
    store, queue, reports, missions, definition, actions = _plan(
        tmp_path,
        mission_id="path-safe-mission",
        prompt=prompt,
    )

    result = apply_mission_repair(actions[0], store, queue, reports, missions)
    payload = json.loads(
        (queue.pending / f"{actions[0].expected_queue_id}.json").read_text(
            encoding="utf-8"
        )
    )

    assert result.status is RepairApplyStatus.APPLIED
    assert payload["prompt"] == prompt


def test_linked_queued_task_with_missing_record_is_safely_rematerialized(
    tmp_path: Path,
):
    from tools.ai_controller.mission.repair import (
        RepairActionKind,
        RepairApplyStatus,
        RepairClassification,
        apply_mission_repair,
        plan_mission_repairs,
    )

    store, queue, reports, missions, definition = _create_mission(
        tmp_path,
        task_status=MissionTaskStatus.queued,
    )
    expected = _expected_task(queue, reports, definition, "task-a")
    state = store.load_state(definition.mission_id)
    state.task_states["task-a"].queue_task_id = expected.id
    store.update_state(definition.mission_id, state)

    action = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]
    result = apply_mission_repair(action, store, queue, reports, missions)

    assert action.classification is RepairClassification.SAFE_REPAIR
    assert action.proposed_action is RepairActionKind.REMATERIALIZE_MISSING_QUEUE_TASK
    assert result.status is RepairApplyStatus.APPLIED
    assert json.loads(
        (queue.pending / f"{expected.id}.json").read_text(encoding="utf-8")
    ) == expected.to_dict()


def test_unsatisfied_dependency_blocks_completion_repair(tmp_path: Path):
    from tools.ai_controller.mission.repair import (
        RepairClassification,
        plan_mission_repairs,
    )

    store, queue, reports, _, definition = _create_mission(
        tmp_path,
        task_ids=("dependency", "dependent"),
        task_status=MissionTaskStatus.queued,
    )
    state = store.load_state(definition.mission_id)
    state.task_states["dependency"].status = MissionTaskStatus.pending
    expected = _expected_task(queue, reports, definition, "dependent")
    state.task_states["dependent"].queue_task_id = expected.id
    store.update_state(definition.mission_id, state)
    _write_succeeded_evidence(queue, reports, expected)

    action = next(
        item
        for item in plan_mission_repairs(
            definition.mission_id, store, queue, reports
        )
        if item.task_id == "dependent"
    )

    assert action.classification is RepairClassification.HUMAN_REVIEW_REQUIRED
    assert action.finding == "DEPENDENCY_AMBIGUITY"


def test_terminal_mission_event_blocks_rematerialization(tmp_path: Path):
    from tools.ai_controller.mission.repair import (
        RepairClassification,
        plan_mission_repairs,
    )

    store, queue, reports, missions, definition = _create_mission(tmp_path)
    MissionEventLog(missions / "events", definition.mission_id).append(
        make_event(
            "task_lost",
            definition.mission_id,
            task_id="task-a",
            queue_task_id=TaskMaterializer(
                queue, reports
            ).make_queue_task_id(definition.mission_id, "task-a"),
        )
    )

    action = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]

    assert action.classification is RepairClassification.HUMAN_REVIEW_REQUIRED
    assert action.finding == "CONTRADICTORY_QUEUE_EVENT"


@pytest.mark.parametrize(
    "event_type",
    [
        "task_succeeded",
        "task_failed",
        "task_lost",
        "task_cancelled",
        "mission_completed",
        "mission_failed",
        "mission_cancelled",
    ],
)
def test_all_terminal_events_block_rematerialization(
    tmp_path: Path,
    event_type: str,
):
    from tools.ai_controller.mission.repair import (
        RepairClassification,
        plan_mission_repairs,
    )

    store, queue, reports, missions, definition = _create_mission(tmp_path)
    expected = _expected_task(queue, reports, definition, "task-a")
    MissionEventLog(missions / "events", definition.mission_id).append(
        make_event(
            event_type,
            definition.mission_id,
            task_id=None if event_type.startswith("mission_") else "task-a",
            queue_task_id=(
                None if event_type.startswith("mission_") else expected.id
            ),
        )
    )

    action = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]

    assert action.classification is RepairClassification.HUMAN_REVIEW_REQUIRED
    assert not list(queue.pending.glob("*.json"))


def test_nonterminal_event_after_terminal_event_requires_human_review(
    tmp_path: Path,
):
    from tools.ai_controller.mission.repair import (
        RepairClassification,
        plan_mission_repairs,
    )

    store, queue, reports, missions, definition = _create_mission(tmp_path)
    expected = _expected_task(queue, reports, definition, "task-a")
    event_log = MissionEventLog(missions / "events", definition.mission_id)
    event_log.append(
        make_event(
            "task_failed",
            definition.mission_id,
            task_id="task-a",
            queue_task_id=expected.id,
        )
    )
    event_log.append(
        make_event(
            "task_running",
            definition.mission_id,
            task_id="task-a",
            queue_task_id=expected.id,
        )
    )

    action = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]

    assert action.classification is RepairClassification.HUMAN_REVIEW_REQUIRED


def test_event_revision_changes_action_identity_and_stales_planned_action(
    tmp_path: Path,
):
    from tools.ai_controller.mission.repair import (
        RepairApplyStatus,
        apply_mission_repair,
        plan_mission_repairs,
    )

    store, queue, reports, missions, definition, actions = _plan(tmp_path)
    original = actions[0]
    MissionEventLog(missions / "events", definition.mission_id).append(
        make_event(
            "task_lost",
            definition.mission_id,
            task_id="task-a",
            queue_task_id=original.expected_queue_id,
        )
    )

    current = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]
    result = apply_mission_repair(
        original, store, queue, reports, missions
    )

    assert current.action_id != original.action_id
    assert any(
        evidence.startswith("event_revision=")
        for evidence in current.evidence
    )
    assert result.status in {
        RepairApplyStatus.STALE,
        RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
    }
    assert not list(queue.pending.glob("*.json"))


def test_event_append_during_application_waits_for_event_lock(
    tmp_path: Path,
    monkeypatch,
):
    from tools.ai_controller.mission import repair

    store, queue, reports, missions, definition, actions = _plan(tmp_path)
    action = actions[0]
    entered_mutation = threading.Event()
    release_mutation = threading.Event()
    append_finished = threading.Event()
    original_queue_write = repair.queue_atomic_json

    def paused_queue_write(path, payload):
        entered_mutation.set()
        assert release_mutation.wait(timeout=5)
        return original_queue_write(path, payload)

    def append_terminal_event():
        assert entered_mutation.wait(timeout=5)
        MissionEventLog(missions / "events", definition.mission_id).append(
            make_event(
                "task_lost",
                definition.mission_id,
                task_id="task-a",
                queue_task_id=action.expected_queue_id,
            )
        )
        append_finished.set()

    monkeypatch.setattr(repair, "queue_atomic_json", paused_queue_write)
    with ThreadPoolExecutor(max_workers=2) as executor:
        apply_future = executor.submit(
            repair.apply_mission_repair,
            action,
            store,
            queue,
            reports,
            missions,
        )
        append_future = executor.submit(append_terminal_event)
        assert entered_mutation.wait(timeout=5)
        assert not append_finished.wait(timeout=0.1)
        release_mutation.set()
        result = apply_future.result(timeout=5)
        append_future.result(timeout=5)

    current = repair.plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]

    assert result.status is repair.RepairApplyStatus.APPLIED
    assert append_finished.is_set()
    assert current.classification is (
        repair.RepairClassification.HUMAN_REVIEW_REQUIRED
    )
    assert current.action_id != action.action_id


def test_matching_active_mission_and_queue_are_already_consistent(tmp_path: Path):
    from tools.ai_controller.mission.repair import (
        RepairClassification,
        plan_mission_repairs,
    )

    store, queue, reports, _, definition = _create_mission(
        tmp_path,
        task_status=MissionTaskStatus.queued,
    )
    expected = _expected_task(queue, reports, definition, "task-a")
    queue.enqueue(expected)
    state = store.load_state(definition.mission_id)
    state.task_states["task-a"].queue_task_id = expected.id
    store.update_state(definition.mission_id, state)

    action = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]

    assert action.classification is RepairClassification.ALREADY_CONSISTENT
    assert action.finding == "MISSION_AND_QUEUE_AGREE"


def test_completion_apply_changes_only_selected_task(tmp_path: Path):
    from tools.ai_controller.mission.repair import (
        RepairApplyStatus,
        apply_mission_repair,
        plan_mission_repairs,
    )

    store, queue, reports, missions, definition = _create_mission(
        tmp_path,
        task_ids=("task-a", "task-b"),
        task_status=MissionTaskStatus.queued,
    )
    state = store.load_state(definition.mission_id)
    state.task_states["task-a"].status = MissionTaskStatus.queued
    for task_id in ("task-a", "task-b"):
        expected = _expected_task(queue, reports, definition, task_id)
        state.task_states[task_id].queue_task_id = expected.id
        _write_succeeded_evidence(queue, reports, expected)
    store.update_state(definition.mission_id, state)
    action = next(
        item
        for item in plan_mission_repairs(
            definition.mission_id, store, queue, reports
        )
        if item.task_id == "task-a"
    )

    result = apply_mission_repair(action, store, queue, reports, missions)
    repaired = store.load_state(definition.mission_id)

    assert result.status is RepairApplyStatus.APPLIED
    assert repaired.task_states["task-a"].status is MissionTaskStatus.succeeded
    assert repaired.task_states["task-b"].status is MissionTaskStatus.queued
    assert repaired.budget_usage.total_attempts == 1


def _completion_repair_with_dependent(tmp_path: Path):
    from tools.ai_controller.mission.repair import plan_mission_repairs

    store, queue, reports, missions, definition = _create_mission(
        tmp_path,
        task_ids=("prerequisite", "dependent"),
    )
    state = store.load_state(definition.mission_id)
    state.task_states["prerequisite"] = MissionTaskState(
        task_id="prerequisite",
        status=MissionTaskStatus.queued,
    )
    state.task_states["dependent"] = MissionTaskState(
        task_id="dependent",
        status=MissionTaskStatus.pending,
    )
    expected = _expected_task(
        queue, reports, definition, "prerequisite"
    )
    state.task_states["prerequisite"].queue_task_id = expected.id
    store.update_state(definition.mission_id, state)
    _write_succeeded_evidence(queue, reports, expected)
    action = next(
        item
        for item in plan_mission_repairs(
            definition.mission_id, store, queue, reports
        )
        if item.task_id == "prerequisite"
    )
    return store, queue, reports, missions, definition, expected, action


def _scheduler_with_completed_prerequisite(tmp_path: Path):
    from tools.ai_controller.mission.scheduler import MissionScheduler

    (
        store,
        queue,
        reports,
        missions,
        definition,
        expected,
        _,
    ) = _completion_repair_with_dependent(tmp_path)
    scheduler = MissionScheduler(
        store, queue, reports, missions, emit_materializer_events=False
    )
    dependent_id = TaskMaterializer(
        queue, reports
    ).make_queue_task_id(definition.mission_id, "dependent")
    return (
        scheduler,
        store,
        queue,
        reports,
        missions,
        definition,
        expected,
        dependent_id,
    )


def _assert_restarted_dependent_is_exactly_once(
    store: MissionStore,
    queue: DurableQueue,
    reports: Path,
    missions: Path,
    definition: MissionDefinition,
    expected: Task,
    dependent_id: str,
) -> None:
    from tools.ai_controller.mission.scheduler import MissionScheduler

    restarted = MissionScheduler(
        MissionStore(missions),
        DurableQueue(queue.root),
        reports,
        missions,
        emit_materializer_events=False,
    )
    restarted.run_once(definition.mission_id)
    restarted.run_once(definition.mission_id)
    recovered = MissionStore(missions).load_state(definition.mission_id)
    events = MissionEventLog(
        missions / "events", definition.mission_id
    ).read_all()

    assert recovered.task_states["prerequisite"].status is (
        MissionTaskStatus.succeeded
    )
    assert recovered.task_states["dependent"].status is (
        MissionTaskStatus.queued
    )
    assert len(list(queue.pending.glob(f"{dependent_id}.json"))) == 1
    assert sum(
        event.event_type == "task_succeeded"
        and event.task_id == "prerequisite"
        and event.queue_task_id == expected.id
        for event in events
    ) == 1
    assert sum(
        event.event_type == "task_enqueued"
        and event.task_id == "dependent"
        and event.queue_task_id == dependent_id
        for event in events
    ) == 1
    assert FileLock._owners == {}


def _run_scheduler_with_prerequisite_event(
    tmp_path: Path,
    *,
    event_type: str = "task_succeeded",
    mission_id: str | None = None,
    task_id: str | None = "prerequisite",
    queue_task_id: str | None = None,
    metadata: dict | None = None,
    event_file_mission_id: str | None = None,
    state_queue_task_id: str | None = None,
):
    from tools.ai_controller.mission.scheduler import MissionScheduler

    (
        _,
        store,
        queue,
        reports,
        missions,
        definition,
        expected,
        dependent_id,
    ) = _scheduler_with_completed_prerequisite(tmp_path)
    state = store.load_state(definition.mission_id)
    state.task_states["prerequisite"].status = MissionTaskStatus.succeeded
    if state_queue_task_id is not None:
        state.task_states["prerequisite"].queue_task_id = state_queue_task_id
    store.update_state(definition.mission_id, state)
    MissionEventLog(
        missions / "events",
        event_file_mission_id or definition.mission_id,
    ).append(
        make_event(
            event_type,
            definition.mission_id if mission_id is None else mission_id,
            task_id=task_id,
            queue_task_id=(
                expected.id if queue_task_id is None else queue_task_id
            ),
            metadata=metadata or {},
        )
    )

    MissionScheduler(
        store,
        queue,
        reports,
        missions,
        emit_materializer_events=False,
    ).run_once(definition.mission_id)
    return store, queue, definition, dependent_id


@pytest.mark.parametrize(
    ("case", "event_kwargs"),
    [
        ("wrong mission_id", {"mission_id": "other-mission"}),
        ("missing mission_id", {"mission_id": ""}),
        ("wrong mission_task_id", {"task_id": "other-task"}),
        ("missing mission_task_id", {"task_id": None}),
        ("wrong queue_task_id", {"queue_task_id": "wrong-queue-id"}),
        ("missing queue_task_id", {"queue_task_id": ""}),
        (
            "unrelated deterministic queue ID",
            {"queue_task_id": "set-unrelated-deterministic-id"},
        ),
        ("wrong event type", {"event_type": "task_failed"}),
        (
            "malformed metadata",
            {"metadata": {"mission_id": ["not", "an", "identity"]}},
        ),
        (
            "event from another mission file",
            {"event_file_mission_id": "other-mission-file"},
        ),
    ],
)
def test_scheduler_requires_exact_prerequisite_success_identity(
    tmp_path: Path,
    case: str,
    event_kwargs: dict,
):
    if case in {
        "wrong mission_id",
        "missing mission_id",
        "missing mission_task_id",
        "missing queue_task_id",
        "event from another mission file",
    }:
        with pytest.raises(EventLogCorruptionError):
            _run_scheduler_with_prerequisite_event(
                tmp_path,
                **event_kwargs,
            )
        return
    if case == "unrelated deterministic queue ID":
        queue = DurableQueue(tmp_path / "controller" / "queue")
        event_kwargs["queue_task_id"] = TaskMaterializer(
            queue, tmp_path / "controller" / "reports"
        ).make_queue_task_id("other-mission", "prerequisite")

    store, queue, definition, dependent_id = (
        _run_scheduler_with_prerequisite_event(
            tmp_path,
            **event_kwargs,
        )
    )
    state = store.load_state(definition.mission_id)

    assert state.task_states["dependent"].status is MissionTaskStatus.pending
    assert state.task_states["dependent"].queue_task_id is None
    assert not (queue.pending / f"{dependent_id}.json").exists()


@pytest.mark.parametrize("queue_identity", ["current", "legacy"])
def test_scheduler_accepts_only_exact_current_or_legacy_prerequisite_id(
    tmp_path: Path,
    queue_identity: str,
):
    queue_id = None
    if queue_identity == "legacy":
        queue_id = make_legacy_queue_task_id(
            "repair-mission", "prerequisite"
        )

    store, queue, definition, dependent_id = (
        _run_scheduler_with_prerequisite_event(
            tmp_path,
            queue_task_id=queue_id,
            state_queue_task_id=queue_id,
        )
    )
    state = store.load_state(definition.mission_id)

    assert state.task_states["dependent"].status is MissionTaskStatus.queued
    assert state.task_states["dependent"].queue_task_id == dependent_id
    assert len(list(queue.pending.glob(f"{dependent_id}.json"))) == 1


def test_scheduler_crash_after_success_event_does_not_enqueue_dependent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    (
        scheduler,
        store,
        queue,
        reports,
        missions,
        definition,
        expected,
        dependent_id,
    ) = _scheduler_with_completed_prerequisite(tmp_path)
    original_update = store.update_state
    failed = False

    def fail_first_completed_state(
        mission_id: str, state: MissionState
    ) -> None:
        nonlocal failed
        if (
            not failed
            and state.task_states["prerequisite"].status
            is MissionTaskStatus.succeeded
        ):
            failed = True
            raise OSError("crash after task_succeeded")
        original_update(mission_id, state)

    monkeypatch.setattr(store, "update_state", fail_first_completed_state)
    with pytest.raises(OSError, match="crash after task_succeeded"):
        scheduler.run_once(definition.mission_id)

    interrupted = MissionStore(missions).load_state(definition.mission_id)
    events = MissionEventLog(
        missions / "events", definition.mission_id
    ).read_all()
    assert interrupted.task_states["prerequisite"].status is (
        MissionTaskStatus.queued
    )
    assert interrupted.task_states["dependent"].status is (
        MissionTaskStatus.pending
    )
    assert not (queue.pending / f"{dependent_id}.json").exists()
    assert sum(event.event_type == "task_succeeded" for event in events) == 1

    _assert_restarted_dependent_is_exactly_once(
        store,
        queue,
        reports,
        missions,
        definition,
        expected,
        dependent_id,
    )


def test_scheduler_persists_completion_before_readiness_computation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    (
        scheduler,
        _,
        queue,
        _,
        missions,
        definition,
        expected,
        dependent_id,
    ) = _scheduler_with_completed_prerequisite(tmp_path)

    def crash_at_readiness(*_args, **_kwargs):
        persisted = MissionStore(missions).load_state(definition.mission_id)
        events = MissionEventLog(
            missions / "events", definition.mission_id
        ).read_all()
        assert persisted.task_states["prerequisite"].status is (
            MissionTaskStatus.succeeded
        )
        assert any(
            event.event_type == "task_succeeded"
            and event.task_id == "prerequisite"
            and event.queue_task_id == expected.id
            for event in events
        )
        raise RuntimeError("crash before dependent enqueue")

    monkeypatch.setattr(
        scheduler, "_compute_ready_tasks", crash_at_readiness
    )
    with pytest.raises(RuntimeError, match="crash before dependent enqueue"):
        scheduler.run_once(definition.mission_id)

    assert not (queue.pending / f"{dependent_id}.json").exists()


@pytest.mark.parametrize("after_queue_creation", [False, True])
def test_scheduler_queue_boundary_restart_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    after_queue_creation: bool,
):
    (
        scheduler,
        store,
        queue,
        reports,
        missions,
        definition,
        expected,
        dependent_id,
    ) = _scheduler_with_completed_prerequisite(tmp_path)
    original_materialize = scheduler._materializer.materialize

    def interrupt_materialize(*args, **kwargs):
        if after_queue_creation:
            original_materialize(*args, **kwargs)
        raise KeyboardInterrupt("injected queue boundary crash")

    monkeypatch.setattr(
        scheduler._materializer, "materialize", interrupt_materialize
    )
    with pytest.raises(KeyboardInterrupt, match="queue boundary"):
        scheduler.run_once(definition.mission_id)

    interrupted = MissionStore(missions).load_state(definition.mission_id)
    assert interrupted.task_states["prerequisite"].status is (
        MissionTaskStatus.succeeded
    )
    assert interrupted.task_states["dependent"].status is (
        MissionTaskStatus.pending
    )
    assert (queue.pending / f"{dependent_id}.json").exists() is (
        after_queue_creation
    )

    _assert_restarted_dependent_is_exactly_once(
        store,
        queue,
        reports,
        missions,
        definition,
        expected,
        dependent_id,
    )


def test_repeated_scheduler_runs_release_every_authoritative_lock(
    tmp_path: Path,
):
    (
        scheduler,
        _,
        _,
        _,
        _,
        definition,
        _,
        _,
    ) = _scheduler_with_completed_prerequisite(tmp_path)

    for _ in range(3):
        scheduler.run_once(definition.mission_id)
        assert FileLock._owners == {}


def test_failed_success_event_blocks_dependency_until_repair_retry(
    tmp_path: Path,
    monkeypatch,
):
    from tools.ai_controller.mission import repair
    from tools.ai_controller.mission.scheduler import MissionScheduler

    (
        store,
        queue,
        reports,
        missions,
        definition,
        expected,
        action,
    ) = _completion_repair_with_dependent(tmp_path)
    original_append = MissionEventLog.append_locked
    failed = False

    def fail_task_succeeded(self, event, token):
        nonlocal failed
        if event.event_type == "task_succeeded" and not failed:
            failed = True
            raise OSError("injected task_succeeded failure")
        return original_append(self, event, token)

    monkeypatch.setattr(
        MissionEventLog, "append_locked", fail_task_succeeded
    )
    first = repair.apply_mission_repair(
        action, store, queue, reports, missions
    )
    monkeypatch.setattr(
        MissionEventLog, "append_locked", original_append
    )

    restarted_scheduler = MissionScheduler(
        store=MissionStore(missions),
        queue=DurableQueue(queue.root),
        reports_root=reports,
        missions_root=missions,
    )
    restarted_scheduler.run_once(definition.mission_id)
    before_retry = MissionStore(missions).load_state(definition.mission_id)

    retry_action = next(
        item
        for item in repair.plan_mission_repairs(
            definition.mission_id,
            MissionStore(missions),
            DurableQueue(queue.root),
            reports,
        )
        if item.task_id == "prerequisite"
    )
    second = repair.apply_mission_repair(
        retry_action,
        MissionStore(missions),
        DurableQueue(queue.root),
        reports,
        missions,
    )
    events = MissionEventLog(
        missions / "events", definition.mission_id
    ).read_all()

    assert first.status is repair.RepairApplyStatus.FAILED
    assert before_retry.task_states["prerequisite"].status is (
        MissionTaskStatus.queued
    )
    assert before_retry.task_states["dependent"].status is (
        MissionTaskStatus.pending
    )
    dependent_id = TaskMaterializer(
        queue, reports
    ).make_queue_task_id(definition.mission_id, "dependent")
    assert not (queue.pending / f"{dependent_id}.json").exists()
    assert second.status in {
        repair.RepairApplyStatus.APPLIED,
        repair.RepairApplyStatus.NO_LONGER_NEEDED,
    }
    assert sum(
        event.event_type == "task_succeeded"
        and event.queue_task_id == expected.id
        for event in events
    ) == 1


def test_interruption_after_success_event_retries_without_duplicate(
    tmp_path: Path,
    monkeypatch,
):
    from tools.ai_controller.mission import repair

    (
        store,
        queue,
        reports,
        missions,
        definition,
        expected,
        action,
    ) = _completion_repair_with_dependent(tmp_path)
    original = repair._append_repair_event
    failed = False

    def fail_once(selected, event_log, *effect):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected interruption before repair_applied")
        return original(selected, event_log, *effect)

    monkeypatch.setattr(repair, "_append_repair_event", fail_once)
    first = repair.apply_mission_repair(
        action, store, queue, reports, missions
    )
    monkeypatch.setattr(repair, "_append_repair_event", original)
    second = repair.apply_mission_repair(
        action,
        MissionStore(missions),
        DurableQueue(queue.root),
        reports,
        missions,
    )
    events = MissionEventLog(
        missions / "events", definition.mission_id
    ).read_all()

    assert first.status is repair.RepairApplyStatus.FAILED
    assert second.status is repair.RepairApplyStatus.APPLIED
    assert sum(
        event.event_type == "task_succeeded"
        and event.queue_task_id == expected.id
        for event in events
    ) == 1
    assert sum(event.event_type == "repair_applied" for event in events) == 1


def test_forged_action_identity_is_rejected_as_stale(tmp_path: Path):
    from tools.ai_controller.mission.repair import (
        RepairApplyStatus,
        apply_mission_repair,
    )

    store, queue, reports, missions, definition, actions = _plan(tmp_path)
    forged = replace(actions[0], action_id="repair-forged")

    result = apply_mission_repair(forged, store, queue, reports, missions)

    assert result.status is RepairApplyStatus.STALE
    assert not list(queue.pending.glob("*.json"))


@pytest.mark.parametrize(
    "forged_created_at",
    [
        "1900-01-01T00:00:00+00:00",
        "not-a-timestamp",
    ],
)
def test_presentation_created_at_never_controls_durable_queue_time(
    tmp_path: Path,
    forged_created_at: str,
):
    from tools.ai_controller.mission.repair import (
        RepairApplyStatus,
        _canonical_action_id,
        apply_mission_repair,
    )

    store, queue, reports, missions, definition, actions = _plan(tmp_path)
    original = actions[0]
    forged = replace(original, created_at=forged_created_at)
    before = datetime.now(timezone.utc)

    result = apply_mission_repair(
        forged, store, queue, reports, missions
    )
    after = datetime.now(timezone.utc)
    durable = store.load_state(definition.mission_id).task_states["task-a"]

    assert _canonical_action_id(forged) == original.action_id
    assert result.status is RepairApplyStatus.APPLIED
    assert durable.queued_at != forged_created_at
    queued_at = datetime.fromisoformat(durable.queued_at)
    assert before <= queued_at <= after


def test_changing_created_at_alone_does_not_change_queue_payload(
    tmp_path: Path,
):
    from tools.ai_controller.mission.repair import (
        RepairApplyStatus,
        apply_mission_repair,
    )

    first = _plan(tmp_path / "first")
    second = _plan(tmp_path / "second")
    first_action = first[-1][0]
    forged = replace(second[-1][0], created_at="invalid")

    first_result = apply_mission_repair(
        first_action, first[0], first[1], first[2], first[3]
    )
    second_result = apply_mission_repair(
        forged, second[0], second[1], second[2], second[3]
    )
    first_payload = json.loads(
        (
            first[1].pending / f"{first_action.expected_queue_id}.json"
        ).read_text(encoding="utf-8")
    )
    second_payload = json.loads(
        (
            second[1].pending / f"{forged.expected_queue_id}.json"
        ).read_text(encoding="utf-8")
    )

    assert first_action.action_id == forged.action_id
    assert first_result.status is RepairApplyStatus.APPLIED
    assert second_result.status is RepairApplyStatus.APPLIED
    assert first_payload == second_payload


def test_full_queue_budget_blocks_rematerialization(tmp_path: Path):
    from tools.ai_controller.mission.repair import (
        RepairClassification,
        plan_mission_repairs,
    )

    store, queue, reports, _, definition = _create_mission(
        tmp_path,
        budget_usage=BudgetUsage(queued_tasks=1),
        budgets=MissionBudget(max_queued_tasks=1),
    )

    action = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]

    assert action.classification is RepairClassification.HUMAN_REVIEW_REQUIRED
    assert action.finding == "EXECUTION_BUDGET_EXHAUSTED"


def test_planning_recomputes_elapsed_runtime_budget(tmp_path: Path):
    from tools.ai_controller.mission.repair import (
        RepairClassification,
        plan_mission_repairs,
    )

    started_at = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()
    store, queue, reports, _, definition = _create_mission(
        tmp_path,
        budgets=MissionBudget(max_runtime_seconds=1),
        budget_usage=BudgetUsage(elapsed_seconds=0),
        started_at=started_at,
    )

    action = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]

    assert action.classification is RepairClassification.HUMAN_REVIEW_REQUIRED
    assert action.finding == "EXECUTION_BUDGET_EXHAUSTED"
    assert not list(queue.pending.glob("*.json"))


@pytest.mark.parametrize("budget_change", ["task_retry", "mission_runtime"])
def test_budget_definition_changes_after_planning_are_rejected(
    tmp_path: Path,
    budget_change: str,
):
    from tools.ai_controller.mission.repair import (
        RepairApplyStatus,
        apply_mission_repair,
    )
    from tools.ai_controller.mission.store import _atomic_json

    store, queue, reports, missions, definition, actions = _plan(tmp_path)
    _, mission_path = store.find_mission(definition.mission_id)
    envelope = store._load_envelope(mission_path)
    if budget_change == "task_retry":
        envelope["definition"]["tasks"][0]["max_attempts"] = 0
    else:
        envelope["definition"]["budgets"]["max_runtime_seconds"] = 0
    _atomic_json(mission_path, envelope)

    result = apply_mission_repair(
        actions[0], store, queue, reports, missions
    )

    assert result.status in {
        RepairApplyStatus.STALE,
        RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
    }
    assert not list(queue.pending.glob("*.json"))


def test_budget_crossing_during_apply_prevents_mutation(
    tmp_path: Path,
    monkeypatch,
):
    from tools.ai_controller.mission.repair import (
        RepairApplyStatus,
        apply_mission_repair,
    )
    from tools.ai_controller.mission.scheduler import MissionScheduler

    store, queue, reports, missions, _, actions = _plan(
        tmp_path,
        budgets=MissionBudget(max_runtime_seconds=1),
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    original = MissionScheduler._update_budget_usage
    calls = 0

    def advancing_budget(self, definition, state):
        nonlocal calls
        refreshed = original(self, definition, state)
        calls += 1
        refreshed.budget_usage.elapsed_seconds = 0 if calls == 1 else 2
        return refreshed

    monkeypatch.setattr(
        MissionScheduler,
        "_update_budget_usage",
        advancing_budget,
    )

    result = apply_mission_repair(
        actions[0], store, queue, reports, missions
    )

    assert calls >= 2
    assert result.status in {
        RepairApplyStatus.STALE,
        RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
    }
    assert not list(queue.pending.glob("*.json"))


def test_store_lock_failure_returns_failed_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from tools.ai_controller.mission.repair import (
        RepairApplyStatus,
        apply_mission_repair,
    )

    store, queue, reports, missions, definition, actions = _plan(tmp_path)

    class FailingLock:
        def __enter__(self):
            raise OSError("injected lock failure")

        def __exit__(self, *_):
            return None

    monkeypatch.setattr(store, "_lock", FailingLock())
    result = apply_mission_repair(actions[0], store, queue, reports, missions)

    assert result.status is RepairApplyStatus.FAILED
    assert not list(queue.pending.glob("*.json"))


@pytest.mark.parametrize(
    ("destination_status", "malformed"),
    [
        ("paused", False),
        ("failed", False),
        ("cancelled", False),
        ("pending", False),
        ("failed", True),
    ],
)
def test_split_mission_state_fails_closed_without_mutation(
    tmp_path: Path,
    destination_status: str,
    malformed: bool,
):
    from tools.ai_controller.mission.repair import (
        RepairApplyStatus,
        RepairClassification,
        apply_mission_repair,
        plan_mission_repairs,
    )

    store, queue, reports, missions, definition, actions = _plan(tmp_path)
    original = actions[0]
    _, source = store.find_mission(definition.mission_id)
    duplicate = (
        missions / destination_status / f"{definition.mission_id}.json"
    )
    duplicate.parent.mkdir(parents=True, exist_ok=True)
    duplicate.write_text(
        "{" if malformed else source.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    replanned = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )
    result = apply_mission_repair(
        original, store, queue, reports, missions
    )
    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    assert replanned
    assert all(
        action.classification
        in {
            RepairClassification.HUMAN_REVIEW_REQUIRED,
            RepairClassification.UNSUPPORTED,
        }
        for action in replanned
    )
    assert result.status is RepairApplyStatus.HUMAN_REVIEW_REQUIRED
    assert before == after
    assert not list(queue.pending.glob("*.json"))


def test_rematerialization_updates_durable_queue_budget(tmp_path: Path):
    from tools.ai_controller.mission.repair import (
        RepairApplyStatus,
        apply_mission_repair,
    )

    store, queue, reports, missions, definition, actions = _plan(tmp_path)

    result = apply_mission_repair(actions[0], store, queue, reports, missions)

    assert result.status is RepairApplyStatus.APPLIED
    assert store.load_state(definition.mission_id).budget_usage.queued_tasks == 1


def test_budget_exhausted_completion_remains_discoverable(tmp_path: Path):
    from tools.ai_controller.mission.repair import (
        RepairApplyStatus,
        apply_mission_repair,
        plan_mission_repairs,
    )

    store, queue, reports, missions, definition = _create_mission(
        tmp_path,
        task_status=MissionTaskStatus.queued,
        budgets=MissionBudget(max_total_attempts=1),
    )
    expected = _expected_task(queue, reports, definition, "task-a")
    _write_succeeded_evidence(queue, reports, expected)
    state = store.load_state(definition.mission_id)
    state.task_states["task-a"].queue_task_id = expected.id
    store.update_state(definition.mission_id, state)
    action = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]

    result = apply_mission_repair(action, store, queue, reports, missions)

    assert result.status is RepairApplyStatus.APPLIED
    assert store.load_state(definition.mission_id).status is (
        MissionStatus.budget_exhausted
    )
    assert store.find_mission(definition.mission_id)[0] is (
        MissionStatus.budget_exhausted
    )


def test_forged_completion_recovery_cannot_write_audit_evidence(tmp_path: Path):
    from tools.ai_controller.mission import repair

    store, queue, reports, missions, definition = _create_mission(
        tmp_path,
        task_status=MissionTaskStatus.queued,
    )
    expected = _expected_task(queue, reports, definition, "task-a")
    _write_succeeded_evidence(queue, reports, expected)
    state = store.load_state(definition.mission_id)
    state.task_states["task-a"].queue_task_id = expected.id
    store.update_state(definition.mission_id, state)
    action = repair.plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]
    assert repair.apply_mission_repair(
        action, store, queue, reports, missions
    ).status is repair.RepairApplyStatus.APPLIED
    forged = replace(
        action,
        action_id="",
        expected_queue_id="forged-queue-id",
        reason="forged recovery",
    )
    forged = replace(
        forged,
        action_id=repair._canonical_action_id(forged),
    )

    result = repair.apply_mission_repair(
        forged, store, queue, reports, missions
    )
    events = MissionEventLog(missions / "events", definition.mission_id).read_all()

    assert result.status is not repair.RepairApplyStatus.APPLIED
    assert not any(
        event.metadata.get("action_id") == forged.action_id
        for event in events
    )


def test_interrupted_after_state_write_recovers_only_started_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from tools.ai_controller.mission import repair

    store, queue, reports, missions, definition, actions = _plan(tmp_path)
    original = repair._append_repair_event
    calls = 0

    def fail_once(action, event_log, *effect):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected interruption after state write")
        return original(action, event_log, *effect)

    monkeypatch.setattr(repair, "_append_repair_event", fail_once)
    first = repair.apply_mission_repair(
        actions[0], store, queue, reports, missions
    )
    second = repair.apply_mission_repair(
        actions[0], store, queue, reports, missions
    )

    assert first.status is repair.RepairApplyStatus.FAILED
    assert second.status is repair.RepairApplyStatus.APPLIED
    events = MissionEventLog(missions / "events", definition.mission_id).read_all()
    assert sum(event.event_type == "repair_started" for event in events) == 1
    assert sum(event.event_type == "repair_applied" for event in events) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tests", []),
        ("tests", [{}]),
        ("tests", [{"exit_code": 0, "timed_out": False}]),
        ("tests", [{"argv": ["python3", "-m", "pytest"], "timed_out": False}]),
        ("started_at", "2026-08-04T12:00:00"),
        ("finished_at", "2026-08-04"),
        ("started_at", "not-a-time"),
        ("finished_at", "2026-08-04T11:59:00+00:00"),
    ],
)
def test_required_completion_evidence_fails_closed(
    tmp_path: Path,
    field: str,
    value,
):
    from tools.ai_controller.mission.repair import (
        RepairClassification,
        plan_mission_repairs,
    )

    store, queue, reports, _, definition = _create_mission(
        tmp_path,
        task_status=MissionTaskStatus.queued,
        task_tests=[["python3", "-m", "pytest"]],
    )
    expected = _expected_task(queue, reports, definition, "task-a")
    _write_succeeded_evidence(queue, reports, expected)
    state = store.load_state(definition.mission_id)
    state.task_states["task-a"].queue_task_id = expected.id
    store.update_state(definition.mission_id, state)
    report_path = reports / f"{expected.id}.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report[field] = value
    report_path.write_text(json.dumps(report), encoding="utf-8")

    action = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]

    assert action.classification is RepairClassification.HUMAN_REVIEW_REQUIRED
    assert action.proposed_action.value != "COMPLETE_FROM_DURABLE_SUCCESS"


def test_empty_verifier_evidence_never_proves_completion(tmp_path: Path):
    from tools.ai_controller.mission.repair import (
        RepairClassification,
        plan_mission_repairs,
    )

    store, queue, reports, _, definition = _create_mission(
        tmp_path,
        task_status=MissionTaskStatus.queued,
    )
    expected = _expected_task(queue, reports, definition, "task-a")
    _write_succeeded_evidence(queue, reports, expected)
    state = store.load_state(definition.mission_id)
    state.task_states["task-a"].queue_task_id = expected.id
    store.update_state(definition.mission_id, state)
    report_path = reports / f"{expected.id}.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["tests"] = []
    report_path.write_text(json.dumps(report), encoding="utf-8")

    action = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]

    assert action.classification is RepairClassification.HUMAN_REVIEW_REQUIRED


@pytest.mark.parametrize(
    "event_type",
    [
        "task_succeeded",
        "task_failed",
        "task_lost",
        "task_cancelled",
        "task_blocked",
    ],
)
def test_legacy_terminal_event_evidence_is_bound_and_blocks_rematerialization(
    tmp_path: Path,
    event_type: str,
):
    from tools.ai_controller.mission.repair import (
        RepairClassification,
        plan_mission_repairs,
    )

    store, queue, reports, missions, definition = _create_mission(tmp_path)
    legacy_id = make_legacy_queue_task_id(definition.mission_id, "task-a")
    MissionEventLog(missions / "events", definition.mission_id).append(
        make_event(
            event_type,
            definition.mission_id,
            task_id="task-a",
            queue_task_id=legacy_id,
        )
    )

    action = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]

    assert action.classification is RepairClassification.HUMAN_REVIEW_REQUIRED
    assert any(
        item
        == "accepted_queue_ids="
        + ",".join(sorted((action.expected_queue_id, legacy_id)))
        for item in action.evidence
    )
    assert not list(queue.pending.glob("*.json"))


def test_event_snapshot_hash_and_events_come_from_one_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from tools.ai_controller.mission import repair

    store, queue, reports, missions, definition = _create_mission(tmp_path)
    log = MissionEventLog(missions / "events", definition.mission_id)
    log.append(make_event("task_ready", definition.mission_id, task_id="task-a"))
    original_read = Path.read_bytes
    reads = 0

    def counted_read(path: Path):
        nonlocal reads
        if path == log._log_path:
            reads += 1
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read)
    actions = repair.plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )

    assert actions
    assert reads == 1


@pytest.mark.parametrize(
    "tail",
    [
        b'{"event_type":"partial"}',
        b'{"event_type":',
        b'{"event_type":"ok"}\n{"event_type":',
        b'{"event_type":"ok"}\n\xff',
    ],
)
def test_corrupt_event_tail_is_preserved_and_blocks_append(
    tmp_path: Path,
    tail: bytes,
):
    from tools.ai_controller.mission.events import EventLogCorruptionError

    path = tmp_path / "events" / "tail.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(tail)
    log = MissionEventLog(path.parent, "tail")

    with pytest.raises(EventLogCorruptionError):
        log.append(make_event("task_ready", "tail", task_id="task-a"))

    assert path.read_bytes() == tail


def test_preexisting_success_requires_restart_recovery_not_new_repair(
    tmp_path: Path,
):
    from tools.ai_controller.mission.repair import (
        RepairClassification,
        plan_mission_repairs,
    )

    store, queue, reports, missions, definition = _create_mission(tmp_path)
    expected = _expected_task(queue, reports, definition, "task-a")
    MissionEventLog(missions / "events", definition.mission_id).append(
        make_event(
            "task_succeeded",
            definition.mission_id,
            task_id="task-a",
            queue_task_id=expected.id,
        )
    )

    action = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]

    assert action.classification is RepairClassification.HUMAN_REVIEW_REQUIRED
    assert action.finding == "PREEXISTING_SUCCESS_REQUIRES_RECONCILIATION"
    assert not any(
        event.event_type == "repair_started"
        for event in MissionEventLog(
            missions / "events", definition.mission_id
        ).read_all()
    )


@pytest.mark.parametrize("mutation", ["replace", "delete", "conflict"])
def test_report_change_between_validation_and_completion_never_mutates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
):
    from tools.ai_controller.mission import repair
    from tools.ai_controller.mission.scheduler import MissionScheduler

    store, queue, reports, missions, definition = _create_mission(
        tmp_path,
        task_status=MissionTaskStatus.queued,
    )
    expected = _expected_task(queue, reports, definition, "task-a")
    _write_succeeded_evidence(queue, reports, expected)
    state = store.load_state(definition.mission_id)
    state.task_states["task-a"].queue_task_id = expected.id
    store.update_state(definition.mission_id, state)
    action = repair.plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )[0]
    report_path = reports / f"{expected.id}.json"
    original = MissionScheduler._reconcile_tasks

    def mutate_then_reconcile(self, mission_definition, mission_state, **kwargs):
        if mutation == "replace":
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            payload["files_changed"] = ["replaced.txt"]
            replacement = reports / "replacement.tmp"
            replacement.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(replacement, report_path)
        elif mutation == "delete":
            report_path.unlink()
        else:
            (reports / "conflict.json").write_bytes(report_path.read_bytes())
        return original(self, mission_definition, mission_state, **kwargs)

    monkeypatch.setattr(
        MissionScheduler, "_reconcile_tasks", mutate_then_reconcile
    )
    result = repair.apply_mission_repair(
        action, store, queue, reports, missions
    )

    assert result.status in {
        repair.RepairApplyStatus.STALE,
        repair.RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
    }
    assert store.load_state(definition.mission_id).task_states[
        "task-a"
    ].status is MissionTaskStatus.queued


def test_budget_expiring_after_repair_started_leaves_explicit_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from tools.ai_controller.mission import repair
    from tools.ai_controller.mission.scheduler import MissionScheduler

    store, queue, reports, missions, definition, actions = _plan(
        tmp_path,
        budgets=MissionBudget(max_runtime_seconds=1),
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    original = MissionScheduler._update_budget_usage
    calls = 0

    def expires_after_intent(self, mission_definition, mission_state):
        nonlocal calls
        refreshed = original(self, mission_definition, mission_state)
        calls += 1
        refreshed.budget_usage.elapsed_seconds = 2 if calls >= 4 else 0
        return refreshed

    monkeypatch.setattr(
        MissionScheduler, "_update_budget_usage", expires_after_intent
    )
    result = repair.apply_mission_repair(
        actions[0], store, queue, reports, missions
    )
    events = MissionEventLog(
        missions / "events", definition.mission_id
    ).read_all()

    assert result.status is repair.RepairApplyStatus.HUMAN_REVIEW_REQUIRED
    assert not list(queue.pending.glob("*.json"))
    assert any(event.event_type == "repair_expired" for event in events)


@pytest.mark.parametrize(
    "expire_after_event,violation",
    [
        ("repair_started", "max_runtime_seconds"),
        ("task_succeeded", "max_runtime_seconds"),
        ("task_succeeded", "task_runtime_seconds"),
    ],
)
def test_completion_budget_crossing_during_durable_event_blocks_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expire_after_event: str,
    violation: str,
):
    from tools.ai_controller.mission import repair
    from tools.ai_controller.mission.scheduler import MissionScheduler

    (
        store,
        queue,
        reports,
        missions,
        definition,
        expected,
        action,
    ) = _completion_repair_with_dependent(tmp_path)
    expired = False
    original_append = MissionEventLog.append_locked

    def append_and_cross_deadline(self, event, token):
        nonlocal expired
        original_append(self, event, token)
        if event.event_type == expire_after_event:
            expired = True

    def runtime_violations(*_args, **_kwargs):
        return [violation] if expired else []

    monkeypatch.setattr(
        MissionEventLog, "append_locked", append_and_cross_deadline
    )
    monkeypatch.setattr(
        repair,
        "_runtime_budget_violations",
        runtime_violations,
        raising=False,
    )
    result = repair.apply_mission_repair(
        action, store, queue, reports, missions
    )
    state = MissionStore(missions).load_state(definition.mission_id)
    events = MissionEventLog(
        missions / "events", definition.mission_id
    ).read_all()
    dependent_id = TaskMaterializer(
        queue, reports
    ).make_queue_task_id(definition.mission_id, "dependent")

    assert result.status is repair.RepairApplyStatus.HUMAN_REVIEW_REQUIRED
    assert state.task_states["prerequisite"].status is MissionTaskStatus.queued
    assert state.task_states["dependent"].status is MissionTaskStatus.pending
    assert not (queue.pending / f"{dependent_id}.json").exists()
    assert sum(event.event_type == "repair_started" for event in events) == 1
    assert sum(event.event_type == "repair_expired" for event in events) == 1
    assert sum(event.event_type == "task_succeeded" for event in events) == (
        expire_after_event == "task_succeeded"
    )
    marker = next(
        event for event in events if event.event_type == "repair_expired"
    )
    assert marker.metadata["budget_violations"] == [violation]
    assert marker.metadata["success_evidence_durable"] is (
        expire_after_event == "task_succeeded"
    )

    retry = repair.apply_mission_repair(
        action,
        MissionStore(missions),
        DurableQueue(queue.root),
        reports,
        missions,
    )
    MissionScheduler(
        MissionStore(missions),
        DurableQueue(queue.root),
        reports,
        missions,
        emit_materializer_events=False,
    ).run_once(definition.mission_id)
    retry_events = MissionEventLog(
        missions / "events", definition.mission_id
    ).read_all()
    retried_state = MissionStore(missions).load_state(definition.mission_id)

    assert retry.status is repair.RepairApplyStatus.HUMAN_REVIEW_REQUIRED
    assert retried_state.task_states["prerequisite"].status is (
        MissionTaskStatus.queued
    )
    assert sum(event.event_type == "repair_started" for event in retry_events) == 1
    assert sum(event.event_type == "repair_expired" for event in retry_events) == 1
    assert sum(event.event_type == "task_succeeded" for event in retry_events) == (
        expire_after_event == "task_succeeded"
    )


def test_completion_budget_rechecked_immediately_before_state_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from tools.ai_controller.mission import repair

    store, queue, reports, missions, definition, _, action = (
        _completion_repair_with_dependent(tmp_path)
    )
    checks = 0

    def expires_on_final_boundary(*_args, **_kwargs):
        nonlocal checks
        checks += 1
        return ["max_runtime_seconds"] if checks >= 4 else []

    monkeypatch.setattr(
        repair,
        "_runtime_budget_violations",
        expires_on_final_boundary,
        raising=False,
    )
    result = repair.apply_mission_repair(
        action, store, queue, reports, missions
    )
    state = MissionStore(missions).load_state(definition.mission_id)

    assert checks >= 4
    assert result.status is repair.RepairApplyStatus.HUMAN_REVIEW_REQUIRED
    assert state.task_states["prerequisite"].status is MissionTaskStatus.queued


def test_scheduler_rejects_success_authorization_marked_expired(
    tmp_path: Path,
):
    from tools.ai_controller.mission.scheduler import MissionScheduler

    (
        _,
        store,
        queue,
        reports,
        missions,
        definition,
        expected,
        dependent_id,
    ) = _scheduler_with_completed_prerequisite(tmp_path)
    log = MissionEventLog(missions / "events", definition.mission_id)
    log.append(
        make_event(
            "repair_started",
            definition.mission_id,
            task_id="prerequisite",
            queue_task_id=expected.id,
            metadata={
                "action_id": "expired-action",
                "repair_action": "COMPLETE_FROM_DURABLE_SUCCESS",
            },
        )
    )
    log.append(
        make_event(
            "task_succeeded",
            definition.mission_id,
            task_id="prerequisite",
            queue_task_id=expected.id,
        )
    )
    log.append(
        make_event(
            "repair_expired",
            definition.mission_id,
            task_id="prerequisite",
            queue_task_id=expected.id,
            metadata={
                "action_id": "expired-action",
                "success_evidence_durable": True,
                "budget_violations": ["max_runtime_seconds"],
            },
        )
    )

    MissionScheduler(
        store,
        queue,
        reports,
        missions,
        emit_materializer_events=False,
    ).run_once(definition.mission_id)
    state = store.load_state(definition.mission_id)

    assert state.task_states["prerequisite"].status is MissionTaskStatus.queued
    assert state.task_states["dependent"].status is MissionTaskStatus.pending
    assert not (queue.pending / f"{dependent_id}.json").exists()


def test_action_id_only_repair_applied_does_not_suppress_required_effect(
    tmp_path: Path,
):
    from tools.ai_controller.mission import repair

    store, queue, reports, missions, definition, actions = _plan(tmp_path)
    action = actions[0]
    MissionEventLog(missions / "events", definition.mission_id).append(
        make_event(
            "repair_applied",
            definition.mission_id,
            task_id=action.task_id,
            queue_task_id=action.expected_queue_id,
            metadata={"action_id": action.action_id},
        )
    )

    result = repair.apply_mission_repair(
        action, store, queue, reports, missions
    )

    assert result.status is repair.RepairApplyStatus.HUMAN_REVIEW_REQUIRED
    assert not list(queue.pending.glob("*.json"))


def test_queue_appearing_after_plan_without_bound_audit_is_not_adopted(
    tmp_path: Path,
):
    from tools.ai_controller.mission import repair

    store, queue, reports, missions, definition, actions = _plan(tmp_path)
    action = actions[0]
    queue.enqueue(_expected_task(
        queue, reports, definition, action.task_id
    ))

    result = repair.apply_mission_repair(
        action, store, queue, reports, missions
    )
    state = store.load_state(definition.mission_id)
    events = MissionEventLog(
        missions / "events", definition.mission_id
    ).read_all()

    assert result.status in {
        repair.RepairApplyStatus.STALE,
        repair.RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
    }
    assert result.status is not repair.RepairApplyStatus.NO_LONGER_NEEDED
    assert state.task_states[action.task_id].status is MissionTaskStatus.pending
    assert not any(event.event_type == "repair_applied" for event in events)


def _append_repair_started(
    missions: Path,
    action,
    *,
    metadata: dict,
    mission_id: str | None = None,
    task_id: str | None = None,
    queue_task_id: str | None = None,
) -> None:
    MissionEventLog(missions / "events", action.mission_id).append(
        make_event(
            "repair_started",
            action.mission_id if mission_id is None else mission_id,
            task_id=action.task_id if task_id is None else task_id,
            queue_task_id=(
                action.expected_queue_id
                if queue_task_id is None
                else queue_task_id
            ),
            reason=action.reason,
            metadata=metadata,
        )
    )


@pytest.mark.parametrize(
    "invalid_binding",
    [
        "matching action_id only",
        "wrong mission_id",
        "missing mission_id",
        "wrong mission_task_id",
        "missing mission_task_id",
        "wrong queue_task_id",
        "missing queue_task_id",
        "wrong finding",
        "missing finding",
        "wrong repair_action",
        "missing repair_action",
        "wrong revision",
        "missing revision",
        "wrong evidence_fingerprint",
        "missing evidence_fingerprint",
        "changed queue payload",
        "queue record belongs to another mission",
        "queue record belongs to another task",
        "no repair_started",
        "historical repair_started",
    ],
)
def test_incomplete_or_conflicting_repair_started_never_authorizes_recovery(
    tmp_path: Path,
    invalid_binding: str,
):
    from tools.ai_controller.mission import repair

    store, queue, reports, missions, definition, actions = _plan(tmp_path)
    action = actions[0]
    metadata = {
        "action_id": action.action_id,
        "mission_id": action.mission_id,
        "mission_task_id": action.task_id,
        "expected_queue_id": action.expected_queue_id,
        "finding": action.finding,
        "repair_action": action.proposed_action.value,
        "revision": action.revision,
        "evidence_fingerprint": repair._evidence_fingerprint(action),
        "expected_queue_payload_fingerprint": hashlib.sha256(
            repair._canonical_json(
                _expected_task(
                    queue, reports, definition, action.task_id
                ).to_dict()
            ).encode("utf-8")
        ).hexdigest(),
    }
    event_kwargs: dict = {}
    queue_task = _expected_task(
        queue, reports, definition, action.task_id
    )

    if invalid_binding == "matching action_id only":
        metadata = {"action_id": action.action_id}
    elif invalid_binding == "wrong mission_id":
        event_kwargs["mission_id"] = "other-mission"
    elif invalid_binding == "missing mission_id":
        event_kwargs["mission_id"] = ""
    elif invalid_binding == "wrong mission_task_id":
        event_kwargs["task_id"] = "other-task"
    elif invalid_binding == "missing mission_task_id":
        event_kwargs["task_id"] = ""
    elif invalid_binding == "wrong queue_task_id":
        event_kwargs["queue_task_id"] = "other-queue-id"
    elif invalid_binding == "missing queue_task_id":
        event_kwargs["queue_task_id"] = ""
    elif invalid_binding == "wrong finding":
        metadata["finding"] = "OTHER_FINDING"
    elif invalid_binding == "missing finding":
        metadata.pop("finding")
    elif invalid_binding == "wrong repair_action":
        metadata["repair_action"] = "COMPLETE_FROM_DURABLE_SUCCESS"
    elif invalid_binding == "missing repair_action":
        metadata.pop("repair_action")
    elif invalid_binding == "wrong revision":
        metadata["revision"] = "other-revision"
    elif invalid_binding == "missing revision":
        metadata.pop("revision")
    elif invalid_binding == "wrong evidence_fingerprint":
        metadata["evidence_fingerprint"] = "other-fingerprint"
    elif invalid_binding == "missing evidence_fingerprint":
        metadata.pop("evidence_fingerprint")
    elif invalid_binding == "changed queue payload":
        queue_task = replace(queue_task, prompt="changed prompt")
    elif invalid_binding == "queue record belongs to another mission":
        queue_task = replace(
            queue_task,
            metadata={
                **queue_task.metadata,
                "mission_id": "other-mission",
            },
        )
    elif invalid_binding == "queue record belongs to another task":
        queue_task = replace(
            queue_task,
            metadata={
                **queue_task.metadata,
                "mission_task_id": "other-task",
            },
        )
    elif invalid_binding == "historical repair_started":
        assert repair.apply_mission_repair(
            action, store, queue, reports, missions
        ).status is repair.RepairApplyStatus.APPLIED
        (
            queue.pending / f"{action.expected_queue_id}.json"
        ).unlink()
        action = repair.plan_mission_repairs(
            definition.mission_id, store, queue, reports
        )[0]
        queue_task = _expected_task(
            queue, reports, definition, action.task_id
        )

    if invalid_binding in {
        "wrong mission_id",
        "missing mission_id",
        "missing mission_task_id",
        "missing queue_task_id",
    }:
        with pytest.raises(EventLogCorruptionError):
            _append_repair_started(
                missions,
                action,
                metadata=metadata,
                **event_kwargs,
            )
        return
    if invalid_binding not in {
        "no repair_started",
        "historical repair_started",
    }:
        _append_repair_started(
            missions,
            action,
            metadata=metadata,
            **event_kwargs,
        )
    queue.enqueue(queue_task)
    before_mission = store.find_mission(definition.mission_id)[1].read_bytes()
    before_state = store.load_state(definition.mission_id).to_dict()
    before_applied = sum(
        event.event_type == "repair_applied"
        for event in MissionEventLog(
            missions / "events", definition.mission_id
        ).read_all()
    )

    result = repair.apply_mission_repair(
        action, store, queue, reports, missions
    )
    state = store.load_state(definition.mission_id)
    events = MissionEventLog(
        missions / "events", definition.mission_id
    ).read_all()

    assert result.status in {
        repair.RepairApplyStatus.STALE,
        repair.RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
    }
    assert result.status is not repair.RepairApplyStatus.APPLIED
    assert result.status is not repair.RepairApplyStatus.NO_LONGER_NEEDED
    assert state.to_dict() == before_state
    assert store.find_mission(definition.mission_id)[1].read_bytes() == (
        before_mission
    )
    assert sum(
        event.event_type == "repair_applied" for event in events
    ) == before_applied


def test_exact_fully_bound_repair_started_authorizes_interrupted_recovery(
    tmp_path: Path,
):
    from tools.ai_controller.mission import repair

    store, queue, reports, missions, definition, actions = _plan(tmp_path)
    action = actions[0]
    queue_task = _expected_task(
        queue, reports, definition, action.task_id
    )
    metadata = {
        "action_id": action.action_id,
        "mission_id": action.mission_id,
        "mission_task_id": action.task_id,
        "expected_queue_id": action.expected_queue_id,
        "finding": action.finding,
        "repair_action": action.proposed_action.value,
        "revision": action.revision,
        "evidence_fingerprint": repair._evidence_fingerprint(action),
        "expected_queue_payload_fingerprint": hashlib.sha256(
            repair._canonical_json(queue_task.to_dict()).encode("utf-8")
        ).hexdigest(),
    }
    _append_repair_started(missions, action, metadata=metadata)
    queue.enqueue(queue_task)

    result = repair.apply_mission_repair(
        action, store, queue, reports, missions
    )
    state = store.load_state(definition.mission_id)
    events = MissionEventLog(
        missions / "events", definition.mission_id
    ).read_all()

    assert result.status is repair.RepairApplyStatus.APPLIED
    assert state.task_states[action.task_id].status is MissionTaskStatus.queued
    assert state.task_states[action.task_id].queue_task_id == (
        action.expected_queue_id
    )
    assert sum(event.event_type == "repair_started" for event in events) == 1
    assert sum(event.event_type == "repair_applied" for event in events) == 1


def test_concurrent_external_queue_creation_during_apply_is_not_adopted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from tools.ai_controller.mission import repair

    store, queue, reports, missions, definition, actions = _plan(tmp_path)
    action = actions[0]
    original_plan = repair.plan_mission_repairs
    created = False

    def plan_after_external_creation(*args, **kwargs):
        nonlocal created
        if not created:
            created = True
            queue_atomic_json(
                queue.pending / f"{action.expected_queue_id}.json",
                _expected_task(
                    queue, reports, definition, action.task_id
                ).to_dict(),
            )
        return original_plan(*args, **kwargs)

    monkeypatch.setattr(
        repair, "plan_mission_repairs", plan_after_external_creation
    )
    result = repair.apply_mission_repair(
        action, store, queue, reports, missions
    )

    assert result.status in {
        repair.RepairApplyStatus.STALE,
        repair.RepairApplyStatus.HUMAN_REVIEW_REQUIRED,
    }
    assert result.status is not repair.RepairApplyStatus.NO_LONGER_NEEDED
    assert store.load_state(definition.mission_id).task_states[
        action.task_id
    ].status is MissionTaskStatus.pending


def test_scheduler_never_persists_success_without_success_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from tools.ai_controller.mission.scheduler import MissionScheduler

    store, queue, reports, missions, definition = _create_mission(
        tmp_path,
        task_status=MissionTaskStatus.queued,
    )
    expected = _expected_task(queue, reports, definition, "task-a")
    _write_succeeded_evidence(queue, reports, expected)
    state = store.load_state(definition.mission_id)
    state.task_states["task-a"].queue_task_id = expected.id
    store.update_state(definition.mission_id, state)
    original = MissionEventLog.append_locked

    def fail_success(self, event, token):
        if event.event_type == "task_succeeded":
            raise OSError("injected causal event failure")
        return original(self, event, token)

    monkeypatch.setattr(MissionEventLog, "append_locked", fail_success)
    scheduler = MissionScheduler(
        store, queue, reports, missions, emit_materializer_events=False
    )

    with pytest.raises(OSError):
        scheduler.run_once(definition.mission_id)

    assert store.load_state(definition.mission_id).task_states[
        "task-a"
    ].status is MissionTaskStatus.queued


@pytest.mark.parametrize("use_legacy_id", [False, True])
def test_scheduler_reconciles_preexisting_success_without_duplicate(
    tmp_path: Path,
    use_legacy_id: bool,
):
    from tools.ai_controller.mission.scheduler import MissionScheduler

    store, queue, reports, missions, definition = _create_mission(
        tmp_path,
        task_status=MissionTaskStatus.queued,
    )
    expected = _expected_task(queue, reports, definition, "task-a")
    if use_legacy_id:
        expected = replace(
            expected,
            id=make_legacy_queue_task_id(definition.mission_id, "task-a"),
        )
    _write_succeeded_evidence(queue, reports, expected)
    state = store.load_state(definition.mission_id)
    state.task_states["task-a"].queue_task_id = expected.id
    store.update_state(definition.mission_id, state)
    log = MissionEventLog(missions / "events", definition.mission_id)
    log.append(
        make_event(
            "task_succeeded",
            definition.mission_id,
            task_id="task-a",
            queue_task_id=expected.id,
        )
    )
    scheduler = MissionScheduler(
        store, queue, reports, missions, emit_materializer_events=False
    )

    scheduler.run_once(definition.mission_id)
    events = log.read_all()

    assert store.load_state(definition.mission_id).task_states[
        "task-a"
    ].status is MissionTaskStatus.succeeded
    assert sum(
        event.event_type == "task_succeeded"
        and event.task_id == "task-a"
        and event.queue_task_id == expected.id
        for event in events
    ) == 1
