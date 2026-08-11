"""Adversarial worker heartbeat, lease, liveness, and routing tests."""

import hashlib
import inspect
import json
import multiprocessing
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from federation import (
    DurableAssignmentRegistry,
    Heartbeat,
    HeartbeatAuthenticationError,
    HeartbeatConflictError,
    HeartbeatCorruptionError,
    HeartbeatRegistry,
    LivenessState,
    NodeCapability,
    NodeRecord,
    NodeRegistry,
    PowerCapability,
    PowerState,
    RoutingOutcome,
    TaskDispatchCoordinator,
    TaskRequest,
    TaskRouter,
)
from federation.task_dispatcher import DispatchWorkerUnavailableError


START = datetime(2026, 8, 5, 21, 0, tzinfo=timezone.utc)
KEY = b"worker-heartbeat-v0.1-test-integrity-key"
WRONG_KEY = b"worker-heartbeat-v0.1-wrong-integrity-key"
GENESIS_TAG = "0" * 64


@dataclass
class MutableClock:
    current: datetime = START

    def __call__(self):
        return self.current

    def advance(self, delta):
        self.current += delta


def node_registry(*worker_ids):
    registry = NodeRegistry(stale_threshold_seconds=10**9)
    for worker_id in worker_ids:
        registry.register(
            NodeRecord(domain_id="test-domain", 
                node_id=worker_id,
                hostname=f"{worker_id}.local",
                operating_system="Linux",
                capabilities={NodeCapability("python_execution")},
            ),
        )
    return registry


def heartbeat(
    *,
    worker_id="worker-1",
    domain_id="test-domain",
    sequence=1,
    session_id="boot-1",
    worker_timestamp=START,
    health="healthy",
    power_capabilities=(
        PowerCapability.DISPLAY_CONTROL,
        PowerCapability.SLEEP,
        PowerCapability.WAKE_ON_LAN,
        PowerCapability.SCHEDULED_WAKE,
    ),
    requested_power_state=PowerState.ACTIVE,
    sleep_reason=None,
    expected_wake_time=None,
    wake_method=None,
    active_work_checkpointed=False,
    previous_authentication_tag=GENESIS_TAG,
    key=KEY,
):
    return Heartbeat.authenticated(
        worker_id=worker_id,
        registry_id="registry-1",
        domain_id=domain_id,
        sequence=sequence,
        session_id=session_id,
        worker_timestamp=worker_timestamp,
        health=health,
        power_capabilities=power_capabilities,
        requested_power_state=requested_power_state,
        sleep_reason=sleep_reason,
        expected_wake_time=expected_wake_time,
        wake_method=wake_method,
        active_work_checkpointed=active_work_checkpointed,
        previous_authentication_tag=previous_authentication_tag,
        integrity_key=key,
    )


def build_registry(tmp_path, clock=None, *, key=KEY, workers=("worker-1",)):
    return HeartbeatRegistry(
        tmp_path / "heartbeats.jsonl",
        registry_id="registry-1",
        node_registry=node_registry(*workers),
        integrity_key=key,
        clock=clock or MutableClock(),
    )


def next_heartbeat(accepted, clock, **overrides):
    values = {
        "sequence": accepted.sequence + 1,
        "worker_timestamp": clock.current,
        "previous_authentication_tag": accepted.authentication_tag,
    }
    values.update(overrides)
    return heartbeat(**values)


def _concurrent_record(path, start, output):
    clock = MutableClock(start)
    registry = build_registry(Path(path), clock)
    try:
        result = registry.record(heartbeat())
        output.put(("accepted", result.authentication_tag))
    except Exception as exc:  # pragma: no cover - reported to the parent
        output.put((type(exc).__name__, str(exc)))


def test_first_authenticated_heartbeat_creates_online_90_second_lease(tmp_path):
    clock = MutableClock()
    registry = build_registry(tmp_path, clock)

    accepted = registry.record(heartbeat())

    assert accepted.state is LivenessState.ONLINE
    assert accepted.controller_received_at == START
    assert accepted.lease_expires_at == START + timedelta(seconds=90)
    assert accepted.routing_eligible is True
    assert registry.inspect("worker-1") == accepted


def test_registry_requires_explicit_controller_owned_integrity_key(tmp_path):
    with pytest.raises(TypeError, match="bytes"):
        HeartbeatRegistry(
            tmp_path / "heartbeats.jsonl",
            registry_id="registry-1",
            node_registry=node_registry("worker-1"),
            integrity_key=None,
        )
    with pytest.raises(ValueError, match="at least 32 bytes"):
        HeartbeatRegistry(
            tmp_path / "heartbeats.jsonl",
            registry_id="registry-1",
            node_registry=node_registry("worker-1"),
            integrity_key=b"short",
        )


def test_exact_duplicate_is_idempotent_without_extending_lease(tmp_path):
    clock = MutableClock()
    registry = build_registry(tmp_path, clock)
    evidence = heartbeat()
    first = registry.record(evidence)
    before = (tmp_path / "heartbeats.jsonl").read_bytes()
    clock.advance(timedelta(seconds=20))

    second = registry.record(evidence)

    assert second == first
    assert second.lease_expires_at == START + timedelta(seconds=90)
    assert (tmp_path / "heartbeats.jsonl").read_bytes() == before


def test_changed_duplicate_sequence_is_rejected(tmp_path):
    registry = build_registry(tmp_path)
    registry.record(heartbeat())

    with pytest.raises(HeartbeatConflictError, match="duplicate"):
        registry.record(heartbeat(health="degraded"))


@pytest.mark.parametrize("sequence", [1, 4])
def test_replayed_and_out_of_order_sequences_are_rejected(tmp_path, sequence):
    clock = MutableClock()
    registry = build_registry(tmp_path, clock)
    first = registry.record(heartbeat())
    clock.advance(timedelta(seconds=30))
    second = registry.record(next_heartbeat(first, clock))

    replay = heartbeat(
        sequence=sequence,
        worker_timestamp=clock.current,
        previous_authentication_tag=(
            first.authentication_tag
            if sequence == 1
            else second.authentication_tag
        ),
    )
    with pytest.raises(HeartbeatConflictError, match="sequence"):
        registry.record(replay)

    assert registry.inspect("worker-1") == second


def test_fabricated_and_substituted_worker_identity_are_rejected(tmp_path):
    registry = build_registry(tmp_path, workers=("worker-1", "worker-2"))

    with pytest.raises(HeartbeatAuthenticationError, match="registered"):
        registry.record(heartbeat(worker_id="fabricated"))

    signed_for_worker_one = heartbeat()
    substituted = replace(signed_for_worker_one, worker_id="worker-2")
    with pytest.raises(HeartbeatAuthenticationError, match="authentication"):
        registry.record(substituted)


def test_wrong_key_restart_fails_closed_and_correct_key_restores(tmp_path):
    registry = build_registry(tmp_path)
    accepted = registry.record(heartbeat())
    path = tmp_path / "heartbeats.jsonl"
    before = path.read_bytes()

    wrong = build_registry(tmp_path, key=WRONG_KEY)
    with pytest.raises(HeartbeatCorruptionError, match="authentication"):
        wrong.inspect("worker-1")
    assert path.read_bytes() == before

    restarted = build_registry(tmp_path)
    assert restarted.inspect("worker-1") == accepted


def test_lease_uses_controller_time_and_expiration_blocks_routing(tmp_path):
    clock = MutableClock()
    registry = build_registry(tmp_path, clock)
    registry.record(
        heartbeat(worker_timestamp=START - timedelta(days=30)),
    )
    assert registry.inspect("worker-1").state is LivenessState.ONLINE

    clock.advance(timedelta(seconds=31))
    assert registry.inspect("worker-1").state is LivenessState.STALE
    clock.advance(timedelta(seconds=59))
    expired = registry.inspect("worker-1")
    assert expired.state is LivenessState.OFFLINE
    assert expired.routing_eligible is False


def test_worker_timestamp_cannot_extend_lease_and_future_time_is_rejected(tmp_path):
    clock = MutableClock()
    registry = build_registry(tmp_path, clock)

    with pytest.raises(ValueError, match="future"):
        registry.record(heartbeat(worker_timestamp=START + timedelta(seconds=1)))

    accepted = registry.record(heartbeat(worker_timestamp=START))
    clock.advance(timedelta(seconds=89))
    renewed = registry.record(
        next_heartbeat(
            accepted,
            clock,
            session_id="boot-2",
            worker_timestamp=START - timedelta(days=1),
        ),
    )
    assert renewed.lease_expires_at == clock.current + timedelta(seconds=90)


def test_controller_clock_rollback_fails_closed_across_restart(tmp_path):
    clock = MutableClock()
    registry = build_registry(tmp_path, clock)
    accepted = registry.record(heartbeat())
    clock.advance(timedelta(seconds=30))
    registry.record(next_heartbeat(accepted, clock))

    rolled_back = MutableClock(START + timedelta(seconds=29))
    restarted = build_registry(tmp_path, rolled_back)
    before = (tmp_path / "heartbeats.jsonl").read_bytes()

    with pytest.raises(HeartbeatConflictError, match="backwards"):
        restarted.inspect("worker-1")
    assert (tmp_path / "heartbeats.jsonl").read_bytes() == before


def test_intentional_sleep_is_distinct_and_power_metadata_survives_restart(tmp_path):
    clock = MutableClock()
    registry = build_registry(tmp_path, clock)
    wake = START + timedelta(hours=8)
    sleeping = registry.record(
        heartbeat(
            requested_power_state=PowerState.SLEEP,
            sleep_reason="Overnight maintenance window",
            expected_wake_time=wake,
            wake_method="wake_on_lan",
            active_work_checkpointed=True,
        ),
    )

    assert sleeping.state is LivenessState.INTENTIONALLY_SLEEPING
    assert sleeping.routing_eligible is False
    assert sleeping.sleep_reason == "Overnight maintenance window"
    assert sleeping.expected_wake_time == wake
    assert sleeping.wake_method == "wake_on_lan"
    assert sleeping.active_work_checkpointed is True

    clock.advance(timedelta(hours=1))
    restored = build_registry(tmp_path, clock).inspect("worker-1")
    assert restored.state is LivenessState.INTENTIONALLY_SLEEPING
    assert restored.expected_wake_time == wake
    assert restored.wake_method == "wake_on_lan"


def test_waking_is_ineligible_until_normal_authenticated_heartbeat(tmp_path):
    clock = MutableClock()
    registry = build_registry(tmp_path, clock)
    waking = registry.record(
        heartbeat(
            requested_power_state=PowerState.WAKING,
            wake_method="scheduled_wake",
        ),
    )
    assert waking.state is LivenessState.WAKING
    assert waking.routing_eligible is False

    clock.advance(timedelta(seconds=30))
    online = registry.record(next_heartbeat(waking, clock))
    assert online.state is LivenessState.ONLINE
    assert online.routing_eligible is True


def test_altered_or_plain_sha256_rebuilt_evidence_is_rejected(tmp_path):
    registry = build_registry(tmp_path)
    registry.record(heartbeat())
    path = tmp_path / "heartbeats.jsonl"
    record = json.loads(path.read_text())
    record["health"] = "degraded"
    unsigned = dict(record)
    unsigned.pop("authentication_tag")
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    record["authentication_tag"] = hashlib.sha256(canonical).hexdigest()
    path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")

    with pytest.raises(HeartbeatCorruptionError, match="authentication"):
        registry.inspect("worker-1")


@pytest.mark.parametrize("replacement", [b'{"schema_version":1', b"not-json\n"])
def test_corrupt_or_truncated_evidence_preserves_original_bytes(
    tmp_path,
    replacement,
):
    registry = build_registry(tmp_path)
    registry.record(heartbeat())
    path = tmp_path / "heartbeats.jsonl"
    path.write_bytes(replacement)
    before = path.read_bytes()

    with pytest.raises(HeartbeatCorruptionError):
        registry.inspect("worker-1")

    assert path.read_bytes() == before


def test_read_only_inspection_of_missing_store_creates_nothing(tmp_path):
    missing = tmp_path / "missing" / "heartbeats.jsonl"
    registry = HeartbeatRegistry(
        missing,
        registry_id="registry-1",
        node_registry=node_registry("worker-1"),
        integrity_key=KEY,
        clock=MutableClock(),
    )

    assert registry.list_workers() == ()
    assert registry.inspect("worker-1") is None
    assert not missing.parent.exists()


def test_concurrent_duplicate_writers_produce_one_valid_history(tmp_path):
    context = multiprocessing.get_context("fork")
    output = context.Queue()
    processes = [
        context.Process(
            target=_concurrent_record,
            args=(str(tmp_path), START, output),
        )
        for _ in range(6)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)

    assert all(process.exitcode == 0 for process in processes)
    results = [output.get(timeout=2) for _ in processes]
    assert {status for status, _ in results} == {"accepted"}
    assert len({tag for _, tag in results}) == 1
    assert len((tmp_path / "heartbeats.jsonl").read_text().splitlines()) == 1
    assert len(build_registry(tmp_path).history("worker-1")) == 1


def test_router_excludes_expired_sleeping_and_waking_without_reranking(tmp_path):
    clock = MutableClock()
    nodes = node_registry("a-worker", "b-worker", "c-worker")
    liveness = HeartbeatRegistry(
        tmp_path / "heartbeats.jsonl",
        registry_id="registry-1",
        node_registry=nodes,
        integrity_key=KEY,
        clock=clock,
    )
    a = liveness.record(heartbeat(worker_id="a-worker"))
    liveness.record(
        heartbeat(
            worker_id="b-worker",
            requested_power_state=PowerState.SLEEP,
            sleep_reason="Planned sleep",
        ),
    )
    liveness.record(
        heartbeat(
            worker_id="c-worker",
            requested_power_state=PowerState.WAKING,
        ),
    )
    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={NodeCapability("python_execution")},
    )

    first = TaskRouter(nodes, heartbeat_registry=liveness).route(request)
    assert first.outcome is RoutingOutcome.SUCCESS
    assert first.assigned_node_id == "a-worker"

    clock.advance(timedelta(seconds=90))
    expired = TaskRouter(nodes, heartbeat_registry=liveness).route(request)
    assert expired.outcome is RoutingOutcome.NO_ELIGIBLE_NODES
    assert {item.node_id for item in expired.excluded_nodes} == {
        "a-worker",
        "b-worker",
        "c-worker",
    }
    assert liveness.history("a-worker")[-1].authentication_tag == a.authentication_tag


def test_public_constructors_have_no_liveness_bypass():
    router_parameter = inspect.signature(TaskRouter).parameters["heartbeat_registry"]
    dispatch_parameter = inspect.signature(TaskDispatchCoordinator).parameters[
        "heartbeat_registry"
    ]

    assert router_parameter.default is inspect.Parameter.empty
    assert dispatch_parameter.default is inspect.Parameter.empty


@pytest.mark.parametrize(
    ("state", "expected_eligible"),
    [
        ("online", True),
        ("stale", False),
        ("offline", False),
        ("intentionally_sleeping", False),
        ("waking", False),
    ],
)
def test_liveness_state_controls_new_routing_and_dispatch(
    tmp_path,
    state,
    expected_eligible,
):
    clock = MutableClock()
    nodes = node_registry("worker-1")
    liveness = HeartbeatRegistry(
        tmp_path / "heartbeats.jsonl",
        registry_id="registry-1",
        node_registry=nodes,
        integrity_key=KEY,
        clock=clock,
    )
    online = liveness.record(heartbeat())
    assignments = DurableAssignmentRegistry(
        tmp_path / "assignments.jsonl",
        coordinator_node_id="coordinator-1",
        integrity_key=KEY,
    )
    router = TaskRouter(
        nodes,
        heartbeat_registry=liveness,
        assignment_store=assignments,
    )
    initial = router.route(
        TaskRequest(
            task_id="task-before-transition",
            mission_id="mission-before-transition",
            required_capabilities={NodeCapability("python_execution")},
        ),
    )
    assert initial.outcome is RoutingOutcome.SUCCESS

    if state == "stale":
        clock.advance(timedelta(seconds=31))
    elif state == "offline":
        clock.advance(timedelta(seconds=1))
        liveness.record(next_heartbeat(online, clock, health="unhealthy"))
    elif state == "intentionally_sleeping":
        clock.advance(timedelta(seconds=1))
        liveness.record(
            next_heartbeat(
                online,
                clock,
                requested_power_state=PowerState.SLEEP,
                sleep_reason="Intentional sleep",
            ),
        )
    elif state == "waking":
        clock.advance(timedelta(seconds=1))
        liveness.record(
            next_heartbeat(
                online,
                clock,
                requested_power_state=PowerState.WAKING,
            ),
        )

    later = router.route(
        TaskRequest(
            task_id="task-after-transition",
            mission_id="mission-after-transition",
            required_capabilities={NodeCapability("python_execution")},
        ),
    )
    coordinator = TaskDispatchCoordinator(
        "coordinator-1",
        assignment_store=assignments,
        dispatch_store_path=tmp_path / "dispatch.jsonl",
        integrity_key=KEY,
        heartbeat_registry=liveness,
        clock=clock,
    )

    if expected_eligible:
        assert later.outcome is RoutingOutcome.SUCCESS
        offer = coordinator.create_offer(
            assignment_id=initial.assignment_id,
            actor_node_id="coordinator-1",
            expires_at=START + timedelta(minutes=5),
        )
        assert offer.worker_node_id == "worker-1"
    else:
        assert later.outcome is RoutingOutcome.NO_ELIGIBLE_NODES
        with pytest.raises(DispatchWorkerUnavailableError, match="not available"):
            coordinator.create_offer(
                assignment_id=initial.assignment_id,
                actor_node_id="coordinator-1",
                expires_at=START + timedelta(minutes=5),
            )
        assert not (tmp_path / "dispatch.jsonl").exists()


def test_worker_becoming_ineligible_after_routing_is_rejected_before_offer(
    tmp_path,
):
    clock = MutableClock()
    nodes = node_registry("worker-1")
    liveness = HeartbeatRegistry(
        tmp_path / "heartbeats.jsonl",
        registry_id="registry-1",
        node_registry=nodes,
        integrity_key=KEY,
        clock=clock,
    )
    online = liveness.record(heartbeat())
    assignments = DurableAssignmentRegistry(
        tmp_path / "assignments.jsonl",
        coordinator_node_id="coordinator-1",
        integrity_key=KEY,
    )
    decision = TaskRouter(
        nodes,
        heartbeat_registry=liveness,
        assignment_store=assignments,
    ).route(
        TaskRequest(
            task_id="task-existing",
            mission_id="mission-existing",
            required_capabilities={NodeCapability("python_execution")},
        ),
    )
    clock.advance(timedelta(seconds=1))
    liveness.record(
        next_heartbeat(
            online,
            clock,
            requested_power_state=PowerState.SLEEP,
            sleep_reason="Intentional sleep",
        ),
    )
    coordinator = TaskDispatchCoordinator(
        "coordinator-1",
        assignment_store=assignments,
        dispatch_store_path=tmp_path / "dispatch.jsonl",
        integrity_key=KEY,
        clock=clock,
        heartbeat_registry=liveness,
    )

    with pytest.raises(DispatchWorkerUnavailableError, match="not available"):
        coordinator.create_offer(
            assignment_id=decision.assignment_id,
            actor_node_id="coordinator-1",
            expires_at=START + timedelta(minutes=5),
        )

    assert assignments.resolve(decision.assignment_id).worker_node_id == "worker-1"
    assert not (tmp_path / "dispatch.jsonl").exists()


def test_existing_offer_can_reach_terminal_state_after_worker_sleeps(tmp_path):
    clock = MutableClock()
    nodes = node_registry("worker-1")
    liveness = HeartbeatRegistry(
        tmp_path / "heartbeats.jsonl",
        registry_id="registry-1",
        node_registry=nodes,
        integrity_key=KEY,
        clock=clock,
    )
    online = liveness.record(heartbeat())
    assignments = DurableAssignmentRegistry(
        tmp_path / "assignments.jsonl",
        coordinator_node_id="coordinator-1",
        integrity_key=KEY,
    )
    decision = TaskRouter(
        nodes,
        heartbeat_registry=liveness,
        assignment_store=assignments,
    ).route(
        TaskRequest(
            task_id="task-existing",
            mission_id="mission-existing",
            required_capabilities={NodeCapability("python_execution")},
        ),
    )
    coordinator = TaskDispatchCoordinator(
        "coordinator-1",
        assignment_store=assignments,
        dispatch_store_path=tmp_path / "dispatch.jsonl",
        integrity_key=KEY,
        clock=clock,
        heartbeat_registry=liveness,
    )
    offer = coordinator.create_offer(
        assignment_id=decision.assignment_id,
        actor_node_id="coordinator-1",
        expires_at=START + timedelta(minutes=5),
    )
    before_sleep = (tmp_path / "dispatch.jsonl").read_bytes()
    clock.advance(timedelta(seconds=30))
    liveness.record(
        next_heartbeat(
            online,
            clock,
            requested_power_state=PowerState.SLEEP,
            sleep_reason="Intentional sleep after offer",
        ),
    )
    assert coordinator.create_offer(
        assignment_id=decision.assignment_id,
        actor_node_id="coordinator-1",
        expires_at=START + timedelta(minutes=5),
    ) == offer
    assert (tmp_path / "dispatch.jsonl").read_bytes() == before_sleep

    accepted = coordinator.accept_offer(
        offer_id=offer.offer_id,
        actor_node_id="worker-1",
    )

    assert accepted.status.value == "accepted"
    assert assignments.resolve(decision.assignment_id).worker_node_id == "worker-1"
