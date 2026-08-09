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
from federation.task_request import AuthorizationLevel
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
    descriptor = LocalModelDescriptor(MODEL_ALIAS, "llama.cpp", NODE_ID,
        args.profile_fingerprint, LocalityType.LOCAL, "qwen2.5", "Q4_K_M", 1024,
        frozenset({LocalModelCapability.TEXT_GENERATION}), LocalModelLoadStatus.LOADED,
        {"model_sha256": MODEL_SHA256, "llamacpp_commit": LLAMACPP_COMMIT,
            "execution": "cpu", "threads": 4, "adapter_mode": "chat"})
    adapter = LlamaCppLocalAdapter(config=LlamaCppAdapterConfig(
        "llamacpp-local-pilot", NODE_ID, args.endpoint, "llama.cpp", MODEL_ALIAS,
        MODEL_SHA256, args.connection_timeout, args.request_timeout,
        args.maximum_response_bytes, LlamaCppAdapterMode.CHAT))

    expected_requests = {}
    runtime = GovernedLocalModelRuntime(adapter,
        LocalModelRuntimeConfig(True, 96, frozenset(), True),
        authority_verifier=lambda request, model: expected_requests.get(request.task_id)
            == request.request_fingerprint())
    records = []
    for task in definition.tasks:
        evidence = authority[task.task_id]
        request = LocalInferenceRequest(
            request_id=f"{args.run_id}:{task.task_id}", task_id=task.task_id,
            worker_node_id=NODE_ID, descriptor_fingerprint=descriptor.descriptor_fingerprint(),
            input_text=task.prompt,
            generation=GenerationConfig(task.maximum_output_tokens, 0.0, 42, (), False),
            authorization_level=AuthorizationLevel.INTERNAL, approval_required=False,
            permitted_tool_capabilities=(), local_only=True,
            execution_attempt_id=evidence["execution_attempt_id"],
            assignment_id=evidence["assignment_id"],
            dispatch_offer_id=evidence["dispatch_offer_id"],
            execution_fingerprint=evidence["execution_fingerprint"],
            benchmark_linkage=BenchmarkLinkage(definition.pilot_id, "single-local",
                args.run_id, f"attempt-{task.task_id}"))
        expected_requests[task.task_id] = request.request_fingerprint()
        result = runtime.execute(descriptor, request)
        records.append(PilotTaskRecord.from_evidence(task, request, result))
    report = PilotReport.create(args.run_id, definition, descriptor, tuple(records))
    _atomic_write(Path(args.output), report.to_dict())
    return report


def parser():
    value = argparse.ArgumentParser(description="Run the reviewed 12-task local-only CHAT pilot")
    value.add_argument("--endpoint", required=True, help="loopback llama-server base URL")
    value.add_argument("--output", required=True, help="explicit JSON report path")
    value.add_argument("--authority-manifest", required=True)
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
