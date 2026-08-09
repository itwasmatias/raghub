"""Fail-closed governed local model runtime contracts."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import json
import math

import pytest

from federation.task_request import AuthorizationLevel
from tools.ai_controller.local_capability_baseline import LocalityType
from tools.ai_controller.local_equivalence_benchmark import BenchmarkTaskSpec, CorrectnessType, TaskDifficulty
from tools.ai_controller.local_equivalence_experiments import ExperimentArm, ExperimentArmType, ExperimentTask
from tools.ai_controller.local_model_runtime import (
    AdapterInferenceResponse, BenchmarkLinkage, GenerationConfig,
    GovernedLocalModelRuntime, LocalInferenceConflictError,
    LocalInferenceContractError, LocalInferenceRequest, LocalInferenceStatus,
    LocalInferenceUsage, LocalModelBenchmarkExecutor, LocalModelCapability,
    LocalModelDescriptor, LocalModelLoadStatus, LocalModelRuntime,
    LocalModelRuntimeConfig, LocalityViolationError,
)

NOW = "2026-08-09T15:00:00Z"


def descriptor(**changes):
    values = dict(model_id="model-1", runtime_id="runtime-1", node_id="worker-1",
        profile_fingerprint="a" * 64, locality=LocalityType.LOCAL,
        family=None, quantization=None, context_limit=4096,
        capabilities=frozenset({LocalModelCapability.TEXT_GENERATION,
            LocalModelCapability.STRUCTURED_OUTPUT}),
        load_status=LocalModelLoadStatus.LOADED,
        runtime_metadata={"version": "1", "nested": {"device": "cpu"}})
    values.update(changes)
    return LocalModelDescriptor(**values)


def generation(**changes):
    values = dict(maximum_output_tokens=128, temperature=0.2, seed=42,
        stop_conditions=("STOP",), structured_output_required=False)
    values.update(changes)
    return GenerationConfig(**values)


def linkage(**changes):
    values = dict(experiment_id="experiment-1", arm_id="arm-1", run_id="run-1",
        attempt_id="attempt-1")
    values.update(changes)
    return BenchmarkLinkage(**values)


def request(**changes):
    values = dict(request_id="request-1", task_id="task-1", worker_node_id="worker-1",
        descriptor_fingerprint=descriptor().descriptor_fingerprint(), input_text="bounded prompt",
        generation=generation(), authorization_level=AuthorizationLevel.INTERNAL,
        approval_required=False, permitted_tool_capabilities=("python",), local_only=True,
        execution_attempt_id="execution-1", assignment_id="assignment-1",
        dispatch_offer_id="offer-1", execution_fingerprint="b" * 64,
        benchmark_linkage=linkage())
    values.update(changes)
    return LocalInferenceRequest(**values)


def response(**changes):
    values = dict(status=LocalInferenceStatus.SUCCEEDED, model_id="model-1",
        runtime_id="runtime-1", node_id="worker-1", locality=LocalityType.LOCAL,
        remote_execution=False, output_text="deterministic output", structured_output=None,
        started_at=NOW, completed_at="2026-08-09T15:00:01Z", elapsed_seconds=1.0,
        usage=LocalInferenceUsage(4, 2), resource_measurements={"peak_memory_mb": 512.0},
        termination_reason="stop", error_code=None, error_message=None,
        cloud_escalation_count=0, cloud_cost_usd=0.0)
    values.update(changes)
    return AdapterInferenceResponse(**values)


class FakeAdapter:
    def __init__(self, result=None):
        self.result = result or response()
        self.calls = 0
        self.seeds = []

    def infer(self, model, inference_request, runtime_config):
        self.calls += 1
        self.seeds.append(inference_request.generation.seed)
        return self.result


def runtime(adapter=None, *, authority=True, approval=True):
    return GovernedLocalModelRuntime(adapter or FakeAdapter(),
        LocalModelRuntimeConfig(True, 512, frozenset({"python"}), True),
        authority_verifier=lambda req, model: authority,
        approval_verifier=lambda req: approval)


def test_descriptor_and_request_fingerprints_are_stable_and_material():
    assert descriptor().descriptor_fingerprint() == descriptor().descriptor_fingerprint()
    assert descriptor(context_limit=8192).descriptor_fingerprint() != descriptor().descriptor_fingerprint()
    assert request().request_fingerprint() == request().request_fingerprint()
    assert request(generation=generation(seed=43)).request_fingerprint() != request().request_fingerprint()
    assert request(input_text="changed").request_fingerprint() != request().request_fingerprint()


def test_nested_descriptor_metadata_is_detached_and_immutable():
    metadata = {"nested": {"device": "cpu"}}
    value = descriptor(runtime_metadata=metadata)
    fingerprint = value.descriptor_fingerprint()
    metadata["nested"]["device"] = "remote"
    assert value.to_dict()["runtime_metadata"]["nested"]["device"] == "cpu"
    assert value.descriptor_fingerprint() == fingerprint
    with pytest.raises(TypeError):
        value.runtime_metadata["x"] = 1


@pytest.mark.parametrize(("field", "value"), [
    ("temperature", math.nan), ("temperature", math.inf),
    ("temperature", -0.1), ("temperature", 2.1),
    ("maximum_output_tokens", -1), ("seed", 1.5), ("seed", True),
    ("stop_conditions", ("",)),
])
def test_invalid_generation_parameters_fail_closed(field, value):
    with pytest.raises((LocalInferenceContractError, TypeError)):
        generation(**{field: value})


def test_seed_survives_end_to_end_without_output_determinism_claim():
    adapter = FakeAdapter()
    result = runtime(adapter).execute(descriptor(), request())
    assert adapter.seeds == [42]
    assert result.request_fingerprint == request().request_fingerprint()
    assert "reproducible" not in result.to_dict()


def test_deterministic_fake_adapter_and_exact_duplicate_execute_once():
    adapter = FakeAdapter()
    service = runtime(adapter)
    first = service.execute(descriptor(), request())
    second = service.execute(descriptor(), request())
    assert first is second and adapter.calls == 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(item is first for item in pool.map(
            lambda _: service.execute(descriptor(), request()), range(16)))
    assert adapter.calls == 1


def test_duplicate_conflicting_request_identity_rejected():
    service = runtime()
    service.execute(descriptor(), request())
    with pytest.raises(LocalInferenceConflictError):
        service.execute(descriptor(), request(input_text="different"))


@pytest.mark.parametrize("change", [
    {"descriptor_fingerprint": "c" * 64}, {"worker_node_id": "worker-2"},
])
def test_foreign_descriptor_or_wrong_worker_linkage_rejected(change):
    with pytest.raises(LocalInferenceContractError):
        runtime().execute(descriptor(), request(**change))


def test_descriptor_mutation_after_request_binding_rejected():
    model = descriptor()
    req = request(descriptor_fingerprint=model.descriptor_fingerprint())
    object.__setattr__(model, "model_id", "mutated")
    with pytest.raises(LocalInferenceContractError):
        runtime().execute(model, req)


def test_worker_authority_and_approval_fail_closed_before_adapter():
    adapter = FakeAdapter()
    with pytest.raises(LocalInferenceContractError, match="authority"):
        runtime(adapter, authority=False).execute(descriptor(), request())
    with pytest.raises(LocalInferenceContractError, match="approval"):
        runtime(adapter, approval=False).execute(descriptor(), request(approval_required=True))
    assert adapter.calls == 0


@pytest.mark.parametrize("changes", [
    {"locality": LocalityType.CLOUD}, {"locality": LocalityType.UNKNOWN},
    {"remote_execution": True}, {"cloud_escalation_count": 1}, {"cloud_cost_usd": 0.01},
])
def test_local_only_request_rejects_nonlocal_adapter_evidence(changes):
    with pytest.raises(LocalityViolationError):
        runtime(FakeAdapter(response(**changes))).execute(descriptor(), request())


@pytest.mark.parametrize("changes", [
    {"elapsed_seconds": -1},
    {"resource_measurements": {"watts": -1}},
])
def test_invalid_result_measurements_rejected(changes):
    with pytest.raises((LocalInferenceContractError, ValueError)):
        response(**changes)


def test_invalid_token_counts_rejected():
    with pytest.raises(LocalInferenceContractError):
        LocalInferenceUsage(-1, 2)


def test_adapter_cannot_mutate_authorized_request_or_descriptor():
    class MutatingAdapter(FakeAdapter):
        def infer(self, model, inference_request, runtime_config):
            object.__setattr__(inference_request, "input_text", "mutated")
            return super().infer(model, inference_request, runtime_config)

    service = runtime(MutatingAdapter())
    with pytest.raises(LocalInferenceContractError, match="mutated"):
        service.execute(descriptor(), request())
    with pytest.raises(Exception, match="reconciliation"):
        service.execute(descriptor(), request())


def test_result_timestamp_order_and_supplied_token_bounds_fail_closed():
    with pytest.raises(LocalInferenceContractError, match="timestamp"):
        response(started_at="2026-08-09T15:00:02Z",
            completed_at="2026-08-09T15:00:01Z")
    adapter = FakeAdapter(response(usage=LocalInferenceUsage(4, 129)))
    with pytest.raises(LocalInferenceContractError, match="generated token"):
        runtime(adapter).execute(descriptor(), request())


def test_success_and_error_state_are_consistent():
    with pytest.raises(LocalInferenceContractError):
        response(error_code="adapter_failure")
    with pytest.raises(LocalInferenceContractError):
        response(status=LocalInferenceStatus.FAILED, output_text="not allowed",
            error_code="adapter_failure", error_message="failed")


def test_stable_serialization_and_frozen_result():
    result = runtime().execute(descriptor(), request())
    payload = result.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    with pytest.raises(FrozenInstanceError):
        result.status = LocalInferenceStatus.FAILED


def benchmark_task():
    return BenchmarkTaskSpec("task-1", "coding", "bounded task", "text",
        TaskDifficulty.SIMPLE, frozenset({"code"}), frozenset({"python"}),
        ("reference-1",), {}, CorrectnessType.OBJECTIVE)


def experiment_arm():
    return ExperimentArm("arm-1", ExperimentArmType.SINGLE_LOCAL, "strategy-arm-1",
        LocalityType.LOCAL, "local arm", frozenset({"code"}))


def experiment_task():
    return ExperimentTask("experiment-1", "task-1", "bounded task", "text",
        frozenset({"code"}), ("source-1",))


def request_factory(task_spec, exp_task, arm, run_id, attempt_id):
    return request(task_id=task_spec.task_id,
        benchmark_linkage=BenchmarkLinkage(exp_task.experiment_id, arm.arm_id,
            run_id, attempt_id), permitted_tool_capabilities=tuple(task_spec.permitted_tools))


def test_benchmark_executor_converts_local_result_without_duplication():
    executor = LocalModelBenchmarkExecutor(runtime(), descriptor(), request_factory)
    result = executor.execute(benchmark_task(), experiment_task(), experiment_arm(),
        "run-1", "attempt-1")
    assert result.experiment_id == "experiment-1"
    assert result.measurement.model_identifier == "model-1"
    assert result.measurement.profile_fingerprint == "a" * 64
    assert result.evidence.task_result_id.startswith("local-result-")


@pytest.mark.parametrize("foreign", [
    BenchmarkLinkage("foreign", "arm-1", "run-1", "attempt-1"),
    BenchmarkLinkage("experiment-1", "foreign", "run-1", "attempt-1"),
    BenchmarkLinkage("experiment-1", "arm-1", "foreign", "attempt-1"),
    BenchmarkLinkage("experiment-1", "arm-1", "run-1", "foreign"),
])
def test_benchmark_linkage_substitution_fails_closed(foreign):
    factory = lambda *args: request(benchmark_linkage=foreign)
    executor = LocalModelBenchmarkExecutor(runtime(), descriptor(), factory)
    with pytest.raises(LocalInferenceContractError):
        executor.execute(benchmark_task(), experiment_task(), experiment_arm(),
            "run-1", "attempt-1")


def test_runtime_protocol_is_satisfied():
    service: LocalModelRuntime = runtime()
    assert callable(service.execute)
