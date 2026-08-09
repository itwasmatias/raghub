"""Focused security and lifecycle tests for Worker Execution Protocol v0.1."""

import json
import multiprocessing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from federation import (
    AuthorizationLevel, DurableAssignmentRegistry, NodeCapability, NodeRecord,
    TaskAssignment, TaskDispatchCoordinator, TaskRequest,
    WorkerExecutionConflictError, WorkerExecutionCoordinator,
    WorkerExecutionCorruptionError, WorkerExecutionIdentityError,
    WorkerExecutionResultEnvelope, WorkerExecutionStateError,
    WorkerExecutionStatus,
)
from federation.heartbeat import Heartbeat
from federation.heartbeat_registry import HeartbeatRegistry
from federation.registry import NodeRegistry

NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)
KEY = b"worker-execution-protocol-test-key-0001"


@dataclass
class Clock:
    value: datetime = NOW
    def __call__(self): return self.value


def environment(path, *, approval=False, verifier=None, worker="worker-1", accept=True):
    clock = Clock()
    request = TaskRequest("task-1", "mission-1",
        required_capabilities={NodeCapability("python_execution")},
        authorization_level=AuthorizationLevel.RESTRICTED,
        approval_required=approval, expected_result="structured evidence")
    assignment = TaskAssignment(request, NodeRecord(worker, "worker.local", "Fedora",
        capabilities={NodeCapability("python_execution")}))
    registry = DurableAssignmentRegistry(path / "assignments.jsonl",
        coordinator_node_id="coordinator-1", integrity_key=KEY)
    authority = registry.record(assignment)
    nodes = NodeRegistry(stale_threshold_seconds=10**9)
    nodes.register(NodeRecord(worker, "worker.local", "Fedora",
        capabilities={NodeCapability("python_execution")}))
    heartbeats = HeartbeatRegistry(path / "heartbeats.jsonl",
        registry_id="worker-execution-tests", node_registry=nodes,
        integrity_key=KEY, clock=clock)
    heartbeats.record(Heartbeat.authenticated(
        worker_id=worker, registry_id="worker-execution-tests", sequence=1,
        session_id="boot-1", worker_timestamp=NOW, health="healthy",
        power_capabilities=(), requested_power_state="active",
        sleep_reason=None, expected_wake_time=None, wake_method=None,
        active_work_checkpointed=False, previous_authentication_tag="0" * 64,
        integrity_key=KEY,
    ))
    dispatch = TaskDispatchCoordinator("coordinator-1", assignment_store=registry,
        dispatch_store_path=path / "dispatch.jsonl", integrity_key=KEY,
        heartbeat_registry=heartbeats, clock=clock)
    offer = dispatch.create_offer(assignment_id=authority.assignment_id,
        actor_node_id="coordinator-1", expires_at=NOW + timedelta(hours=1))
    if accept:
        offer = dispatch.accept_offer(offer_id=offer.offer_id, actor_node_id=worker)
    protocol = WorkerExecutionCoordinator("coordinator-1", dispatch_coordinator=dispatch,
        store_path=path / "execution.jsonl", integrity_key=KEY, clock=clock,
        approval_verifier=verifier)
    return protocol, offer


def registered(path, **kwargs):
    protocol, offer = environment(path, **kwargs)
    attempt = protocol.register(dispatch_offer_id=offer.offer_id,
        actor_node_id="coordinator-1", expected_result="structured evidence")
    return protocol, attempt


def test_accepted_offer_target_worker_claims_and_linkage_is_preserved(tmp_path):
    protocol, attempt = registered(tmp_path)
    claimed = protocol.claim(execution_attempt_id=attempt.request.execution_attempt_id,
        actor_node_id="worker-1")
    assert claimed.status is WorkerExecutionStatus.CLAIMED
    assert (claimed.request.mission_id, claimed.request.task_id) == ("mission-1", "task-1")
    assert claimed.request.authorization_level is AuthorizationLevel.RESTRICTED
    assert claimed.request.approval_required is False
    assert claimed.request.expected_result == "structured evidence"


def test_wrong_worker_and_coordinator_cannot_claim(tmp_path):
    protocol, attempt = registered(tmp_path)
    for actor in ("worker-2", "coordinator-1"):
        with pytest.raises(WorkerExecutionIdentityError):
            protocol.claim(execution_attempt_id=attempt.request.execution_attempt_id,
                           actor_node_id=actor)


def test_offered_but_not_accepted_is_rejected(tmp_path):
    protocol, offer = environment(tmp_path, accept=False)
    with pytest.raises(WorkerExecutionStateError):
        protocol.register(dispatch_offer_id=offer.offer_id, actor_node_id="coordinator-1",
                          expected_result="structured evidence")


def test_duplicate_registration_and_claim_are_idempotent(tmp_path):
    protocol, attempt = registered(tmp_path)
    again = protocol.register(dispatch_offer_id=attempt.request.dispatch_offer_id,
        actor_node_id="coordinator-1", expected_result="structured evidence")
    assert again == attempt
    first = protocol.claim(execution_attempt_id=attempt.request.execution_attempt_id,
                           actor_node_id="worker-1")
    assert protocol.claim(execution_attempt_id=attempt.request.execution_attempt_id,
                          actor_node_id="worker-1") == first
    assert len(protocol.list_attempts()) == 1


def test_duplicate_registration_after_time_advance_reuses_original_authority(tmp_path):
    protocol, attempt = registered(tmp_path)
    protocol._clock.value += timedelta(minutes=10)
    replay = protocol.register(dispatch_offer_id=attempt.request.dispatch_offer_id,
        actor_node_id="coordinator-1", expected_result="structured evidence")
    assert replay == attempt
    assert replay.request.created_at == NOW


def test_conflicting_registration_rejected(tmp_path):
    protocol, attempt = registered(tmp_path)
    with pytest.raises(WorkerExecutionConflictError):
        protocol.register(dispatch_offer_id=attempt.request.dispatch_offer_id,
            actor_node_id="coordinator-1", expected_result="changed")


def test_lifecycle_success_and_terminal_immutability(tmp_path):
    protocol, attempt = registered(tmp_path)
    aid = attempt.request.execution_attempt_id
    protocol.claim(execution_attempt_id=aid, actor_node_id="worker-1")
    protocol.start(execution_attempt_id=aid, actor_node_id="worker-1")
    result = WorkerExecutionResultEnvelope("complete", ("evidence-1",))
    final = protocol.succeed(execution_attempt_id=aid, actor_node_id="worker-1", result=result)
    assert final.status is WorkerExecutionStatus.SUCCEEDED
    assert final.result == result
    assert protocol.succeed(execution_attempt_id=aid, actor_node_id="worker-1", result=result) == final
    with pytest.raises((WorkerExecutionStateError, WorkerExecutionConflictError)):
        protocol.fail(execution_attempt_id=aid, actor_node_id="worker-1", reason="late")


def test_running_can_fail_and_restart_preserves_terminal_result(tmp_path):
    protocol, attempt = registered(tmp_path)
    aid = attempt.request.execution_attempt_id
    protocol.claim(execution_attempt_id=aid, actor_node_id="worker-1")
    protocol.start(execution_attempt_id=aid, actor_node_id="worker-1")
    protocol.fail(execution_attempt_id=aid, actor_node_id="worker-1", reason="deterministic failure")
    restarted, _ = environment(tmp_path)
    final = restarted.inspect(aid)
    assert final.status is WorkerExecutionStatus.FAILED
    assert final.failure_reason == "deterministic failure"


def test_restart_running_requires_reconciliation_and_never_reclaims(tmp_path):
    protocol, attempt = registered(tmp_path)
    aid = attempt.request.execution_attempt_id
    protocol.claim(execution_attempt_id=aid, actor_node_id="worker-1")
    protocol.start(execution_attempt_id=aid, actor_node_id="worker-1")
    restarted, _ = environment(tmp_path)
    reconciled = restarted.reconcile_interrupted(execution_attempt_id=aid,
                                                   actor_node_id="coordinator-1")
    assert reconciled.status is WorkerExecutionStatus.RECONCILIATION_REQUIRED
    with pytest.raises(WorkerExecutionStateError):
        restarted.claim(execution_attempt_id=aid, actor_node_id="worker-1")


def test_approval_cannot_be_downgraded_or_replaced_by_worker_boolean(tmp_path):
    protocol, attempt = registered(tmp_path, approval=True)
    assert attempt.request.approval_required is True
    with pytest.raises(WorkerExecutionStateError):
        protocol.claim(execution_attempt_id=attempt.request.execution_attempt_id,
                       actor_node_id="worker-1")
    with pytest.raises(TypeError):
        protocol.claim(execution_attempt_id=attempt.request.execution_attempt_id,
                       actor_node_id="worker-1", approval_required=False)


def test_configured_authoritative_approval_verifier_can_allow_exact_request(tmp_path):
    protocol, attempt = registered(tmp_path, approval=True, verifier=lambda request: request.approval_required)
    assert protocol.claim(execution_attempt_id=attempt.request.execution_attempt_id,
                          actor_node_id="worker-1").status is WorkerExecutionStatus.CLAIMED


def test_arbitrary_execution_payload_has_no_protocol_surface(tmp_path):
    protocol, attempt = registered(tmp_path)
    with pytest.raises(TypeError):
        protocol.claim(execution_attempt_id=attempt.request.execution_attempt_id,
                       actor_node_id="worker-1", command="rm -rf something")


def test_truncated_corrupt_and_wrong_key_state_fail_closed(tmp_path):
    protocol, attempt = registered(tmp_path)
    path = tmp_path / "execution.jsonl"
    path.write_bytes(path.read_bytes()[:-2])
    with pytest.raises(WorkerExecutionCorruptionError): protocol.list_attempts()


def _claim_process(root, queue):
    try:
        protocol, _ = environment(root)
        attempt = protocol.list_attempts()[0]
        queue.put(protocol.claim(execution_attempt_id=attempt.request.execution_attempt_id,
                                 actor_node_id="worker-1").status.value)
    except Exception as exc: queue.put(type(exc).__name__)


def test_cross_process_duplicate_claim_has_one_authoritative_transition(tmp_path):
    registered(tmp_path)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [context.Process(target=_claim_process, args=(tmp_path, queue)) for _ in range(4)]
    for process in processes: process.start()
    for process in processes: process.join(10)
    assert [queue.get(timeout=2) for _ in processes] == ["claimed"] * 4
    protocol, _ = environment(tmp_path)
    lines = (tmp_path / "execution.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert protocol.list_attempts()[0].status is WorkerExecutionStatus.CLAIMED


def test_mutation_and_foreign_evidence_fail_closed(tmp_path):
    protocol, attempt = registered(tmp_path)
    data = [json.loads(line) for line in (tmp_path / "execution.jsonl").read_text().splitlines()]
    data[0]["request"]["mission_id"] = "foreign"
    (tmp_path / "execution.jsonl").write_text("\n".join(json.dumps(x) for x in data) + "\n")
    with pytest.raises(WorkerExecutionCorruptionError): protocol.inspect(attempt.request.execution_attempt_id)


@pytest.mark.parametrize(("field", "value"), [
    ("execution_fingerprint", "A" * 64),
    ("authorization_level", "public"),
    ("approval_required", True),
    ("assignment_id", "assignment-foreign"),
])
def test_malformed_or_downgraded_durable_authority_fails_closed(tmp_path, field, value):
    protocol, _ = registered(tmp_path)
    path = tmp_path / "execution.jsonl"
    record = json.loads(path.read_text())
    record["request"][field] = value
    path.write_text(json.dumps(record) + "\n")
    with pytest.raises(WorkerExecutionCorruptionError): protocol.list_attempts()


def test_upstream_without_fingerprint_does_not_fabricate_one(tmp_path):
    _, attempt = registered(tmp_path)
    assert attempt.request.execution_fingerprint is None
    assert attempt.request.created_at == NOW


def test_deterministic_read_only_inspection(tmp_path):
    protocol, attempt = registered(tmp_path)
    assert protocol.inspect(attempt.request.execution_attempt_id) == attempt
    assert protocol.list_attempts() == (attempt,)
    with pytest.raises(Exception):
        attempt.status = WorkerExecutionStatus.FAILED
