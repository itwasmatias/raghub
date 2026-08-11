"""Mission recovery provenance, policy, durability, and corruption tests."""

import json
import multiprocessing
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

import pytest

from federation import (
    AuthorizationLevel, BudgetPolicy, BudgetRoutingGovernance, NodeCapability,
    NodeRecord, RoutingDecision, RoutingOutcome, TaskAssignment, WorkerExecutionAttempt,
    WorkerExecutionRequest, WorkerExecutionStatus, WorkerProviderMetadata,
)
from research_mission import (
    MissionCheckpointStore, MissionRecoveryCorruptionError,
    MissionRecoveryEvidenceRuntime, MissionRecoveryEvidenceStore, RecoveryOutcome,
    ResearchMission, ResearchMissionPlan, ResearchMissionResultCoordinator,
    ResearchMissionStatus, ResearchRole, ResearchTaskResult,
    ResearchTaskResultStatus, ResearchTaskSpec,
    ReplicationClaimEvaluation, ReplicationClaimStatus, ResearchEvidence,
    ResearchReplicationResult,
)

NOW = datetime(2026, 8, 9, 15, 0, tzinfo=timezone.utc)
KEY = b"mission-recovery-evidence-test-key-0001"


def mission():
    tasks = tuple(ResearchTaskSpec(f"task-{index}", "mission-1", role, index,
        f"objective-{index}", {}, f"result-{index}", AuthorizationLevel.INTERNAL,
        index == 2, depends_on=(() if index == 1 else (f"task-{index-1}",)))
        for index, role in enumerate(ResearchRole, 1))
    item = ResearchMission("mission-1", "recover mission")
    item.plan = ResearchMissionPlan("mission-1", tasks)
    item.status = ResearchMissionStatus.PLANNED
    return item


def request(task_id, *, worker="worker-old", approval=False):
    return WorkerExecutionRequest(f"attempt-{task_id}", "mission-1", task_id,
        f"assignment-{task_id}", f"offer-{task_id}", worker, "coordinator-1",
        AuthorizationLevel.INTERNAL, approval, f"result-{task_id[-1]}", None, NOW)


def attempt(task_id, status, *, worker="worker-old", approval=False):
    return WorkerExecutionAttempt(request(task_id, worker=worker, approval=approval), status,
        claimed_at=NOW if status is not WorkerExecutionStatus.ACCEPTED else None,
        started_at=NOW if status is WorkerExecutionStatus.RUNNING else None,
        terminal_at=NOW if status in {WorkerExecutionStatus.FAILED,
            WorkerExecutionStatus.RECONCILIATION_REQUIRED} else None,
        failure_reason="terminal" if status in {WorkerExecutionStatus.FAILED,
            WorkerExecutionStatus.RECONCILIATION_REQUIRED} else None)


def result(task, status=ResearchTaskResultStatus.COMPLETED):
    return ResearchTaskResult("mission-1", task.task_id, task.role, status,
        f"{task.task_id} result", NOW,
        challenger_assessment=None, judge_decision=None)


def runtime(tmp_path, *, checkpoint_attempts=(), completed=()):
    item = mission(); coordinator = ResearchMissionResultCoordinator(item)
    for task_id in completed:
        task = next(value for value in item.plan.tasks if value.task_id == task_id)
        # Only researcher completion is needed by these checkpoint classifications.
        coordinator.register_result(result(task))
    checkpoint_store = MissionCheckpointStore(tmp_path / "checkpoints.jsonl", integrity_key=KEY)
    from research_mission import MissionResumeCoordinator
    checkpoint = MissionResumeCoordinator(checkpoint_store, clock=lambda: NOW).checkpoint(
        item, result_state=coordinator.inspect(), attempts=checkpoint_attempts)
    recovery_store = MissionRecoveryEvidenceStore(tmp_path / "recovery.jsonl", integrity_key=KEY)
    return item, coordinator.inspect(), checkpoint, MissionRecoveryEvidenceRuntime(
        checkpoint_store, recovery_store, clock=lambda: NOW)


def routing(task, metadata_by_node, policy, selected):
    nodes = tuple(NodeRecord(
        domain_id="test-domain",
        node_id=node_id,
        hostname=f"{node_id}.local",
        operating_system="generic",
        capabilities={NodeCapability("python_execution")}) for node_id in metadata_by_node)
    governance = BudgetRoutingGovernance(metadata_by_node, policy)
    task_request = task.to_task_request()
    evidence = governance.evaluate(task_request, nodes)
    chosen = next(node for node in nodes if node.node_id == selected) if selected else None
    return RoutingDecision(task_request,
        RoutingOutcome.SUCCESS if chosen else RoutingOutcome.NO_ELIGIBLE_NODES,
        assignment=None if chosen is None else TaskAssignment(task_request, chosen),
        assignment_id=None if chosen is None else f"assignment-{task.task_id}-recovery",
        budget_evidence=evidence)


def meta(node_id, *, locality="local", provider=None, availability="available"):
    return WorkerProviderMetadata(node_id, locality, "free", availability, "available",
        AuthorizationLevel.CONFIDENTIAL, provider_id=provider, local_fallback_eligible=True)


def test_safe_pending_blocked_and_already_complete_outcomes(tmp_path):
    _, state, checkpoint, authority = runtime(tmp_path, completed=("task-1",))
    evidence = authority.record("mission-1", checkpoint_id=checkpoint.checkpoint_id,
        result_state=state, attempts=(), routing_decisions=())
    assert evidence.tasks[0].outcomes == (RecoveryOutcome.ALREADY_COMPLETE,)
    assert evidence.tasks[1].outcomes == (RecoveryOutcome.BLOCKED_ON_APPROVAL,)


def test_claimed_or_running_checkpoint_remains_reconciliation_required(tmp_path):
    _, state, checkpoint, authority = runtime(tmp_path,
        checkpoint_attempts=(attempt("task-1", WorkerExecutionStatus.RUNNING),))
    evidence = authority.record("mission-1", checkpoint_id=checkpoint.checkpoint_id,
        result_state=state, attempts=(attempt("task-1", WorkerExecutionStatus.RUNNING),),
        routing_decisions=())
    assert RecoveryOutcome.RECONCILIATION_REQUIRED in evidence.tasks[0].outcomes


def test_new_attempt_after_safe_checkpoint_records_resumed_safely(tmp_path):
    _, state, checkpoint, authority = runtime(tmp_path)
    evidence = authority.record("mission-1", checkpoint_id=checkpoint.checkpoint_id,
        result_state=state, attempts=(attempt("task-1", WorkerExecutionStatus.CLAIMED),),
        routing_decisions=())
    assert RecoveryOutcome.RESUMED_SAFELY in evidence.tasks[0].outcomes
    assert RecoveryOutcome.PENDING not in evidence.tasks[0].outcomes


def test_terminal_worker_failure_is_preserved(tmp_path):
    _, state, checkpoint, authority = runtime(tmp_path)
    evidence = authority.record("mission-1", checkpoint_id=checkpoint.checkpoint_id,
        result_state=state, attempts=(attempt("task-1", WorkerExecutionStatus.FAILED),),
        routing_decisions=())
    assert RecoveryOutcome.TERMINAL_FAILURE in evidence.tasks[0].outcomes


def test_local_fallback_and_worker_unavailability_require_policy_evidence(tmp_path):
    item, state, checkpoint, authority = runtime(tmp_path,
        checkpoint_attempts=(attempt("task-1", WorkerExecutionStatus.ACCEPTED),))
    task = item.plan.tasks[0]
    decision = routing(task, {"worker-old": meta("worker-old", locality="cloud",
        provider="opaque", availability="unavailable"), "worker-local": meta("worker-local")},
        BudgetPolicy(allow_cloud_escalation=True), "worker-local")
    resumed = attempt("task-1", WorkerExecutionStatus.CLAIMED, worker="worker-local")
    resumed = replace(resumed, request=replace(resumed.request,
        execution_attempt_id="attempt-task-1-recovery",
        assignment_id="assignment-task-1-recovery",
        dispatch_offer_id="offer-task-1-recovery"))
    evidence = authority.record("mission-1", checkpoint_id=checkpoint.checkpoint_id,
        result_state=state, attempts=(resumed,), routing_decisions=(decision,))
    assert RecoveryOutcome.RECOVERED_AFTER_WORKER_UNAVAILABLE in evidence.tasks[0].outcomes
    assert RecoveryOutcome.RECOVERED_USING_LOCAL_FALLBACK in evidence.tasks[0].outcomes
    assert evidence.tasks[0].budget_evidence_fingerprint is not None


def test_cloud_escalation_prevented_and_authorized_are_distinct(tmp_path):
    item, state, checkpoint, authority = runtime(tmp_path); task = item.plan.tasks[0]
    prevented = routing(task, {"cloud": meta("cloud", locality="cloud", provider="opaque")},
        BudgetPolicy(local_only=True, allow_cloud_escalation=True), None)
    first = authority.record("mission-1", checkpoint_id=checkpoint.checkpoint_id,
        result_state=state, attempts=(), routing_decisions=(prevented,))
    assert RecoveryOutcome.CLOUD_ESCALATION_PREVENTED in first.tasks[0].outcomes
    other_path = tmp_path / "allowed"; item, state, checkpoint, authority = runtime(other_path)
    allowed = routing(item.plan.tasks[0], {"cloud": meta("cloud", locality="cloud", provider="opaque")},
        BudgetPolicy(allow_cloud_escalation=True), "cloud")
    second = authority.record("mission-1", checkpoint_id=checkpoint.checkpoint_id,
        result_state=state, attempts=(), routing_decisions=(allowed,))
    assert RecoveryOutcome.CLOUD_ESCALATION_AUTHORIZED in second.tasks[0].outcomes


def test_routing_suggestion_alone_does_not_claim_local_recovery(tmp_path):
    item, state, checkpoint, authority = runtime(tmp_path); task = item.plan.tasks[0]
    decision = routing(task, {"cloud": meta("cloud", locality="cloud", provider="opaque"),
        "local": meta("local")}, BudgetPolicy(local_only=True, allow_cloud_escalation=True), "local")
    evidence = authority.record("mission-1", checkpoint_id=checkpoint.checkpoint_id,
        result_state=state, attempts=(), routing_decisions=(decision,))
    assert RecoveryOutcome.CLOUD_ESCALATION_PREVENTED in evidence.tasks[0].outcomes
    assert RecoveryOutcome.RECOVERED_USING_LOCAL_FALLBACK not in evidence.tasks[0].outcomes


def test_foreign_checkpoint_result_attempt_and_routing_fail_closed(tmp_path):
    item, state, checkpoint, authority = runtime(tmp_path)
    with pytest.raises(ValueError): authority.record("foreign", checkpoint_id=checkpoint.checkpoint_id,
        result_state=state, attempts=(), routing_decisions=())
    foreign = WorkerExecutionAttempt(
        WorkerExecutionRequest("attempt-x", "foreign", "task-1", "assignment-x", "offer-x",
            "worker", "coordinator", AuthorizationLevel.INTERNAL, False, "result-1", None, NOW),
        WorkerExecutionStatus.ACCEPTED)
    with pytest.raises(ValueError): authority.record("mission-1", checkpoint_id=checkpoint.checkpoint_id,
        result_state=state, attempts=(foreign,), routing_decisions=())


def test_replication_status_and_evidence_linkage_are_preserved(tmp_path):
    _, state, checkpoint, authority = runtime(tmp_path, completed=("task-1",))
    supplied = ResearchEvidence("source-1", "mission-1", "task-1", "record",
        "source", "summary", NOW)
    state = replace(state, evidence=(supplied,))
    claim = ReplicationClaimEvaluation("claim-1", "claim",
        ReplicationClaimStatus.REPLICATED, ("source-1",), ("replication-ev",), ())
    replication = ResearchReplicationResult("replication-1", "mission-1", "evaluation-1",
        (claim,), ("source-1",), ("replication-ev",))
    evidence = authority.record("mission-1", checkpoint_id=checkpoint.checkpoint_id,
        result_state=state, attempts=(), routing_decisions=(),
        replication_result=replication)
    assert evidence.replication_id == "replication-1"
    assert evidence.replication_claim_statuses == (("claim-1", "replicated"),)
    assert evidence.replication_evidence_ids == ("replication-ev",)


def test_exact_replay_is_idempotent_restart_safe_and_immutable(tmp_path):
    _, state, checkpoint, authority = runtime(tmp_path)
    first = authority.record("mission-1", checkpoint_id=checkpoint.checkpoint_id,
        result_state=state, attempts=(), routing_decisions=())
    second = authority.record("mission-1", checkpoint_id=checkpoint.checkpoint_id,
        result_state=state, attempts=(), routing_decisions=())
    assert first == second
    assert len((tmp_path / "recovery.jsonl").read_text().splitlines()) == 1
    assert authority.store.latest("mission-1") == first
    with pytest.raises(FrozenInstanceError): first.tasks[0].outcomes = ()


@pytest.mark.parametrize("mutation", ["truncate", "payload", "duplicate"])
def test_recovery_persistence_corruption_fails_closed(tmp_path, mutation):
    _, state, checkpoint, authority = runtime(tmp_path)
    authority.record("mission-1", checkpoint_id=checkpoint.checkpoint_id,
        result_state=state, attempts=(), routing_decisions=())
    path = tmp_path / "recovery.jsonl"; raw = path.read_bytes()
    if mutation == "truncate": path.write_bytes(raw[:-1])
    elif mutation == "duplicate": path.write_bytes(raw + raw)
    else:
        record = json.loads(raw); record["payload"]["mission_id"] = "foreign"
        path.write_text(json.dumps(record) + "\n")
    with pytest.raises(MissionRecoveryCorruptionError): authority.store.latest("mission-1")


def _record_process(path, queue):
    try:
        _, state, checkpoint, authority = runtime(path)
        queue.put(authority.record("mission-1", checkpoint_id=checkpoint.checkpoint_id,
            result_state=state, attempts=(), routing_decisions=()).recovery_id)
    except Exception as exc: queue.put(type(exc).__name__)


def test_concurrent_duplicate_recovery_evidence_has_one_record(tmp_path):
    runtime(tmp_path)
    context = multiprocessing.get_context("spawn"); queue = context.Queue()
    processes = [context.Process(target=_record_process, args=(tmp_path, queue)) for _ in range(4)]
    for process in processes: process.start()
    for process in processes: process.join(10)
    ids = [queue.get(timeout=2) for _ in processes]
    assert len(set(ids)) == 1
    assert len((tmp_path / "recovery.jsonl").read_text().splitlines()) == 1
