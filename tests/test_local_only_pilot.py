from dataclasses import FrozenInstanceError, replace
import math
import json

import pytest

from federation.task_request import AuthorizationLevel
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
    MODEL_ALIAS, MODEL_SHA256, _load_authority, parser,
)


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
        resource_measurements={"prompt_ms": 1000.0, "predicted_ms": 500.0},
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
    assert report.metrics.mean_prompt_throughput == 8.0
    assert report.metrics.mean_generation_throughput == 4.0
    assert report.metrics.total_cloud_cost_usd == 0.0
    assert report.to_dict() == PilotReport.create(
        "pilot-run-1", build_default_pilot_definition(), model(), (record,)).to_dict()
    assert "proof" not in str(report.to_dict()).lower()
    assert "winner" not in str(report.to_dict()).lower()


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
