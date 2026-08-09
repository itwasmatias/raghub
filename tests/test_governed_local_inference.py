"""Adversarial contracts for governed local inference integration v0.1."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from federation.local_model_server_lifecycle import (
    FEDORA_MODEL_ALIAS, EndpointObservation, LocalModelServerLifecycleState,
    ProcessObservation,
)
from federation.task_request import AuthorizationLevel
from federation.worker_execution import WorkerExecutionStatus
from tests.test_local_model_runtime import descriptor as runtime_descriptor
from tests.test_local_model_runtime import generation, response
from tests.test_local_model_server_lifecycle import KEY, make_authority, start_attested
from tools.ai_controller.local_capability_baseline import LocalityType
from tools.ai_controller.local_model_runtime import (
    GovernedLocalModelRuntime, LocalInferenceContractError, LocalInferenceRequest,
    LocalModelAdapterError, LocalModelCapability,
    LocalModelDescriptor, LocalModelLoadStatus, LocalModelRuntimeConfig,
)
from tools.ai_controller.governed_local_inference import (
    GovernedLocalInferenceConflictError, GovernedLocalInferenceCoordinator,
    GovernedLocalInferenceAuthorityError,
    GovernedLocalInferenceIdentityError, GovernedLocalInferenceLifecycleError,
    GovernedLocalInferenceReconciliationRequired, GovernedLocalInferenceRequest,
    GovernedLocalInferenceStore,
)


class Adapter:
    def __init__(self, result=None, hook=None):
        self.result = result
        self.hook = hook
        self.calls = 0

    def infer(self, model, request, config):
        self.calls += 1
        if self.hook:
            self.hook()
        return self.result or response(model_id=model.model_id,
            runtime_id=model.runtime_id, node_id=model.node_id)


def governed_fixture(tmp_path: Path, *, hook=None):
    lifecycle, start_request, record, process, _, lifecycle_execution, registry = \
        start_attested(tmp_path / "lifecycle")
    inference_execution, attempt = make_authority(
        tmp_path / "inference", fingerprint="f" * 64)
    attempt = inference_execution.start(
        execution_attempt_id=attempt.request.execution_attempt_id,
        actor_node_id=attempt.request.worker_node_id)
    model = LocalModelDescriptor(
        model_id=FEDORA_MODEL_ALIAS, runtime_id="llama-cpp-runtime-1",
        node_id="worker-1", profile_fingerprint=start_request.profile.fingerprint,
        locality=LocalityType.LOCAL, family="qwen2.5", quantization="Q4_K_M",
        context_limit=1024,
        capabilities=frozenset({LocalModelCapability.TEXT_GENERATION}),
        load_status=LocalModelLoadStatus.LOADED,
        runtime_metadata={"backend": "llama.cpp", "profile_id": start_request.profile.profile_id},
    )
    adapter = Adapter(hook=hook)
    runtime = GovernedLocalModelRuntime(
        adapter, LocalModelRuntimeConfig(True, 64, frozenset(), True),
        authority_verifier=lambda req, desc: (
            inference_execution.inspect(req.execution_attempt_id).status
            is WorkerExecutionStatus.RUNNING
            and inference_execution.inspect(req.execution_attempt_id).request.worker_node_id
            == req.worker_node_id
            and inference_execution.inspect(req.execution_attempt_id).request.execution_fingerprint
            == req.execution_fingerprint
        ),
    )
    runtime_request = LocalInferenceRequest(
        request_id="runtime-request-1", task_id=attempt.request.task_id,
        worker_node_id=attempt.request.worker_node_id,
        descriptor_fingerprint=model.descriptor_fingerprint(), input_text="Reply exactly: 4",
        generation=generation(maximum_output_tokens=8, temperature=0.0, seed=7,
            stop_conditions=()), authorization_level=AuthorizationLevel.RESTRICTED,
        approval_required=False, permitted_tool_capabilities=(), local_only=True,
        execution_attempt_id=attempt.request.execution_attempt_id,
        assignment_id=attempt.request.assignment_id,
        dispatch_offer_id=attempt.request.dispatch_offer_id,
        execution_fingerprint=attempt.request.execution_fingerprint,
        benchmark_linkage=None,
    )
    request = GovernedLocalInferenceRequest(
        integration_request_id="integration-1",
        worker_node_id="worker-1",
        inference_execution_attempt_id=attempt.request.execution_attempt_id,
        inference_execution_fingerprint=attempt.request.execution_fingerprint,
        lifecycle_request_id=start_request.lifecycle_request_id,
        expected_lifecycle_record_fingerprint=record.record_fingerprint,
        expected_lifecycle_attestation_fingerprint=record.attestation.fingerprint,
        expected_endpoint=start_request.profile.endpoint,
        expected_backend="llama.cpp",
        expected_profile_fingerprint=start_request.profile.fingerprint,
        runtime_request=runtime_request,
    )
    integration = GovernedLocalInferenceCoordinator(
        lifecycle_store=lifecycle.store, process_operations=process,
        worker_execution=inference_execution, runtime=runtime,
        store=GovernedLocalInferenceStore(tmp_path / "integration.jsonl", integrity_key=KEY),
    )
    return integration, request, model, adapter, process, lifecycle, record


def test_authoritative_attested_lifecycle_executes_and_persists_evidence(tmp_path):
    integration, request, model, adapter, _, _, record = governed_fixture(tmp_path)
    result = integration.execute(request, model)
    assert adapter.calls == 1
    assert result.runtime_result.request_fingerprint == request.runtime_request.request_fingerprint()
    assert result.evidence.lifecycle_record_fingerprint == record.record_fingerprint
    assert result.evidence.lifecycle_attestation_fingerprint == record.attestation.fingerprint
    assert result.evidence.pre_process_observation == ProcessObservation.MATCHING.value
    assert result.evidence.post_process_observation == ProcessObservation.MATCHING.value
    assert integration.store.current(request.integration_request_id) == result
    with pytest.raises(FrozenInstanceError):
        result.evidence.endpoint = "http://127.0.0.1:1"


@pytest.mark.parametrize("state", [
    LocalModelServerLifecycleState.REQUESTED,
    LocalModelServerLifecycleState.PREFLIGHT_PASSED,
    LocalModelServerLifecycleState.STARTING,
    LocalModelServerLifecycleState.RUNNING_UNATTESTED,
    LocalModelServerLifecycleState.STOPPING,
    LocalModelServerLifecycleState.STOPPED,
    LocalModelServerLifecycleState.FAILED,
    LocalModelServerLifecycleState.RECONCILIATION_REQUIRED,
])
def test_every_non_attested_state_is_rejected(tmp_path, state, monkeypatch):
    integration, request, model, adapter, _, _, record = governed_fixture(tmp_path)
    monkeypatch.setattr(integration.lifecycle_store, "current",
        lambda ignored: SimpleNamespace(state=state))
    with pytest.raises(GovernedLocalInferenceLifecycleError):
        integration.execute(request, model)
    assert adapter.calls == 0


@pytest.mark.parametrize("field,value", [
    ("lifecycle_request_id", "missing-lifecycle"),
    ("expected_lifecycle_record_fingerprint", "1" * 64),
    ("expected_lifecycle_attestation_fingerprint", "2" * 64),
    ("expected_endpoint", "http://127.0.0.1:18081"),
    ("expected_backend", "other"),
    ("worker_node_id", "worker-2"),
    ("inference_execution_fingerprint", "3" * 64),
])
def test_authority_and_lifecycle_binding_mismatches_fail_before_runtime(tmp_path, field, value):
    integration, request, model, adapter, *_ = governed_fixture(tmp_path)
    with pytest.raises((GovernedLocalInferenceLifecycleError,
                        GovernedLocalInferenceIdentityError,
                        GovernedLocalInferenceAuthorityError)):
        integration.execute(replace(request, **{field: value}), model)
    assert adapter.calls == 0


@pytest.mark.parametrize("observation", [ProcessObservation.ABSENT,
                                           ProcessObservation.IDENTITY_MISMATCH])
def test_process_absence_or_pid_reuse_fails_closed(tmp_path, observation):
    integration, request, model, adapter, process, *_ = governed_fixture(tmp_path)
    process.observation = observation
    with pytest.raises(GovernedLocalInferenceIdentityError):
        integration.execute(request, model)
    assert adapter.calls == 0


def test_endpoint_owner_must_be_exact(tmp_path):
    integration, request, model, adapter, process, *_ = governed_fixture(tmp_path)
    process.foreign_owner = 9999
    process.terminated = True
    process.observation = ProcessObservation.MATCHING
    with pytest.raises(GovernedLocalInferenceIdentityError):
        integration.execute(request, model)
    assert adapter.calls == 0


def test_identity_change_during_inference_is_ambiguous_and_not_persisted_as_success(tmp_path):
    holder = {}
    integration, request, model, adapter, process, *_ = governed_fixture(
        tmp_path, hook=lambda: setattr(holder["process"], "observation",
                                      ProcessObservation.IDENTITY_MISMATCH))
    holder["process"] = process
    with pytest.raises(GovernedLocalInferenceReconciliationRequired):
        integration.execute(request, model)
    assert adapter.calls == 1


def test_replay_uses_integration_and_runtime_idempotency(tmp_path):
    integration, request, model, adapter, *_ = governed_fixture(tmp_path)
    first = integration.execute(request, model)
    assert integration.execute(request, model) == first
    assert adapter.calls == 1
    with pytest.raises(GovernedLocalInferenceConflictError):
        integration.execute(replace(request, expected_backend="changed"), model)


def test_request_fingerprint_is_deterministic_and_material(tmp_path):
    _, request, *_ = governed_fixture(tmp_path)
    assert request.request_fingerprint == request.request_fingerprint
    assert replace(request, expected_backend="changed").request_fingerprint != request.request_fingerprint
    with pytest.raises(Exception) as error:
        replace(request, expected_profile_fingerprint=True)
    assert "lowercase SHA-256" in str(error.value)


def test_no_lifecycle_or_http_side_effects_are_owned_by_integration(tmp_path):
    integration, request, model, adapter, process, lifecycle, *_ = governed_fixture(tmp_path)
    before_launches, before_signals = process.launch_count, process.signals
    integration.execute(request, model)
    assert process.launch_count == before_launches
    assert process.signals == before_signals
    assert not hasattr(integration, "start") and not hasattr(integration, "stop")
    assert adapter.calls == 1


@pytest.mark.parametrize("change", [
    {"model_id": "wrong-model"},
    {"profile_fingerprint": "9" * 64},
    {"runtime_metadata": {"backend": "other", "profile_id":
                          "fedora-llama-server-qwen2.5-0.5b-v0.1"}},
    {"runtime_metadata": {"backend": "llama.cpp", "profile_id": "wrong-profile"}},
])
def test_model_backend_and_profile_mismatches_are_rejected(tmp_path, change):
    integration, request, model, adapter, *_ = governed_fixture(tmp_path)
    wrong = replace(model, **change)
    wrong_request = replace(request, runtime_request=replace(
        request.runtime_request, descriptor_fingerprint=wrong.descriptor_fingerprint()))
    with pytest.raises(GovernedLocalInferenceIdentityError):
        integration.execute(wrong_request, wrong)
    assert adapter.calls == 0


def test_missing_inference_authority_is_typed(tmp_path):
    integration, request, model, adapter, *_ = governed_fixture(tmp_path)
    changed = replace(request,
        inference_execution_attempt_id="execution-does-not-exist",
        runtime_request=replace(request.runtime_request,
            execution_attempt_id="execution-does-not-exist"))
    with pytest.raises(GovernedLocalInferenceAuthorityError):
        integration.execute(changed, model)
    assert adapter.calls == 0


def test_runtime_authority_rejection_propagates_without_fallback(tmp_path):
    integration, request, model, adapter, *_ = governed_fixture(tmp_path)
    integration.runtime._authority_verifier = lambda req, desc: False
    with pytest.raises(LocalInferenceContractError,
                       match="worker execution authority verification failed"):
        integration.execute(request, model)
    assert adapter.calls == 0


def test_malformed_runtime_result_is_rejected(tmp_path):
    integration, request, model, adapter, *_ = governed_fixture(tmp_path)
    integration.runtime = SimpleNamespace(execute=lambda *_: {"forged": "result"})
    with pytest.raises(TypeError):
        integration.execute(request, model)


def test_process_disappears_during_inference(tmp_path):
    holder = {}
    integration, request, model, adapter, process, *_ = governed_fixture(
        tmp_path, hook=lambda: setattr(holder["process"], "observation",
                                      ProcessObservation.ABSENT))
    holder["process"] = process
    with pytest.raises(GovernedLocalInferenceReconciliationRequired):
        integration.execute(request, model)


def test_endpoint_owner_changes_during_inference(tmp_path):
    integration, request, model, adapter, process, *_ = governed_fixture(tmp_path)
    original = process.endpoint_observation
    calls = 0
    def changing_endpoint():
        nonlocal calls
        calls += 1
        if calls == 1:
            return original()
        value = original()
        return replace(value, owner_pid=9999)
    process.endpoint_observation = changing_endpoint
    with pytest.raises(GovernedLocalInferenceReconciliationRequired):
        integration.execute(request, model)


def test_lifecycle_fingerprint_changes_during_inference(tmp_path, monkeypatch):
    integration, request, model, adapter, _, _, record = governed_fixture(tmp_path)
    calls = 0
    original = integration.lifecycle_store.current
    def changing_record(identity):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(identity)
        return SimpleNamespace(state=LocalModelServerLifecycleState.ATTESTED,
                               record_fingerprint="0" * 64)
    monkeypatch.setattr(integration.lifecycle_store, "current", changing_record)
    with pytest.raises(GovernedLocalInferenceReconciliationRequired):
        integration.execute(request, model)


def test_durable_store_reopen_preserves_exact_result(tmp_path):
    integration, request, model, adapter, *_ = governed_fixture(tmp_path)
    result = integration.execute(request, model)
    reopened = GovernedLocalInferenceStore(tmp_path / "integration.jsonl", integrity_key=KEY)
    assert reopened.current(request.integration_request_id) == result


def test_mutable_runtime_metadata_is_detached(tmp_path):
    integration, request, model, adapter, *_ = governed_fixture(tmp_path)
    metadata = {"backend": "llama.cpp", "profile_id":
                "fedora-llama-server-qwen2.5-0.5b-v0.1", "nested": {"cpu": 1}}
    detached = replace(model, runtime_metadata=metadata)
    bound = replace(request, runtime_request=replace(
        request.runtime_request, descriptor_fingerprint=detached.descriptor_fingerprint()))
    fingerprint = bound.request_fingerprint
    metadata["nested"]["cpu"] = 2
    assert bound.request_fingerprint == fingerprint
    integration.execute(bound, detached)


def test_every_execution_affecting_reference_is_fingerprinted(tmp_path):
    _, request, *_ = governed_fixture(tmp_path)
    variants = [
        replace(request, integration_request_id="integration-2"),
        replace(request, lifecycle_request_id="lifecycle-2"),
        replace(request, expected_lifecycle_record_fingerprint="1" * 64),
        replace(request, expected_lifecycle_attestation_fingerprint="2" * 64),
        replace(request, inference_execution_attempt_id="execution-2"),
        replace(request, inference_execution_fingerprint="3" * 64),
        replace(request, expected_backend="backend-2"),
        replace(request, expected_profile_fingerprint="4" * 64),
        replace(request, runtime_request=replace(request.runtime_request,
            generation=generation(maximum_output_tokens=7, temperature=0.0,
                                  seed=7, stop_conditions=()))),
    ]
    assert all(item.request_fingerprint != request.request_fingerprint for item in variants)


def test_caller_forged_attested_object_is_not_authority(tmp_path):
    integration, request, model, adapter, *_ = governed_fixture(tmp_path)
    forged = SimpleNamespace(state=LocalModelServerLifecycleState.ATTESTED)
    with pytest.raises(TypeError):
        integration.execute(forged, model)
    assert adapter.calls == 0


def test_unoccupied_endpoint_is_rejected(tmp_path):
    integration, request, model, adapter, process, *_ = governed_fixture(tmp_path)
    process.endpoint_observation = lambda: EndpointObservation(False, None,
        datetime.now(timezone.utc))
    with pytest.raises(GovernedLocalInferenceIdentityError):
        integration.execute(request, model)
    assert adapter.calls == 0


def test_artifact_binding_change_is_rejected(tmp_path, monkeypatch):
    integration, request, model, adapter, _, _, record = governed_fixture(tmp_path)
    changed_start = replace(record.request, artifact_fingerprint="7" * 64)
    changed_record = replace(record, request=changed_start)
    changed_request = replace(request,
        expected_lifecycle_record_fingerprint=changed_record.record_fingerprint)
    monkeypatch.setattr(integration.lifecycle_store, "current", lambda ignored: changed_record)
    with pytest.raises(GovernedLocalInferenceIdentityError):
        integration.execute(changed_request, model)


def test_binary_binding_change_is_rejected(tmp_path, monkeypatch):
    integration, request, model, adapter, _, _, record = governed_fixture(tmp_path)
    changed_binary = replace(record.binary_identity, sha256="8" * 64)
    changed_record = replace(record, binary_identity=changed_binary)
    changed_request = replace(request,
        expected_lifecycle_record_fingerprint=changed_record.record_fingerprint)
    monkeypatch.setattr(integration.lifecycle_store, "current", lambda ignored: changed_record)
    with pytest.raises(GovernedLocalInferenceIdentityError):
        integration.execute(changed_request, model)


def test_runtime_execution_failure_has_no_retry_or_cloud_fallback(tmp_path):
    integration, request, model, adapter, *_ = governed_fixture(tmp_path)
    def fail():
        raise RuntimeError("local failure")
    adapter.hook = fail
    with pytest.raises(LocalModelAdapterError):
        integration.execute(request, model)
    assert adapter.calls == 1


def test_failed_runtime_response_is_preserved_as_local_result(tmp_path):
    integration, request, model, adapter, *_ = governed_fixture(tmp_path)
    adapter.result = response(status="failed", model_id=model.model_id,
        runtime_id=model.runtime_id, node_id=model.node_id, output_text=None,
        termination_reason="runtime_error", error_code="local_failure",
        error_message="local inference failed")
    result = integration.execute(request, model)
    assert result.runtime_result.status.value == "failed"
    assert result.runtime_result.response.cloud_escalation_count == 0


def test_execution_authority_wrong_state_is_rejected(tmp_path):
    integration, request, model, adapter, *_ = governed_fixture(tmp_path)
    current = integration.worker_execution.inspect(request.inference_execution_attempt_id)
    integration.worker_execution.inspect = lambda ignored: replace(
        current, status=WorkerExecutionStatus.CLAIMED)
    with pytest.raises(GovernedLocalInferenceAuthorityError):
        integration.execute(request, model)


def test_runtime_request_worker_conflict_is_rejected(tmp_path):
    integration, request, model, adapter, *_ = governed_fixture(tmp_path)
    changed = replace(request, runtime_request=replace(request.runtime_request,
        worker_node_id="worker-2"))
    with pytest.raises(GovernedLocalInferenceAuthorityError):
        integration.execute(changed, model)


def test_nan_generation_parameter_fails_closed_before_integration(tmp_path):
    _, request, *_ = governed_fixture(tmp_path)
    with pytest.raises(LocalInferenceContractError, match="finite"):
        replace(request.runtime_request,
            generation=generation(temperature=float("nan")))


def test_bool_numeric_generation_parameter_fails_closed(tmp_path):
    _, request, *_ = governed_fixture(tmp_path)
    with pytest.raises(LocalInferenceContractError):
        replace(request.runtime_request,
            generation=generation(maximum_output_tokens=True))


def test_durable_evidence_tampering_is_rejected(tmp_path):
    integration, request, model, adapter, *_ = governed_fixture(tmp_path)
    integration.execute(request, model)
    path = tmp_path / "integration.jsonl"
    data = path.read_bytes().replace(b'"backend":"llama.cpp"', b'"backend":"evil....."')
    path.write_bytes(data)
    with pytest.raises(GovernedLocalInferenceConflictError):
        integration.store.current(request.integration_request_id)


def test_integration_result_rejects_foreign_runtime_result_fingerprint(tmp_path):
    integration, request, model, adapter, *_ = governed_fixture(tmp_path)
    result = integration.execute(request, model)
    with pytest.raises(GovernedLocalInferenceIdentityError,
                       match="runtime result fingerprint"):
        replace(result, evidence=replace(
            result.evidence, runtime_result_fingerprint="6" * 64))
