"""Durability and fail-closed tests for Mission Checkpoint & Resume v0.1."""

import json
import multiprocessing
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from federation import (
    AuthorizationLevel,
    WorkerExecutionAttempt,
    WorkerExecutionRequest,
    WorkerExecutionResultEnvelope,
    WorkerExecutionStatus,
)
from research_mission import (
    MissionCheckpointConflictError, MissionCheckpointCorruptionError,
    MissionCheckpointStore, MissionResumeCoordinator, ResearchMission,
    ResearchMissionPlan, ResearchMissionResultCoordinator, ResearchMissionStatus,
    ResearchRole, ResearchTaskResult, ResearchTaskResultStatus, ResearchTaskSpec,
    ResumeClassification,
)

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
KEY = b"mission-checkpoint-resume-test-key-0001"


def mission():
    tasks = tuple(ResearchTaskSpec(
        task_id=f"task-{index}", mission_id="mission-1", role=role, sequence=index,
        objective=f"objective {index}", research_context={"set": {2, 1}},
        expected_result=f"result {index}", authorization_level=AuthorizationLevel.INTERNAL,
        approval_required=(index == 2), depends_on=(() if index == 1 else (f"task-{index-1}",)),
    ) for index, role in enumerate(ResearchRole, 1))
    item = ResearchMission("mission-1", "objective")
    item.plan = ResearchMissionPlan("mission-1", tasks)
    item.status = ResearchMissionStatus.DISPATCHED
    return item


def request(task_id, *, approval=False):
    return WorkerExecutionRequest(
        execution_attempt_id=f"attempt-{task_id}", mission_id="mission-1", task_id=task_id,
        assignment_id=f"assignment-{task_id}", dispatch_offer_id=f"offer-{task_id}",
        worker_node_id="worker-1", coordinator_node_id="coordinator-1",
        authorization_level=AuthorizationLevel.INTERNAL, approval_required=approval,
        expected_result=f"result {task_id[-1]}", execution_fingerprint=None, created_at=NOW,
    )


def attempt(task_id, status, *, approval=False):
    req = request(task_id, approval=approval)
    return WorkerExecutionAttempt(req, status,
        claimed_at=NOW if status is not WorkerExecutionStatus.ACCEPTED else None,
        started_at=NOW if status in {WorkerExecutionStatus.RUNNING, WorkerExecutionStatus.SUCCEEDED,
                                     WorkerExecutionStatus.FAILED} else None,
        terminal_at=NOW if status in {WorkerExecutionStatus.SUCCEEDED, WorkerExecutionStatus.FAILED,
                                      WorkerExecutionStatus.RECONCILIATION_REQUIRED} else None,
        result=WorkerExecutionResultEnvelope("done", ("evidence-worker",))
            if status is WorkerExecutionStatus.SUCCEEDED else None,
        failure_reason="failed" if status is WorkerExecutionStatus.FAILED else
            ("ambiguous" if status is WorkerExecutionStatus.RECONCILIATION_REQUIRED else None))


def coordinator(tmp_path):
    return MissionResumeCoordinator(MissionCheckpointStore(tmp_path / "checkpoints.jsonl",
        integrity_key=KEY), clock=lambda: NOW)


def test_checkpoint_active_mission_and_deterministic_pending_resume(tmp_path):
    item = mission(); results = ResearchMissionResultCoordinator(item).inspect()
    saved = coordinator(tmp_path).checkpoint(item, result_state=results, attempts=())
    assert saved.mission_id == "mission-1"
    assert [task.classification for task in saved.tasks] == [
        ResumeClassification.SAFE_TO_RESUME, ResumeClassification.PENDING,
        ResumeClassification.PENDING]
    assert coordinator(tmp_path).resume(item).checkpoint == saved


def test_completed_task_never_reruns_and_unblocks_dependency(tmp_path):
    item = mission(); results = ResearchMissionResultCoordinator(item)
    results.register_result(ResearchTaskResult("mission-1", "task-1", ResearchRole.RESEARCHER,
        ResearchTaskResultStatus.COMPLETED, "done", NOW))
    saved = coordinator(tmp_path).checkpoint(item, result_state=results.inspect(), attempts=())
    assert saved.tasks[0].classification is ResumeClassification.ALREADY_COMPLETED
    assert saved.tasks[1].classification is ResumeClassification.BLOCKED_ON_APPROVAL


@pytest.mark.parametrize("status", [WorkerExecutionStatus.CLAIMED, WorkerExecutionStatus.RUNNING,
                                     WorkerExecutionStatus.RECONCILIATION_REQUIRED])
def test_ambiguous_execution_requires_reconciliation(tmp_path, status):
    item = mission(); state = ResearchMissionResultCoordinator(item).inspect()
    saved = coordinator(tmp_path).checkpoint(item, result_state=state,
        attempts=(attempt("task-1", status),))
    assert saved.tasks[0].classification is ResumeClassification.RECONCILIATION_REQUIRED


def test_worker_success_and_failure_are_terminal(tmp_path):
    item = mission(); state = ResearchMissionResultCoordinator(item).inspect()
    saved = coordinator(tmp_path).checkpoint(item, result_state=state,
        attempts=(attempt("task-1", WorkerExecutionStatus.SUCCEEDED),
                  attempt("task-2", WorkerExecutionStatus.FAILED, approval=True)))
    assert saved.tasks[0].classification is ResumeClassification.ALREADY_COMPLETED
    assert saved.tasks[1].classification is ResumeClassification.FAILED_TERMINAL
    assert saved.tasks[0].evidence_references == ("evidence-worker",)
    assert saved.tasks[0].assignment_id == "assignment-task-1"
    assert saved.tasks[0].dispatch_offer_id == "offer-task-1"


def test_duplicate_checkpoint_is_idempotent_and_restart_persists(tmp_path):
    item = mission(); state = ResearchMissionResultCoordinator(item).inspect()
    first = coordinator(tmp_path).checkpoint(item, result_state=state, attempts=())
    second = coordinator(tmp_path).checkpoint(item, result_state=state, attempts=())
    assert first == second
    assert MissionCheckpointStore(tmp_path / "checkpoints.jsonl", integrity_key=KEY).latest("mission-1") == first
    assert len((tmp_path / "checkpoints.jsonl").read_text().splitlines()) == 1


def test_changed_state_creates_new_revision_and_stale_checkpoint_is_rejected(tmp_path):
    item = mission(); state = ResearchMissionResultCoordinator(item).inspect(); authority = coordinator(tmp_path)
    first = authority.checkpoint(item, result_state=state, attempts=())
    second = authority.checkpoint(item, result_state=state,
        attempts=(attempt("task-1", WorkerExecutionStatus.ACCEPTED),))
    assert second.revision == first.revision + 1
    with pytest.raises(MissionCheckpointConflictError): authority.resume(item, checkpoint_id=first.checkpoint_id)


def test_foreign_mission_result_and_attempt_are_rejected(tmp_path):
    item = mission(); state = ResearchMissionResultCoordinator(item).inspect()
    item.mission_id = "foreign"
    with pytest.raises(ValueError):
        coordinator(tmp_path).checkpoint(item, result_state=state, attempts=())
    item = mission()
    foreign = replace(request("task-1"), mission_id="foreign")
    with pytest.raises(ValueError):
        coordinator(tmp_path).checkpoint(item, result_state=state,
            attempts=(WorkerExecutionAttempt(foreign, WorkerExecutionStatus.ACCEPTED),))


def test_plan_mutation_after_checkpoint_is_detected(tmp_path):
    item = mission(); state = ResearchMissionResultCoordinator(item).inspect(); authority = coordinator(tmp_path)
    authority.checkpoint(item, result_state=state, attempts=())
    item.plan.tasks[0].research_context["changed"] = True
    with pytest.raises(MissionCheckpointConflictError): authority.resume(item)


def test_changed_plan_cannot_be_saved_as_idempotent_state(tmp_path):
    item = mission(); state = ResearchMissionResultCoordinator(item).inspect(); authority = coordinator(tmp_path)
    authority.checkpoint(item, result_state=state, attempts=())
    item.plan.tasks[0].research_context["changed"] = True
    with pytest.raises(MissionCheckpointConflictError):
        authority.checkpoint(item, result_state=state, attempts=())


@pytest.mark.parametrize("mutation", ["truncate", "payload", "duplicate"])
def test_corrupt_incomplete_and_duplicate_persistence_fail_closed(tmp_path, mutation):
    item = mission(); state = ResearchMissionResultCoordinator(item).inspect(); authority = coordinator(tmp_path)
    authority.checkpoint(item, result_state=state, attempts=())
    path = tmp_path / "checkpoints.jsonl"; raw = path.read_bytes()
    if mutation == "truncate": path.write_bytes(raw[:-1])
    elif mutation == "duplicate": path.write_bytes(raw + raw)
    else:
        record = json.loads(raw); record["payload"]["mission_id"] = "foreign"
        path.write_text(json.dumps(record) + "\n")
    with pytest.raises(MissionCheckpointCorruptionError): authority.store.latest("mission-1")


def _checkpoint_process(path, queue):
    try:
        item = mission(); state = ResearchMissionResultCoordinator(item).inspect()
        queue.put(coordinator(path).checkpoint(item, result_state=state, attempts=()).checkpoint_id)
    except Exception as exc: queue.put(type(exc).__name__)


def test_concurrent_checkpoint_creation_has_one_authoritative_revision(tmp_path):
    context = multiprocessing.get_context("spawn"); queue = context.Queue()
    processes = [context.Process(target=_checkpoint_process, args=(tmp_path, queue)) for _ in range(4)]
    for process in processes: process.start()
    for process in processes: process.join(10)
    ids = [queue.get(timeout=2) for _ in processes]
    assert len(set(ids)) == 1
    assert len((tmp_path / "checkpoints.jsonl").read_text().splitlines()) == 1


def test_duplicate_attempt_or_missing_linkage_fails_closed(tmp_path):
    item = mission(); state = ResearchMissionResultCoordinator(item).inspect(); one = attempt("task-1", WorkerExecutionStatus.ACCEPTED)
    with pytest.raises(ValueError): coordinator(tmp_path).checkpoint(item, result_state=state, attempts=(one, one))
    broken = replace(one, request=replace(one.request, assignment_id=""))
    with pytest.raises(ValueError): coordinator(tmp_path).checkpoint(item, result_state=state, attempts=(broken,))


def test_composed_contradictory_terminal_attempt_fails_closed(tmp_path):
    item = mission(); state = ResearchMissionResultCoordinator(item).inspect()
    forged = WorkerExecutionAttempt(request("task-1"), WorkerExecutionStatus.SUCCEEDED,
        claimed_at=NOW, started_at=NOW, terminal_at=NOW, result=None)
    with pytest.raises(ValueError):
        coordinator(tmp_path).checkpoint(item, result_state=state, attempts=(forged,))


def test_composed_contradictory_result_projection_fails_closed(tmp_path):
    item = mission(); state = ResearchMissionResultCoordinator(item).inspect()
    forged = replace(state, completed_results=(ResearchTaskResult(
        "mission-1", "task-1", ResearchRole.RESEARCHER,
        ResearchTaskResultStatus.COMPLETED, "forged", NOW),))
    with pytest.raises(ValueError):
        coordinator(tmp_path).checkpoint(item, result_state=forged, attempts=())


def test_resume_decision_is_immutable_and_deterministically_ordered(tmp_path):
    item = mission(); state = ResearchMissionResultCoordinator(item).inspect(); authority = coordinator(tmp_path)
    authority.checkpoint(item, result_state=state, attempts=())
    decision = authority.resume(item)
    assert tuple(task.task_id for task in decision.tasks) == ("task-1", "task-2", "task-3")
    with pytest.raises(Exception): decision.tasks += ()
