"""Bind governed local inference to an exact live ATTESTED server lifecycle."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from federation.file_lock import fcntl
from federation.integrity import authentication_tag, authenticates, require_integrity_key
from federation.local_model_server_lifecycle import (
    EndpointObservation, LocalModelServerLifecycleState,
    LocalModelServerLifecycleStore, ProcessObservation,
)
from federation.worker_execution import WorkerExecutionCoordinator, WorkerExecutionStatus
from tools.ai_controller.local_model_runtime import (
    AdapterInferenceResponse, BenchmarkLinkage, LocalInferenceResult,
    LocalInferenceStatus, LocalInferenceUsage, LocalModelDescriptor,
    LocalModelRuntime, _digest, _fingerprint, _identifier,
)


_DOMAIN = b"raghub-governed-local-inference-v0.1"


class GovernedLocalInferenceError(Exception):
    """Base integration failure."""


class GovernedLocalInferenceAuthorityError(GovernedLocalInferenceError):
    """Inference execution authority is absent or does not compose."""


class GovernedLocalInferenceLifecycleError(GovernedLocalInferenceError):
    """Authoritative server lifecycle is missing, ineligible, or conflicting."""


class GovernedLocalInferenceIdentityError(GovernedLocalInferenceError):
    """Live process, endpoint, artifact, model, or binary identity is unproven."""


class GovernedLocalInferenceConflictError(GovernedLocalInferenceError):
    """Integration request identity was reused with different content."""


class GovernedLocalInferenceReconciliationRequired(GovernedLocalInferenceError):
    """Identity changed across inference and the outcome is ambiguous."""


@dataclass(frozen=True, slots=True)
class GovernedLocalInferenceRequest:
    integration_request_id: str
    worker_node_id: str
    inference_execution_attempt_id: str
    inference_execution_fingerprint: str
    lifecycle_request_id: str
    expected_lifecycle_record_fingerprint: str
    expected_lifecycle_attestation_fingerprint: str
    expected_endpoint: str
    expected_backend: str
    expected_profile_fingerprint: str
    runtime_request: Any
    request_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        from tools.ai_controller.local_model_runtime import LocalInferenceRequest
        for name in ("integration_request_id", "worker_node_id",
                     "inference_execution_attempt_id", "lifecycle_request_id",
                     "expected_backend"):
            _identifier(getattr(self, name), name)
        for name in ("inference_execution_fingerprint",
                     "expected_lifecycle_record_fingerprint",
                     "expected_lifecycle_attestation_fingerprint",
                     "expected_profile_fingerprint"):
            _digest(getattr(self, name), name)
        if type(self.expected_endpoint) is not str or self.expected_endpoint != \
                "http://127.0.0.1:18080":
            raise GovernedLocalInferenceIdentityError(
                "expected endpoint must be the exact governed loopback endpoint")
        if type(self.runtime_request) is not LocalInferenceRequest:
            raise TypeError("runtime_request must be LocalInferenceRequest")
        object.__setattr__(self, "request_fingerprint", _fingerprint(self.to_dict(False)))

    def to_dict(self, include_fingerprint: bool = True) -> dict[str, Any]:
        value = {
            "integration_request_id": self.integration_request_id,
            "worker_node_id": self.worker_node_id,
            "inference_execution_attempt_id": self.inference_execution_attempt_id,
            "inference_execution_fingerprint": self.inference_execution_fingerprint,
            "lifecycle_request_id": self.lifecycle_request_id,
            "expected_lifecycle_record_fingerprint": self.expected_lifecycle_record_fingerprint,
            "expected_lifecycle_attestation_fingerprint": self.expected_lifecycle_attestation_fingerprint,
            "expected_endpoint": self.expected_endpoint,
            "expected_backend": self.expected_backend,
            "expected_profile_fingerprint": self.expected_profile_fingerprint,
            "runtime_request_fingerprint": self.runtime_request.request_fingerprint(),
        }
        if include_fingerprint:
            value["request_fingerprint"] = self.request_fingerprint
        return value


@dataclass(frozen=True, slots=True)
class GovernedLocalInferenceEvidence:
    integration_request_fingerprint: str
    inference_execution_attempt_id: str
    inference_execution_fingerprint: str
    lifecycle_request_id: str
    lifecycle_record_fingerprint: str
    lifecycle_attestation_fingerprint: str
    lifecycle_process_identity_fingerprint: str
    artifact_fingerprint: str
    artifact_sha256: str
    binary_fingerprint: str
    binary_sha256: str
    model_alias: str
    backend: str
    profile_fingerprint: str
    endpoint: str
    pre_process_observation: str
    pre_endpoint_observation: EndpointObservation
    runtime_request_fingerprint: str
    runtime_result_fingerprint: str
    post_process_observation: str
    post_endpoint_observation: EndpointObservation
    evidence_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("integration_request_fingerprint", "inference_execution_fingerprint",
                     "lifecycle_record_fingerprint", "lifecycle_attestation_fingerprint",
                     "lifecycle_process_identity_fingerprint", "artifact_fingerprint",
                     "artifact_sha256", "binary_fingerprint", "binary_sha256",
                     "profile_fingerprint", "runtime_request_fingerprint",
                     "runtime_result_fingerprint"):
            _digest(getattr(self, name), name)
        for name in ("inference_execution_attempt_id", "lifecycle_request_id",
                     "model_alias", "backend"):
            _identifier(getattr(self, name), name)
        if type(self.pre_endpoint_observation) is not EndpointObservation or \
                type(self.post_endpoint_observation) is not EndpointObservation:
            raise TypeError("endpoint observations must use lifecycle evidence")
        if self.pre_process_observation != ProcessObservation.MATCHING.value or \
                self.post_process_observation != ProcessObservation.MATCHING.value:
            raise GovernedLocalInferenceIdentityError("trusted evidence requires matching process")
        object.__setattr__(self, "evidence_fingerprint", _fingerprint(self.to_dict(False)))

    def to_dict(self, include_fingerprint=True) -> dict[str, Any]:
        value = {name: getattr(self, name) for name in (
            "integration_request_fingerprint", "inference_execution_attempt_id",
            "inference_execution_fingerprint", "lifecycle_request_id",
            "lifecycle_record_fingerprint", "lifecycle_attestation_fingerprint",
            "lifecycle_process_identity_fingerprint", "artifact_fingerprint",
            "artifact_sha256", "binary_fingerprint", "binary_sha256", "model_alias",
            "backend", "profile_fingerprint", "endpoint", "pre_process_observation",
            "runtime_request_fingerprint", "runtime_result_fingerprint",
            "post_process_observation")}
        value["pre_endpoint_observation"] = self.pre_endpoint_observation.to_dict()
        value["post_endpoint_observation"] = self.post_endpoint_observation.to_dict()
        if include_fingerprint:
            value["evidence_fingerprint"] = self.evidence_fingerprint
        return value


@dataclass(frozen=True, slots=True)
class GovernedLocalInferenceResult:
    integration_request_id: str
    request_fingerprint: str
    runtime_result: LocalInferenceResult
    evidence: GovernedLocalInferenceEvidence
    result_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        _identifier(self.integration_request_id, "integration_request_id")
        _digest(self.request_fingerprint, "request_fingerprint")
        if type(self.runtime_result) is not LocalInferenceResult or \
                type(self.evidence) is not GovernedLocalInferenceEvidence:
            raise TypeError("result requires exact runtime and integration evidence")
        if self.request_fingerprint != self.evidence.integration_request_fingerprint or \
                self.runtime_result.request_fingerprint != self.evidence.runtime_request_fingerprint:
            raise GovernedLocalInferenceIdentityError("integration result evidence does not compose")
        if _fingerprint(self.runtime_result.to_dict()) != \
                self.evidence.runtime_result_fingerprint:
            raise GovernedLocalInferenceIdentityError(
                "runtime result fingerprint does not bind exact runtime evidence")
        object.__setattr__(self, "result_fingerprint", _fingerprint(self.to_dict(False)))

    def to_dict(self, include_fingerprint=True):
        value = {"integration_request_id": self.integration_request_id,
                 "request_fingerprint": self.request_fingerprint,
                 "runtime_result": self.runtime_result.to_dict(),
                 "evidence": self.evidence.to_dict()}
        if include_fingerprint:
            value["result_fingerprint"] = self.result_fingerprint
        return value


class GovernedLocalInferenceStore:
    """Authenticated durable result store; the runtime remains replay authority."""

    def __init__(self, path: str | os.PathLike[str], *, integrity_key: bytes):
        self.path = Path(path)
        self._key = require_integrity_key(integrity_key)
        self._lock = threading.RLock()

    def current(self, request_id: str) -> GovernedLocalInferenceResult | None:
        _identifier(request_id, "integration_request_id")
        values = self._read()
        return values.get(request_id)

    def append(self, result: GovernedLocalInferenceResult) -> GovernedLocalInferenceResult:
        if type(result) is not GovernedLocalInferenceResult:
            raise TypeError("result must be GovernedLocalInferenceResult")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                current = self._decode(handle.read())
                prior = current.get(result.integration_request_id)
                if prior is not None:
                    if prior.request_fingerprint != result.request_fingerprint:
                        raise GovernedLocalInferenceConflictError(
                            "integration request identity conflicts with durable evidence")
                    return prior
                payload = result.to_dict()
                tag = authentication_tag(self._key, _DOMAIN, _canonical(payload))
                handle.seek(0, os.SEEK_END)
                handle.write(_canonical({"result": payload, "authentication_tag": tag}) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
                return result
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self):
        if not self.path.exists():
            return {}
        with self.path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return self._decode(handle.read())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _decode(self, data: bytes):
        if not data:
            return {}
        if not data.endswith(b"\n"):
            raise GovernedLocalInferenceConflictError("integration evidence is truncated")
        values = {}
        for line in data.splitlines():
            raw = json.loads(line)
            if set(raw) != {"result", "authentication_tag"} or not authenticates(
                    self._key, _DOMAIN, _canonical(raw["result"]), raw["authentication_tag"]):
                raise GovernedLocalInferenceConflictError("integration evidence authentication failed")
            result = _result_from_dict(raw["result"])
            if result.integration_request_id in values:
                raise GovernedLocalInferenceConflictError("duplicate integration evidence identity")
            values[result.integration_request_id] = result
        return values


class GovernedLocalInferenceCoordinator:
    """Pre/post guard around the accepted governed local model runtime."""

    def __init__(self, *, lifecycle_store, process_operations, worker_execution,
                 runtime, store):
        if type(lifecycle_store) is not LocalModelServerLifecycleStore:
            raise TypeError("lifecycle_store must be authoritative lifecycle store")
        if type(worker_execution) is not WorkerExecutionCoordinator:
            raise TypeError("worker_execution must be authoritative execution coordinator")
        if not callable(getattr(process_operations, "observe", None)) or not callable(
                getattr(process_operations, "endpoint_observation", None)):
            raise TypeError("process_operations must expose lifecycle observations")
        if not callable(getattr(runtime, "execute", None)):
            raise TypeError("runtime must implement accepted LocalModelRuntime")
        if type(store) is not GovernedLocalInferenceStore:
            raise TypeError("store must be GovernedLocalInferenceStore")
        self.lifecycle_store = lifecycle_store
        self.process = process_operations
        self.worker_execution = worker_execution
        self.runtime: LocalModelRuntime = runtime
        self.store = store
        self._lock = threading.RLock()

    def execute(self, request, model):
        if type(request) is not GovernedLocalInferenceRequest or \
                type(model) is not LocalModelDescriptor:
            raise TypeError("exact integration request and model descriptor are required")
        with self._lock:
            prior = self.store.current(request.integration_request_id)
            if prior is not None:
                if prior.request_fingerprint != request.request_fingerprint:
                    raise GovernedLocalInferenceConflictError(
                        "integration request identity conflicts with prior execution")
                return prior
            self._validate_authority(request)
            record, pre_endpoint = self._verify_lifecycle(request, model)
            runtime_result = self.runtime.execute(model, request.runtime_request)
            if type(runtime_result) is not LocalInferenceResult:
                raise TypeError("runtime returned a non-authoritative result contract")
            try:
                post_record, post_endpoint = self._verify_lifecycle(request, model)
                if post_record.record_fingerprint != record.record_fingerprint:
                    raise GovernedLocalInferenceIdentityError(
                        "lifecycle fingerprint changed during inference")
            except (GovernedLocalInferenceLifecycleError,
                    GovernedLocalInferenceIdentityError) as exc:
                raise GovernedLocalInferenceReconciliationRequired(
                    "server identity changed during inference; outcome is ambiguous") from exc
            evidence = self._evidence(request, record, pre_endpoint, runtime_result,
                                      post_endpoint)
            return self.store.append(GovernedLocalInferenceResult(
                request.integration_request_id, request.request_fingerprint,
                runtime_result, evidence))

    def _validate_authority(self, request):
        rr = request.runtime_request
        if (rr.execution_attempt_id != request.inference_execution_attempt_id or
                rr.execution_fingerprint != request.inference_execution_fingerprint or
                rr.worker_node_id != request.worker_node_id):
            raise GovernedLocalInferenceAuthorityError(
                "integration and runtime inference authority references conflict")
        try:
            attempt = self.worker_execution.inspect(request.inference_execution_attempt_id)
        except Exception as exc:
            raise GovernedLocalInferenceAuthorityError(
                "authoritative inference execution is missing") from exc
        if (attempt.status is not WorkerExecutionStatus.RUNNING or
                attempt.request.worker_node_id != request.worker_node_id or
                attempt.request.execution_fingerprint != request.inference_execution_fingerprint or
                attempt.request.task_id != rr.task_id or
                attempt.request.assignment_id != rr.assignment_id or
                attempt.request.dispatch_offer_id != rr.dispatch_offer_id):
            raise GovernedLocalInferenceAuthorityError(
                "inference authority state, worker, or identity is not exact")

    def _verify_lifecycle(self, request, model):
        record = self.lifecycle_store.current(request.lifecycle_request_id)
        if record is None:
            raise GovernedLocalInferenceLifecycleError("authoritative lifecycle is missing")
        if record.state is not LocalModelServerLifecycleState.ATTESTED:
            raise GovernedLocalInferenceLifecycleError(
                f"lifecycle state {record.state.value} is not ATTESTED")
        if record.record_fingerprint != request.expected_lifecycle_record_fingerprint:
            raise GovernedLocalInferenceLifecycleError("lifecycle fingerprint mismatch")
        if record.attestation is None or record.attestation.fingerprint != \
                request.expected_lifecycle_attestation_fingerprint:
            raise GovernedLocalInferenceLifecycleError("attestation fingerprint mismatch")
        process = record.process_identity
        if process is None or self.process.observe(process) is not ProcessObservation.MATCHING:
            raise GovernedLocalInferenceIdentityError(
                "exact process birth, executable, and argv identity is not matching")
        endpoint = self.process.endpoint_observation()
        if (not endpoint.occupied or endpoint.owner_pid is None or
                endpoint.owner_pid != process.pid):
            raise GovernedLocalInferenceIdentityError("endpoint ownership is not exact")
        if (request.expected_endpoint != process.endpoint or
                request.expected_endpoint != record.attestation.endpoint or
                record.request.profile.endpoint != request.expected_endpoint):
            raise GovernedLocalInferenceIdentityError("endpoint identity mismatch")
        if (record.request.worker_node_id != request.worker_node_id or
                record.attestation.lifecycle_request_fingerprint != record.request.request_fingerprint or
                record.attestation.execution_attempt_id != record.request.execution_attempt_id or
                record.attestation.execution_fingerprint != record.request.execution_fingerprint or
                record.attestation.process_identity_fingerprint != process.fingerprint or
                record.attestation.invocation_fingerprint != process.argv_fingerprint or
                record.attestation.artifact_evidence_fingerprint != record.artifact_evidence.fingerprint or
                record.attestation.binary_fingerprint != record.binary_identity.fingerprint or
                record.request.artifact_fingerprint != record.artifact_evidence.artifact_fingerprint or
                record.request.artifact_sha256 != record.artifact_evidence.artifact_sha256 or
                record.request.profile.binary_sha256 != record.binary_identity.sha256):
            raise GovernedLocalInferenceIdentityError(
                "lifecycle artifact, binary, process, or invocation identity mismatch")
        if (model.model_id != record.attestation.model_alias or
                model.profile_fingerprint != request.expected_profile_fingerprint or
                request.expected_profile_fingerprint != record.request.profile.fingerprint or
                model.runtime_metadata.get("backend") != request.expected_backend or
                model.runtime_metadata.get("profile_id") != record.request.profile.profile_id):
            raise GovernedLocalInferenceIdentityError(
                "model, backend, or profile identity mismatch")
        return record, endpoint

    @staticmethod
    def _evidence(request, record, pre_endpoint, runtime_result, post_endpoint):
        return GovernedLocalInferenceEvidence(
            request.request_fingerprint, request.inference_execution_attempt_id,
            request.inference_execution_fingerprint, request.lifecycle_request_id,
            record.record_fingerprint, record.attestation.fingerprint,
            record.process_identity.fingerprint, record.artifact_evidence.fingerprint,
            record.artifact_evidence.artifact_sha256, record.binary_identity.fingerprint,
            record.binary_identity.sha256, record.attestation.model_alias,
            request.expected_backend, request.expected_profile_fingerprint,
            request.expected_endpoint, ProcessObservation.MATCHING.value, pre_endpoint,
            request.runtime_request.request_fingerprint(),
            _fingerprint(runtime_result.to_dict()), ProcessObservation.MATCHING.value,
            post_endpoint)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def _result_from_dict(value):
    runtime = value["runtime_result"]
    response = runtime["response"]
    response["usage"] = LocalInferenceUsage(**response["usage"])
    response = AdapterInferenceResponse(**response)
    benchmark = runtime["benchmark_linkage"]
    runtime_result = LocalInferenceResult(runtime["result_id"], runtime["request_fingerprint"],
        runtime["descriptor_fingerprint"], response,
        None if benchmark is None else BenchmarkLinkage(**benchmark))
    evidence = dict(value["evidence"])
    claimed_evidence = evidence.pop("evidence_fingerprint")
    evidence["pre_endpoint_observation"] = EndpointObservation.from_dict(
        evidence["pre_endpoint_observation"])
    evidence["post_endpoint_observation"] = EndpointObservation.from_dict(
        evidence["post_endpoint_observation"])
    evidence = GovernedLocalInferenceEvidence(**evidence)
    if evidence.evidence_fingerprint != claimed_evidence:
        raise GovernedLocalInferenceConflictError("integration evidence fingerprint mismatch")
    result = GovernedLocalInferenceResult(value["integration_request_id"],
        value["request_fingerprint"], runtime_result, evidence)
    if result.result_fingerprint != value["result_fingerprint"]:
        raise GovernedLocalInferenceConflictError("integration result fingerprint mismatch")
    return result
