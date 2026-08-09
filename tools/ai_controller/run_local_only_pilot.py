"""Explicit real-run entrypoint for the reviewed small local-only pilot.

This module connects to an already-running loopback llama-server. It never
starts a process, downloads a model, selects a cloud arm, or performs fallback.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from federation.llamacpp_adapter import (
    LlamaCppAdapterConfig, LlamaCppAdapterMode, LlamaCppLocalAdapter,
)
from federation.assignment_registry import DurableAssignmentRegistry
from federation.dispatch_offer import DispatchStatus
from federation.heartbeat_registry import HeartbeatRegistry
from federation.registry import NodeRegistry
from federation.task_dispatcher import TaskDispatchCoordinator
from federation.worker_execution import (
    WorkerExecutionCoordinator, WorkerExecutionResultEnvelope, WorkerExecutionStatus,
)
from tools.ai_controller.local_capability_baseline import LocalityType
from tools.ai_controller.local_model_runtime import (
    BenchmarkLinkage, GenerationConfig, GovernedLocalModelRuntime, LocalInferenceRequest,
    LocalModelCapability, LocalModelDescriptor, LocalModelLoadStatus,
    LocalModelRuntimeConfig,
)
from tools.ai_controller.local_only_pilot import (
    PilotContractError, PilotReport, PilotTaskRecord, build_default_pilot_definition,
)


MODEL_ALIAS = "raghub-qwen2.5-0.5b-q4km"
MODEL_SHA256 = "74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db"
LLAMACPP_COMMIT = "876a4321163249c43ca4e986818fab5ab081f282"
NODE_ID = "fedora-local-node"
_AUTHORITY_FIELDS = frozenset({"execution_attempt_id", "assignment_id",
    "dispatch_offer_id", "execution_fingerprint"})


def _load_authority(path: Path, task_ids: set[str]):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PilotContractError("authority manifest contains duplicate JSON keys")
            result[key] = value
        return result
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (OSError, json.JSONDecodeError) as exc:
        raise PilotContractError("authority manifest is unavailable or malformed") from exc
    if not isinstance(raw, dict) or set(raw) != task_ids:
        raise PilotContractError("authority manifest must bind every exact pilot task")
    for task_id, evidence in raw.items():
        if not isinstance(evidence, dict) or set(evidence) != _AUTHORITY_FIELDS:
            raise PilotContractError(f"authority evidence schema mismatch for {task_id}")
    return raw


class PilotExecutionAuthority:
    """Read and transition canonical authenticated worker-execution authority."""

    def __init__(self, coordinator: WorkerExecutionCoordinator):
        if not isinstance(coordinator, WorkerExecutionCoordinator):
            raise TypeError("coordinator must be WorkerExecutionCoordinator")
        self._coordinator = coordinator
        self._validated = {}
        self._running = {}

    def preflight(self, definition, manifest, *, worker_node_id):
        """Validate the complete manifest without changing authoritative state."""
        validated = {}
        seen_attempts = set()
        for task in definition.tasks:
            claim = manifest[task.task_id]
            try:
                attempt = self._coordinator.inspect(claim["execution_attempt_id"])
                request = attempt.request
                assignment = self._coordinator.dispatch.assignment_store.resolve(
                    claim["assignment_id"])
                offer = self._coordinator.dispatch.inspect_offer(claim["dispatch_offer_id"])
            except Exception as exc:
                raise PilotContractError(
                    f"authoritative execution evidence unavailable for {task.task_id}") from exc
            if attempt.status is not WorkerExecutionStatus.CLAIMED:
                raise PilotContractError(
                    f"execution attempt for {task.task_id} is not uniquely eligible")
            expected = (definition.pilot_id, task.task_id, worker_node_id, claim["assignment_id"],
                claim["dispatch_offer_id"], claim["execution_attempt_id"],
                claim["execution_fingerprint"])
            actual = (request.mission_id, request.task_id, request.worker_node_id, request.assignment_id,
                request.dispatch_offer_id, request.execution_attempt_id,
                request.execution_fingerprint)
            if actual != expected:
                raise PilotContractError(
                    f"manifest disagrees with worker execution authority for {task.task_id}")
            if (assignment.assignment_id != request.assignment_id
                    or assignment.mission_id != request.mission_id
                    or assignment.task_id != request.task_id
                    or assignment.worker_node_id != request.worker_node_id
                    or offer.offer_id != request.dispatch_offer_id
                    or offer.assignment_id != request.assignment_id
                    or offer.assignment_fingerprint != assignment.assignment_fingerprint
                    or offer.mission_id != request.mission_id
                    or offer.task_id != request.task_id
                    or offer.worker_node_id != request.worker_node_id
                    or offer.status is not DispatchStatus.ACCEPTED):
                raise PilotContractError(
                    f"assignment, dispatch, and attempt do not compose for {task.task_id}")
            if request.execution_attempt_id in seen_attempts:
                raise PilotContractError("execution attempt authority cannot be reused across tasks")
            seen_attempts.add(request.execution_attempt_id)
            validated[task.task_id] = attempt
        self._validated = validated
        return tuple(validated[task.task_id] for task in definition.tasks)

    def begin(self, task_id):
        attempt = self._validated.get(task_id)
        if attempt is None:
            raise PilotContractError("task authority was not preflighted")
        try:
            running = self._coordinator.start(
                execution_attempt_id=attempt.request.execution_attempt_id,
                actor_node_id=attempt.request.worker_node_id)
        except Exception as exc:
            raise PilotContractError("execution authority could not transition to running") from exc
        self._running[task_id] = running
        return running

    def verifies(self, inference_request, model):
        running = self._running.get(inference_request.task_id)
        if running is None or running.status is not WorkerExecutionStatus.RUNNING:
            return False
        try:
            current = self._coordinator.inspect(running.request.execution_attempt_id)
        except Exception:
            return False
        if current != running or current.status is not WorkerExecutionStatus.RUNNING:
            return False
        authority = running.request
        return (inference_request.worker_node_id == model.node_id == authority.worker_node_id
            and inference_request.task_id == authority.task_id
            and inference_request.execution_attempt_id == authority.execution_attempt_id
            and inference_request.assignment_id == authority.assignment_id
            and inference_request.dispatch_offer_id == authority.dispatch_offer_id
            and inference_request.execution_fingerprint == authority.execution_fingerprint
            and inference_request.authorization_level is authority.authorization_level
            and inference_request.approval_required is authority.approval_required)

    def complete(self, task_id, result):
        running = self._running[task_id]
        response = result.response
        if response.status.value == "succeeded":
            return self._coordinator.succeed(
                execution_attempt_id=running.request.execution_attempt_id,
                actor_node_id=running.request.worker_node_id,
                result=WorkerExecutionResultEnvelope("local_inference_succeeded", (result.result_id,)))
        return self._coordinator.fail(
            execution_attempt_id=running.request.execution_attempt_id,
            actor_node_id=running.request.worker_node_id,
            reason=response.error_code or "local inference failed")


def _authority_from_args(args):
    key_text = os.environ.get(args.integrity_key_env)
    if key_text is None:
        raise PilotContractError(f"integrity key environment variable {args.integrity_key_env} is missing")
    key = key_text.encode("utf-8")
    assignments = DurableAssignmentRegistry(args.assignment_store,
        coordinator_node_id=args.coordinator_node_id, integrity_key=key)
    heartbeats = HeartbeatRegistry(args.heartbeat_store,
        registry_id=args.heartbeat_registry_id, node_registry=NodeRegistry(), integrity_key=key)
    dispatch = TaskDispatchCoordinator(args.coordinator_node_id,
        assignment_store=assignments, dispatch_store_path=args.dispatch_store,
        integrity_key=key, heartbeat_registry=heartbeats)
    coordinator = WorkerExecutionCoordinator(args.coordinator_node_id,
        dispatch_coordinator=dispatch, store_path=args.execution_store, integrity_key=key)
    return PilotExecutionAuthority(coordinator)


def _atomic_write(path: Path, payload: dict):
    path = path.resolve()
    if not path.parent.is_dir():
        raise PilotContractError("output parent directory must already exist")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode() + b"\n"
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    finally:
        if temporary.exists(): temporary.unlink()


def run(args) -> PilotReport:
    if args.model_alias != MODEL_ALIAS or args.model_sha256 != MODEL_SHA256:
        raise PilotContractError("pilot requires the reviewed exact model alias and SHA-256")
    definition = build_default_pilot_definition()
    # Materialize the accepted benchmark/experiment projections before execution;
    # this fails closed if the pilot no longer composes with those contracts.
    definition.benchmark_suite()
    definition.experiment_definition()
    authority = _load_authority(Path(args.authority_manifest),
        {task.task_id for task in definition.tasks})
    authority_reader = _authority_from_args(args)
    authority_reader.preflight(definition, authority, worker_node_id=NODE_ID)
    descriptor = LocalModelDescriptor(MODEL_ALIAS, "llama.cpp", NODE_ID,
        args.profile_fingerprint, LocalityType.LOCAL, "qwen2.5", "Q4_K_M", 1024,
        frozenset({LocalModelCapability.TEXT_GENERATION}), LocalModelLoadStatus.LOADED,
        {"model_sha256": MODEL_SHA256, "llamacpp_commit": LLAMACPP_COMMIT,
            "execution": "cpu", "threads": 4, "adapter_mode": "chat"})
    adapter = LlamaCppLocalAdapter(config=LlamaCppAdapterConfig(
        "llamacpp-local-pilot", NODE_ID, args.endpoint, "llama.cpp", MODEL_ALIAS,
        MODEL_SHA256, args.connection_timeout, args.request_timeout,
        args.maximum_response_bytes, LlamaCppAdapterMode.CHAT))

    runtime = GovernedLocalModelRuntime(adapter,
        LocalModelRuntimeConfig(True, 96, frozenset(), True),
        authority_verifier=authority_reader.verifies,
        approval_verifier=lambda request: authority_reader.verifies(request, descriptor))
    records = []
    for task in definition.tasks:
        running = authority_reader.begin(task.task_id)
        evidence = running.request
        request = LocalInferenceRequest(
            request_id=f"{args.run_id}:{task.task_id}", task_id=task.task_id,
            worker_node_id=NODE_ID, descriptor_fingerprint=descriptor.descriptor_fingerprint(),
            input_text=task.prompt,
            generation=GenerationConfig(task.maximum_output_tokens, 0.0, 42, (), False),
            authorization_level=evidence.authorization_level,
            approval_required=evidence.approval_required,
            permitted_tool_capabilities=(), local_only=True,
            execution_attempt_id=evidence.execution_attempt_id,
            assignment_id=evidence.assignment_id,
            dispatch_offer_id=evidence.dispatch_offer_id,
            execution_fingerprint=evidence.execution_fingerprint,
            benchmark_linkage=BenchmarkLinkage(definition.pilot_id, "single-local",
                args.run_id, f"attempt-{task.task_id}"))
        result = runtime.execute(descriptor, request)
        authority_reader.complete(task.task_id, result)
        records.append(PilotTaskRecord.from_evidence(task, request, result))
    report = PilotReport.create(args.run_id, definition, descriptor, tuple(records))
    _atomic_write(Path(args.output), report.to_dict())
    return report


def parser():
    value = argparse.ArgumentParser(description="Run the reviewed 12-task local-only CHAT pilot")
    value.add_argument("--endpoint", required=True, help="loopback llama-server base URL")
    value.add_argument("--output", required=True, help="explicit JSON report path")
    value.add_argument("--authority-manifest", required=True)
    value.add_argument("--assignment-store", required=True)
    value.add_argument("--dispatch-store", required=True)
    value.add_argument("--execution-store", required=True)
    value.add_argument("--heartbeat-store", required=True)
    value.add_argument("--heartbeat-registry-id", required=True)
    value.add_argument("--coordinator-node-id", required=True)
    value.add_argument("--integrity-key-env", default="RAGHUB_FEDERATION_INTEGRITY_KEY")
    value.add_argument("--run-id", required=True)
    value.add_argument("--profile-fingerprint", required=True)
    value.add_argument("--model-alias", required=True)
    value.add_argument("--model-sha256", required=True)
    value.add_argument("--connection-timeout", type=float, default=2.0)
    value.add_argument("--request-timeout", type=float, default=120.0)
    value.add_argument("--maximum-response-bytes", type=int, default=1_048_576)
    return value


def main(argv=None):
    run(parser().parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
