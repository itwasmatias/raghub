"""Local Equivalence experiment contracts remain descriptive and fail closed."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import json
import math

import pytest

from tools.ai_controller.local_capability_baseline import LocalityType, TaskMeasurement, TaskOutcome
from tools.ai_controller.local_equivalence_experiments import (
    ExperimentArm, ExperimentArmType, ExperimentAttempt, ExperimentComparison,
    ExperimentConflictError, ExperimentContractError, ExperimentCoordinator,
    ExperimentDefinition, ExperimentEvidence, ExperimentRun, ExperimentRunStatus,
    ExperimentTask, VerificationAssessment,
)

NOW = "2026-08-09T14:00:00Z"
ARMS = (
    (ExperimentArmType.CLOUD_FRONTIER_BASELINE, LocalityType.CLOUD),
    (ExperimentArmType.SINGLE_LOCAL, LocalityType.LOCAL),
    (ExperimentArmType.LOCAL_WITH_TOOLS, LocalityType.LOCAL),
    (ExperimentArmType.INDEPENDENT_LOCAL_ATTEMPTS, LocalityType.LOCAL),
    (ExperimentArmType.LOCAL_WITH_VERIFIER, LocalityType.LOCAL),
    (ExperimentArmType.LOCAL_WITH_PERSISTENT_MEMORY, LocalityType.LOCAL),
    (ExperimentArmType.HYBRID_RARE_CLOUD_ESCALATION, LocalityType.HYBRID),
)


def arm(arm_type=ExperimentArmType.SINGLE_LOCAL, locality=LocalityType.LOCAL, arm_id="arm-1"):
    return ExperimentArm(arm_id, arm_type, f"strategy-{arm_id}", locality,
        "descriptive orchestration arm", frozenset({"analysis"}))


def task(task_id="task-1"):
    return ExperimentTask("experiment-1", task_id, "bounded task", "structured result",
        frozenset({"analysis"}), ("task-source-1",))


def definition(arms=None):
    return ExperimentDefinition("experiment-1", NOW, "commodity orchestration recovery",
        tuple(arms or (arm(),)), (task(),))


def measurement(*, arm_value=None, task_id="task-1", worker="worker-1", success=True,
                escalation=0, cost=None, zero=True):
    selected = arm_value or arm()
    return TaskMeasurement("measurement-1", task_id, worker, "a" * 64, NOW,
        "2026-08-09T14:01:00Z", 60.0,
        TaskOutcome.SUCCESS if success else TaskOutcome.FAILURE, success, "verified",
        1, 0, 0, zero, escalation, cost, selected.execution_strategy,
        selected.locality, "opaque-provider" if selected.locality is not LocalityType.LOCAL else None,
        "opaque-model", {"source": "measured"})


def evidence(*, arm_id="arm-1", task_id="task-1"):
    return ExperimentEvidence("experiment-1", arm_id, task_id, "result-1",
        ("evidence-1",), "evaluation-1", "confidence-1", "checkpoint-1", "recovery-1")


def verification(*, arm_id="arm-1", task_id="task-1", run_id="run-1", proof=False):
    return VerificationAssessment("verification-1", "experiment-1", arm_id, task_id,
        run_id, "verifier-1", True, 0.8, ("verification-evidence",),
        "bounded verification", proof)


def attempt(*, selected=None, run_id="run-1", attempt_id="attempt-1",
            measure=None, verify=True, resource=None):
    selected = selected or arm()
    return ExperimentAttempt("experiment-1", selected.arm_id, "task-1", run_id,
        attempt_id, "worker-1", "a" * 64, selected.execution_strategy,
        measure or measurement(arm_value=selected), evidence(arm_id=selected.arm_id),
        verification(arm_id=selected.arm_id, run_id=run_id) if verify else None,
        resource or {"peak_memory_mb": 512.0})


def run(*, selected=None, run_id="run-1", attempts=None):
    selected = selected or arm()
    source = attempts or (attempt(selected=selected, run_id=run_id),)
    return ExperimentRun("experiment-1", selected.arm_id, run_id,
        definition((selected,)).definition_fingerprint(), ExperimentRunStatus.COMPLETED,
        ("task-1",), tuple(source), NOW)


@pytest.mark.parametrize(("arm_type", "locality"), ARMS)
def test_all_required_experiment_arms(arm_type, locality):
    value = arm(arm_type, locality)
    assert value.arm_type is arm_type
    assert value.locality is locality


def test_arm_locality_semantics_fail_closed():
    with pytest.raises(ExperimentContractError):
        arm(ExperimentArmType.SINGLE_LOCAL, LocalityType.CLOUD)
    with pytest.raises(ExperimentContractError):
        arm(ExperimentArmType.HYBRID_RARE_CLOUD_ESCALATION, LocalityType.LOCAL)


def test_definition_fingerprint_is_deterministic_and_ordered():
    arms = tuple(arm(kind, locality, f"arm-{index}") for index, (kind, locality) in enumerate(ARMS))
    first = definition(arms)
    assert first.definition_fingerprint() == definition(tuple(reversed(arms))).definition_fingerprint()


def test_nested_contract_data_is_detached_and_immutable():
    resources = {"nested": {"watts": 20.0}}
    value = attempt(resource=resources)
    fingerprint = value.attempt_fingerprint()
    resources["nested"]["watts"] = 999.0
    assert value.to_dict()["resource_measurements"]["nested"]["watts"] == 20.0
    assert value.attempt_fingerprint() == fingerprint
    with pytest.raises(TypeError):
        value.resource_measurements["new"] = 1


def test_exact_duplicate_run_is_idempotent_and_conflict_rejected():
    coordinator = ExperimentCoordinator(definition())
    original = run()
    assert coordinator.register_run(original) is coordinator.register_run(original)
    conflicting = replace(original, status=ExperimentRunStatus.FAILED)
    with pytest.raises(ExperimentConflictError):
        coordinator.register_run(conflicting)


def test_concurrent_exact_duplicates_create_one_authoritative_run():
    coordinator = ExperimentCoordinator(definition())
    original = run()
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = tuple(pool.map(coordinator.register_run, (original,) * 32))
    assert len({id(value) for value in values}) == 1


@pytest.mark.parametrize("change", [
    {"experiment_id": "foreign"}, {"arm_id": "foreign"}, {"task_id": "foreign"},
    {"capability_profile_fingerprint": "b" * 64},
])
def test_foreign_or_substituted_attempt_provenance_rejected(change):
    original = attempt()
    with pytest.raises(ExperimentContractError):
        ExperimentCoordinator(definition()).register_run(run(attempts=(replace(original, **change),)))


def test_cross_arm_evidence_isolation():
    other = arm(ExperimentArmType.LOCAL_WITH_TOOLS, LocalityType.LOCAL, "arm-2")
    with pytest.raises(ExperimentContractError):
        replace(attempt(), evidence=replace(evidence(), arm_id=other.arm_id))


def test_measurement_fingerprint_substitution_rejected():
    altered = replace(measurement(), worker_id="worker-2")
    with pytest.raises(ExperimentContractError):
        replace(attempt(), measurement=altered)


def test_contradictory_success_outcome_rejected_by_reused_measurement_contract():
    with pytest.raises(ValueError, match="success must match outcome"):
        replace(measurement(), outcome=TaskOutcome.FAILURE)


@pytest.mark.parametrize("field", ["elapsed_seconds", "estimated_cloud_cost_usd"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_measurements_rejected(field, value):
    with pytest.raises(ValueError, match="finite"):
        replace(measurement(), **{field: value})


def test_negative_resource_and_cost_values_rejected():
    with pytest.raises(ExperimentContractError):
        attempt(resource={"cpu_seconds": -1})
    with pytest.raises(ValueError):
        measurement(cost=-0.01, zero=False)
    with pytest.raises(ExperimentContractError):
        attempt(resource={"nested": {"watts": math.nan}})


def test_zero_cloud_cost_semantics_and_local_governance():
    with pytest.raises(ExperimentContractError):
        ExperimentCoordinator(definition()).register_run(run(attempts=(
            attempt(measure=measurement(cost=1.0, zero=True)),)))
    with pytest.raises(ExperimentContractError):
        ExperimentCoordinator(definition()).register_run(run(attempts=(
            attempt(measure=measurement(escalation=1, zero=False, cost=1.0)),)))


def test_hybrid_escalation_and_provider_neutral_cost_accounting():
    selected = arm(ExperimentArmType.HYBRID_RARE_CLOUD_ESCALATION,
        LocalityType.HYBRID)
    measured = measurement(arm_value=selected, escalation=1, cost=0.25, zero=False)
    result = ExperimentCoordinator(definition((selected,))).register_run(
        run(selected=selected, attempts=(attempt(selected=selected, measure=measured),)))
    assert result.metrics.cloud_escalation_count == 1
    assert result.metrics.marginal_cloud_cost_usd == 0.25
    assert "opaque-provider" not in result.metrics.to_dict().values()


def test_verification_confidence_is_distinct_from_correctness_and_not_proof():
    value = verification()
    assert value.correct is True and value.confidence == 0.8
    assert value.is_proof is False
    with pytest.raises(ExperimentContractError, match="proof"):
        verification(proof=True)


def test_inconsistent_run_aggregates_and_duplicate_attempts_rejected():
    duplicate = attempt()
    with pytest.raises(ExperimentContractError):
        ExperimentRun("experiment-1", "arm-1", "run-1", definition().definition_fingerprint(),
            ExperimentRunStatus.COMPLETED, ("task-1",), (duplicate, duplicate), NOW)


def test_independent_attempt_arm_requires_distinct_workers():
    selected = arm(ExperimentArmType.INDEPENDENT_LOCAL_ATTEMPTS, LocalityType.LOCAL)
    with pytest.raises(ExperimentContractError, match="independent"):
        ExperimentCoordinator(definition((selected,))).register_run(run(selected=selected))


def test_verifier_arm_requires_verification_evidence():
    selected = arm(ExperimentArmType.LOCAL_WITH_VERIFIER, LocalityType.LOCAL)
    without = attempt(selected=selected, verify=False)
    with pytest.raises(ExperimentContractError, match="verification"):
        ExperimentCoordinator(definition((selected,))).register_run(
            run(selected=selected, attempts=(without,)))


def test_verifier_disagreement_is_preserved_and_not_counted_correct():
    selected = arm(ExperimentArmType.LOCAL_WITH_VERIFIER, LocalityType.LOCAL)
    first = attempt(selected=selected)
    second_measurement = replace(measurement(arm_value=selected),
        measurement_id="measurement-2", worker_id="worker-2")
    second_verification = replace(verification(), assessment_id="verification-2",
        verifier_id="verifier-2", correct=False, confidence=0.6)
    second_evidence = replace(evidence(), task_result_id="result-2",
        evidence_ids=("evidence-2",))
    second = ExperimentAttempt("experiment-1", "arm-1", "task-1", "run-1",
        "attempt-2", "worker-2", "a" * 64, selected.execution_strategy,
        second_measurement, second_evidence, second_verification,
        {"peak_memory_mb": 512.0})
    registered = ExperimentCoordinator(definition((selected,))).register_run(
        run(selected=selected, attempts=(first, second)))
    assert registered.metrics.correct_task_count == 0
    assert registered.metrics.verification_disagreement_task_count == 1
    assert registered.metrics.mean_verification_confidence == pytest.approx(0.7)


def test_cross_arm_provenance_identity_reuse_rejected():
    first_arm = arm()
    second_arm = arm(ExperimentArmType.LOCAL_WITH_TOOLS, LocalityType.LOCAL, "arm-2")
    shared = definition((first_arm, second_arm))
    coordinator = ExperimentCoordinator(shared)
    coordinator.register_run(replace(run(selected=first_arm),
        definition_fingerprint=shared.definition_fingerprint()))
    reused_measurement = replace(measurement(arm_value=second_arm),
        execution_strategy=second_arm.execution_strategy)
    reused = ExperimentAttempt("experiment-1", "arm-2", "task-1", "run-2",
        "attempt-2", "worker-1", "a" * 64, second_arm.execution_strategy,
        reused_measurement, replace(evidence(arm_id="arm-2")),
        replace(verification(arm_id="arm-2", run_id="run-2")), {})
    with pytest.raises(ExperimentContractError, match="reuses provenance"):
        coordinator.register_run(replace(
            run(selected=second_arm, run_id="run-2", attempts=(reused,)),
            definition_fingerprint=shared.definition_fingerprint()))


def test_comparison_is_deterministic_serializable_and_has_no_winner_claim():
    coordinator = ExperimentCoordinator(definition())
    registered = coordinator.register_run(run())
    first = coordinator.compare((registered,))
    assert first == coordinator.compare((registered,))
    payload = first.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert "winner" not in payload and "proof" not in payload
    with pytest.raises(FrozenInstanceError):
        first.comparison_id = "changed"


def test_definition_mutation_after_coordinator_capture_is_detected():
    source = definition()
    coordinator = ExperimentCoordinator(source)
    object.__setattr__(source, "experiment_id", "mutated")
    with pytest.raises(ExperimentContractError, match="mutated"):
        coordinator.register_run(run())
