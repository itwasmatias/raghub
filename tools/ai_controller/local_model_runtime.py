"""Governed, provider-neutral local language-model inference boundary v0.1.

Only injected adapters perform inference. This module has no subprocess, network,
provider, routing, dispatch, approval, or cloud-fallback implementation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

from federation.task_request import AuthorizationLevel
from tools.ai_controller.local_capability_baseline import LocalityType, TaskMeasurement, TaskOutcome
from tools.ai_controller.local_equivalence_benchmark import BenchmarkExecutor, BenchmarkTaskSpec
from tools.ai_controller.local_equivalence_experiments import (
    ExperimentArm, ExperimentAttempt, ExperimentEvidence, ExperimentTask,
)


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class LocalModelExecutionError(Exception):
    """Base local-model runtime error."""


class LocalInferenceContractError(LocalModelExecutionError):
    """Inference authority or evidence does not compose exactly."""


class LocalInferenceConflictError(LocalModelExecutionError):
    """A request identity was reused with different immutable content."""


class LocalityViolationError(LocalModelExecutionError):
    """Adapter evidence violates the local-only execution requirement."""


class LocalModelAdapterError(LocalModelExecutionError):
    """The injected adapter failed or returned invalid evidence."""


class LocalInferenceReconciliationRequired(LocalModelExecutionError):
    """Prior adapter execution is ambiguous and must not be retried blindly."""


class LocalModelCapability(str, Enum):
    TEXT_GENERATION = "text_generation"
    STRUCTURED_OUTPUT = "structured_output"
    TOOL_REFERENCES = "tool_references"


class LocalModelLoadStatus(str, Enum):
    LOADED = "loaded"
    UNLOADED = "unloaded"
    UNKNOWN = "unknown"


class LocalInferenceStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise LocalInferenceContractError(f"{name} must be a canonical identifier")
    return value


def _text(value: Any, name: str, *, optional=False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise LocalInferenceContractError(f"{name} must be canonical non-empty text")
    return value


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise LocalInferenceContractError(f"{name} must be lowercase SHA-256 hex")
    return value


def _timestamp(value: Any, name: str) -> str:
    from datetime import datetime
    if not isinstance(value, str):
        raise LocalInferenceContractError(f"{name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LocalInferenceContractError(f"{name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LocalInferenceContractError(f"{name} must be timezone-aware")
    return value


def _finite_nonnegative(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise LocalInferenceContractError(f"{name} must be numeric")
    if not math.isfinite(value):
        raise LocalInferenceContractError(f"{name} must be finite")
    if value < 0:
        raise LocalInferenceContractError(f"{name} cannot be negative")
    return float(value)


def _public(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _public(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_public(child) for child in value]
    if isinstance(value, Enum):
        return value.value
    return value


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(_public(value), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise LocalInferenceContractError("record must contain finite JSON values") from exc


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _freeze_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LocalInferenceContractError(f"{name} must be a mapping")
    try:
        snapshot = json.loads(_canonical(value))
    except LocalInferenceContractError as exc:
        raise LocalInferenceContractError(f"{name} must contain finite JSON values") from exc

    def freeze(item):
        if isinstance(item, dict):
            return MappingProxyType({key: freeze(child) for key, child in item.items()})
        if isinstance(item, list):
            return tuple(freeze(child) for child in item)
        return item

    return freeze(snapshot)


def _validate_resources(value: Any, name="resource_measurements") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            _identifier(key, f"{name} name")
            _validate_resources(child, f"{name}.{key}")
        return
    if isinstance(value, tuple):
        for index, child in enumerate(value):
            _validate_resources(child, f"{name}[{index}]")
        return
    _finite_nonnegative(value, name)


@dataclass(frozen=True, slots=True)
class LocalModelDescriptor:
    model_id: str
    runtime_id: str
    node_id: str
    profile_fingerprint: str
    locality: LocalityType
    family: str | None
    quantization: str | None
    context_limit: int | None
    capabilities: frozenset[LocalModelCapability]
    load_status: LocalModelLoadStatus
    runtime_metadata: Mapping[str, Any]

    def __post_init__(self):
        for name in ("model_id", "runtime_id", "node_id"):
            _identifier(getattr(self, name), name)
        _digest(self.profile_fingerprint, "profile_fingerprint")
        for name in ("family", "quantization"):
            value = getattr(self, name)
            if value is not None:
                _text(value, name)
        if self.context_limit is not None and (
                not isinstance(self.context_limit, int) or isinstance(self.context_limit, bool)
                or self.context_limit < 1):
            raise LocalInferenceContractError("context_limit must be positive integer or None")
        try:
            object.__setattr__(self, "locality", LocalityType(self.locality))
            object.__setattr__(self, "load_status", LocalModelLoadStatus(self.load_status))
            capabilities = frozenset(LocalModelCapability(item) for item in self.capabilities)
        except (TypeError, ValueError) as exc:
            raise LocalInferenceContractError("invalid locality, load status, or capability") from exc
        object.__setattr__(self, "capabilities", capabilities)
        metadata = _freeze_mapping(self.runtime_metadata, "runtime_metadata")
        object.__setattr__(self, "runtime_metadata", metadata)

    def descriptor_fingerprint(self) -> str:
        return _fingerprint(self.to_dict(include_fingerprint=False))

    def to_dict(self, *, include_fingerprint=True):
        payload = {"model_id": self.model_id, "runtime_id": self.runtime_id,
            "node_id": self.node_id, "profile_fingerprint": self.profile_fingerprint,
            "locality": self.locality.value, "family": self.family,
            "quantization": self.quantization, "context_limit": self.context_limit,
            "capabilities": sorted(item.value for item in self.capabilities),
            "load_status": self.load_status.value,
            "runtime_metadata": _public(self.runtime_metadata)}
        if include_fingerprint:
            payload["descriptor_fingerprint"] = self.descriptor_fingerprint()
        return payload


@dataclass(frozen=True, slots=True)
class GenerationConfig:
    maximum_output_tokens: int | None
    temperature: float | None
    seed: int | None
    stop_conditions: tuple[str, ...]
    structured_output_required: bool

    def __post_init__(self):
        if self.maximum_output_tokens is not None and (
                not isinstance(self.maximum_output_tokens, int)
                or isinstance(self.maximum_output_tokens, bool)
                or self.maximum_output_tokens < 1):
            raise LocalInferenceContractError(
                "maximum_output_tokens must be positive integer or None")
        if self.temperature is not None:
            temperature = _finite_nonnegative(self.temperature, "temperature")
            if temperature > 2:
                raise LocalInferenceContractError("temperature must be within [0, 2]")
            object.__setattr__(self, "temperature", temperature)
        if self.seed is not None and (
                not isinstance(self.seed, int) or isinstance(self.seed, bool)
                or self.seed < 0 or self.seed > 2**63 - 1):
            raise LocalInferenceContractError("seed must be a non-negative 63-bit integer or None")
        if not isinstance(self.stop_conditions, tuple):
            raise LocalInferenceContractError("stop_conditions must be a tuple")
        stops = tuple(_text(item, "stop condition") for item in self.stop_conditions)
        if len(set(stops)) != len(stops):
            raise LocalInferenceContractError("stop_conditions must be unique")
        object.__setattr__(self, "stop_conditions", stops)
        if not isinstance(self.structured_output_required, bool):
            raise LocalInferenceContractError("structured_output_required must be boolean")

    def config_fingerprint(self) -> str:
        return _fingerprint(self.to_dict())

    def to_dict(self):
        return {"maximum_output_tokens": self.maximum_output_tokens,
            "temperature": self.temperature, "seed": self.seed,
            "stop_conditions": list(self.stop_conditions),
            "structured_output_required": self.structured_output_required}


@dataclass(frozen=True, slots=True)
class BenchmarkLinkage:
    experiment_id: str
    arm_id: str
    run_id: str
    attempt_id: str

    def __post_init__(self):
        for name in ("experiment_id", "arm_id", "run_id", "attempt_id"):
            _identifier(getattr(self, name), name)

    def to_dict(self):
        return {"experiment_id": self.experiment_id, "arm_id": self.arm_id,
            "run_id": self.run_id, "attempt_id": self.attempt_id}


@dataclass(frozen=True, slots=True)
class LocalInferenceRequest:
    request_id: str
    task_id: str
    worker_node_id: str
    descriptor_fingerprint: str
    input_text: str
    generation: GenerationConfig
    authorization_level: AuthorizationLevel
    approval_required: bool
    permitted_tool_capabilities: tuple[str, ...]
    local_only: bool
    execution_attempt_id: str
    assignment_id: str
    dispatch_offer_id: str
    execution_fingerprint: str
    benchmark_linkage: BenchmarkLinkage | None = None
    input_fingerprint: str = field(init=False)

    def __post_init__(self):
        for name in ("request_id", "task_id", "worker_node_id", "execution_attempt_id",
                     "assignment_id", "dispatch_offer_id"):
            _identifier(getattr(self, name), name)
        _digest(self.descriptor_fingerprint, "descriptor_fingerprint")
        _digest(self.execution_fingerprint, "execution_fingerprint")
        _text(self.input_text, "input_text")
        if type(self.generation) is not GenerationConfig:
            raise LocalInferenceContractError("generation must be GenerationConfig")
        try:
            object.__setattr__(self, "authorization_level",
                AuthorizationLevel(self.authorization_level))
        except (TypeError, ValueError) as exc:
            raise LocalInferenceContractError("authorization_level is invalid") from exc
        if not isinstance(self.approval_required, bool) or not isinstance(self.local_only, bool):
            raise LocalInferenceContractError("approval_required and local_only must be boolean")
        tools = tuple(sorted(_identifier(item, "permitted tool capability")
            for item in self.permitted_tool_capabilities))
        if len(set(tools)) != len(tools):
            raise LocalInferenceContractError("permitted tool capabilities must be unique")
        object.__setattr__(self, "permitted_tool_capabilities", tools)
        if self.benchmark_linkage is not None and type(self.benchmark_linkage) is not BenchmarkLinkage:
            raise LocalInferenceContractError("benchmark_linkage must be BenchmarkLinkage or None")
        object.__setattr__(self, "input_fingerprint",
            hashlib.sha256(self.input_text.encode()).hexdigest())

    def request_fingerprint(self) -> str:
        if hashlib.sha256(self.input_text.encode()).hexdigest() != self.input_fingerprint:
            raise LocalInferenceContractError("authorized input mutated")
        return _fingerprint(self.to_dict(include_fingerprint=False))

    def to_dict(self, *, include_fingerprint=True, include_input=False):
        payload = {"request_id": self.request_id, "task_id": self.task_id,
            "worker_node_id": self.worker_node_id,
            "descriptor_fingerprint": self.descriptor_fingerprint,
            "input_fingerprint": self.input_fingerprint,
            "generation": self.generation.to_dict(),
            "authorization_level": self.authorization_level.value,
            "approval_required": self.approval_required,
            "permitted_tool_capabilities": list(self.permitted_tool_capabilities),
            "local_only": self.local_only,
            "execution_attempt_id": self.execution_attempt_id,
            "assignment_id": self.assignment_id,
            "dispatch_offer_id": self.dispatch_offer_id,
            "execution_fingerprint": self.execution_fingerprint,
            "benchmark_linkage": None if self.benchmark_linkage is None
                else self.benchmark_linkage.to_dict()}
        if include_input:
            payload["input_text"] = self.input_text
        if include_fingerprint:
            payload["request_fingerprint"] = self.request_fingerprint()
        return payload


@dataclass(frozen=True, slots=True)
class LocalInferenceUsage:
    prompt_tokens: int | None
    generated_tokens: int | None

    def __post_init__(self):
        for name in ("prompt_tokens", "generated_tokens"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                                      or value < 0):
                raise LocalInferenceContractError(f"{name} must be non-negative integer or None")

    def to_dict(self):
        return {"prompt_tokens": self.prompt_tokens,
            "generated_tokens": self.generated_tokens}


@dataclass(frozen=True, slots=True)
class AdapterInferenceResponse:
    status: LocalInferenceStatus
    model_id: str
    runtime_id: str
    node_id: str
    locality: LocalityType
    remote_execution: bool
    output_text: str | None
    structured_output: Mapping[str, Any] | None
    started_at: str
    completed_at: str
    elapsed_seconds: float
    usage: LocalInferenceUsage
    resource_measurements: Mapping[str, Any]
    termination_reason: str
    error_code: str | None
    error_message: str | None
    cloud_escalation_count: int
    cloud_cost_usd: float | None

    def __post_init__(self):
        try:
            object.__setattr__(self, "status", LocalInferenceStatus(self.status))
            object.__setattr__(self, "locality", LocalityType(self.locality))
        except (TypeError, ValueError) as exc:
            raise LocalInferenceContractError("invalid inference status or locality") from exc
        for name in ("model_id", "runtime_id", "node_id", "termination_reason"):
            _identifier(getattr(self, name), name)
        if not isinstance(self.remote_execution, bool):
            raise LocalInferenceContractError("remote_execution must be boolean")
        _timestamp(self.started_at, "started_at")
        _timestamp(self.completed_at, "completed_at")
        from datetime import datetime
        started = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
        completed = datetime.fromisoformat(self.completed_at.replace("Z", "+00:00"))
        if completed < started:
            raise LocalInferenceContractError("completed timestamp precedes started timestamp")
        object.__setattr__(self, "elapsed_seconds",
            _finite_nonnegative(self.elapsed_seconds, "elapsed_seconds"))
        if type(self.usage) is not LocalInferenceUsage:
            raise LocalInferenceContractError("usage must be LocalInferenceUsage")
        resources = _freeze_mapping(self.resource_measurements, "resource_measurements")
        _validate_resources(resources)
        object.__setattr__(self, "resource_measurements", resources)
        if not isinstance(self.cloud_escalation_count, int) or isinstance(
                self.cloud_escalation_count, bool) or self.cloud_escalation_count < 0:
            raise LocalInferenceContractError("cloud_escalation_count must be non-negative integer")
        if self.cloud_cost_usd is not None:
            object.__setattr__(self, "cloud_cost_usd",
                _finite_nonnegative(self.cloud_cost_usd, "cloud_cost_usd"))
        if self.structured_output is not None:
            object.__setattr__(self, "structured_output",
                _freeze_mapping(self.structured_output, "structured_output"))
        if self.status is LocalInferenceStatus.SUCCEEDED:
            if self.error_code is not None or self.error_message is not None:
                raise LocalInferenceContractError("successful inference cannot contain error state")
            if self.output_text is None and self.structured_output is None:
                raise LocalInferenceContractError("successful inference requires output")
        else:
            if self.output_text is not None or self.structured_output is not None:
                raise LocalInferenceContractError("failed inference cannot contain output")
            _identifier(self.error_code, "error_code")
            _text(self.error_message, "error_message")

    def to_dict(self):
        return {"status": self.status.value, "model_id": self.model_id,
            "runtime_id": self.runtime_id, "node_id": self.node_id,
            "locality": self.locality.value, "remote_execution": self.remote_execution,
            "output_text": self.output_text,
            "structured_output": None if self.structured_output is None else _public(self.structured_output),
            "started_at": self.started_at, "completed_at": self.completed_at,
            "elapsed_seconds": self.elapsed_seconds, "usage": self.usage.to_dict(),
            "resource_measurements": _public(self.resource_measurements),
            "termination_reason": self.termination_reason, "error_code": self.error_code,
            "error_message": self.error_message,
            "cloud_escalation_count": self.cloud_escalation_count,
            "cloud_cost_usd": self.cloud_cost_usd}


@dataclass(frozen=True, slots=True)
class LocalInferenceResult:
    result_id: str
    request_fingerprint: str
    descriptor_fingerprint: str
    response: AdapterInferenceResponse
    benchmark_linkage: BenchmarkLinkage | None

    def __post_init__(self):
        _identifier(self.result_id, "result_id")
        _digest(self.request_fingerprint, "request_fingerprint")
        _digest(self.descriptor_fingerprint, "descriptor_fingerprint")
        if type(self.response) is not AdapterInferenceResponse:
            raise LocalInferenceContractError("response must be AdapterInferenceResponse")
        if self.benchmark_linkage is not None and type(self.benchmark_linkage) is not BenchmarkLinkage:
            raise LocalInferenceContractError("benchmark_linkage is invalid")
        expected = "local-result-" + _fingerprint({"request": self.request_fingerprint,
            "descriptor": self.descriptor_fingerprint, "response": self.response.to_dict(),
            "benchmark": None if self.benchmark_linkage is None else self.benchmark_linkage.to_dict()})
        if self.result_id != expected:
            raise LocalInferenceContractError("result identity does not bind exact evidence")

    @property
    def status(self): return self.response.status

    def to_dict(self):
        return {"result_id": self.result_id,
            "request_fingerprint": self.request_fingerprint,
            "descriptor_fingerprint": self.descriptor_fingerprint,
            "response": self.response.to_dict(),
            "benchmark_linkage": None if self.benchmark_linkage is None
                else self.benchmark_linkage.to_dict()}


@dataclass(frozen=True, slots=True)
class LocalModelRuntimeConfig:
    local_only: bool
    maximum_output_tokens_limit: int
    allowed_tool_capabilities: frozenset[str]
    require_loaded_model: bool

    def __post_init__(self):
        if self.local_only is not True:
            raise LocalInferenceContractError("governed local runtime must remain local-only")
        if (not isinstance(self.maximum_output_tokens_limit, int)
                or isinstance(self.maximum_output_tokens_limit, bool)
                or self.maximum_output_tokens_limit < 1):
            raise LocalInferenceContractError("maximum_output_tokens_limit must be positive")
        tools = frozenset(_identifier(item, "allowed tool capability")
            for item in self.allowed_tool_capabilities)
        object.__setattr__(self, "allowed_tool_capabilities", tools)
        if not isinstance(self.require_loaded_model, bool):
            raise LocalInferenceContractError("require_loaded_model must be boolean")

    def config_fingerprint(self):
        return _fingerprint({"local_only": self.local_only,
            "maximum_output_tokens_limit": self.maximum_output_tokens_limit,
            "allowed_tool_capabilities": sorted(self.allowed_tool_capabilities),
            "require_loaded_model": self.require_loaded_model})


class LocalModelAdapter(Protocol):
    def infer(self, model: LocalModelDescriptor, inference_request: LocalInferenceRequest,
              runtime_config: LocalModelRuntimeConfig) -> AdapterInferenceResponse: ...


class LocalModelRuntime(Protocol):
    def execute(self, model: LocalModelDescriptor,
                inference_request: LocalInferenceRequest) -> LocalInferenceResult: ...


class GovernedLocalModelRuntime:
    """Execute only through an injected adapter after existing authority verification."""

    def __init__(self, adapter: LocalModelAdapter, config: LocalModelRuntimeConfig, *,
                 authority_verifier: Callable[[LocalInferenceRequest, LocalModelDescriptor], bool],
                 approval_verifier: Callable[[LocalInferenceRequest], bool] | None = None):
        if not callable(getattr(adapter, "infer", None)):
            raise TypeError("adapter must implement infer")
        if type(config) is not LocalModelRuntimeConfig:
            raise TypeError("config must be LocalModelRuntimeConfig")
        if not callable(authority_verifier):
            raise TypeError("authority_verifier must be callable")
        if approval_verifier is not None and not callable(approval_verifier):
            raise TypeError("approval_verifier must be callable or None")
        self._adapter = adapter
        self.config = config
        self._authority_verifier = authority_verifier
        self._approval_verifier = approval_verifier
        self._config_fingerprint = config.config_fingerprint()
        self._results: dict[str, tuple[str, LocalInferenceResult]] = {}
        self._in_doubt: dict[str, str] = {}
        self._lock = RLock()

    def execute(self, model, inference_request):
        if type(model) is not LocalModelDescriptor or type(inference_request) is not LocalInferenceRequest:
            raise TypeError("model and inference_request must use local runtime contracts")
        request_fp = inference_request.request_fingerprint()
        with self._lock:
            if self.config.config_fingerprint() != self._config_fingerprint:
                raise LocalInferenceContractError("governed runtime configuration mutated")
            prior = self._results.get(inference_request.request_id)
            if prior is not None:
                if prior[0] != request_fp:
                    raise LocalInferenceConflictError("request identity conflicts with prior execution")
                return prior[1]
            ambiguous = self._in_doubt.get(inference_request.request_id)
            if ambiguous is not None:
                if ambiguous != request_fp:
                    raise LocalInferenceConflictError("request identity conflicts with ambiguous execution")
                raise LocalInferenceReconciliationRequired(
                    "prior adapter execution is ambiguous; reconciliation required")
            self._validate_request(model, inference_request)
            descriptor_fp = model.descriptor_fingerprint()
            try:
                response = self._adapter.infer(model, inference_request, self.config)
            except Exception as exc:
                self._in_doubt[inference_request.request_id] = request_fp
                raise LocalModelAdapterError("local adapter execution failed ambiguously") from exc
            try:
                if (inference_request.request_fingerprint() != request_fp
                        or model.descriptor_fingerprint() != descriptor_fp):
                    raise LocalInferenceContractError(
                        "authorized request or descriptor mutated during adapter execution")
                self._validate_response(model, inference_request, response)
            except LocalModelExecutionError:
                self._in_doubt[inference_request.request_id] = request_fp
                raise
            result_id = "local-result-" + _fingerprint({"request": request_fp,
                "descriptor": descriptor_fp, "response": response.to_dict(),
                "benchmark": None if inference_request.benchmark_linkage is None
                    else inference_request.benchmark_linkage.to_dict()})
            result = LocalInferenceResult(result_id, request_fp, descriptor_fp, response,
                inference_request.benchmark_linkage)
            self._results[inference_request.request_id] = (request_fp, result)
            return result

    def _validate_request(self, model, request):
        descriptor_fp = model.descriptor_fingerprint()
        if request.descriptor_fingerprint != descriptor_fp:
            raise LocalInferenceContractError("foreign model descriptor fingerprint")
        if request.worker_node_id != model.node_id:
            raise LocalInferenceContractError("worker/node linkage mismatch")
        if not request.local_only or model.locality is not LocalityType.LOCAL:
            raise LocalityViolationError("local-only request requires a local descriptor")
        if self.config.require_loaded_model and model.load_status is not LocalModelLoadStatus.LOADED:
            raise LocalInferenceContractError("model is not authoritatively loaded")
        if LocalModelCapability.TEXT_GENERATION not in model.capabilities:
            raise LocalInferenceContractError("descriptor lacks text-generation capability")
        if (request.generation.structured_output_required
                and LocalModelCapability.STRUCTURED_OUTPUT not in model.capabilities):
            raise LocalInferenceContractError("descriptor lacks structured-output capability")
        if (request.generation.maximum_output_tokens is not None
                and request.generation.maximum_output_tokens > self.config.maximum_output_tokens_limit):
            raise LocalInferenceContractError("maximum output tokens exceeds governed limit")
        if not set(request.permitted_tool_capabilities).issubset(
                self.config.allowed_tool_capabilities):
            raise LocalInferenceContractError("request contains ungoverned tool capability")
        if not self._authority_verifier(request, model):
            raise LocalInferenceContractError("worker execution authority verification failed")
        if request.approval_required and (
                self._approval_verifier is None or not self._approval_verifier(request)):
            raise LocalInferenceContractError("authoritative approval verification failed")

    def _validate_response(self, model, request, response):
        if type(response) is not AdapterInferenceResponse:
            raise LocalModelAdapterError("adapter returned an invalid response type")
        if (response.model_id, response.runtime_id, response.node_id) != (
                model.model_id, model.runtime_id, model.node_id):
            raise LocalInferenceContractError("adapter result identity is foreign")
        if request.local_only and (response.locality is not LocalityType.LOCAL
                or response.remote_execution or response.cloud_escalation_count != 0
                or (response.cloud_cost_usd or 0) != 0):
            raise LocalityViolationError("adapter result violates local-only guarantee")
        if request.generation.structured_output_required and response.status is LocalInferenceStatus.SUCCEEDED:
            if response.structured_output is None:
                raise LocalInferenceContractError("structured output was required but not supplied")
        if (request.generation.maximum_output_tokens is not None
                and response.usage.generated_tokens is not None
                and response.usage.generated_tokens > request.generation.maximum_output_tokens):
            raise LocalInferenceContractError("generated token count exceeds authorized maximum")
        if (model.context_limit is not None and response.usage.prompt_tokens is not None
                and response.usage.generated_tokens is not None
                and response.usage.prompt_tokens + response.usage.generated_tokens
                > model.context_limit):
            raise LocalInferenceContractError("reported token usage exceeds model context limit")


class LocalModelBenchmarkExecutor(BenchmarkExecutor):
    """Convert governed local inference evidence into the existing benchmark contract."""

    def __init__(self, runtime: LocalModelRuntime, descriptor: LocalModelDescriptor,
                 request_factory: Callable[..., LocalInferenceRequest]):
        if not callable(getattr(runtime, "execute", None)):
            raise TypeError("runtime must implement execute")
        if type(descriptor) is not LocalModelDescriptor:
            raise TypeError("descriptor must be LocalModelDescriptor")
        if not callable(request_factory):
            raise TypeError("request_factory must be callable")
        self._runtime = runtime
        self._descriptor = descriptor
        self._request_factory = request_factory

    def execute(self, task_spec, experiment_task, arm, run_id, attempt_id):
        if type(task_spec) is not BenchmarkTaskSpec or type(experiment_task) is not ExperimentTask:
            raise TypeError("benchmark task contracts are required")
        if type(arm) is not ExperimentArm:
            raise TypeError("arm must be ExperimentArm")
        _identifier(run_id, "run_id")
        _identifier(attempt_id, "attempt_id")
        request = self._request_factory(task_spec, experiment_task, arm, run_id, attempt_id)
        if type(request) is not LocalInferenceRequest:
            raise LocalInferenceContractError("request_factory returned invalid request")
        expected = (experiment_task.experiment_id, arm.arm_id, run_id, attempt_id)
        actual = None if request.benchmark_linkage is None else (
            request.benchmark_linkage.experiment_id, request.benchmark_linkage.arm_id,
            request.benchmark_linkage.run_id, request.benchmark_linkage.attempt_id)
        if actual != expected or request.task_id != task_spec.task_id:
            raise LocalInferenceContractError("benchmark request linkage substitution")
        if set(request.permitted_tool_capabilities) != set(task_spec.permitted_tools):
            raise LocalInferenceContractError("benchmark permitted-tool linkage mismatch")
        result = self._runtime.execute(self._descriptor, request)
        response = result.response
        success = response.status is LocalInferenceStatus.SUCCEEDED
        measurement = TaskMeasurement(
            measurement_id="measurement-" + result.result_id.removeprefix("local-result-"),
            task_id=task_spec.task_id, worker_id=self._descriptor.node_id,
            profile_fingerprint=self._descriptor.profile_fingerprint,
            started_at=response.started_at, completed_at=response.completed_at,
            elapsed_seconds=response.elapsed_seconds,
            outcome=TaskOutcome.SUCCESS if success else TaskOutcome.FAILURE,
            success=success, verification_result=response.status.value,
            attempt_number=1, human_intervention_count=0, retry_count=0,
            zero_cloud_cost=response.cloud_escalation_count == 0
                and (response.cloud_cost_usd or 0) == 0,
            cloud_escalation_count=response.cloud_escalation_count,
            estimated_cloud_cost_usd=response.cloud_cost_usd,
            execution_strategy=arm.execution_strategy, locality=response.locality,
            provider_identifier=None, model_identifier=self._descriptor.model_id,
            evidence_metadata={"request_fingerprint": result.request_fingerprint,
                "descriptor_fingerprint": result.descriptor_fingerprint,
                "runtime_id": self._descriptor.runtime_id})
        evidence = ExperimentEvidence(experiment_task.experiment_id, arm.arm_id,
            task_spec.task_id, result.result_id, (result.result_id,))
        return ExperimentAttempt(experiment_task.experiment_id, arm.arm_id,
            task_spec.task_id, run_id, attempt_id, self._descriptor.node_id,
            self._descriptor.profile_fingerprint, arm.execution_strategy, measurement,
            evidence, None, response.resource_measurements)
