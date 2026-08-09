"""Focused contracts for Research Evaluation & Synthesis Runtime v0.1."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from research_mission import (
    ChallengerAssessment,
    JudgeDecision,
    JudgeOutcome,
    ResearchEvaluationContractError,
    ResearchEvaluationIncompleteError,
    ResearchEvaluationRuntime,
    ResearchEvaluationStatus,
    ResearchEvidence,
    ResearchMission,
    ResearchMissionPlan,
    ResearchMissionPlanner,
    ResearchMissionResultCoordinator,
    ResearchMissionStatus,
    ResearchRole,
    ResearchTaskResult,
    ResearchTaskResultStatus,
)


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
CLAIM = "The supplied observation supports the bounded claim."
CHALLENGED = "The bounded claim depends on one observation."
WEAKNESS = "The sample is limited."
CONTRADICTION = "A supplied observation points in the opposite direction."
MISSING = "Independent replication is absent."
ALTERNATIVE = "Measurement drift could explain the observation."


def mission_for(mission_id="mission-evaluation"):
    mission = ResearchMission(
        mission_id=mission_id,
        objective="Evaluate supplied research evidence",
        research_context={"scope": "supplied evidence only"},
    )
    mission.plan = ResearchMissionPlanner().plan(mission)
    mission.status = ResearchMissionStatus.ROUTED
    return mission


def task_for(mission, role):
    return next(task for task in mission.plan.tasks if task.role is role)


def evidence_for(task, suffix, summary):
    return ResearchEvidence(
        evidence_id=f"evidence-{suffix}",
        mission_id=task.mission_id,
        task_id=task.task_id,
        evidence_type="supplied-record",
        source=f"source-{suffix}",
        summary=summary,
        observed_at=NOW - timedelta(hours=1),
        reference=f"input://{suffix}",
        metadata={"ordinal": suffix},
    )


def result_for(task, *, outcome=JudgeOutcome.ACCEPTED, accepted=True):
    challenge = None
    decision = None
    summary = CLAIM if task.role is ResearchRole.RESEARCHER else f"{task.role.value} result"
    if task.role is ResearchRole.CHALLENGER:
        challenge = ChallengerAssessment(
            challenged_findings=(CHALLENGED,),
            weaknesses=(WEAKNESS,),
            contradictions=(CONTRADICTION,),
            missing_evidence=(MISSING,),
            alternative_explanations=(ALTERNATIVE,),
        )
    if task.role is ResearchRole.JUDGE:
        accepted_findings = (CLAIM, CHALLENGED) if accepted else (CHALLENGED,)
        decision = JudgeDecision(
            outcome=outcome,
            rationale="Every supplied challenge is addressed explicitly.",
            accepted_findings=accepted_findings,
            rejected_findings=(WEAKNESS,),
            contested_findings=(CONTRADICTION,),
            unresolved_questions=(MISSING, ALTERNATIVE),
            confidence=0.7,
        )
    return ResearchTaskResult(
        mission_id=task.mission_id,
        task_id=task.task_id,
        role=task.role,
        status=ResearchTaskResultStatus.COMPLETED,
        summary=summary,
        produced_at=NOW,
        challenger_assessment=challenge,
        judge_decision=decision,
    )


def completed_runtime(*, outcome=JudgeOutcome.ACCEPTED, accepted=True):
    mission = mission_for()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    challenger = task_for(mission, ResearchRole.CHALLENGER)
    judge = task_for(mission, ResearchRole.JUDGE)
    coordinator.register_result(
        result_for(researcher),
        evidence=(evidence_for(researcher, "researcher", "Research source evidence"),),
    )
    coordinator.register_result(
        result_for(challenger),
        evidence=(evidence_for(challenger, "challenger", "Challenge source evidence"),),
    )
    coordinator.register_result(
        result_for(judge, outcome=outcome, accepted=accepted),
        evidence=(evidence_for(judge, "judge", "Judge source evidence"),),
    )
    return mission, coordinator, ResearchEvaluationRuntime(coordinator)


def test_researcher_claim_and_evidence_are_preserved():
    _, _, runtime = completed_runtime()
    evaluation = runtime.evaluate()
    assert evaluation.claims[0].statement == CLAIM
    assert evaluation.claims[0].evidence_ids == ("evidence-researcher",)
    assert evaluation.researcher_task_ids == ("mission-evaluation-researcher-1",)


def test_challenger_and_contradiction_are_explicit_provenance():
    _, _, runtime = completed_runtime()
    evaluation = runtime.evaluate()
    assert evaluation.challenge.evidence_ids == ("evidence-challenger",)
    assert evaluation.challenge.challenged_claims == (CHALLENGED,)
    assert evaluation.contradictions[0].statement == CONTRADICTION
    assert evaluation.synthesis.contradiction_ids == (
        evaluation.contradictions[0].contradiction_id,
    )


def test_judge_eligibility_and_synthesis_include_only_eligible_evidence():
    _, _, runtime = completed_runtime()
    evaluation = runtime.evaluate()
    assert evaluation.synthesis.status is ResearchEvaluationStatus.ELIGIBLE
    assert evaluation.judge_decision.eligible_evidence_ids == (
        "evidence-researcher",
    )
    assert evaluation.synthesis.included_evidence_ids == (
        "evidence-researcher",
    )
    assert "evidence-challenger" not in evaluation.synthesis.included_evidence_ids
    assert "evidence-judge" not in evaluation.synthesis.included_evidence_ids


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (JudgeOutcome.REJECTED, ResearchEvaluationStatus.INELIGIBLE),
        (JudgeOutcome.CONTESTED, ResearchEvaluationStatus.CONTESTED),
        (JudgeOutcome.INCONCLUSIVE, ResearchEvaluationStatus.INCONCLUSIVE),
    ],
)
def test_nonaccepted_judge_outcome_excludes_research_evidence(outcome, expected):
    _, _, runtime = completed_runtime(outcome=outcome, accepted=False)
    evaluation = runtime.evaluate()
    assert evaluation.synthesis.status is expected
    assert evaluation.synthesis.eligible_claims == ()
    assert evaluation.synthesis.included_evidence_ids == ()
    assert evaluation.synthesis.excluded_evidence_ids == ("evidence-researcher",)


def test_accepted_outcome_without_explicit_claim_acceptance_fails_closed():
    _, _, runtime = completed_runtime(accepted=False)
    evaluation = runtime.evaluate()
    assert evaluation.synthesis.status is ResearchEvaluationStatus.INELIGIBLE
    assert evaluation.synthesis.included_evidence_ids == ()


def test_missing_challenger_or_judge_result_fails_closed():
    mission = mission_for()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    coordinator.register_result(
        result_for(researcher),
        evidence=(evidence_for(researcher, "researcher", "Research evidence"),),
    )
    with pytest.raises(ResearchEvaluationIncompleteError):
        ResearchEvaluationRuntime(coordinator).evaluate()


def test_missing_required_role_evidence_fails_closed():
    mission = mission_for()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    challenger = task_for(mission, ResearchRole.CHALLENGER)
    judge = task_for(mission, ResearchRole.JUDGE)
    coordinator.register_result(result_for(researcher))
    coordinator.register_result(
        result_for(challenger),
        evidence=(evidence_for(challenger, "challenger", "Challenge evidence"),),
    )
    coordinator.register_result(
        result_for(judge),
        evidence=(evidence_for(judge, "judge", "Judge evidence"),),
    )
    with pytest.raises(ResearchEvaluationIncompleteError, match="role evidence"):
        ResearchEvaluationRuntime(coordinator).evaluate()


def test_judge_cannot_silently_ignore_challenger_statement():
    mission, coordinator, _ = completed_runtime()
    judge = task_for(mission, ResearchRole.JUDGE)
    ignored = result_for(judge)
    object.__setattr__(
        ignored,
        "judge_decision",
        JudgeDecision(
            outcome=JudgeOutcome.ACCEPTED,
            rationale="Incomplete disposition",
            accepted_findings=(CLAIM,),
        ),
    )
    # A separate authoritative coordinator proves evaluation rejects the supplied
    # judge contract rather than mutating already-registered evidence.
    alternate = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    challenger = task_for(mission, ResearchRole.CHALLENGER)
    alternate.register_result(
        result_for(researcher),
        evidence=(evidence_for(researcher, "researcher", "Research evidence"),),
    )
    alternate.register_result(
        result_for(challenger),
        evidence=(evidence_for(challenger, "challenger", "Challenge evidence"),),
    )
    alternate.register_result(
        ignored,
        evidence=(evidence_for(judge, "judge", "Judge evidence"),),
    )
    with pytest.raises(ResearchEvaluationContractError, match="does not address"):
        ResearchEvaluationRuntime(alternate).evaluate()


def test_conflicting_judge_dispositions_fail_closed():
    mission = mission_for()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    challenger = task_for(mission, ResearchRole.CHALLENGER)
    judge = task_for(mission, ResearchRole.JUDGE)
    coordinator.register_result(
        result_for(researcher),
        evidence=(evidence_for(researcher, "researcher", "Research evidence"),),
    )
    coordinator.register_result(
        result_for(challenger),
        evidence=(evidence_for(challenger, "challenger", "Challenge evidence"),),
    )
    conflicting = ResearchTaskResult(
        mission_id=judge.mission_id,
        task_id=judge.task_id,
        role=ResearchRole.JUDGE,
        status=ResearchTaskResultStatus.COMPLETED,
        summary="judge result",
        produced_at=NOW,
        judge_decision=JudgeDecision(
            outcome=JudgeOutcome.ACCEPTED,
            rationale="Contradictory disposition",
            accepted_findings=(CLAIM, CHALLENGED, WEAKNESS),
            rejected_findings=(WEAKNESS,),
            contested_findings=(CONTRADICTION,),
            unresolved_questions=(MISSING, ALTERNATIVE),
        ),
    )
    coordinator.register_result(
        conflicting,
        evidence=(evidence_for(judge, "judge", "Judge evidence"),),
    )
    with pytest.raises(ResearchEvaluationContractError, match="mutually exclusive"):
        ResearchEvaluationRuntime(coordinator).evaluate()


def test_foreign_mission_evidence_is_rejected_by_authoritative_result_layer():
    mission = mission_for()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    foreign = replace(
        evidence_for(researcher, "foreign", "Foreign evidence"),
        mission_id="foreign-mission",
    )
    with pytest.raises(ValueError, match="mission_id"):
        coordinator.register_result(result_for(researcher), evidence=(foreign,))
    assert coordinator.results() == ()


def test_repeated_evaluation_is_idempotent_and_deterministic():
    _, _, runtime = completed_runtime()
    first = runtime.evaluate()
    second = runtime.evaluate()
    assert second == first
    assert second.evaluation_id == first.evaluation_id
    assert second.source_evidence_ids == (
        "evidence-researcher",
        "evidence-challenger",
        "evidence-judge",
    )


def test_duplicate_task_evidence_is_ordered_deterministically_by_identity():
    mission = mission_for()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    coordinator.register_result(
        result_for(researcher),
        evidence=(
            evidence_for(researcher, "researcher-z", "Later evidence"),
            evidence_for(researcher, "researcher-a", "Earlier evidence"),
        ),
    )
    challenger = task_for(mission, ResearchRole.CHALLENGER)
    judge = task_for(mission, ResearchRole.JUDGE)
    coordinator.register_result(
        result_for(challenger),
        evidence=(evidence_for(challenger, "challenger", "Challenge evidence"),),
    )
    coordinator.register_result(
        result_for(judge),
        evidence=(evidence_for(judge, "judge", "Judge evidence"),),
    )
    evaluation = ResearchEvaluationRuntime(coordinator).evaluate()
    assert evaluation.claims[0].evidence_ids == (
        "evidence-researcher-a",
        "evidence-researcher-z",
    )


def test_evaluation_outputs_are_immutable_and_do_not_alias_runtime_state():
    _, _, runtime = completed_runtime()
    evaluation = runtime.evaluate()
    with pytest.raises(FrozenInstanceError):
        evaluation.mission_id = "forged"
    object.__setattr__(evaluation.synthesis, "status", ResearchEvaluationStatus.INELIGIBLE)
    assert runtime.evaluate().synthesis.status is ResearchEvaluationStatus.ELIGIBLE


def test_mutating_original_result_after_registration_cannot_change_evaluation():
    mission = mission_for()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    original = result_for(researcher)
    coordinator.register_result(
        original,
        evidence=(evidence_for(researcher, "researcher", "Research evidence"),),
    )
    object.__setattr__(original, "summary", "forged claim")
    challenger = task_for(mission, ResearchRole.CHALLENGER)
    judge = task_for(mission, ResearchRole.JUDGE)
    coordinator.register_result(
        result_for(challenger),
        evidence=(evidence_for(challenger, "challenger", "Challenge evidence"),),
    )
    coordinator.register_result(
        result_for(judge),
        evidence=(evidence_for(judge, "judge", "Judge evidence"),),
    )
    assert ResearchEvaluationRuntime(coordinator).evaluate().claims[0].statement == CLAIM


def test_plan_mutation_after_runtime_capture_is_rejected():
    mission, _, runtime = completed_runtime()
    object.__setattr__(mission.plan.tasks[0], "objective", "forged objective")
    with pytest.raises(RuntimeError, match="plan no longer matches"):
        runtime.evaluate()


def test_concurrent_evaluation_is_thread_safe_and_identical():
    _, _, runtime = completed_runtime()
    with ThreadPoolExecutor(max_workers=16) as pool:
        evaluations = list(pool.map(lambda _: runtime.evaluate(), range(64)))
    assert len({item.evaluation_id for item in evaluations}) == 1
    assert all(item == evaluations[0] for item in evaluations)


def test_incomplete_or_ambiguous_plan_is_rejected_before_evaluation():
    mission = mission_for()
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    with pytest.raises(ValueError, match="researcher, challenger, and judge"):
        ResearchMissionPlan(mission_id=mission.mission_id, tasks=(researcher,))
