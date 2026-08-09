"""
Comprehensive fail-closed tests for Local Equivalence Benchmark Harness v0.1

Tests adversarial scenarios and contract enforcement.
"""

import json
import math
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from tools.ai_controller.local_capability_baseline import (
    LocalityType,
    TaskMeasurement,
    TaskOutcome,
)
from tools.ai_controller.local_equivalence_experiments import (
    ExperimentArm,
    ExperimentArmType,
    ExperimentAttempt,
    ExperimentDefinition,
    ExperimentEvidence,
    ExperimentRun,
    ExperimentRunStatus,
    ExperimentTask,
    VerificationAssessment,
)
from tools.ai_controller.local_equivalence_benchmark import (
    BenchmarkAggregation,
    BenchmarkAttemptResult,
    BenchmarkComparisonReport,
    BenchmarkContractError,
    BenchmarkCoordinator,
    BenchmarkEvaluator,
    BenchmarkExecutionError,
    BenchmarkExecutor,
    BenchmarkRunManifest,
    BenchmarkRunPlan,
    BenchmarkRunResult,
    BenchmarkSuite,
    BenchmarkTaskSpec,
    CorrectnessType,
    EvaluationResult,
    TaskDifficulty,
)


NOW = datetime.now(timezone.utc).isoformat()


def task_spec(task_id="task-1", category="coding", difficulty=TaskDifficulty.MODERATE,
              correctness=CorrectnessType.OBJECTIVE):
    return BenchmarkTaskSpec(
        task_id=task_id,
        category=category,
        description="Write a function that returns the sum of two numbers",
        expected_output="def add(a, b): return a + b",
        difficulty=difficulty,
        required_capabilities=frozenset({"code"}),
        permitted_tools=frozenset({"python"}),
        reference_evidence_ids=("ref-1",),
        evaluation_requirements={"check_syntax": True, "check_correctness": True},
        correctness_type=correctness,
    )


def suite(tasks=None):
    return BenchmarkSuite(
        suite_id="suite-1",
        name="Basic Coding Tasks",
        version="1.0.0",
        created_at=NOW,
        description="Simple coding tasks for testing",
        tasks=tuple(tasks or [task_spec()]),
        suite_metadata={"author": "test"},
    )


def arm(arm_id="arm-1", arm_type=ExperimentArmType.SINGLE_LOCAL):
    locality = (LocalityType.CLOUD if arm_type is ExperimentArmType.CLOUD_FRONTIER_BASELINE
                else LocalityType.HYBRID if arm_type is ExperimentArmType.HYBRID_RARE_CLOUD_ESCALATION
                else LocalityType.LOCAL)
    return ExperimentArm(
        arm_id=arm_id,
        arm_type=arm_type,
        execution_strategy=f"strategy-{arm_id}",
        locality=locality,
        description="Test arm",
        required_capabilities=frozenset({"code"}),
    )


def experiment_task(task_id="task-1", experiment_id="exp-1"):
    return ExperimentTask(
        experiment_id=experiment_id,
        task_id=task_id,
        description="Experiment task",
        expected_result="Success",
        required_capabilities=frozenset({"code"}),
        source_evidence_ids=("source-1",),
    )


def definition(arms=None, tasks=None, experiment_id="exp-1"):
    return ExperimentDefinition(
        experiment_id=experiment_id,
        created_at=NOW,
        purpose="Test experiment",
        arms=tuple(arms or [arm()]),
        tasks=tuple(tasks or [experiment_task(experiment_id=experiment_id)]),
    )


def measurement(task_id="task-1", worker="worker-1", success=True, elapsed=60.0):
    selected = arm()
    return TaskMeasurement(
        measurement_id="meas-1",
        task_id=task_id,
        worker_id=worker,
        profile_fingerprint="a" * 64,
        started_at=NOW,
        completed_at=NOW,
        elapsed_seconds=elapsed,
        outcome=TaskOutcome.SUCCESS if success else TaskOutcome.FAILURE,
        success=success,
        verification_result="passed" if success else "failed",
        attempt_number=1,
        human_intervention_count=0,
        retry_count=0,
        zero_cloud_cost=True,
        cloud_escalation_count=0,
        estimated_cloud_cost_usd=None,
        execution_strategy=selected.execution_strategy,
        locality=selected.locality,
        provider_identifier=None,
        model_identifier="test-model",
        evidence_metadata={},
    )


def evidence_item(arm_id="arm-1", task_id="task-1"):
    return ExperimentEvidence(
        experiment_id="exp-1",
        arm_id=arm_id,
        task_id=task_id,
        task_result_id="result-1",
        evidence_ids=("ev-1",),
    )


def attempt_item(arm_id="arm-1", task_id="task-1", run_id="run-1", attempt_id="attempt-1",
                 measure=None):
    selected = arm(arm_id=arm_id)
    return ExperimentAttempt(
        experiment_id="exp-1",
        arm_id=arm_id,
        task_id=task_id,
        run_id=run_id,
        attempt_id=attempt_id,
        worker_node_id="worker-1",
        capability_profile_fingerprint="a" * 64,
        execution_strategy=selected.execution_strategy,
        measurement=measure or measurement(task_id=task_id),
        evidence=evidence_item(arm_id=arm_id, task_id=task_id),
        verification=None,
        resource_measurements={"cpu_seconds": 10.0},
    )


def run_item(arm_id="arm-1", run_id="run-1", task_ids=None, attempts=None):
    task_ids = task_ids or ("task-1",)
    attempts = attempts or (attempt_item(arm_id=arm_id, run_id=run_id),)
    defn = definition(arms=[arm(arm_id=arm_id)], tasks=[experiment_task(tid) for tid in task_ids])
    return ExperimentRun(
        experiment_id="exp-1",
        arm_id=arm_id,
        run_id=run_id,
        definition_fingerprint=defn.definition_fingerprint(),
        status=ExperimentRunStatus.COMPLETED,
        task_ids=task_ids,
        attempts=tuple(attempts),
        observed_at=NOW,
    )


def evaluation(task_id="task-1", attempt_id="attempt-1", correct=True,
               correctness_type=CorrectnessType.OBJECTIVE):
    return EvaluationResult(
        evaluation_id="eval-1",
        task_id=task_id,
        attempt_id=attempt_id,
        evaluator_id="evaluator-1",
        correct=correct,
        confidence=0.9,
        correctness_type=correctness_type,
        evidence_ids=("eval-ev-1",),
        rationale="Passes all tests",
        is_ground_truth=(correctness_type is CorrectnessType.OBJECTIVE),
    )


class FakeExecutor:
    def execute(self, task_spec, experiment_task, arm, run_id, attempt_id):
        return attempt_item(arm.arm_id, experiment_task.task_id, run_id, attempt_id)


class FakeEvaluator:
    def evaluate(self, task_spec, attempt):
        return evaluation(attempt.task_id, attempt.attempt_id)


# ==================== TASK SPEC TESTS ====================


def test_task_spec_creation_and_immutability():
    spec = task_spec()
    assert spec.task_id == "task-1"
    assert spec.difficulty is TaskDifficulty.MODERATE
    assert spec.correctness_type is CorrectnessType.OBJECTIVE
    with pytest.raises(Exception):  # Frozen dataclass
        spec.task_id = "changed"


def test_task_spec_invalid_identifier():
    with pytest.raises(BenchmarkContractError, match="identifier"):
        task_spec(task_id="")
    with pytest.raises(BenchmarkContractError, match="identifier"):
        task_spec(task_id="invalid space")


def test_task_spec_invalid_difficulty():
    with pytest.raises(BenchmarkContractError):
        task_spec(difficulty="invalid")


def test_task_spec_nested_data_immutability():
    spec = task_spec()
    with pytest.raises(TypeError):
        spec.evaluation_requirements["new_key"] = "value"


# ==================== SUITE TESTS ====================


def test_suite_deterministic_fingerprint():
    s1 = suite([task_spec("task-1"), task_spec("task-2")])
    s2 = suite([task_spec("task-2"), task_spec("task-1")])
    assert s1.suite_fingerprint() == s2.suite_fingerprint()


def test_suite_duplicate_task_ids_rejected():
    with pytest.raises(BenchmarkContractError, match="unique"):
        suite([task_spec("task-1"), task_spec("task-1")])


def test_suite_empty_tasks_rejected():
    with pytest.raises(BenchmarkContractError, match="unique"):
        BenchmarkSuite(
            suite_id="suite-1",
            name="Test",
            version="1.0",
            created_at=NOW,
            description="Test",
            tasks=(),
            suite_metadata={},
        )


def test_suite_serializable():
    s = suite()
    payload = s.to_dict()
    assert json.loads(json.dumps(payload)) == payload


# ==================== RUN PLAN TESTS ====================


def test_run_plan_deterministic_fingerprint():
    defn = definition()
    s = suite()
    plan1 = BenchmarkRunPlan(
        plan_id="plan-1",
        suite_fingerprint=s.suite_fingerprint(),
        experiment_fingerprint=defn.definition_fingerprint(),
        arm_ids=("arm-1",),
        task_ids=("task-1",),
        repetitions_per_task=3,
        random_seed=42,
        worker_requirements={},
        evaluator_config={},
        resource_measurement_config={},
        created_at=NOW,
        software_version={"version": "1.0"},
    )
    plan2 = BenchmarkRunPlan(
        plan_id="plan-2",
        suite_fingerprint=s.suite_fingerprint(),
        experiment_fingerprint=defn.definition_fingerprint(),
        arm_ids=("arm-1",),
        task_ids=("task-1",),
        repetitions_per_task=3,
        random_seed=42,
        worker_requirements={},
        evaluator_config={},
        resource_measurement_config={},
        created_at=NOW,
        software_version={"version": "1.0"},
    )
    # Different plan_id but same content should have same fingerprint
    # (plan_id is excluded from fingerprint)
    assert plan1.plan_fingerprint() == plan2.plan_fingerprint()


def test_run_plan_changed_seed_changes_fingerprint():
    defn = definition()
    s = suite()
    plan1 = BenchmarkRunPlan(
        plan_id="plan-1",
        suite_fingerprint=s.suite_fingerprint(),
        experiment_fingerprint=defn.definition_fingerprint(),
        arm_ids=("arm-1",),
        task_ids=("task-1",),
        repetitions_per_task=1,
        random_seed=42,
        worker_requirements={},
        evaluator_config={},
        resource_measurement_config={},
        created_at=NOW,
        software_version={},
    )
    plan2 = replace(plan1, random_seed=43)
    assert plan1.plan_fingerprint() != plan2.plan_fingerprint()


def test_run_plan_invalid_repetitions():
    defn = definition()
    s = suite()
    with pytest.raises(BenchmarkContractError, match="positive"):
        BenchmarkRunPlan(
            plan_id="plan-1",
            suite_fingerprint=s.suite_fingerprint(),
            experiment_fingerprint=defn.definition_fingerprint(),
            arm_ids=("arm-1",),
            task_ids=("task-1",),
            repetitions_per_task=0,
            random_seed=None,
            worker_requirements={},
            evaluator_config={},
            resource_measurement_config={},
            created_at=NOW,
            software_version={},
        )


def test_run_plan_invalid_fingerprint():
    defn = definition()
    with pytest.raises(BenchmarkContractError, match="SHA-256"):
        BenchmarkRunPlan(
            plan_id="plan-1",
            suite_fingerprint="invalid",
            experiment_fingerprint=defn.definition_fingerprint(),
            arm_ids=("arm-1",),
            task_ids=("task-1",),
            repetitions_per_task=1,
            random_seed=None,
            worker_requirements={},
            evaluator_config={},
            resource_measurement_config={},
            created_at=NOW,
            software_version={},
        )


# ==================== EVALUATION RESULT TESTS ====================


def test_evaluation_objective_vs_judgment_distinction():
    objective = evaluation(correctness_type=CorrectnessType.OBJECTIVE)
    assert objective.correctness_type is CorrectnessType.OBJECTIVE
    assert objective.is_ground_truth is True

    judged = evaluation(correctness_type=CorrectnessType.EVALUATOR_JUDGMENT)
    assert judged.correctness_type is CorrectnessType.EVALUATOR_JUDGMENT
    assert judged.is_ground_truth is False


def test_evaluation_ground_truth_requires_objective():
    with pytest.raises(BenchmarkContractError, match="ground truth"):
        EvaluationResult(
            evaluation_id="eval-1",
            task_id="task-1",
            attempt_id="attempt-1",
            evaluator_id="evaluator-1",
            correct=True,
            confidence=0.9,
            correctness_type=CorrectnessType.EVALUATOR_JUDGMENT,
            evidence_ids=("ev-1",),
            rationale="Test",
            is_ground_truth=True,
        )


def test_evaluation_confidence_bounds():
    with pytest.raises(BenchmarkContractError, match="confidence"):
        evaluation(correctness_type=CorrectnessType.OBJECTIVE).to_dict()
        EvaluationResult(
            evaluation_id="eval-1",
            task_id="task-1",
            attempt_id="attempt-1",
            evaluator_id="evaluator-1",
            correct=True,
            confidence=1.5,
            correctness_type=CorrectnessType.OBJECTIVE,
            evidence_ids=("ev-1",),
            rationale="Test",
        )


def test_evaluation_negative_confidence_rejected():
    with pytest.raises(BenchmarkContractError):
        EvaluationResult(
            evaluation_id="eval-1",
            task_id="task-1",
            attempt_id="attempt-1",
            evaluator_id="evaluator-1",
            correct=True,
            confidence=-0.1,
            correctness_type=CorrectnessType.OBJECTIVE,
            evidence_ids=("ev-1",),
            rationale="Test",
        )


# ==================== BENCHMARK ATTEMPT RESULT TESTS ====================


def test_benchmark_attempt_result_validation():
    att = attempt_item()
    ev = evaluation()
    result = BenchmarkAttemptResult(attempt=att, evaluation=ev)
    assert result.attempt.task_id == result.evaluation.task_id


def test_benchmark_attempt_result_task_id_mismatch():
    att = attempt_item(task_id="task-1")
    ev = evaluation(task_id="task-2")
    with pytest.raises(BenchmarkContractError, match="task_id mismatch"):
        BenchmarkAttemptResult(attempt=att, evaluation=ev)


def test_benchmark_attempt_result_attempt_id_mismatch():
    att = attempt_item(attempt_id="attempt-1")
    ev = evaluation(attempt_id="attempt-2")
    with pytest.raises(BenchmarkContractError, match="attempt_id mismatch"):
        BenchmarkAttemptResult(attempt=att, evaluation=ev)


# ==================== BENCHMARK RUN RESULT TESTS ====================


def test_benchmark_run_result_validation():
    r = run_item()
    att = attempt_item()
    ev = evaluation()
    result = BenchmarkAttemptResult(attempt=att, evaluation=ev)
    defn = definition()
    run_result = BenchmarkRunResult(
        run=r,
        task_results=(result,),
        plan_fingerprint=defn.definition_fingerprint(),
    )
    assert len(run_result.task_results) == 1


def test_benchmark_run_result_foreign_task_rejected():
    r = run_item(arm_id="arm-1", run_id="run-1")
    # Create a valid attempt but for a different run
    selected_arm = arm(arm_id="arm-1")
    meas = TaskMeasurement(
        measurement_id="meas-2",
        task_id="task-1",
        worker_id="worker-1",
        profile_fingerprint="a" * 64,
        started_at=NOW,
        completed_at=NOW,
        elapsed_seconds=60.0,
        outcome=TaskOutcome.SUCCESS,
        success=True,
        verification_result="passed",
        attempt_number=1,
        human_intervention_count=0,
        retry_count=0,
        zero_cloud_cost=True,
        cloud_escalation_count=0,
        estimated_cloud_cost_usd=None,
        execution_strategy=selected_arm.execution_strategy,
        locality=selected_arm.locality,
        provider_identifier=None,
        model_identifier="test-model",
        evidence_metadata={},
    )
    ev_item = ExperimentEvidence(
        experiment_id="exp-1",
        arm_id="arm-1",
        task_id="task-1",
        task_result_id="result-2",
        evidence_ids=("ev-2",),
    )
    att = ExperimentAttempt(
        experiment_id="exp-1",
        arm_id="arm-1",
        task_id="task-1",
        run_id="run-2",  # Different run_id
        attempt_id="attempt-2",
        worker_node_id="worker-1",
        capability_profile_fingerprint="a" * 64,
        execution_strategy=selected_arm.execution_strategy,
        measurement=meas,
        evidence=ev_item,
        verification=None,
        resource_measurements={"cpu_seconds": 10.0},
    )
    ev = evaluation(attempt_id="attempt-2")
    result = BenchmarkAttemptResult(attempt=att, evaluation=ev)
    defn = definition()
    with pytest.raises(BenchmarkContractError, match="foreign"):
        BenchmarkRunResult(
            run=r,
            task_results=(result,),
            plan_fingerprint=defn.definition_fingerprint(),
        )


# ==================== AGGREGATION TESTS ====================


def test_aggregation_success_and_correctness_rates():
    plan_fp = "a" * 64
    agg = BenchmarkAggregation(
        aggregation_id="agg-1",
        plan_fingerprint=plan_fp,
        arm_id="arm-1",
        task_count=10,
        attempt_count=20,
        successful_attempts=18,
        correct_attempts=15,
        objective_correct_count=10,
        judged_correct_count=5,
        mean_confidence=0.85,
        elapsed_seconds_total=1200.0,
        elapsed_seconds_mean=60.0,
        elapsed_seconds_median=58.0,
        retry_count_total=2,
        human_intervention_count_total=0,
        cloud_escalation_count_total=0,
        marginal_cloud_cost_total_usd=0.0,
        resource_totals={},
    )
    payload = agg.to_dict()
    assert payload["success_rate"] == pytest.approx(0.9)
    assert payload["correctness_rate"] == pytest.approx(0.75)


def test_aggregation_negative_values_rejected():
    plan_fp = "a" * 64
    with pytest.raises(BenchmarkContractError, match="non-negative"):
        BenchmarkAggregation(
            aggregation_id="agg-1",
            plan_fingerprint=plan_fp,
            arm_id="arm-1",
            task_count=-1,
            attempt_count=10,
            successful_attempts=8,
            correct_attempts=6,
            objective_correct_count=4,
            judged_correct_count=2,
            mean_confidence=0.8,
            elapsed_seconds_total=600.0,
            elapsed_seconds_mean=60.0,
            elapsed_seconds_median=58.0,
            retry_count_total=0,
            human_intervention_count_total=0,
            cloud_escalation_count_total=0,
            marginal_cloud_cost_total_usd=0.0,
            resource_totals={},
        )


def test_aggregation_confidence_out_of_bounds():
    plan_fp = "a" * 64
    with pytest.raises(BenchmarkContractError, match="confidence"):
        BenchmarkAggregation(
            aggregation_id="agg-1",
            plan_fingerprint=plan_fp,
            arm_id="arm-1",
            task_count=10,
            attempt_count=10,
            successful_attempts=8,
            correct_attempts=6,
            objective_correct_count=4,
            judged_correct_count=2,
            mean_confidence=1.5,
            elapsed_seconds_total=600.0,
            elapsed_seconds_mean=60.0,
            elapsed_seconds_median=58.0,
            retry_count_total=0,
            human_intervention_count_total=0,
            cloud_escalation_count_total=0,
            marginal_cloud_cost_total_usd=0.0,
            resource_totals={},
        )


# ==================== COMPARISON REPORT TESTS ====================


def test_comparison_report_requires_aggregations():
    with pytest.raises(BenchmarkContractError, match="at least one"):
        BenchmarkComparisonReport(
            comparison_id="comp-1",
            plan_fingerprint="a" * 64,
            aggregations=(),
            comparison_metadata={},
        )


def test_comparison_report_duplicate_arm_rejected():
    plan_fp = "a" * 64
    agg1 = BenchmarkAggregation(
        aggregation_id="agg-1",
        plan_fingerprint=plan_fp,
        arm_id="arm-1",
        task_count=10,
        attempt_count=10,
        successful_attempts=8,
        correct_attempts=6,
        objective_correct_count=4,
        judged_correct_count=2,
        mean_confidence=0.8,
        elapsed_seconds_total=600.0,
        elapsed_seconds_mean=60.0,
        elapsed_seconds_median=58.0,
        retry_count_total=0,
        human_intervention_count_total=0,
        cloud_escalation_count_total=0,
        marginal_cloud_cost_total_usd=0.0,
        resource_totals={},
    )
    agg2 = replace(agg1, aggregation_id="agg-2")
    with pytest.raises(BenchmarkContractError, match="duplicate arm"):
        BenchmarkComparisonReport(
            comparison_id="comp-1",
            plan_fingerprint=plan_fp,
            aggregations=(agg1, agg2),
            comparison_metadata={},
        )


def test_comparison_report_incompatible_plans_rejected():
    plan_fp1 = "a" * 64
    plan_fp2 = "b" * 64
    agg1 = BenchmarkAggregation(
        aggregation_id="agg-1",
        plan_fingerprint=plan_fp1,
        arm_id="arm-1",
        task_count=10,
        attempt_count=10,
        successful_attempts=8,
        correct_attempts=6,
        objective_correct_count=4,
        judged_correct_count=2,
        mean_confidence=0.8,
        elapsed_seconds_total=600.0,
        elapsed_seconds_mean=60.0,
        elapsed_seconds_median=58.0,
        retry_count_total=0,
        human_intervention_count_total=0,
        cloud_escalation_count_total=0,
        marginal_cloud_cost_total_usd=0.0,
        resource_totals={},
    )
    agg2 = replace(agg1, aggregation_id="agg-2", arm_id="arm-2", plan_fingerprint=plan_fp2)
    with pytest.raises(BenchmarkContractError, match="compatible plan fingerprints"):
        BenchmarkComparisonReport(
            comparison_id="comp-1",
            plan_fingerprint=plan_fp1,
            aggregations=(agg1, agg2),
            comparison_metadata={},
        )


def test_comparison_report_serializable_no_winner_claim():
    plan_fp = "a" * 64
    agg1 = BenchmarkAggregation(
        aggregation_id="agg-1",
        plan_fingerprint=plan_fp,
        arm_id="arm-1",
        task_count=10,
        attempt_count=10,
        successful_attempts=8,
        correct_attempts=6,
        objective_correct_count=4,
        judged_correct_count=2,
        mean_confidence=0.8,
        elapsed_seconds_total=600.0,
        elapsed_seconds_mean=60.0,
        elapsed_seconds_median=58.0,
        retry_count_total=0,
        human_intervention_count_total=0,
        cloud_escalation_count_total=0,
        marginal_cloud_cost_total_usd=0.0,
        resource_totals={},
    )
    agg2 = replace(agg1, aggregation_id="agg-2", arm_id="arm-2")
    report = BenchmarkComparisonReport(
        comparison_id="comp-1",
        plan_fingerprint=plan_fp,
        aggregations=(agg1, agg2),
        comparison_metadata={},
    )
    payload = report.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert "winner" not in payload
    assert "best" not in payload
    assert "proof" not in payload


# ==================== MANIFEST TESTS ====================


def test_manifest_deterministic_fingerprint():
    defn = definition()
    s = suite()
    plan = BenchmarkRunPlan(
        plan_id="plan-1",
        suite_fingerprint=s.suite_fingerprint(),
        experiment_fingerprint=defn.definition_fingerprint(),
        arm_ids=("arm-1",),
        task_ids=("task-1",),
        repetitions_per_task=1,
        random_seed=42,
        worker_requirements={},
        evaluator_config={},
        resource_measurement_config={},
        created_at=NOW,
        software_version={},
    )
    manifest = BenchmarkRunManifest(
        manifest_id="manifest-1",
        plan=plan,
        suite_fingerprint=s.suite_fingerprint(),
        experiment_fingerprint=defn.definition_fingerprint(),
        created_at=NOW,
        manifest_metadata={"run_by": "test"},
    )
    assert manifest.manifest_fingerprint() == manifest.manifest_fingerprint()


def test_manifest_fingerprint_substitution_rejected():
    defn = definition()
    s = suite()
    plan = BenchmarkRunPlan(
        plan_id="plan-1",
        suite_fingerprint=s.suite_fingerprint(),
        experiment_fingerprint=defn.definition_fingerprint(),
        arm_ids=("arm-1",),
        task_ids=("task-1",),
        repetitions_per_task=1,
        random_seed=None,
        worker_requirements={},
        evaluator_config={},
        resource_measurement_config={},
        created_at=NOW,
        software_version={},
    )
    with pytest.raises(BenchmarkContractError, match="mismatch"):
        BenchmarkRunManifest(
            manifest_id="manifest-1",
            plan=plan,
            suite_fingerprint="b" * 64,
            experiment_fingerprint=defn.definition_fingerprint(),
            created_at=NOW,
            manifest_metadata={},
        )


# ==================== COORDINATOR TESTS ====================


def test_coordinator_create_plan():
    s = suite()
    defn = definition()
    coordinator = BenchmarkCoordinator(s, defn, FakeExecutor(), FakeEvaluator())
    plan = coordinator.create_plan(
        plan_id="plan-1",
        arm_ids=("arm-1",),
        task_ids=("task-1",),
        repetitions_per_task=3,
        random_seed=42,
    )
    assert plan.plan_id == "plan-1"
    assert plan.repetitions_per_task == 3
    assert plan.random_seed == 42


def test_coordinator_create_manifest():
    s = suite()
    defn = definition()
    coordinator = BenchmarkCoordinator(s, defn, FakeExecutor(), FakeEvaluator())
    plan = coordinator.create_plan(
        plan_id="plan-1",
        arm_ids=("arm-1",),
        task_ids=("task-1",),
    )
    manifest = coordinator.create_manifest("manifest-1", plan)
    assert manifest.manifest_id == "manifest-1"
    assert manifest.plan.plan_id == "plan-1"


def test_coordinator_execute_plan_not_implemented():
    s = suite()
    defn = definition()
    coordinator = BenchmarkCoordinator(s, defn, FakeExecutor(), FakeEvaluator())
    plan = coordinator.create_plan(
        plan_id="plan-1",
        arm_ids=("arm-1",),
        task_ids=("task-1",),
    )
    with pytest.raises(BenchmarkExecutionError, match="not yet implemented"):
        coordinator.execute_plan(plan, "arm-1")


def test_coordinator_plan_fingerprint_mismatch_rejected():
    s = suite()
    defn = definition()
    coordinator = BenchmarkCoordinator(s, defn, FakeExecutor(), FakeEvaluator())
    other_defn = definition(experiment_id="exp-2")
    plan = BenchmarkRunPlan(
        plan_id="plan-1",
        suite_fingerprint=s.suite_fingerprint(),
        experiment_fingerprint=other_defn.definition_fingerprint(),
        arm_ids=("arm-1",),
        task_ids=("task-1",),
        repetitions_per_task=1,
        random_seed=None,
        worker_requirements={},
        evaluator_config={},
        resource_measurement_config={},
        created_at=NOW,
        software_version={},
    )
    with pytest.raises(BenchmarkContractError, match="fingerprints do not match"):
        coordinator.execute_plan(plan, "arm-1")


def test_coordinator_aggregate_incompatible_plans_rejected():
    s = suite()
    defn = definition()
    coordinator = BenchmarkCoordinator(s, defn, FakeExecutor(), FakeEvaluator())

    r1 = run_item()
    att1 = attempt_item()
    ev1 = evaluation()
    result1 = BenchmarkAttemptResult(attempt=att1, evaluation=ev1)
    run_result1 = BenchmarkRunResult(
        run=r1,
        task_results=(result1,),
        plan_fingerprint="a" * 64,
    )

    r2 = run_item(run_id="run-2")
    att2 = attempt_item(run_id="run-2", attempt_id="attempt-2")
    ev2 = evaluation(attempt_id="attempt-2")
    result2 = BenchmarkAttemptResult(attempt=att2, evaluation=ev2)
    run_result2 = BenchmarkRunResult(
        run=r2,
        task_results=(result2,),
        plan_fingerprint="b" * 64,
    )

    with pytest.raises(BenchmarkContractError, match="incompatible plan fingerprints"):
        coordinator.aggregate("a" * 64, "arm-1", (run_result1, run_result2))


def test_coordinator_aggregate_incompatible_arms_rejected():
    s = suite()
    defn = definition(arms=[arm("arm-1"), arm("arm-2")])
    coordinator = BenchmarkCoordinator(s, defn, FakeExecutor(), FakeEvaluator())

    plan_fp = "a" * 64
    r1 = run_item(arm_id="arm-1")
    att1 = attempt_item(arm_id="arm-1")
    ev1 = evaluation()
    result1 = BenchmarkAttemptResult(attempt=att1, evaluation=ev1)
    run_result1 = BenchmarkRunResult(
        run=r1,
        task_results=(result1,),
        plan_fingerprint=plan_fp,
    )

    # Create valid attempt for arm-2
    selected_arm2 = arm(arm_id="arm-2")
    meas2 = TaskMeasurement(
        measurement_id="meas-2",
        task_id="task-1",
        worker_id="worker-1",
        profile_fingerprint="a" * 64,
        started_at=NOW,
        completed_at=NOW,
        elapsed_seconds=60.0,
        outcome=TaskOutcome.SUCCESS,
        success=True,
        verification_result="passed",
        attempt_number=1,
        human_intervention_count=0,
        retry_count=0,
        zero_cloud_cost=True,
        cloud_escalation_count=0,
        estimated_cloud_cost_usd=None,
        execution_strategy=selected_arm2.execution_strategy,
        locality=selected_arm2.locality,
        provider_identifier=None,
        model_identifier="test-model",
        evidence_metadata={},
    )
    ev_item2 = ExperimentEvidence(
        experiment_id="exp-1",
        arm_id="arm-2",
        task_id="task-1",
        task_result_id="result-2",
        evidence_ids=("ev-2",),
    )
    att2 = ExperimentAttempt(
        experiment_id="exp-1",
        arm_id="arm-2",
        task_id="task-1",
        run_id="run-2",
        attempt_id="attempt-2",
        worker_node_id="worker-1",
        capability_profile_fingerprint="a" * 64,
        execution_strategy=selected_arm2.execution_strategy,
        measurement=meas2,
        evidence=ev_item2,
        verification=None,
        resource_measurements={"cpu_seconds": 10.0},
    )

    r2 = run_item(arm_id="arm-2", run_id="run-2", attempts=(att2,))
    ev2 = evaluation(attempt_id="attempt-2")
    result2 = BenchmarkAttemptResult(attempt=att2, evaluation=ev2)
    run_result2 = BenchmarkRunResult(
        run=r2,
        task_results=(result2,),
        plan_fingerprint=plan_fp,
    )

    with pytest.raises(BenchmarkContractError, match="incompatible arm_ids"):
        coordinator.aggregate(plan_fp, "arm-1", (run_result1, run_result2))


def test_coordinator_aggregate_calculates_metrics():
    s = suite()
    defn = definition()
    coordinator = BenchmarkCoordinator(s, defn, FakeExecutor(), FakeEvaluator())

    plan_fp = "a" * 64

    # Create two successful attempts
    r1 = run_item()
    att1 = attempt_item(measure=measurement(success=True, elapsed=60.0))
    ev1 = evaluation(correct=True)
    result1 = BenchmarkAttemptResult(attempt=att1, evaluation=ev1)
    run_result1 = BenchmarkRunResult(
        run=r1,
        task_results=(result1,),
        plan_fingerprint=plan_fp,
    )

    # Create one failed attempt
    r2 = run_item(run_id="run-2")
    meas2 = replace(measurement(success=False, elapsed=30.0), measurement_id="meas-2")
    att2 = attempt_item(run_id="run-2", attempt_id="attempt-2", measure=meas2)
    ev2 = evaluation(attempt_id="attempt-2", correct=False)
    result2 = BenchmarkAttemptResult(attempt=att2, evaluation=ev2)
    run_result2 = BenchmarkRunResult(
        run=r2,
        task_results=(result2,),
        plan_fingerprint=plan_fp,
    )

    agg = coordinator.aggregate(plan_fp, "arm-1", (run_result1, run_result2))

    assert agg.attempt_count == 2
    assert agg.successful_attempts == 1
    assert agg.correct_attempts == 1
    assert agg.elapsed_seconds_total == 90.0
    assert agg.elapsed_seconds_mean == pytest.approx(45.0)


def test_coordinator_compare_creates_report():
    s = suite()
    defn = definition()
    coordinator = BenchmarkCoordinator(s, defn, FakeExecutor(), FakeEvaluator())

    plan_fp = "a" * 64
    agg1 = BenchmarkAggregation(
        aggregation_id="agg-1",
        plan_fingerprint=plan_fp,
        arm_id="arm-1",
        task_count=10,
        attempt_count=10,
        successful_attempts=8,
        correct_attempts=6,
        objective_correct_count=4,
        judged_correct_count=2,
        mean_confidence=0.8,
        elapsed_seconds_total=600.0,
        elapsed_seconds_mean=60.0,
        elapsed_seconds_median=58.0,
        retry_count_total=0,
        human_intervention_count_total=0,
        cloud_escalation_count_total=0,
        marginal_cloud_cost_total_usd=0.0,
        resource_totals={},
    )
    agg2 = replace(agg1, aggregation_id="agg-2", arm_id="arm-2")

    report = coordinator.compare("comp-1", plan_fp, (agg1, agg2))
    assert report.comparison_id == "comp-1"
    assert len(report.aggregations) == 2


# ==================== PROTOCOL TESTS ====================


def test_fake_executor_implements_protocol():
    executor: BenchmarkExecutor = FakeExecutor()
    assert callable(executor.execute)


def test_fake_evaluator_implements_protocol():
    evaluator: BenchmarkEvaluator = FakeEvaluator()
    assert callable(evaluator.evaluate)
