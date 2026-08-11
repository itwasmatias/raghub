"""State-machine, persistence, concurrency, and corruption tests."""

import hashlib
import json
import multiprocessing
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from federation.assignment_registry import (
    AssignmentConflictError,
    DurableAssignmentRegistry,
)
from federation.capability import NodeCapability
from federation.dispatch_offer import DispatchEventType, DispatchStatus
from federation.heartbeat import Heartbeat
from federation.heartbeat_registry import HeartbeatRegistry
from federation.node_record import NodeRecord
from federation.registry import NodeRegistry
from federation.task_assignment import TaskAssignment
from federation.task_dispatcher import (
    DispatchCorruptionError,
    DispatchIdentityMismatchError,
    DispatchOfferExpiredError,
    DispatchTerminalStateError,
    TaskDispatchCoordinator,
)
from federation.task_request import AuthorizationLevel, TaskRequest


START = datetime(2026, 8, 5, 21, 0, tzinfo=timezone.utc)
INTEGRITY_KEY = b"task-dispatch-v0.1-test-integrity-key"
WRONG_INTEGRITY_KEY = b"task-dispatch-v0.1-wrong-integrity-key"


@dataclass
class MutableClock:
    current: datetime = START

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def make_assignment(
    *,
    task_id="task-1",
    mission_id="mission-1",
    worker_id="worker-1",
    coordinator_id="coordinator-1",
):
    request = TaskRequest(
        task_id=task_id,
        mission_id=mission_id,
        required_capabilities={NodeCapability("python_execution")},
        authorization_level=AuthorizationLevel.RESTRICTED,
        approval_required=True,
    )
    worker = NodeRecord(domain_id="test-domain", 
        node_id=worker_id,
        hostname=f"{worker_id}.local",
        operating_system="Fedora",
        capabilities={NodeCapability("python_execution")},
    )
    return TaskAssignment(task_request=request, assigned_node=worker), coordinator_id


def build_liveness(
    path,
    *,
    worker_ids=("worker-1",),
    clock=None,
    key=INTEGRITY_KEY,
    record=True,
):
    nodes = NodeRegistry(stale_threshold_seconds=10**9)
    for worker_id in worker_ids:
        nodes.register(
            NodeRecord(domain_id="test-domain", 
                node_id=worker_id,
                hostname=f"{worker_id}.local",
                operating_system="Fedora",
                capabilities={NodeCapability("python_execution")},
            ),
        )
    liveness = HeartbeatRegistry(
        Path(path) / "heartbeats.jsonl",
        registry_id="task-dispatch-tests",
        node_registry=nodes,
        integrity_key=key,
        clock=clock or MutableClock(),
    )
    if record:
        for worker_id in worker_ids:
            liveness.record(
                Heartbeat.authenticated(
                    worker_id=worker_id,
                    registry_id="task-dispatch-tests",
                    domain_id="test-domain",
                    sequence=1,
                    session_id="boot-1",
                    worker_timestamp=START,
                    health="healthy",
                    power_capabilities=(),
                    requested_power_state="active",
                    sleep_reason=None,
                    expected_wake_time=None,
                    wake_method=None,
                    active_work_checkpointed=False,
                    previous_authentication_tag="0" * 64,
                    integrity_key=key,
                ),
            )
    return liveness


def build_coordinator(tmp_path, clock=None, worker_ids=None, **assignment_overrides):
    assignment, coordinator_id = make_assignment(**assignment_overrides)
    coordinator_clock = clock or MutableClock()
    heartbeat_worker_ids = worker_ids or (assignment.node_id,)
    assignments = DurableAssignmentRegistry(
        tmp_path / "assignments.jsonl",
        coordinator_node_id=coordinator_id,
        integrity_key=INTEGRITY_KEY,
    )
    authoritative = assignments.record(assignment)
    coordinator = TaskDispatchCoordinator(
        coordinator_id,
        assignment_store=assignments,
        dispatch_store_path=tmp_path / "dispatch.jsonl",
        integrity_key=INTEGRITY_KEY,
        heartbeat_registry=build_liveness(
            tmp_path,
            worker_ids=heartbeat_worker_ids,
            clock=coordinator_clock,
        ),
        clock=coordinator_clock,
    )
    return coordinator, authoritative


def create_offer(coordinator, authoritative, clock=None):
    now = (clock or MutableClock()).current
    return coordinator.create_offer(
        assignment_id=authoritative.assignment_id,
        actor_node_id="coordinator-1",
        expires_at=now + timedelta(minutes=5),
    )


def _routing_payload(record):
    return {
        key: record[key]
        for key in (
            "routing_revision",
            "mission_id",
            "task_id",
            "coordinator_node_id",
            "worker_node_id",
            "required_capabilities",
            "authorization_metadata",
            "approval_metadata",
        )
    }


def _forge_assignment_with_plain_digest(record):
    fingerprint = hashlib.sha256(_canonical(_routing_payload(record))).hexdigest()
    record["assignment_fingerprint"] = fingerprint
    record["assignment_id"] = f"assignment-{fingerprint}"
    record.pop("authentication_tag", None)
    return record


def test_fabricated_assignment_with_recomputed_plain_digest_is_rejected(tmp_path):
    coordinator, _ = build_coordinator(tmp_path)
    record = {
        "schema_version": 1,
        "sequence": 1,
        "assignment_id": "",
        "assignment_fingerprint": "",
        "routing_revision": 1,
        "mission_id": "fabricated-mission",
        "task_id": "fabricated-task",
        "coordinator_node_id": "coordinator-1",
        "worker_node_id": "attacker-worker",
        "required_capabilities": ["python_execution"],
        "authorization_metadata": {"level": "restricted"},
        "approval_metadata": {"required": True},
    }
    _forge_assignment_with_plain_digest(record)
    (tmp_path / "assignments.jsonl").write_text(
        json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(DispatchCorruptionError):
        coordinator.create_offer(
            assignment_id=record["assignment_id"],
            actor_node_id="coordinator-1",
            expires_at=START + timedelta(minutes=5),
        )


def test_altered_assignment_with_recomputed_plain_digest_is_rejected(tmp_path):
    coordinator, _ = build_coordinator(tmp_path)
    path = tmp_path / "assignments.jsonl"
    record = json.loads(path.read_text().splitlines()[0])
    record["worker_node_id"] = "attacker-worker"
    _forge_assignment_with_plain_digest(record)
    path.write_bytes(_canonical(record) + b"\n")

    with pytest.raises(DispatchCorruptionError):
        coordinator.create_offer(
            assignment_id=record["assignment_id"],
            actor_node_id="coordinator-1",
            expires_at=START + timedelta(minutes=5),
        )


def test_caller_created_assignment_object_cannot_establish_authority(tmp_path):
    coordinator, authoritative = build_coordinator(tmp_path)
    caller_created = replace(authoritative, worker_node_id="attacker-worker")

    with pytest.raises(ValueError, match="assignment_id"):
        coordinator.create_offer(
            assignment_id=caller_created,
            actor_node_id="coordinator-1",
            expires_at=START + timedelta(minutes=5),
        )


def test_integrity_key_is_explicit_bytes_and_shared_by_both_stores(tmp_path):
    with pytest.raises(TypeError, match="integrity_key"):
        DurableAssignmentRegistry(
            tmp_path / "missing-key.jsonl",
            coordinator_node_id="coordinator-1",
        )
    with pytest.raises(TypeError, match="bytes"):
        DurableAssignmentRegistry(
            tmp_path / "text-key.jsonl",
            coordinator_node_id="coordinator-1",
            integrity_key="not-bytes",
        )
    with pytest.raises(ValueError, match="at least 32 bytes"):
        DurableAssignmentRegistry(
            tmp_path / "short-key.jsonl",
            coordinator_node_id="coordinator-1",
            integrity_key=b"too-short",
        )

    store = DurableAssignmentRegistry(
        tmp_path / "assignments.jsonl",
        coordinator_node_id="coordinator-1",
        integrity_key=INTEGRITY_KEY,
    )
    with pytest.raises(ValueError, match="same integrity key"):
        TaskDispatchCoordinator(
            "coordinator-1",
            assignment_store=store,
            dispatch_store_path=tmp_path / "dispatch.jsonl",
            integrity_key=WRONG_INTEGRITY_KEY,
            heartbeat_registry=build_liveness(
                tmp_path / "wrong-key",
                key=WRONG_INTEGRITY_KEY,
            ),
        )


def test_dispatch_coordinator_requires_authoritative_heartbeat_registry(tmp_path):
    class FabricatedHeartbeatRegistry(HeartbeatRegistry):
        def __init__(self):
            pass

    store = DurableAssignmentRegistry(
        tmp_path / "assignments.jsonl",
        coordinator_node_id="coordinator-1",
        integrity_key=INTEGRITY_KEY,
    )
    kwargs = {
        "assignment_store": store,
        "dispatch_store_path": tmp_path / "dispatch.jsonl",
        "integrity_key": INTEGRITY_KEY,
    }

    with pytest.raises(TypeError, match="heartbeat_registry"):
        TaskDispatchCoordinator("coordinator-1", **kwargs)
    with pytest.raises(TypeError, match="HeartbeatRegistry"):
        TaskDispatchCoordinator(
            "coordinator-1",
            heartbeat_registry=None,
            **kwargs,
        )
    with pytest.raises(TypeError, match="HeartbeatRegistry"):
        TaskDispatchCoordinator(
            "coordinator-1",
            heartbeat_registry=object(),
            **kwargs,
        )
    with pytest.raises(TypeError, match="HeartbeatRegistry"):
        TaskDispatchCoordinator(
            "coordinator-1",
            heartbeat_registry=FabricatedHeartbeatRegistry(),
            **kwargs,
        )


def test_authoritative_assignment_binds_routing_metadata(tmp_path):
    coordinator, authoritative = build_coordinator(tmp_path)
    offer = create_offer(coordinator, authoritative)

    assert offer.offer_id.startswith("dispatch-")
    assert offer.assignment_id == authoritative.assignment_id
    assert offer.assignment_fingerprint == authoritative.assignment_fingerprint
    assert offer.required_capabilities == ("python_execution",)
    assert offer.authorization_metadata == (("level", "restricted"),)
    assert offer.approval_metadata == (("required", True),)


def test_assignment_registry_rejects_ambiguous_mission_task(tmp_path):
    assignments = DurableAssignmentRegistry(
        tmp_path / "assignments.jsonl",
        coordinator_node_id="coordinator-1",
        integrity_key=INTEGRITY_KEY,
    )
    assignments.record(make_assignment()[0])

    with pytest.raises(AssignmentConflictError):
        assignments.record(make_assignment(worker_id="worker-2")[0])


def test_foreign_coordinator_assignment_is_rejected(tmp_path):
    foreign_store = DurableAssignmentRegistry(
        tmp_path / "assignments.jsonl",
        coordinator_node_id="coordinator-2",
        integrity_key=INTEGRITY_KEY,
    )
    authoritative = foreign_store.record(make_assignment()[0])
    coordinator = TaskDispatchCoordinator(
        "coordinator-1",
        assignment_store=foreign_store,
        dispatch_store_path=tmp_path / "dispatch.jsonl",
        integrity_key=INTEGRITY_KEY,
        heartbeat_registry=build_liveness(tmp_path),
        clock=MutableClock(),
    )

    with pytest.raises(DispatchIdentityMismatchError):
        create_offer(coordinator, authoritative)


def test_offered_and_terminal_state_survive_restart(tmp_path):
    clock = MutableClock()
    coordinator, assignment = build_coordinator(tmp_path, clock)
    offered = create_offer(coordinator, assignment, clock)

    restarted = TaskDispatchCoordinator(
        "coordinator-1",
        assignment_store=coordinator.assignment_store,
        dispatch_store_path=tmp_path / "dispatch.jsonl",
        integrity_key=INTEGRITY_KEY,
        heartbeat_registry=coordinator._heartbeat_registry,
        clock=clock,
    )
    assert restarted.inspect_offer(offered.offer_id) == offered

    accepted = restarted.accept_offer(
        offer_id=offered.offer_id,
        actor_node_id="worker-1",
    )
    restarted_again = TaskDispatchCoordinator(
        "coordinator-1",
        assignment_store=coordinator.assignment_store,
        dispatch_store_path=tmp_path / "dispatch.jsonl",
        integrity_key=INTEGRITY_KEY,
        heartbeat_registry=coordinator._heartbeat_registry,
        clock=clock,
    )
    assert restarted_again.inspect_offer(offered.offer_id) == accepted


def test_interleaved_offer_histories_survive_restart(tmp_path):
    clock = MutableClock()
    coordinator, first_assignment = build_coordinator(
        tmp_path,
        clock,
        worker_ids=("worker-1", "worker-2"),
    )
    second, _ = make_assignment(task_id="task-2", worker_id="worker-2")
    second_assignment = coordinator.assignment_store.record(second)
    first_offer = create_offer(coordinator, first_assignment, clock)
    second_offer = coordinator.create_offer(
        assignment_id=second_assignment.assignment_id,
        actor_node_id="coordinator-1",
        expires_at=clock.current + timedelta(minutes=5),
    )

    accepted = coordinator.accept_offer(
        offer_id=first_offer.offer_id,
        actor_node_id="worker-1",
    )
    restarted = TaskDispatchCoordinator(
        "coordinator-1",
        assignment_store=coordinator.assignment_store,
        dispatch_store_path=tmp_path / "dispatch.jsonl",
        integrity_key=INTEGRITY_KEY,
        heartbeat_registry=coordinator._heartbeat_registry,
        clock=clock,
    )

    assert restarted.inspect_offer(first_offer.offer_id) == accepted
    assert restarted.inspect_offer(second_offer.offer_id) == second_offer
    assert [event.offer_id for event in restarted.audit_log()] == [
        first_offer.offer_id,
        second_offer.offer_id,
        first_offer.offer_id,
    ]


def test_expire_due_offers_handles_interleaved_offer_histories(tmp_path):
    clock = MutableClock()
    coordinator, first_assignment = build_coordinator(
        tmp_path,
        clock,
        worker_ids=("worker-1", "worker-2"),
    )
    second, _ = make_assignment(task_id="task-2", worker_id="worker-2")
    second_assignment = coordinator.assignment_store.record(second)
    first_offer = create_offer(coordinator, first_assignment, clock)
    second_offer = coordinator.create_offer(
        assignment_id=second_assignment.assignment_id,
        actor_node_id="coordinator-1",
        expires_at=clock.current + timedelta(minutes=5),
    )
    clock.advance(timedelta(minutes=5))

    expired = coordinator.expire_due_offers()

    assert {offer.offer_id for offer in expired} == {
        first_offer.offer_id,
        second_offer.offer_id,
    }
    assert all(offer.status is DispatchStatus.EXPIRED for offer in expired)
    assert all(
        offer.status is DispatchStatus.EXPIRED for offer in coordinator.list_offers()
    )


def test_identical_creation_after_restart_is_idempotent(tmp_path):
    coordinator, assignment = build_coordinator(tmp_path)
    first = create_offer(coordinator, assignment)
    before = (tmp_path / "dispatch.jsonl").read_bytes()
    restarted = TaskDispatchCoordinator(
        "coordinator-1",
        assignment_store=coordinator.assignment_store,
        dispatch_store_path=tmp_path / "dispatch.jsonl",
        integrity_key=INTEGRITY_KEY,
        heartbeat_registry=coordinator._heartbeat_registry,
        clock=MutableClock(),
    )

    second = create_offer(restarted, assignment)

    assert second == first
    assert (tmp_path / "dispatch.jsonl").read_bytes() == before


@pytest.mark.parametrize(
    ("method", "kwargs", "status"),
    [
        ("accept_offer", {}, DispatchStatus.ACCEPTED),
        ("reject_offer", {"reason": "declined"}, DispatchStatus.REJECTED),
        ("cancel_offer", {"reason": "cancelled"}, DispatchStatus.CANCELLED),
    ],
)
def test_exact_terminal_replay_is_idempotent(tmp_path, method, kwargs, status):
    coordinator, assignment = build_coordinator(tmp_path)
    offer = create_offer(coordinator, assignment)
    actor = "coordinator-1" if method == "cancel_offer" else "worker-1"
    action = getattr(coordinator, method)
    first = action(offer_id=offer.offer_id, actor_node_id=actor, **kwargs)
    before = (tmp_path / "dispatch.jsonl").read_bytes()

    second = action(offer_id=offer.offer_id, actor_node_id=actor, **kwargs)

    assert second == first
    assert second.status is status
    assert (tmp_path / "dispatch.jsonl").read_bytes() == before


def test_exact_expiration_replay_is_idempotent(tmp_path):
    clock = MutableClock()
    coordinator, assignment = build_coordinator(tmp_path, clock)
    offer = create_offer(coordinator, assignment, clock)
    clock.advance(timedelta(minutes=5))
    first = coordinator.expire_offer(offer_id=offer.offer_id)
    before = (tmp_path / "dispatch.jsonl").read_bytes()

    second = coordinator.expire_offer(offer_id=offer.offer_id)

    assert second == first
    assert coordinator.inspect_offer(offer.offer_id) == first
    assert (tmp_path / "dispatch.jsonl").read_bytes() == before


def test_acceptance_wins_over_later_expiration(tmp_path):
    clock = MutableClock()
    coordinator, assignment = build_coordinator(tmp_path, clock)
    offer = create_offer(coordinator, assignment, clock)
    accepted = coordinator.accept_offer(
        offer_id=offer.offer_id,
        actor_node_id="worker-1",
    )
    clock.advance(timedelta(minutes=5))

    with pytest.raises(DispatchTerminalStateError) as exc:
        coordinator.expire_offer(offer_id=offer.offer_id)

    assert exc.value.offer == accepted


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("accept", "reject"),
        ("accept", "cancel"),
        ("reject", "cancel"),
    ],
)
def test_conflicting_terminal_decision_reports_authoritative_state(
    tmp_path,
    first,
    second,
):
    coordinator, assignment = build_coordinator(tmp_path)
    offer = create_offer(coordinator, assignment)

    def act(name):
        if name == "accept":
            return coordinator.accept_offer(
                offer_id=offer.offer_id,
                actor_node_id="worker-1",
            )
        if name == "reject":
            return coordinator.reject_offer(
                offer_id=offer.offer_id,
                actor_node_id="worker-1",
                reason="declined",
            )
        return coordinator.cancel_offer(
            offer_id=offer.offer_id,
            actor_node_id="coordinator-1",
            reason="cancelled",
        )

    winner = act(first)
    with pytest.raises(DispatchTerminalStateError) as exc:
        act(second)
    assert exc.value.offer == winner


def test_acceptance_at_expiration_records_authoritative_expiration(tmp_path):
    clock = MutableClock()
    coordinator, assignment = build_coordinator(tmp_path, clock)
    offer = create_offer(coordinator, assignment, clock)
    clock.advance(timedelta(minutes=5))

    with pytest.raises(DispatchOfferExpiredError) as exc:
        coordinator.accept_offer(
            offer_id=offer.offer_id,
            actor_node_id="worker-1",
        )

    assert exc.value.offer.status is DispatchStatus.EXPIRED
    assert coordinator.inspect_offer(offer.offer_id) == exc.value.offer


def _race_transition(path, assignment_id, offer_id, action, barrier, results, key):
    clock = MutableClock()
    store = DurableAssignmentRegistry(
        Path(path) / "assignments.jsonl",
        coordinator_node_id="coordinator-1",
        integrity_key=key,
    )
    coordinator = TaskDispatchCoordinator(
        "coordinator-1",
        assignment_store=store,
        dispatch_store_path=Path(path) / "dispatch.jsonl",
        integrity_key=key,
        heartbeat_registry=build_liveness(
            path,
            clock=clock,
            key=key,
            record=False,
        ),
        clock=clock,
    )
    barrier.wait()
    try:
        if action == "accept":
            result = coordinator.accept_offer(
                offer_id=offer_id,
                actor_node_id="worker-1",
            )
        else:
            result = coordinator.reject_offer(
                offer_id=offer_id,
                actor_node_id="worker-1",
                reason="declined",
            )
        results.put(("ok", result.status.value))
    except DispatchTerminalStateError as exc:
        results.put(("conflict", exc.offer.status.value))


def test_first_terminal_transition_wins_across_processes(tmp_path):
    coordinator, assignment = build_coordinator(tmp_path)
    offer = create_offer(coordinator, assignment)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_race_transition,
            args=(
                str(tmp_path),
                assignment.assignment_id,
                offer.offer_id,
                action,
                barrier,
                results,
                INTEGRITY_KEY,
            ),
        )
        for action in ("accept", "reject")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0

    outcomes = [results.get(timeout=2) for _ in processes]
    states = {state for _, state in outcomes}
    assert len(states) == 1
    assert sorted(kind for kind, _ in outcomes) == ["conflict", "ok"]
    assert len(coordinator.audit_log()) == 2


def _race_create(path, assignment_id, barrier, results, key):
    store = DurableAssignmentRegistry(
        Path(path) / "assignments.jsonl",
        coordinator_node_id="coordinator-1",
        integrity_key=key,
    )
    coordinator = TaskDispatchCoordinator(
        "coordinator-1",
        assignment_store=store,
        dispatch_store_path=Path(path) / "dispatch.jsonl",
        integrity_key=key,
        heartbeat_registry=build_liveness(
            path,
            key=key,
            record=False,
        ),
        clock=MutableClock(),
    )
    barrier.wait()
    result = coordinator.create_offer(
        assignment_id=assignment_id,
        actor_node_id="coordinator-1",
        expires_at=START + timedelta(minutes=5),
    )
    results.put(result.offer_id)


def test_concurrent_identical_creation_appends_one_event(tmp_path):
    coordinator, assignment = build_coordinator(tmp_path)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_race_create,
            args=(
                str(tmp_path),
                assignment.assignment_id,
                barrier,
                results,
                INTEGRITY_KEY,
            ),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0

    assert len({results.get(timeout=2) for _ in processes}) == 1
    assert len(coordinator.audit_log()) == 1


def _canonical(record):
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _redigest(records):
    predecessor = "0" * 64
    for sequence, record in enumerate(records, 1):
        record["sequence"] = sequence
        record["predecessor_digest"] = predecessor
        unsigned = dict(record)
        unsigned.pop("resulting_digest", None)
        record["resulting_digest"] = hashlib.sha256(_canonical(unsigned)).hexdigest()
        predecessor = record["resulting_digest"]
    return b"".join(_canonical(record) + b"\n" for record in records)


def _plain_redigest_record(record):
    unsigned = dict(record)
    unsigned.pop("resulting_digest", None)
    record["resulting_digest"] = hashlib.sha256(_canonical(unsigned)).hexdigest()


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda data: b"\xff\n",
        lambda data: data.rstrip(b"\n"),
        lambda data: b'{"schema_version":',
        lambda data: data.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1', 1),
        lambda data: data.replace(b'"schema_version":1,', b"", 1),
        lambda data: data.replace(b'"schema_version":1', b'"schema_version":1,"unknown":true', 1),
        lambda data: data.replace(b'"sequence":1', b'"sequence":"1"', 1),
        lambda data: data.replace(b'"sequence":1', b'"sequence":2', 1),
        lambda data: data.replace(b'"resulting_digest":"', b'"resulting_digest":"0', 1),
    ],
    ids=[
        "invalid-utf8",
        "missing-final-newline",
        "truncated-json",
        "duplicate-json-key",
        "missing-field",
        "unknown-field",
        "wrong-type",
        "broken-sequence",
        "broken-digest",
    ],
)
def test_dispatch_log_basic_corruption_fails_closed(tmp_path, corrupt):
    coordinator, assignment = build_coordinator(tmp_path)
    offer = create_offer(coordinator, assignment)
    path = tmp_path / "dispatch.jsonl"
    path.write_bytes(corrupt(path.read_bytes()))

    with pytest.raises(DispatchCorruptionError):
        coordinator.inspect_offer(offer.offer_id)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda records: records[1].update(
            previous_state=None,
            event_type="offer_created",
            new_state="offered",
        ),
        lambda records: records.append(dict(records[1])),
        lambda records: records[1].update(worker_node_id="foreign-worker"),
        lambda records: records.append(
            {
                **records[0],
                "offer_id": "dispatch-conflict",
                "routing_assignment_id": "assignment-conflict",
            },
        ),
    ],
    ids=[
        "impossible-transition",
        "contradictory-terminal-event",
        "foreign-offer-event",
        "conflicting-offer-creation",
    ],
)
def test_semantic_corruption_fails_closed(tmp_path, mutation):
    coordinator, assignment = build_coordinator(tmp_path)
    offer = create_offer(coordinator, assignment)
    coordinator.accept_offer(offer_id=offer.offer_id, actor_node_id="worker-1")
    path = tmp_path / "dispatch.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    mutation(records)
    path.write_bytes(_redigest(records))

    with pytest.raises(DispatchCorruptionError):
        coordinator.inspect_offer(offer.offer_id)


def test_rewritten_terminal_event_with_plain_digest_is_rejected(tmp_path):
    coordinator, assignment = build_coordinator(tmp_path)
    offer = create_offer(coordinator, assignment)
    coordinator.accept_offer(offer_id=offer.offer_id, actor_node_id="worker-1")
    path = tmp_path / "dispatch.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[1].update(
        event_type="offer_rejected",
        new_state="rejected",
        reason="forged rejection",
    )
    _plain_redigest_record(records[1])
    path.write_bytes(b"".join(_canonical(record) + b"\n" for record in records))

    with pytest.raises(DispatchCorruptionError):
        coordinator.inspect_offer(offer.offer_id)


def test_fully_rewritten_unkeyed_event_chain_is_rejected(tmp_path):
    coordinator, assignment = build_coordinator(tmp_path)
    offer = create_offer(coordinator, assignment)
    coordinator.accept_offer(offer_id=offer.offer_id, actor_node_id="worker-1")
    path = tmp_path / "dispatch.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[1].update(
        event_type="offer_rejected",
        new_state="rejected",
        reason="forged rejection",
    )
    path.write_bytes(_redigest(records))

    with pytest.raises(DispatchCorruptionError):
        coordinator.inspect_offer(offer.offer_id)


def test_wrong_key_restart_fails_closed_and_preserves_original_bytes(tmp_path):
    coordinator, assignment = build_coordinator(tmp_path)
    offer = create_offer(coordinator, assignment)
    coordinator.accept_offer(offer_id=offer.offer_id, actor_node_id="worker-1")
    assignment_path = tmp_path / "assignments.jsonl"
    dispatch_path = tmp_path / "dispatch.jsonl"
    before_assignments = assignment_path.read_bytes()
    before_dispatch = dispatch_path.read_bytes()
    wrong_store = DurableAssignmentRegistry(
        assignment_path,
        coordinator_node_id="coordinator-1",
        integrity_key=WRONG_INTEGRITY_KEY,
    )
    restarted = TaskDispatchCoordinator(
        "coordinator-1",
        assignment_store=wrong_store,
        dispatch_store_path=dispatch_path,
        integrity_key=WRONG_INTEGRITY_KEY,
        heartbeat_registry=build_liveness(
            tmp_path,
            key=WRONG_INTEGRITY_KEY,
            record=False,
        ),
        clock=MutableClock(),
    )

    with pytest.raises(DispatchCorruptionError):
        restarted.inspect_offer(offer.offer_id)

    assert assignment_path.read_bytes() == before_assignments
    assert dispatch_path.read_bytes() == before_dispatch


def test_audit_records_bind_complete_required_schema(tmp_path):
    coordinator, assignment = build_coordinator(tmp_path)
    offer = create_offer(coordinator, assignment)
    rejected = coordinator.reject_offer(
        offer_id=offer.offer_id,
        actor_node_id="worker-1",
        reason="declined",
    )
    records = [
        json.loads(line)
        for line in (tmp_path / "dispatch.jsonl").read_text().splitlines()
    ]
    required = {
        "schema_version",
        "sequence",
        "event_type",
        "offer_id",
        "routing_assignment_id",
        "routing_assignment_fingerprint",
        "mission_id",
        "task_id",
        "coordinator_node_id",
        "worker_node_id",
        "required_capabilities",
        "previous_state",
        "new_state",
        "actor_type",
        "actor_node_id",
        "authorization_metadata",
        "approval_metadata",
        "timestamp",
        "expires_at",
        "reason",
        "predecessor_digest",
        "resulting_digest",
    }
    assert all(set(record) == required for record in records)
    assert records[-1]["resulting_digest"] == rejected.audit_history[-1].resulting_digest


def test_dispatch_foundation_exposes_no_execution_or_reassignment_methods(tmp_path):
    coordinator, _ = build_coordinator(tmp_path)
    assert {"execute", "run", "retry", "reassign", "send", "deploy"}.isdisjoint(
        dir(coordinator),
    )
