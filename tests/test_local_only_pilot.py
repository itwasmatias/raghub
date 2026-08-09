from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import math
import json

import pytest

from federation.task_request import AuthorizationLevel
from federation.assignment_registry import DurableAssignmentRegistry
from federation.capability import NodeCapability
from federation.heartbeat import Heartbeat
from federation.heartbeat_registry import HeartbeatRegistry
from federation.node_record import NodeRecord
from federation.registry import NodeRegistry
from federation.task_assignment import TaskAssignment
from federation.task_dispatcher import TaskDispatchCoordinator
from federation.task_request import TaskRequest
from federation.worker_execution import WorkerExecutionCoordinator
from tools.ai_controller.local_capability_baseline import LocalityType
from tools.ai_controller.local_model_runtime import (
    AdapterInferenceResponse, GenerationConfig, LocalInferenceRequest,
    LocalInferenceResult, LocalInferenceStatus, LocalInferenceUsage,
    LocalModelDescriptor, LocalModelCapability, LocalModelLoadStatus,
)
from tools.ai_controller.local_only_pilot import (
    PilotContractError, PilotCorrectness, PilotEvaluation,
    PilotEvaluationKind, PilotReport, PilotTask, PilotTaskFamily,
    PilotTaskRecord, build_default_pilot_definition, evaluate_output,
)
from tools.ai_controller.run_local_only_pilot import (
    MODEL_ALIAS, MODEL_SHA256, NODE_ID, PilotExecutionAuthority, _load_authority, parser,
)

AUTHORITY_KEY = b"local-pilot-authority-test-key-00001"
AUTHORITY_NOW = datetime(2026, 8, 9, tzinfo=timezone.utc)


def authoritative_environment(path, definition=None, *, worker=NODE_ID):
    definition = definition or build_default_pilot_definition()
    node = NodeRecord(worker, "worker.local", "Fedora",
        capabilities={NodeCapability("local_model_inference")})
    nodes = NodeRegistry(stale_threshold_seconds=10**9)
    nodes.register(node)
    assignments = DurableAssignmentRegistry(path / "assignments.jsonl",
        coordinator_node_id="coordinator-1", integrity_key=AUTHORITY_KEY)
    heartbeats = HeartbeatRegistry(path / "heartbeats.jsonl", registry_id="pilot-tests",
        node_registry=nodes, integrity_key=AUTHORITY_KEY, clock=lambda: AUTHORITY_NOW)
    heartbeats.record(Heartbeat.authenticated(worker_id=worker, registry_id="pilot-tests",
        sequence=1, session_id="boot-1", worker_timestamp=AUTHORITY_NOW, health="healthy",
        power_capabilities=(), requested_power_state="active", sleep_reason=None,
        expected_wake_time=None, wake_method=None, active_work_checkpointed=False,
        previous_authentication_tag="0" * 64, integrity_key=AUTHORITY_KEY))
    dispatch = TaskDispatchCoordinator("coordinator-1", assignment_store=assignments,
        dispatch_store_path=path / "dispatch.jsonl", integrity_key=AUTHORITY_KEY,
        heartbeat_registry=heartbeats, clock=lambda: AUTHORITY_NOW)
    protocol = WorkerExecutionCoordinator("coordinator-1", dispatch_coordinator=dispatch,
        store_path=path / "execution.jsonl", integrity_key=AUTHORITY_KEY,
        clock=lambda: AUTHORITY_NOW)
    manifest = {}
    for task in definition.tasks:
        fingerprint = task.task_fingerprint()
        request_value = TaskRequest(task.task_id, definition.pilot_id,
            required_capabilities={NodeCapability("local_model_inference")},
            authorization_level=AuthorizationLevel.INTERNAL, approval_required=False,
            input_data={"execution_fingerprint": fingerprint}, expected_result="pilot evidence")
        assignment = assignments.record(TaskAssignment(request_value, node))
        offer = dispatch.create_offer(assignment_id=assignment.assignment_id,
            actor_node_id="coordinator-1", expires_at=AUTHORITY_NOW + timedelta(hours=1))
        offer = dispatch.accept_offer(offer_id=offer.offer_id, actor_node_id=worker)
        attempt = protocol.register(dispatch_offer_id=offer.offer_id,
            actor_node_id="coordinator-1", expected_result="pilot evidence")
        attempt = protocol.claim(execution_attempt_id=attempt.request.execution_attempt_id,
            actor_node_id=worker)
        manifest[task.task_id] = {"execution_attempt_id": attempt.request.execution_attempt_id,
            "assignment_id": assignment.assignment_id, "dispatch_offer_id": offer.offer_id,
            "execution_fingerprint": fingerprint}
    return PilotExecutionAuthority(protocol), manifest, protocol


def model():
    return LocalModelDescriptor(
        "raghub-qwen2.5-0.5b-q4km", "llama.cpp", "fedora-local-node", "a" * 64,
        LocalityType.LOCAL, "qwen2.5", "Q4_K_M", 1024,
        frozenset({LocalModelCapability.TEXT_GENERATION}), LocalModelLoadStatus.LOADED,
        {"model_sha256": "74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db"})


def request(task, **changes):
    values = dict(request_id=f"request-{task.task_id}", task_id=task.task_id,
        worker_node_id="fedora-local-node", descriptor_fingerprint=model().descriptor_fingerprint(),
        input_text=task.prompt, generation=GenerationConfig(task.maximum_output_tokens, 0.0, 42, (), False),
        authorization_level=AuthorizationLevel.INTERNAL, approval_required=False,
        permitted_tool_capabilities=(), local_only=True,
        execution_attempt_id=f"attempt-{task.task_id}", assignment_id=f"assignment-{task.task_id}",
        dispatch_offer_id=f"offer-{task.task_id}", execution_fingerprint="b" * 64)
    values.update(changes)
    return LocalInferenceRequest(**values)


def result(req, output="LOCAL_OK", **changes):
    response_values = dict(status=LocalInferenceStatus.SUCCEEDED,
        model_id=model().model_id, runtime_id=model().runtime_id, node_id=model().node_id,
        locality=LocalityType.LOCAL, remote_execution=False, output_text=output,
        structured_output=None, started_at="2026-08-09T15:00:00Z",
        completed_at="2026-08-09T15:00:02Z", elapsed_seconds=2.0,
        usage=LocalInferenceUsage(8, 2),
        resource_measurements={"prompt_evaluated_tokens": 4,
            "generated_evaluated_tokens": 2,
            "prompt_ms": 1000.0, "predicted_ms": 500.0},
        termination_reason="stop", error_code=None, error_message=None,
        cloud_escalation_count=0, cloud_cost_usd=0.0)
    response_values.update(changes)
    response = AdapterInferenceResponse(**response_values)
    result_id = "local-result-" + __import__(
        "tools.ai_controller.local_model_runtime", fromlist=["_fingerprint"]
    )._fingerprint({"request": req.request_fingerprint(),
        "descriptor": model().descriptor_fingerprint(), "response": response.to_dict(),
        "benchmark": None})
    return LocalInferenceResult(result_id, req.request_fingerprint(),
        model().descriptor_fingerprint(), response, None)


def test_default_definition_has_twelve_bounded_deterministic_tasks_and_all_families():
    first = build_default_pilot_definition()
    second = build_default_pilot_definition()
    assert len(first.tasks) == 12
    assert first.definition_fingerprint() == second.definition_fingerprint()
    assert {task.family for task in first.tasks} == set(PilotTaskFamily)
    assert all(task.maximum_output_tokens <= 96 for task in first.tasks)
    assert len({task.task_id for task in first.tasks}) == 12
    assert len(first.benchmark_suite().tasks) == 12
    experiment = first.experiment_definition()
    assert len(experiment.arms) == 1
    assert experiment.arms[0].arm_type.value == "single_local"


def test_task_validation_fingerprints_and_nested_immutability():
    task = build_default_pilot_definition().tasks[0]
    assert task.task_fingerprint() == replace(task).task_fingerprint()
    assert replace(task, prompt=task.prompt + " now").task_fingerprint() != task.task_fingerprint()
    with pytest.raises(FrozenInstanceError):
        task.prompt = "mutated"
    with pytest.raises(PilotContractError):
        replace(task, maximum_output_tokens=97)


def test_duplicate_and_conflicting_task_identities_fail_closed():
    definition = build_default_pilot_definition()
    duplicate = definition.tasks + (definition.tasks[0],)
    with pytest.raises(PilotContractError):
        replace(definition, tasks=duplicate)
    with pytest.raises(PilotContractError):
        replace(definition, tasks=definition.tasks + (replace(definition.tasks[0], prompt="foreign"),))


@pytest.mark.parametrize(("kind", "output", "expected", "kwargs"), [
    (PilotEvaluationKind.EXACT, "  LOCAL_OK\n", PilotCorrectness.CORRECT, {"expected_answer": "LOCAL_OK"}),
    (PilotEvaluationKind.CLASSIFICATION, "blue", PilotCorrectness.CORRECT,
        {"expected_answer": "BLUE", "allowed_answers": ("BLUE", "RED")}),
    (PilotEvaluationKind.NUMERIC, "42", PilotCorrectness.CORRECT, {"expected_answer": "42"}),
    (PilotEvaluationKind.REQUIRED_FACTS, "Ada wrote notes on the engine.", PilotCorrectness.CORRECT,
        {"required_facts": ("Ada", "engine")}),
    (PilotEvaluationKind.UNEVALUABLE, "nice prose", PilotCorrectness.UNEVALUABLE, {}),
])
def test_objective_evaluators(kind, output, expected, kwargs):
    task = PilotTask("eval-task", PilotTaskFamily.EXACT_INSTRUCTION, "Prompt", 32, kind, **kwargs)
    assert evaluate_output(task, output).correctness is expected


def test_execution_success_is_separate_from_objective_correctness_and_metrics_are_exact():
    task = build_default_pilot_definition().tasks[0]
    req = request(task)
    record = PilotTaskRecord.from_evidence(task, req, result(req, "WRONG"))
    assert record.execution_succeeded is True
    assert record.evaluation.correctness is PilotCorrectness.INCORRECT
    report = PilotReport.create("pilot-run-1", build_default_pilot_definition(), model(), (record,))
    assert report.metrics.tasks_attempted == 1
    assert report.metrics.tasks_successfully_executed == 1
    assert report.metrics.objectively_incorrect == 1
    assert record.prompt_tokens == 8
    assert record.prompt_evaluated_tokens == 4
    assert report.metrics.mean_prompt_throughput == 4.0
    assert report.metrics.mean_generation_throughput == 4.0
    assert report.metrics.total_cloud_cost_usd == 0.0
    assert report.to_dict() == PilotReport.create(
        "pilot-run-1", build_default_pilot_definition(), model(), (record,)).to_dict()
    assert "proof" not in str(report.to_dict()).lower()
    assert "winner" not in str(report.to_dict()).lower()


def test_report_fingerprint_and_throughput_bind_evaluated_token_work():
    task = build_default_pilot_definition().tasks[0]
    req = request(task)
    original = PilotTaskRecord.from_evidence(task, req, result(req))
    changed = replace(original, prompt_evaluated_tokens=1)
    first = PilotReport.create("pilot-run-1", build_default_pilot_definition(), model(), (original,))
    second = PilotReport.create("pilot-run-1", build_default_pilot_definition(), model(), (changed,))
    assert first.report_fingerprint != second.report_fingerprint
    assert first.metrics.mean_prompt_throughput == 4.0
    assert second.metrics.mean_prompt_throughput == 1.0


def test_logical_counts_without_evaluated_work_do_not_fabricate_throughput():
    task = build_default_pilot_definition().tasks[0]
    req = request(task)
    evidence = result(req, resource_measurements={"prompt_ms": 1000.0,
        "predicted_ms": 500.0})
    record = PilotTaskRecord.from_evidence(task, req, evidence)
    report = PilotReport.create("pilot-run-1", build_default_pilot_definition(), model(), (record,))
    assert record.prompt_tokens == 8
    assert record.prompt_evaluated_tokens is None
    assert report.metrics.mean_prompt_throughput is None
    assert report.metrics.mean_generation_throughput is None


def test_record_rejects_foreign_linkage_cloud_evidence_and_malformed_measurements():
    task = build_default_pilot_definition().tasks[0]
    req = request(task)
    with pytest.raises(PilotContractError):
        PilotTaskRecord.from_evidence(task, replace(req, task_id="foreign-task"), result(req))
    for changes in (
        {"locality": LocalityType.CLOUD}, {"remote_execution": True},
        {"cloud_escalation_count": 1}, {"cloud_cost_usd": 0.1},
        {"resource_measurements": {"prompt_ms": math.nan}},
    ):
        with pytest.raises(Exception):
            PilotTaskRecord.from_evidence(task, req, result(req, **changes))
    valid = PilotTaskRecord.from_evidence(task, req, result(req))
    with pytest.raises(PilotContractError):
        replace(valid, elapsed_seconds=math.inf)
    with pytest.raises(PilotContractError):
        replace(valid, execution_succeeded=False)


def test_result_request_and_descriptor_substitution_fail_closed():
    task = build_default_pilot_definition().tasks[0]
    req = request(task)
    evidence = result(req)
    with pytest.raises(PilotContractError):
        PilotTaskRecord.from_evidence(task, replace(req, input_text="changed"), evidence)
    with pytest.raises(PilotContractError):
        PilotReport.create("pilot-run-1", build_default_pilot_definition(),
            replace(model(), profile_fingerprint="c" * 64),
            (PilotTaskRecord.from_evidence(task, req, evidence),))


def test_evaluation_is_immutable_and_does_not_treat_agreement_as_correctness():
    evaluation = PilotEvaluation(PilotCorrectness.UNEVALUABLE, "no_objective_rubric", ())
    with pytest.raises(FrozenInstanceError):
        evaluation.reason = "agents_agree"


def test_real_entrypoint_requires_exact_model_and_explicit_governed_inputs(tmp_path):
    parsed = parser().parse_args(["--endpoint", "http://127.0.0.1:8080",
        "--output", str(tmp_path / "report.json"), "--authority-manifest", "authority.json",
        "--assignment-store", "assignments.jsonl", "--dispatch-store", "dispatch.jsonl",
        "--execution-store", "execution.jsonl", "--heartbeat-store", "heartbeats.jsonl",
        "--heartbeat-registry-id", "pilot-registry", "--coordinator-node-id", "coordinator-1",
        "--run-id", "run-1", "--profile-fingerprint", "a" * 64,
        "--model-alias", MODEL_ALIAS, "--model-sha256", MODEL_SHA256])
    assert parsed.endpoint == "http://127.0.0.1:8080"
    assert parsed.model_alias == MODEL_ALIAS
    assert parsed.model_sha256 == MODEL_SHA256


def test_authority_manifest_requires_exact_task_set_and_schema(tmp_path):
    definition = build_default_pilot_definition()
    manifest = {task.task_id: {"execution_attempt_id": f"attempt-{task.task_id}",
        "assignment_id": f"assignment-{task.task_id}", "dispatch_offer_id": f"offer-{task.task_id}",
        "execution_fingerprint": "b" * 64} for task in definition.tasks}
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(manifest))
    assert _load_authority(path, set(manifest)) == manifest
    manifest[definition.tasks[0].task_id]["command"] = "sh"
    path.write_text(json.dumps(manifest))
    with pytest.raises(PilotContractError):
        _load_authority(path, set(manifest))


def test_schema_valid_fabricated_manifest_and_missing_authority_fail(tmp_path):
    definition = build_default_pilot_definition()
    authority, manifest, _ = authoritative_environment(tmp_path, definition)
    fabricated = {key: dict(value) for key, value in manifest.items()}
    fabricated[definition.tasks[0].task_id]["execution_attempt_id"] = "execution-" + "f" * 64
    with pytest.raises(PilotContractError, match="unavailable"):
        authority.preflight(definition, fabricated, worker_node_id=NODE_ID)


def test_authoritative_matching_evidence_passes_and_replay_is_deterministic(tmp_path):
    definition = build_default_pilot_definition()
    authority, manifest, _ = authoritative_environment(tmp_path, definition)
    first = authority.preflight(definition, manifest, worker_node_id=NODE_ID)
    second = authority.preflight(definition, manifest, worker_node_id=NODE_ID)
    assert first == second
    assert tuple(item.request.task_id for item in first) == tuple(task.task_id for task in definition.tasks)


def test_only_claimed_attempt_can_begin_and_running_attempt_cannot_be_replayed(tmp_path):
    definition = build_default_pilot_definition()
    authority, manifest, protocol = authoritative_environment(tmp_path, definition)
    authority.preflight(definition, manifest, worker_node_id=NODE_ID)
    running = authority.begin(definition.tasks[0].task_id)
    assert running.status.value == "running"
    assert protocol.inspect(running.request.execution_attempt_id) == running
    with pytest.raises(PilotContractError, match="not uniquely eligible"):
        authority.preflight(definition, manifest, worker_node_id=NODE_ID)


def test_runtime_verifier_rereads_current_authoritative_attempt(tmp_path):
    definition = build_default_pilot_definition()
    authority, manifest, protocol = authoritative_environment(tmp_path, definition)
    authority.preflight(definition, manifest, worker_node_id=NODE_ID)
    task = definition.tasks[0]
    running = authority.begin(task.task_id)
    evidence = running.request
    inference = LocalInferenceRequest("pilot-request", task.task_id, NODE_ID,
        model().descriptor_fingerprint(), task.prompt,
        GenerationConfig(task.maximum_output_tokens, 0.0, 42, (), False),
        evidence.authorization_level, evidence.approval_required, (), True,
        evidence.execution_attempt_id, evidence.assignment_id, evidence.dispatch_offer_id,
        evidence.execution_fingerprint)
    assert authority.verifies(inference, model()) is True
    protocol.fail(execution_attempt_id=evidence.execution_attempt_id,
        actor_node_id=NODE_ID, reason="external terminal transition")
    assert authority.verifies(inference, model()) is False


@pytest.mark.parametrize(("field", "replacement"), [
    ("assignment_id", "assignment-" + "1" * 64),
    ("dispatch_offer_id", "dispatch-" + "2" * 64),
    ("execution_attempt_id", "execution-" + "3" * 64),
    ("execution_fingerprint", "4" * 64),
])
def test_swapped_authority_identifiers_fail_closed(tmp_path, field, replacement):
    definition = build_default_pilot_definition()
    authority, manifest, _ = authoritative_environment(tmp_path, definition)
    changed = {key: dict(value) for key, value in manifest.items()}
    changed[definition.tasks[0].task_id][field] = replacement
    with pytest.raises(PilotContractError):
        authority.preflight(definition, changed, worker_node_id=NODE_ID)


def test_foreign_task_and_node_authority_fail_closed(tmp_path):
    definition = build_default_pilot_definition()
    authority, manifest, _ = authoritative_environment(tmp_path, definition)
    swapped = {key: dict(value) for key, value in manifest.items()}
    first, second = definition.tasks[:2]
    swapped[first.task_id], swapped[second.task_id] = swapped[second.task_id], swapped[first.task_id]
    with pytest.raises(PilotContractError, match="disagrees"):
        authority.preflight(definition, swapped, worker_node_id=NODE_ID)
    with pytest.raises(PilotContractError, match="disagrees"):
        authority.preflight(definition, manifest, worker_node_id="foreign-node")


def test_corrupt_authenticated_execution_evidence_fails_closed(tmp_path):
    definition = build_default_pilot_definition()
    authority, manifest, _ = authoritative_environment(tmp_path, definition)
    execution_path = tmp_path / "execution.jsonl"
    execution_path.write_bytes(execution_path.read_bytes()[:-1])
    with pytest.raises(PilotContractError, match="unavailable"):
        authority.preflight(definition, manifest, worker_node_id=NODE_ID)
