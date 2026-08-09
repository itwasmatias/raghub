"""Focused security, durability, and lifecycle tests for server lifecycle v0.1."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from federation import (
    AuthorizationLevel,
    DurableAssignmentRegistry,
    NodeCapability,
    NodeRecord,
    TaskAssignment,
    TaskDispatchCoordinator,
    TaskRequest,
    WorkerExecutionCoordinator,
    WorkerExecutionStatus,
)
from federation.heartbeat import Heartbeat
from federation.heartbeat_registry import HeartbeatRegistry
from federation.host_resource_capacity import (
    HostCapacityDecision,
    HostCapacityGuard,
    HostCapacityReason,
    HostCapacityStatus,
    HostCpuSnapshot,
    HostMemorySnapshot,
    HostResourceSnapshot,
    create_policy,
    create_requirement,
    _fingerprint as capacity_fingerprint,
    _format_timestamp as capacity_timestamp,
)
from federation.local_model_artifact_registry import (
    ArtifactRegistration,
    ArtifactStatus,
    LocalModelArtifactRegistry,
)
from federation.local_model_server_lifecycle import (
    FEDORA_LLAMA_SERVER_PATH,
    FEDORA_LLAMA_SERVER_SHA256,
    FEDORA_MODEL_ALIAS,
    ArtifactPreflightEvidence,
    CapacityPreflightEvidence,
    EndpointObservation,
    HttpResult,
    LifecycleArtifactError,
    LifecycleAuthorizationError,
    LifecycleBinaryIdentityError,
    LifecycleCapacityConstrainedError,
    LifecycleCapacityUnsafeError,
    LifecycleConflictError,
    LifecycleHealthAttestationError,
    LifecycleModelAttestationError,
    LifecyclePortConflictError,
    LifecycleProcessExitedError,
    LifecycleProcessLaunchError,
    LifecyclePropsAttestationError,
    LifecycleReconciliationRequired,
    LifecycleShutdownTimeoutError,
    LifecycleStartupTimeoutError,
    LifecycleStateError,
    LifecycleStopIdentityError,
    LifecycleStoreIntegrityError,
    LocalModelServerAttestation,
    LocalModelServerAttestor,
    LocalModelServerIdentity,
    LocalModelServerLifecycleCoordinator,
    LocalModelServerLifecycleState,
    LocalModelServerLifecycleStore,
    LocalModelServerProfile,
    LocalModelServerRecord,
    LocalModelServerStartRequest,
    LocalModelServerStopRequest,
    ProcessObservation,
    ProcessOutput,
    StableFileIdentity,
    _environment_policy_fingerprint,
    _fixed_argv,
)
from federation.registry import NodeRegistry


NOW = datetime(2026, 8, 9, 22, 0, tzinfo=timezone.utc)
KEY = b"local-model-server-lifecycle-test-key-0001"
EXECUTION_FINGERPRINT = "e" * 64


@dataclass
class Clock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        self.value += timedelta(microseconds=1)
        return self.value


@dataclass
class Monotonic:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def make_authority(root: Path, *, worker: str = "worker-1", fingerprint=EXECUTION_FINGERPRINT):
    clock = Clock()
    task = TaskRequest(
        "task-lifecycle",
        "mission-lifecycle",
        required_capabilities={NodeCapability("python_execution")},
        authorization_level=AuthorizationLevel.RESTRICTED,
        input_data={"execution_fingerprint": fingerprint} if fingerprint is not None else {},
        expected_result="governed local server lifecycle evidence",
    )
    assignment = TaskAssignment(
        task,
        NodeRecord(
            worker, "worker.local", "Fedora",
            capabilities={NodeCapability("python_execution")},
        ),
    )
    assignments = DurableAssignmentRegistry(
        root / "assignments.jsonl",
        coordinator_node_id="coordinator-1",
        integrity_key=KEY,
    )
    authoritative = assignments.record(assignment)
    nodes = NodeRegistry(stale_threshold_seconds=10**9)
    nodes.register(
        NodeRecord(
            worker, "worker.local", "Fedora",
            capabilities={NodeCapability("python_execution")},
        )
    )
    heartbeats = HeartbeatRegistry(
        root / "heartbeats.jsonl",
        registry_id="lifecycle-tests",
        node_registry=nodes,
        integrity_key=KEY,
        clock=clock,
    )
    heartbeats.record(
        Heartbeat.authenticated(
            worker_id=worker,
            registry_id="lifecycle-tests",
            sequence=1,
            session_id="boot-1",
            worker_timestamp=NOW,
            health="healthy",
            power_capabilities=(),
            requested_power_state="active",
            sleep_reason=None,
            expected_wake_time=None,
            wake_method=None,
            active_work_checkpointed=False,
            previous_authentication_tag="0" * 64,
            integrity_key=KEY,
        )
    )
    dispatch = TaskDispatchCoordinator(
        "coordinator-1",
        assignment_store=assignments,
        dispatch_store_path=root / "dispatch.jsonl",
        integrity_key=KEY,
        heartbeat_registry=heartbeats,
        clock=clock,
    )
    offer = dispatch.create_offer(
        assignment_id=authoritative.assignment_id,
        actor_node_id="coordinator-1",
        expires_at=NOW + timedelta(days=1),
    )
    offer = dispatch.accept_offer(offer_id=offer.offer_id, actor_node_id=worker)
    execution = WorkerExecutionCoordinator(
        "coordinator-1",
        dispatch_coordinator=dispatch,
        store_path=root / "execution.jsonl",
        integrity_key=KEY,
        clock=clock,
    )
    attempt = execution.register(
        dispatch_offer_id=offer.offer_id,
        actor_node_id="coordinator-1",
        expected_result="governed local server lifecycle evidence",
    )
    attempt = execution.claim(
        execution_attempt_id=attempt.request.execution_attempt_id,
        actor_node_id=worker,
    )
    return execution, attempt


def make_artifact(root: Path):
    models = root / "models"
    models.mkdir(parents=True, exist_ok=True)
    path = models / "model.gguf"
    path.write_bytes(b"GGUF" + b"\0" * 128)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    registry = LocalModelArtifactRegistry(
        root / "artifacts.jsonl",
        integrity_key=KEY,
        trusted_roots=[models],
        clock=Clock(),
    )
    state = registry.register(
        ArtifactRegistration(
            artifact_id="artifact-1",
            model_id="qwen2.5-0.5b-instruct",
            path=str(path),
            expected_sha256=digest,
            alias=FEDORA_MODEL_ALIAS,
        )
    )
    state = registry.verify(state.artifact_id)
    assert state.status is ArtifactStatus.VERIFIED
    return registry, state, path


def make_snapshot(node_id="worker-1", *, memory=16 << 30, load=0.1):
    memory_snapshot = HostMemorySnapshot(
        total_bytes=32 << 30,
        available_bytes=memory,
        total_swap_bytes=8 << 30,
        free_swap_bytes=8 << 30,
    )
    cpu = HostCpuSnapshot(
        logical_count=8,
        load_average_1m=load,
        load_average_5m=load,
        load_average_15m=load,
    )
    payload = {
        "node_id": node_id,
        "collected_at": capacity_timestamp(NOW),
        "memory": {
            "total_bytes": memory_snapshot.total_bytes,
            "available_bytes": memory_snapshot.available_bytes,
            "total_swap_bytes": memory_snapshot.total_swap_bytes,
            "free_swap_bytes": memory_snapshot.free_swap_bytes,
        },
        "cpu": {
            "logical_count": cpu.logical_count,
            "load_average_1m": cpu.load_average_1m,
            "load_average_5m": cpu.load_average_5m,
            "load_average_15m": cpu.load_average_15m,
        },
        "storage": [],
    }
    return HostResourceSnapshot(
        node_id=node_id,
        collected_at=NOW,
        memory=memory_snapshot,
        cpu=cpu,
        storage=(),
        fingerprint=capacity_fingerprint(payload),
    )


class StaticCollector:
    def __init__(self, snapshot=None):
        self.snapshot = snapshot or make_snapshot()
        self.calls = 0

    def collect(self, node_id, storage_roots=()):
        self.calls += 1
        if node_id != self.snapshot.node_id:
            raise ValueError("wrong node")
        return self.snapshot


class ForcedGuard:
    def __init__(self, status: HostCapacityStatus):
        self.status = status

    def evaluate(self, snapshot, requirement, policy):
        reasons = ()
        if self.status is HostCapacityStatus.CONSTRAINED:
            reasons = (HostCapacityReason.CONSTRAINED_CPU,)
        elif self.status is HostCapacityStatus.UNSAFE_TO_START:
            reasons = (HostCapacityReason.INSUFFICIENT_MEMORY,)
        payload = {
            "snapshot_fingerprint": snapshot.fingerprint,
            "requirement_fingerprint": requirement.fingerprint,
            "policy_fingerprint": policy.fingerprint,
            "status": self.status.value,
            "reasons": [reason.value for reason in reasons],
        }
        return HostCapacityDecision(
            snapshot_fingerprint=snapshot.fingerprint,
            requirement_fingerprint=requirement.fingerprint,
            policy_fingerprint=policy.fingerprint,
            status=self.status,
            reasons=reasons,
            fingerprint=capacity_fingerprint(payload),
        )


class FakeOpened:
    def __init__(self, identity, descriptor=9):
        self.identity = identity
        self.launch_path = f"/proc/self/fd/{descriptor}"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class FakeProcessOperations:
    def __init__(self, artifact_state, *, shared_launches=None):
        self.artifact_state = artifact_state
        self.binary = StableFileIdentity(
            canonical_path=FEDORA_LLAMA_SERVER_PATH,
            sha256=FEDORA_LLAMA_SERVER_SHA256,
            size_bytes=100,
            device=10,
            inode=20,
            mode=stat.S_IFREG | 0o755,
        )
        self.artifact = StableFileIdentity(
            canonical_path=artifact_state.path,
            sha256=artifact_state.expected_sha256,
            size_bytes=artifact_state.size_bytes,
            device=artifact_state.device,
            inode=artifact_state.inode,
            mode=stat.S_IFREG | 0o644,
        )
        self.launch_count = 0
        self.shared_launches = shared_launches
        self.pid = 4242
        self.observation = ProcessObservation.MATCHING
        self.terminated = False
        self.wait_observation = ProcessObservation.ABSENT
        self.port_preoccupied = False
        self.foreign_owner = None
        self.launch_error = None
        self.binary_error = None
        self.artifact_error = None
        self.terminate_error = None
        self.output_value = ProcessOutput("", "", False)
        self.signals = 0

    def open_binary(self, profile):
        if self.binary_error:
            raise self.binary_error
        return FakeOpened(self.binary, 8)

    def open_artifact(self, artifact):
        if self.artifact_error:
            raise self.artifact_error
        return FakeOpened(self.artifact, 9)

    def endpoint_observation(self):
        if self.port_preoccupied and not self.launch_count:
            return EndpointObservation(True, self.foreign_owner, NOW)
        if self.launch_count and not self.terminated:
            return EndpointObservation(True, self.pid, NOW)
        if self.foreign_owner is not None and self.terminated:
            return EndpointObservation(True, self.foreign_owner, NOW)
        return EndpointObservation(False, None, NOW)

    def launch(self, request, binary, artifact):
        if self.launch_error:
            raise self.launch_error
        self.launch_count += 1
        if self.shared_launches is not None:
            with self.shared_launches.get_lock():
                self.shared_launches.value += 1
        argv = _fixed_argv(request.profile, artifact.launch_path)
        return LocalModelServerIdentity(
            lifecycle_request_id=request.lifecycle_request_id,
            pid=self.pid,
            kernel_start_ticks=12345,
            boot_id="boot-id-1",
            executable=binary.identity,
            argv=argv,
            argv_fingerprint=hashlib.sha256(
                json.dumps(list(argv), sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            environment_policy_fingerprint=_environment_policy_fingerprint(),
            endpoint=request.profile.endpoint,
            created_at=NOW,
        )

    def observe(self, identity):
        if self.terminated:
            return ProcessObservation.ABSENT
        return self.observation

    def terminate(self, identity):
        self.signals += 1
        if self.terminate_error:
            raise self.terminate_error
        if self.observation is not ProcessObservation.MATCHING:
            raise LifecycleStopIdentityError("identity changed")
        if self.wait_observation is ProcessObservation.ABSENT:
            self.terminated = True

    def wait(self, identity, timeout_seconds):
        return ProcessObservation.ABSENT if self.terminated else self.wait_observation

    def output(self, identity):
        return self.output_value


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.responses = {
            "/health": HttpResult(200, (), b'{"status":"ok"}'),
            "/v1/models": HttpResult(
                200, (), json.dumps({"data": [{"id": FEDORA_MODEL_ALIAS}]}).encode()
            ),
            "/props": HttpResult(
                200, (), b'{"chat_template":"{{ prompt }}","chat_template_caps":{}}'
            ),
        }
        self.errors = {}

    def get(self, route, timeout_seconds):
        self.calls.append((route, timeout_seconds))
        error = self.errors.get(route)
        if error:
            raise error
        return self.responses[route]


def make_request(attempt, artifact, **overrides):
    requirement = create_requirement(256 << 20)
    policy = create_policy(
        minimum_host_memory_reserve_bytes=256 << 20,
        minimum_swap_reserve_bytes=0,
        maximum_normalized_load_threshold=100.0,
        minimum_storage_reserve_bytes=0,
        swap_pressure_threshold_bytes=100 << 30,
    )
    values = {
        "lifecycle_request_id": "lifecycle-1",
        "worker_node_id": attempt.request.worker_node_id,
        "execution_attempt_id": attempt.request.execution_attempt_id,
        "execution_fingerprint": attempt.request.execution_fingerprint,
        "artifact_id": artifact.artifact_id,
        "artifact_fingerprint": artifact.fingerprint,
        "artifact_sha256": artifact.expected_sha256,
        "profile": LocalModelServerProfile(),
        "capacity_requirement": requirement,
        "capacity_policy": policy,
        "startup_timeout_seconds": 1.0,
        "shutdown_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return LocalModelServerStartRequest(**values)


def make_coordinator(
    root, *, capacity=HostCapacityStatus.AVAILABLE, process=None, transport=None,
    worker="worker-1", fingerprint=EXECUTION_FINGERPRINT, request_overrides=None,
):
    execution, attempt = make_authority(root, worker=worker, fingerprint=fingerprint)
    registry, artifact, path = make_artifact(root)
    process = process or FakeProcessOperations(artifact)
    transport = transport or FakeTransport()
    clock = Clock()
    monotonic = Monotonic()
    store = LocalModelServerLifecycleStore(
        root / "lifecycle.jsonl", integrity_key=KEY
    )
    coordinator = LocalModelServerLifecycleCoordinator(
        store=store,
        worker_execution=execution,
        artifact_registry=registry,
        host_collector=StaticCollector(),
        capacity_guard=ForcedGuard(capacity),
        process_operations=process,
        http_transport=transport,
        clock=clock,
        monotonic=monotonic,
        sleeper=monotonic.sleep,
    )
    return (
        coordinator,
        make_request(attempt, artifact, **(request_overrides or {})),
        process,
        transport,
        execution,
        registry,
    )


def start_attested(tmp_path, **kwargs):
    coordinator, request, process, transport, execution, registry = make_coordinator(
        tmp_path, **kwargs
    )
    record = coordinator.start(request)
    assert record.state is LocalModelServerLifecycleState.ATTESTED
    return coordinator, request, record, process, transport, execution, registry


def make_stop(request, record, **overrides):
    values = {
        "stop_request_id": "stop-1",
        "lifecycle_request_id": request.lifecycle_request_id,
        "expected_lifecycle_fingerprint": record.record_fingerprint,
        "expected_process_identity": record.process_identity,
        "worker_node_id": request.worker_node_id,
        "execution_attempt_id": request.execution_attempt_id,
        "execution_fingerprint": request.execution_fingerprint,
        "shutdown_timeout_seconds": request.shutdown_timeout_seconds,
    }
    values.update(overrides)
    return LocalModelServerStopRequest(**values)


# Authority and composition


def test_valid_authoritative_execution_starts_and_remains_running(tmp_path):
    coordinator, request, record, _, _, execution, _ = start_attested(tmp_path)
    assert execution.inspect(request.execution_attempt_id).status is WorkerExecutionStatus.RUNNING
    assert record.request.execution_fingerprint == EXECUTION_FINGERPRINT


def test_missing_execution_attempt_rejected(tmp_path):
    coordinator, request, *_ = make_coordinator(tmp_path)
    request = replace(request, execution_attempt_id="missing")
    with pytest.raises(LifecycleAuthorizationError):
        coordinator.start(request)


def test_wrong_worker_rejected(tmp_path):
    coordinator, request, *_ = make_coordinator(tmp_path)
    request = replace(request, worker_node_id="worker-foreign")
    with pytest.raises(LifecycleAuthorizationError):
        coordinator.start(request)


def test_wrong_execution_fingerprint_rejected(tmp_path):
    coordinator, request, *_ = make_coordinator(tmp_path)
    request = replace(request, execution_fingerprint="f" * 64)
    with pytest.raises(LifecycleAuthorizationError):
        coordinator.start(request)


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "reconciliation"])
def test_terminal_or_invalid_execution_authority_rejected(tmp_path, terminal):
    coordinator, request, _, _, execution, _ = make_coordinator(tmp_path)
    execution.start(
        execution_attempt_id=request.execution_attempt_id,
        actor_node_id=request.worker_node_id,
    )
    if terminal == "succeeded":
        from federation.worker_execution import WorkerExecutionResultEnvelope
        execution.succeed(
            execution_attempt_id=request.execution_attempt_id,
            actor_node_id=request.worker_node_id,
            result=WorkerExecutionResultEnvelope("done"),
        )
    elif terminal == "failed":
        execution.fail(
            execution_attempt_id=request.execution_attempt_id,
            actor_node_id=request.worker_node_id,
            reason="failed",
        )
    else:
        execution.reconcile_interrupted(
            execution_attempt_id=request.execution_attempt_id,
            actor_node_id="coordinator-1",
        )
    with pytest.raises(LifecycleAuthorizationError):
        coordinator.start(request)


def test_authority_without_upstream_fingerprint_rejected(tmp_path):
    coordinator, request, *_ = make_coordinator(
        tmp_path,
        fingerprint=None,
        request_overrides={"execution_fingerprint": EXECUTION_FINGERPRINT},
    )
    with pytest.raises(
        LifecycleAuthorizationError,
        match="real upstream execution fingerprint",
    ):
        coordinator.start(request)
    assert coordinator.worker_execution.inspect(
        request.execution_attempt_id
    ).request.execution_fingerprint is None


def test_coordinator_rejects_self_authored_authority_object(tmp_path):
    execution, _ = make_authority(tmp_path)
    registry, _, _ = make_artifact(tmp_path)
    with pytest.raises(TypeError, match="authoritative"):
        LocalModelServerLifecycleCoordinator(
            store=LocalModelServerLifecycleStore(tmp_path / "l.jsonl", integrity_key=KEY),
            worker_execution=object(),
            artifact_registry=registry,
            host_collector=StaticCollector(),
            capacity_guard=HostCapacityGuard(),
            process_operations=object(),
            http_transport=FakeTransport(),
        )


# Artifact and binary


def test_verified_artifact_evidence_is_preserved(tmp_path):
    _, request, record, _, _, _, registry = start_attested(tmp_path)
    state = registry.resolve(request.artifact_id)
    assert record.artifact_evidence.artifact_fingerprint == state.fingerprint
    assert record.artifact_evidence.artifact_sha256 == state.expected_sha256
    assert record.artifact_evidence.device == state.device
    assert record.artifact_evidence.inode == state.inode


def test_invalidated_artifact_rejected(tmp_path):
    coordinator, request, _, _, _, registry = make_coordinator(tmp_path)
    Path(registry.resolve(request.artifact_id).path).write_bytes(b"GGUFchanged")
    with pytest.raises(LifecycleArtifactError):
        coordinator.start(request)
    assert coordinator.store.current(request.lifecycle_request_id).state is LocalModelServerLifecycleState.FAILED


@pytest.mark.parametrize(
    ("field", "value"),
    [("artifact_sha256", "a" * 64), ("artifact_fingerprint", "b" * 64)],
)
def test_artifact_request_identity_mismatch_rejected(tmp_path, field, value):
    coordinator, request, *_ = make_coordinator(tmp_path)
    with pytest.raises(LifecycleArtifactError):
        coordinator.start(replace(request, **{field: value}))


def test_artifact_changed_between_registry_and_launch_rejected(tmp_path):
    coordinator, request, process, *_ = make_coordinator(tmp_path)
    process.artifact_error = LifecycleArtifactError("descriptor mismatch")
    with pytest.raises(LifecycleArtifactError):
        coordinator.start(request)
    assert process.launch_count == 0


def test_artifact_stable_descriptor_physical_mismatch_rejected(tmp_path):
    coordinator, request, process, *_ = make_coordinator(tmp_path)
    process.artifact = replace(process.artifact, inode=process.artifact.inode + 1)
    with pytest.raises(LifecycleArtifactError):
        coordinator.start(request)


def test_binary_identity_preserved(tmp_path):
    _, _, record, process, *_ = start_attested(tmp_path)
    assert record.binary_identity == process.binary
    assert record.binary_identity.sha256 == FEDORA_LLAMA_SERVER_SHA256


@pytest.mark.parametrize(
    "change",
    [
        {"sha256": "a" * 64},
        {"canonical_path": "/tmp/llama-server"},
        {"mode": stat.S_IFREG | 0o644},
        {"mode": stat.S_IFDIR | 0o755},
    ],
)
def test_binary_mismatch_nonexecutable_or_nonregular_rejected(tmp_path, change):
    coordinator, request, process, *_ = make_coordinator(tmp_path)
    process.binary = replace(process.binary, **change)
    with pytest.raises(LifecycleBinaryIdentityError):
        coordinator.start(request)
    assert process.launch_count == 0


def test_binary_open_symlink_failure_propagates_typed(tmp_path):
    coordinator, request, process, *_ = make_coordinator(tmp_path)
    process.binary_error = LifecycleBinaryIdentityError("symlink")
    with pytest.raises(LifecycleBinaryIdentityError):
        coordinator.start(request)


# Capacity and request fingerprinting


def test_available_capacity_launches(tmp_path):
    _, _, _, process, *_ = start_attested(tmp_path)
    assert process.launch_count == 1


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (HostCapacityStatus.UNSAFE_TO_START, LifecycleCapacityUnsafeError),
        (HostCapacityStatus.CONSTRAINED, LifecycleCapacityConstrainedError),
    ],
)
def test_unsafe_and_constrained_capacity_fail_closed(tmp_path, status, error):
    coordinator, request, process, *_ = make_coordinator(tmp_path, capacity=status)
    with pytest.raises(error):
        coordinator.start(request)
    assert process.launch_count == 0


def test_explicit_typed_constrained_policy_can_allow_launch(tmp_path):
    coordinator, request, process, *_ = make_coordinator(
        tmp_path, capacity=HostCapacityStatus.CONSTRAINED
    )
    record = coordinator.start(replace(request, allow_constrained_capacity=True))
    assert record.state is LocalModelServerLifecycleState.ATTESTED
    assert process.launch_count == 1


def test_capacity_fingerprints_preserved(tmp_path):
    _, request, record, *_ = start_attested(tmp_path)
    evidence = record.capacity_evidence
    assert evidence.requirement_fingerprint == request.capacity_requirement.fingerprint
    assert evidence.policy_fingerprint == request.capacity_policy.fingerprint
    assert len(evidence.snapshot_fingerprint) == 64
    assert len(evidence.decision_fingerprint) == 64


def test_changed_capacity_policy_changes_request_fingerprint(tmp_path):
    _, request, *_ = make_coordinator(tmp_path)
    changed = create_policy(
        minimum_host_memory_reserve_bytes=1,
        minimum_swap_reserve_bytes=0,
        maximum_normalized_load_threshold=100.0,
        minimum_storage_reserve_bytes=0,
        swap_pressure_threshold_bytes=100 << 30,
    )
    assert replace(request, capacity_policy=changed).request_fingerprint != request.request_fingerprint


def test_request_fingerprint_is_deterministic_and_round_trips(tmp_path):
    _, request, *_ = make_coordinator(tmp_path)
    copy = LocalModelServerStartRequest.from_dict(request.to_dict())
    assert copy == request
    assert copy.request_fingerprint == request.request_fingerprint


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lifecycle_request_id", "lifecycle-2"),
        ("worker_node_id", "worker-2"),
        ("execution_attempt_id", "execution-2"),
        ("execution_fingerprint", "a" * 64),
        ("artifact_id", "artifact-2"),
        ("artifact_fingerprint", "b" * 64),
        ("artifact_sha256", "c" * 64),
        ("allow_constrained_capacity", True),
        ("startup_timeout_seconds", 2.0),
        ("shutdown_timeout_seconds", 2.0),
    ],
)
def test_every_start_field_changes_request_fingerprint(tmp_path, field, value):
    _, request, *_ = make_coordinator(tmp_path)
    changed = replace(request, **{field: value})
    assert changed.request_fingerprint != request.request_fingerprint


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("startup_timeout_seconds", True),
        ("shutdown_timeout_seconds", False),
        ("allow_constrained_capacity", 1),
    ],
)
def test_bool_numeric_aliases_rejected(tmp_path, field, value):
    _, request, *_ = make_coordinator(tmp_path)
    with pytest.raises((TypeError, ValueError)):
        replace(request, **{field: value})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -1, 301])
def test_invalid_timeout_rejected(tmp_path, value):
    _, request, *_ = make_coordinator(tmp_path)
    with pytest.raises((TypeError, ValueError)):
        replace(request, startup_timeout_seconds=value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bind_host", "0.0.0.0"),
        ("bind_host", "localhost"),
        ("port", 18081),
        ("binary_path", "/tmp/llama-server"),
        ("model_alias", "other"),
        ("offline", False),
        ("web_ui_disabled", False),
    ],
)
def test_profile_rejects_remote_dynamic_or_arbitrary_values(field, value):
    with pytest.raises(ValueError, match="fixed"):
        LocalModelServerProfile(**{field: value})


# Process creation and attestation


def test_fixed_argv_is_exact_and_contains_no_inference_route(tmp_path):
    _, _, record, *_ = start_attested(tmp_path)
    argv = record.process_identity.argv
    assert argv == (
        FEDORA_LLAMA_SERVER_PATH,
        "--model", "/proc/self/fd/9",
        "--alias", FEDORA_MODEL_ALIAS,
        "--host", "127.0.0.1",
        "--port", "18080",
        "--ctx-size", "1024",
        "--threads", "4",
        "--threads-batch", "4",
        "--parallel", "1",
        "--n-gpu-layers", "0",
        "--offline",
        "--no-webui",
    )
    assert not any("completion" in item for item in argv)


def test_environment_policy_is_explicit_and_deterministic():
    assert _environment_policy_fingerprint() == _environment_policy_fingerprint()
    assert len(_environment_policy_fingerprint()) == 64


def test_process_pid_birth_executable_argv_and_request_captured(tmp_path):
    _, request, record, *_ = start_attested(tmp_path)
    identity = record.process_identity
    assert identity.pid == 4242
    assert identity.kernel_start_ticks == 12345
    assert identity.boot_id == "boot-id-1"
    assert identity.executable == record.binary_identity
    assert identity.lifecycle_request_id == request.lifecycle_request_id
    assert identity.argv_fingerprint == record.attestation.invocation_fingerprint


def test_process_launch_failure_records_failed(tmp_path):
    coordinator, request, process, *_ = make_coordinator(tmp_path)
    process.launch_error = LifecycleProcessLaunchError("exec failed")
    with pytest.raises(LifecycleProcessLaunchError):
        coordinator.start(request)
    assert coordinator.store.current(request.lifecycle_request_id).state is LocalModelServerLifecycleState.FAILED


def test_early_process_exit_records_failed(tmp_path):
    coordinator, request, process, *_ = make_coordinator(tmp_path)
    process.observation = ProcessObservation.ABSENT
    with pytest.raises(LifecycleProcessExitedError):
        coordinator.start(request)
    assert coordinator.store.current(request.lifecycle_request_id).state is LocalModelServerLifecycleState.FAILED


def test_startup_timeout_performs_exact_normal_cleanup(tmp_path):
    coordinator, request, process, transport, *_ = make_coordinator(tmp_path)
    transport.errors["/health"] = ConnectionRefusedError()
    with pytest.raises(LifecycleStartupTimeoutError):
        coordinator.start(request)
    assert process.signals == 1
    assert coordinator.store.current(request.lifecycle_request_id).state is LocalModelServerLifecycleState.FAILED


def test_startup_cleanup_timeout_requires_reconciliation(tmp_path):
    coordinator, request, process, transport, *_ = make_coordinator(tmp_path)
    transport.errors["/health"] = ConnectionRefusedError()
    process.wait_observation = ProcessObservation.MATCHING
    with pytest.raises(LifecycleStartupTimeoutError):
        coordinator.start(request)
    current = coordinator.store.current(request.lifecycle_request_id)
    assert current.state is LocalModelServerLifecycleState.RECONCILIATION_REQUIRED
    assert current.failure_code == "startup_cleanup_ambiguous"
    assert process.launch_count == 1


def test_bounded_output_is_preserved(tmp_path):
    coordinator, request, process, *_ = make_coordinator(tmp_path)
    process.output_value = ProcessOutput("out-tail", "err-tail", True)
    record = coordinator.start(request)
    assert (record.stdout_tail, record.stderr_tail, record.output_truncated) == (
        "out-tail", "err-tail", True
    )


def test_unknown_occupied_port_prevents_launch_and_is_never_killed(tmp_path):
    coordinator, request, process, *_ = make_coordinator(tmp_path)
    process.port_preoccupied = True
    process.foreign_owner = 9999
    with pytest.raises(LifecyclePortConflictError):
        coordinator.start(request)
    assert process.launch_count == 0
    assert process.signals == 0
    current = coordinator.store.current(request.lifecycle_request_id)
    assert current.state is LocalModelServerLifecycleState.RECONCILIATION_REQUIRED
    assert current.capacity_evidence is not None
    assert current.artifact_evidence is None
    assert current.binary_identity is None
    assert current.final_endpoint_observation.owner_pid == 9999


def test_complete_attestation_requires_only_health_models_and_props(tmp_path):
    _, _, record, _, transport, *_ = start_attested(tmp_path)
    assert [route for route, _ in transport.calls] == ["/health", "/v1/models", "/props"]
    assert record.attestation.props is not None
    assert record.state is LocalModelServerLifecycleState.ATTESTED


@pytest.mark.parametrize(
    ("route", "result", "error"),
    [
        ("/health", HttpResult(200, (), b'{"status":"bad"}'), LifecycleHealthAttestationError),
        ("/health", HttpResult(302, (("Location", "http://example.com"),), b"{}"), LifecycleHealthAttestationError),
        ("/health", HttpResult(200, (), b"not-json"), LifecycleHealthAttestationError),
        ("/v1/models", HttpResult(200, (), b'{"data":[]}'), LifecycleModelAttestationError),
        ("/v1/models", HttpResult(200, (), b'{"data":[{"id":"a"},{"id":"b"}]}'), LifecycleModelAttestationError),
        ("/v1/models", HttpResult(200, (), b'{"data":[{"id":"wrong"}]}'), LifecycleModelAttestationError),
        ("/v1/models", HttpResult(200, (), b"[]"), LifecycleModelAttestationError),
        ("/props", HttpResult(200, (), b'{"chat_template":""}'), LifecyclePropsAttestationError),
        ("/props", HttpResult(200, (), b'{"chat_template":"x","chat_template_caps":[]}'), LifecyclePropsAttestationError),
        ("/props", HttpResult(307, (("Location", "/other"),), b"{}"), LifecyclePropsAttestationError),
    ],
)
def test_attestation_failures_are_typed_and_cleaned_up(tmp_path, route, result, error):
    coordinator, request, process, transport, *_ = make_coordinator(tmp_path)
    transport.responses[route] = result
    with pytest.raises(error):
        coordinator.start(request)
    assert process.signals == 1
    assert coordinator.store.current(request.lifecycle_request_id).state is LocalModelServerLifecycleState.FAILED


def test_endpoint_owner_mismatch_requires_reconciliation(tmp_path):
    coordinator, request, process, *_ = make_coordinator(tmp_path)
    process.foreign_owner = 9999
    original = process.endpoint_observation

    def observation():
        if process.launch_count:
            return EndpointObservation(True, 9999, NOW)
        return original()

    process.endpoint_observation = observation
    with pytest.raises(LifecycleReconciliationRequired):
        coordinator.start(request)
    current = coordinator.store.current(request.lifecycle_request_id)
    assert current.state is LocalModelServerLifecycleState.RECONCILIATION_REQUIRED
    assert current.failure_code == "LifecycleReconciliationRequired"
    assert process.signals == 0
    assert process.launch_count == 1


def test_running_unattested_is_not_usable(tmp_path):
    coordinator, request, process, transport, *_ = make_coordinator(tmp_path)
    transport.errors["/health"] = ConnectionRefusedError()
    process.wait_observation = ProcessObservation.MATCHING
    with pytest.raises(LifecycleStartupTimeoutError):
        coordinator.start(request)
    current = coordinator.store.current(request.lifecycle_request_id)
    assert current.state is LocalModelServerLifecycleState.RECONCILIATION_REQUIRED
    assert current.failure_code == "startup_cleanup_ambiguous"
    assert process.launch_count == 1


# Idempotency, stop, durability, reconciliation


def test_exact_repeated_start_returns_existing_without_relaunch(tmp_path):
    coordinator, request, first, process, *_ = start_attested(tmp_path)
    second = coordinator.start(request)
    assert second == first
    assert process.launch_count == 1


def test_conflicting_same_lifecycle_request_id_rejected(tmp_path):
    coordinator, request, *_ = start_attested(tmp_path)
    with pytest.raises(LifecycleConflictError):
        coordinator.start(replace(request, startup_timeout_seconds=2.0))


def test_failed_lifecycle_does_not_replay(tmp_path):
    coordinator, request, process, *_ = make_coordinator(tmp_path)
    process.launch_error = LifecycleProcessLaunchError("no")
    with pytest.raises(LifecycleProcessLaunchError):
        coordinator.start(request)
    with pytest.raises(LifecycleStateError):
        coordinator.start(request)
    assert process.launch_count == 0


def test_new_request_cannot_reuse_execution_attempt(tmp_path):
    coordinator, request, *_ = start_attested(tmp_path)
    with pytest.raises(LifecycleConflictError):
        coordinator.start(replace(request, lifecycle_request_id="lifecycle-2"))


def test_exact_governed_process_stops_normally(tmp_path):
    coordinator, request, record, process, _, execution, _ = start_attested(tmp_path)
    stopped = coordinator.stop(make_stop(request, record))
    assert stopped.state is LocalModelServerLifecycleState.STOPPED
    assert stopped.final_process_observation == "absent"
    assert process.signals == 1
    assert execution.inspect(request.execution_attempt_id).status is WorkerExecutionStatus.SUCCEEDED


def test_arbitrary_pid_stop_is_not_an_api(tmp_path):
    coordinator, request, record, *_ = start_attested(tmp_path)
    with pytest.raises(TypeError):
        coordinator.stop(4242)
    with pytest.raises(TypeError):
        coordinator.stop(request=make_stop(request, record), pid=9999)


@pytest.mark.parametrize(
    "observation",
    [ProcessObservation.IDENTITY_MISMATCH, ProcessObservation.ABSENT],
)
def test_pid_reuse_or_absent_identity_refuses_signal(tmp_path, observation):
    coordinator, request, record, process, *_ = start_attested(tmp_path)
    process.observation = observation
    with pytest.raises(LifecycleReconciliationRequired):
        coordinator.stop(make_stop(request, record))
    assert process.signals == 0


def test_stop_request_must_bind_current_lifecycle_fingerprint(tmp_path):
    coordinator, request, record, *_ = start_attested(tmp_path)
    with pytest.raises(LifecycleStopIdentityError):
        coordinator.stop(
            make_stop(request, record, expected_lifecycle_fingerprint="a" * 64)
        )


def test_shutdown_timeout_never_fabricates_stopped_or_sigkill(tmp_path):
    coordinator, request, record, process, *_ = start_attested(tmp_path)
    process.wait_observation = ProcessObservation.MATCHING
    with pytest.raises(LifecycleShutdownTimeoutError):
        coordinator.stop(make_stop(request, record))
    current = coordinator.store.current(request.lifecycle_request_id)
    assert current.state is LocalModelServerLifecycleState.RECONCILIATION_REQUIRED
    assert current.failure_code == "shutdown_timeout"
    assert process.signals == 1


def test_unrelated_process_rebinding_after_exit_is_not_killed(tmp_path):
    coordinator, request, record, process, *_ = start_attested(tmp_path)
    process.foreign_owner = 9999
    stopped = coordinator.stop(make_stop(request, record))
    assert stopped.state is LocalModelServerLifecycleState.STOPPED
    assert stopped.final_endpoint_observation.owner_pid == 9999
    assert process.signals == 1


def test_repeated_exact_stop_is_idempotent(tmp_path):
    coordinator, request, record, process, *_ = start_attested(tmp_path)
    stop = make_stop(request, record)
    first = coordinator.stop(stop)
    second = coordinator.stop(stop)
    assert second == first
    assert process.signals == 1


def test_conflicting_stop_after_stopped_rejected(tmp_path):
    coordinator, request, record, *_ = start_attested(tmp_path)
    stop = make_stop(request, record)
    coordinator.stop(stop)
    with pytest.raises(LifecycleConflictError):
        coordinator.stop(replace(stop, stop_request_id="stop-2"))


def test_lifecycle_survives_store_restart(tmp_path):
    coordinator, request, record, *_ = start_attested(tmp_path)
    restarted = LocalModelServerLifecycleStore(
        coordinator.store.path, integrity_key=KEY
    )
    assert restarted.current(request.lifecycle_request_id) == record
    assert len(restarted.history(request.lifecycle_request_id)) == 5


@pytest.mark.parametrize("mutation", ["truncate", "byte", "duplicate", "chain"])
def test_tampered_truncated_or_malformed_history_fails_closed(tmp_path, mutation):
    coordinator, request, *_ = start_attested(tmp_path)
    path = coordinator.store.path
    data = path.read_bytes()
    if mutation == "truncate":
        path.write_bytes(data[:-1])
    elif mutation == "byte":
        path.write_bytes(data.replace(b"requested", b"requesTed", 1))
    elif mutation == "duplicate":
        first, rest = data.split(b"\n", 1)
        path.write_bytes(first.replace(b'"sequence":1', b'"sequence":1,"sequence":1') + b"\n" + rest)
    else:
        lines = data.splitlines()
        item = json.loads(lines[1])
        item["predecessor_tag"] = "0" * 64
        lines[1] = json.dumps(item).encode()
        path.write_bytes(b"\n".join(lines) + b"\n")
    with pytest.raises(LifecycleStoreIntegrityError):
        coordinator.store.current(request.lifecycle_request_id)


def test_wrong_integrity_key_fails_closed(tmp_path):
    coordinator, request, *_ = start_attested(tmp_path)
    wrong = LocalModelServerLifecycleStore(
        coordinator.store.path, integrity_key=b"x" * 32
    )
    with pytest.raises(LifecycleStoreIntegrityError):
        wrong.current(request.lifecycle_request_id)


def test_authenticated_history_has_predecessor_chain(tmp_path):
    coordinator, *_ = start_attested(tmp_path)
    lines = [json.loads(line) for line in coordinator.store.path.read_text().splitlines()]
    assert lines[0]["predecessor_tag"] == "0" * 64
    assert all(
        lines[index]["predecessor_tag"] == lines[index - 1]["authentication_tag"]
        for index in range(1, len(lines))
    )


def test_starting_without_process_and_closed_endpoint_fails_reconciliation(tmp_path):
    coordinator, request, process, *_ = make_coordinator(tmp_path)
    requested = coordinator.store.append(
        LocalModelServerRecord(
            request=request,
            state=LocalModelServerLifecycleState.REQUESTED,
            updated_at=NOW,
            previous_record_fingerprint=None,
        )
    )
    failed = coordinator._transition(
        requested,
        LocalModelServerLifecycleState.FAILED,
        failure_code="fixture",
        failure_detail="fixture terminal",
    )
    assert coordinator.reconcile(request.lifecycle_request_id) == failed
    assert process.launch_count == 0


def test_running_matching_process_reconciles_to_attested_without_launch(tmp_path):
    coordinator, request, process, *_ = make_coordinator(tmp_path)
    original_attest = coordinator._attestor.attest
    captured = {}

    def interrupt(*args, **kwargs):
        captured["current"] = coordinator.store.current(request.lifecycle_request_id)
        raise KeyboardInterrupt

    coordinator._attestor.attest = interrupt
    with pytest.raises(KeyboardInterrupt):
        coordinator.start(request)
    coordinator._attestor.attest = original_attest
    assert captured["current"].state is LocalModelServerLifecycleState.RUNNING_UNATTESTED
    record = coordinator.reconcile(request.lifecycle_request_id)
    assert record.state is LocalModelServerLifecycleState.ATTESTED
    assert process.launch_count == 1


def test_running_foreign_pid_reconciliation_requires_operator(tmp_path):
    coordinator, request, process, *_ = make_coordinator(tmp_path)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    coordinator._attestor.attest = interrupt
    with pytest.raises(KeyboardInterrupt):
        coordinator.start(request)
    process.observation = ProcessObservation.IDENTITY_MISMATCH
    with pytest.raises(LifecycleReconciliationRequired):
        coordinator.reconcile(request.lifecycle_request_id)
    assert process.launch_count == 1


def test_running_absent_process_reconciliation_fails_deterministically(tmp_path):
    coordinator, request, process, *_ = make_coordinator(tmp_path)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    coordinator._attestor.attest = interrupt
    with pytest.raises(KeyboardInterrupt):
        coordinator.start(request)
    process.observation = ProcessObservation.ABSENT
    record = coordinator.reconcile(request.lifecycle_request_id)
    assert record.state is LocalModelServerLifecycleState.FAILED
    assert process.launch_count == 1


def test_reconciliation_never_launches_replacement(tmp_path):
    coordinator, request, process, *_ = make_coordinator(tmp_path)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    coordinator._attestor.attest = interrupt
    with pytest.raises(KeyboardInterrupt):
        coordinator.start(request)
    before = process.launch_count
    process.observation = ProcessObservation.ABSENT
    coordinator.reconcile(request.lifecycle_request_id)
    assert process.launch_count == before


def test_stop_request_round_trip_and_fingerprint(tmp_path):
    _, request, record, *_ = start_attested(tmp_path)
    stop = make_stop(request, record)
    assert LocalModelServerStopRequest.from_dict(stop.to_dict()) == stop
    assert len(stop.request_fingerprint) == 64


def test_attestation_binds_all_preflight_and_process_evidence(tmp_path):
    _, request, record, *_ = start_attested(tmp_path)
    attestation = record.attestation
    assert attestation.lifecycle_request_fingerprint == request.request_fingerprint
    assert attestation.execution_fingerprint == request.execution_fingerprint
    assert attestation.artifact_evidence_fingerprint == record.artifact_evidence.fingerprint
    assert attestation.binary_fingerprint == record.binary_identity.fingerprint
    assert attestation.capacity_evidence_fingerprint == record.capacity_evidence.fingerprint
    assert attestation.process_identity_fingerprint == record.process_identity.fingerprint


def test_no_cloud_download_shell_or_arbitrary_command_surface(tmp_path):
    coordinator, request, *_ = make_coordinator(tmp_path)
    for kwargs in (
        {"command": "sh -c anything"},
        {"argv": ("anything",)},
        {"environment": {"HF_TOKEN": "secret"}},
        {"download_url": "https://example.com/model"},
        {"shell": True},
    ):
        with pytest.raises(TypeError):
            LocalModelServerStartRequest(**{**request.to_dict(False), **kwargs})
    assert not hasattr(coordinator, "run_command")
    assert not hasattr(coordinator, "kill_pid")


def _concurrent_start(root, shared_launches, output):
    try:
        execution, attempt = make_authority(Path(root))
        registry = LocalModelArtifactRegistry(
            Path(root) / "artifacts.jsonl",
            integrity_key=KEY,
            trusted_roots=[Path(root) / "models"],
            clock=Clock(),
        )
        artifact = registry.resolve("artifact-1")
        process = FakeProcessOperations(artifact, shared_launches=shared_launches)
        monotonic = Monotonic()
        coordinator = LocalModelServerLifecycleCoordinator(
            store=LocalModelServerLifecycleStore(
                Path(root) / "lifecycle.jsonl", integrity_key=KEY
            ),
            worker_execution=execution,
            artifact_registry=registry,
            host_collector=StaticCollector(),
            capacity_guard=ForcedGuard(HostCapacityStatus.AVAILABLE),
            process_operations=process,
            http_transport=FakeTransport(),
            clock=Clock(),
            monotonic=monotonic,
            sleeper=monotonic.sleep,
        )
        request = make_request(attempt, artifact)
        output.put(coordinator.start(request).state.value)
    except Exception as exc:
        output.put(type(exc).__name__)


def test_cross_process_duplicate_start_launches_once(tmp_path):
    execution, attempt = make_authority(tmp_path)
    registry, artifact, _ = make_artifact(tmp_path)
    # Remove the setup execution history so both children reconstruct the same authority.
    assert execution.inspect(attempt.request.execution_attempt_id).status is WorkerExecutionStatus.CLAIMED
    context = multiprocessing.get_context("fork")
    launches = context.Value("i", 0)
    output = context.Queue()
    workers = [
        context.Process(
            target=_concurrent_start,
            args=(str(tmp_path), launches, output),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(20)
    assert all(worker.exitcode == 0 for worker in workers)
    assert [output.get(timeout=2) for _ in workers] == ["attested", "attested"]
    assert launches.value == 1
    store = LocalModelServerLifecycleStore(tmp_path / "lifecycle.jsonl", integrity_key=KEY)
    assert store.current("lifecycle-1").state is LocalModelServerLifecycleState.ATTESTED
