"""Focused tests for governed research mission result coordination."""

import ast
import inspect
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from federation.capability import NodeCapability
import research_mission.result_runtime as result_runtime_module
import research_mission.results as result_models_module
from research_mission import (
    ChallengerAssessment,
    JudgeDecision,
    JudgeOutcome,
    ResearchEvidence,
    ResearchEvidenceConflictError,
    ResearchMission,
    ResearchMissionPlan,
    ResearchMissionPlanner,
    ResearchMissionResultCoordinator,
    ResearchMissionResultState,
    ResearchMissionContractError,
    ResearchMissionRuntime,
    ResearchMissionStatus,
    ResearchResultConflictError,
    ResearchResultPrerequisiteError,
    ResearchRole,
    ResearchTaskResult,
    ResearchTaskResultStatus,
)


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def make_mission(
    mission_id="mission-1",
    *,
    status=ResearchMissionStatus.ROUTED,
):
    mission = ResearchMission(
        mission_id=mission_id,
        objective="Assess a supplied technical claim",
        research_context={"constraint": "Use supplied evidence only"},
    )
    mission.plan = ResearchMissionPlanner().plan(mission)
    mission.status = status
    return mission


def task_for(mission, role):
    return next(task for task in mission.plan.tasks if task.role is role)


def result_for(
    task,
    *,
    status=ResearchTaskResultStatus.COMPLETED,
    summary=None,
    produced_at=NOW,
    metadata=None,
    judge_outcome=JudgeOutcome.ACCEPTED,
):
    challenge = None
    decision = None
    if task.role is ResearchRole.CHALLENGER and (
        status is ResearchTaskResultStatus.COMPLETED
    ):
        challenge = ChallengerAssessment(
            challenged_findings=["researcher finding"],
            weaknesses=["Limited supplied sample"],
            missing_evidence=["Independent replication"],
            alternative_explanations=["Measurement drift"],
        )
    if task.role is ResearchRole.JUDGE and (
        status is ResearchTaskResultStatus.COMPLETED
    ):
        decision = JudgeDecision(
            outcome=judge_outcome,
            rationale="The decision evaluates only the supplied results.",
            confidence=0.7,
            accepted_findings=["supported finding"],
            unresolved_questions=["Long-term persistence"],
        )
    return ResearchTaskResult(
        mission_id=task.mission_id,
        task_id=task.task_id,
        role=task.role,
        status=status,
        summary=summary or f"Structured {task.role.value} result",
        produced_at=produced_at,
        metadata={} if metadata is None else metadata,
        challenger_assessment=challenge,
        judge_decision=decision,
    )


def evidence_for(task, evidence_id="evidence-1", **overrides):
    values = {
        "evidence_id": evidence_id,
        "mission_id": task.mission_id,
        "task_id": task.task_id,
        "evidence_type": "supplied-record",
        "source": "mission-input",
        "reference": f"input://{evidence_id}",
        "summary": "A supplied record supporting inspection of the task result.",
        "observed_at": NOW - timedelta(hours=1),
        "metadata": {"ordinal": 1},
    }
    values.update(overrides)
    return ResearchEvidence(**values)


def register_completed_chain(coordinator, mission, *, judge_outcome=JudgeOutcome.ACCEPTED):
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    challenger = task_for(mission, ResearchRole.CHALLENGER)
    judge = task_for(mission, ResearchRole.JUDGE)
    coordinator.register_result(result_for(researcher))
    coordinator.register_result(result_for(challenger))
    coordinator.register_result(result_for(judge, judge_outcome=judge_outcome))


def make_four_task_mission():
    mission = make_mission("mission-four")
    researcher_1, challenger, judge = mission.plan.tasks
    researcher_2 = replace(
        researcher_1,
        task_id="mission-four-researcher-2",
        sequence=2,
    )
    challenger = replace(
        challenger,
        sequence=3,
        depends_on=(researcher_1.task_id, researcher_2.task_id),
    )
    judge = replace(
        judge,
        sequence=4,
        depends_on=(researcher_1.task_id, researcher_2.task_id, challenger.task_id),
    )
    mission.plan = ResearchMissionPlan(
        mission_id=mission.mission_id,
        tasks=(researcher_1, researcher_2, challenger, judge),
    )
    return mission


def test_coordinator_requires_an_existing_planned_mission():
    with pytest.raises(TypeError, match="ResearchMission"):
        ResearchMissionResultCoordinator(object())

    unplanned = ResearchMission(mission_id="mission-unplanned", objective="Test")
    with pytest.raises(ValueError, match="planned"):
        ResearchMissionResultCoordinator(unplanned)


def test_coordinator_rejects_a_forged_plan_task_container():
    mission = make_mission()
    object.__setattr__(mission.plan, "tasks", list(mission.plan.tasks))

    with pytest.raises((TypeError, ResearchMissionContractError), match="tasks"):
        ResearchMissionResultCoordinator(mission)


def test_researcher_result_is_registered_against_the_planned_task():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    result = result_for(researcher)

    stored = coordinator.register_result(result)

    assert stored == result
    assert coordinator.get_result(researcher.task_id) == result
    assert coordinator.results() == (result,)
    assert coordinator.completed_results() == (result,)
    assert coordinator.pending_tasks() == mission.plan.tasks[1:]


@pytest.mark.parametrize(
    "invalid_result",
    [
        lambda mission: ResearchTaskResult(
            mission_id="foreign-mission",
            task_id=task_for(mission, ResearchRole.RESEARCHER).task_id,
            role=ResearchRole.RESEARCHER,
            status=ResearchTaskResultStatus.COMPLETED,
            summary="Wrong mission",
            produced_at=NOW,
        ),
        lambda mission: ResearchTaskResult(
            mission_id=mission.mission_id,
            task_id="unknown-task",
            role=ResearchRole.RESEARCHER,
            status=ResearchTaskResultStatus.COMPLETED,
            summary="Unknown task",
            produced_at=NOW,
        ),
        lambda mission: ResearchTaskResult(
            mission_id=mission.mission_id,
            task_id=task_for(mission, ResearchRole.RESEARCHER).task_id,
            role=ResearchRole.CHALLENGER,
            status=ResearchTaskResultStatus.COMPLETED,
            summary="Wrong role",
            produced_at=NOW,
            challenger_assessment=ChallengerAssessment(weaknesses=["Mismatch"]),
        ),
    ],
)
def test_identity_and_role_mismatches_fail_without_partial_state(invalid_result):
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)

    with pytest.raises(ValueError):
        coordinator.register_result(invalid_result(mission))

    assert coordinator.results() == ()
    assert coordinator.evidence() == ()


def test_challenger_result_is_rejected_until_researcher_prerequisites_complete():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    challenger = task_for(mission, ResearchRole.CHALLENGER)

    with pytest.raises(ResearchResultPrerequisiteError, match="prerequisite"):
        coordinator.register_result(result_for(challenger))

    assert coordinator.results() == ()

    researcher = task_for(mission, ResearchRole.RESEARCHER)
    coordinator.register_result(result_for(researcher))
    assert coordinator.prerequisites_satisfied(challenger.task_id) is True
    coordinator.register_result(result_for(challenger))


def test_judge_result_is_rejected_until_researcher_and_challenger_complete():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    challenger = task_for(mission, ResearchRole.CHALLENGER)
    judge = task_for(mission, ResearchRole.JUDGE)

    with pytest.raises(ResearchResultPrerequisiteError):
        coordinator.register_result(result_for(judge))

    coordinator.register_result(result_for(researcher))
    with pytest.raises(ResearchResultPrerequisiteError):
        coordinator.register_result(result_for(judge))

    coordinator.register_result(result_for(challenger))
    coordinator.register_result(result_for(judge))
    assert [result.role for result in coordinator.results()] == [
        ResearchRole.RESEARCHER,
        ResearchRole.CHALLENGER,
        ResearchRole.JUDGE,
    ]


@pytest.mark.parametrize(
    "terminal_status",
    [ResearchTaskResultStatus.FAILED, ResearchTaskResultStatus.BLOCKED],
)
def test_failed_or_blocked_results_are_terminal_but_do_not_unlock_progression(
    terminal_status,
):
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    challenger = task_for(mission, ResearchRole.CHALLENGER)
    terminal = result_for(
        researcher,
        status=terminal_status,
        summary=f"Researcher task is {terminal_status.value}",
    )

    coordinator.register_result(terminal)

    assert coordinator.results() == (terminal,)
    assert coordinator.completed_results() == ()
    assert coordinator.pending_tasks() == mission.plan.tasks[1:]
    assert coordinator.unsatisfied_tasks() == mission.plan.tasks
    assert coordinator.prerequisites_satisfied(challenger.task_id) is False
    assert coordinator.has_sufficient_results() is False
    assert coordinator.is_completion_eligible() is False
    with pytest.raises(ResearchResultPrerequisiteError):
        coordinator.register_result(result_for(challenger))


def test_completion_eligibility_is_advisory_and_completion_remains_explicit():
    mission = make_mission(status=ResearchMissionStatus.ROUTED)
    coordinator = ResearchMissionResultCoordinator(mission)
    register_completed_chain(coordinator, mission)

    assert coordinator.has_sufficient_results() is True
    assert coordinator.is_completion_eligible() is True
    assert mission.status is ResearchMissionStatus.ROUTED

    class UnusedRouter:
        def __init__(self):
            self.calls = 0

        def route(self, request):
            self.calls += 1
            raise AssertionError("result completion must not route tasks")

    router = UnusedRouter()
    ResearchMissionRuntime(router).complete(mission)

    assert mission.status is ResearchMissionStatus.COMPLETED
    assert router.calls == 0
    assert coordinator.has_sufficient_results() is True
    assert coordinator.is_completion_eligible() is False


@pytest.mark.parametrize(
    "mission_status",
    [
        ResearchMissionStatus.CREATED,
        ResearchMissionStatus.PLANNED,
        ResearchMissionStatus.FAILED,
        ResearchMissionStatus.CANCELLED,
        ResearchMissionStatus.COMPLETED,
    ],
)
def test_sufficient_results_do_not_override_mission_lifecycle(mission_status):
    mission = make_mission(status=mission_status)
    coordinator = ResearchMissionResultCoordinator(mission)
    register_completed_chain(coordinator, mission)

    assert coordinator.has_sufficient_results() is True
    assert coordinator.is_completion_eligible() is False
    assert mission.status is mission_status


def test_completed_inconclusive_judgment_is_not_autonomously_reinterpreted():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)

    register_completed_chain(
        coordinator,
        mission,
        judge_outcome=JudgeOutcome.INCONCLUSIVE,
    )

    judge_result = coordinator.completed_results()[-1]
    assert judge_result.judge_decision.outcome is JudgeOutcome.INCONCLUSIVE
    assert coordinator.is_completion_eligible() is True


def test_identical_result_replay_is_idempotent():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    result = result_for(task_for(mission, ResearchRole.RESEARCHER))

    first = coordinator.register_result(result)
    second = coordinator.register_result(result)

    assert second == first
    assert coordinator.results() == (first,)


def test_equivalent_utc_timestamp_replay_is_idempotent():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    task = task_for(mission, ResearchRole.RESEARCHER)
    local_time = datetime(
        2026,
        8,
        8,
        7,
        0,
        tzinfo=timezone(timedelta(hours=-5)),
    )

    first = coordinator.register_result(result_for(task, produced_at=NOW))
    second = coordinator.register_result(result_for(task, produced_at=local_time))

    assert second == first


def test_conflicting_duplicate_result_fails_closed():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    task = task_for(mission, ResearchRole.RESEARCHER)
    original = result_for(task, summary="Original")
    coordinator.register_result(original)

    with pytest.raises(ResearchResultConflictError, match="conflicting"):
        coordinator.register_result(result_for(task, summary="Changed"))

    assert coordinator.results() == (original,)


def test_result_conflict_identity_distinguishes_boolean_and_integer_metadata():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    task = task_for(mission, ResearchRole.RESEARCHER)
    coordinator.register_result(result_for(task, metadata={"value": True}))

    with pytest.raises(ResearchResultConflictError):
        coordinator.register_result(result_for(task, metadata={"value": 1}))


def test_large_integer_metadata_remains_deterministic_for_idempotent_replay():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    task = task_for(mission, ResearchRole.RESEARCHER)
    result = result_for(task, metadata={"large": 10**5000})

    first = coordinator.register_result(result)
    second = coordinator.register_result(result)

    assert first == second
    assert coordinator.results() == (first,)


def test_evidence_requires_an_existing_exact_task_result():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)

    with pytest.raises(ValueError, match="result"):
        coordinator.attach_evidence(evidence_for(researcher))

    assert coordinator.evidence() == ()


def test_result_and_supplied_evidence_register_atomically_with_full_provenance():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    result = result_for(researcher)
    first = evidence_for(researcher, "evidence-b")
    second = evidence_for(researcher, "evidence-a")

    coordinator.register_result(result, evidence=(first, second))

    assert coordinator.get_result(researcher.task_id) == result
    assert coordinator.evidence_for_task(researcher.task_id) == (second, first)
    assert coordinator.evidence() == (second, first)


@pytest.mark.parametrize(
    "evidence_overrides",
    [
        {"mission_id": "foreign-mission"},
        {"task_id": "unknown-task"},
    ],
)
def test_wrong_evidence_provenance_fails_without_registering_result(
    evidence_overrides,
):
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    invalid = evidence_for(researcher, **evidence_overrides)

    with pytest.raises(ValueError):
        coordinator.register_result(result_for(researcher), evidence=(invalid,))

    assert coordinator.results() == ()
    assert coordinator.evidence() == ()


def test_cross_task_evidence_cannot_be_attached_during_result_registration():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    challenger = task_for(mission, ResearchRole.CHALLENGER)

    with pytest.raises(ValueError, match="task"):
        coordinator.register_result(
            result_for(researcher),
            evidence=(evidence_for(challenger),),
        )

    assert coordinator.results() == ()
    assert coordinator.evidence() == ()


def test_cross_mission_evidence_is_rejected_even_when_task_ids_are_forged_equal():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    coordinator.register_result(result_for(researcher))
    foreign = evidence_for(researcher, mission_id="foreign-mission")

    with pytest.raises(ValueError, match="mission"):
        coordinator.attach_evidence(foreign)

    assert coordinator.evidence() == ()


def test_duplicate_evidence_id_is_rejected_even_for_identical_replay():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    coordinator.register_result(result_for(researcher))
    evidence = evidence_for(researcher)
    coordinator.attach_evidence(evidence)

    with pytest.raises(ResearchEvidenceConflictError, match="evidence_id"):
        coordinator.attach_evidence(evidence)

    assert coordinator.evidence() == (evidence,)


def test_duplicate_evidence_inside_batch_rejects_entire_batch():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    coordinator.register_result(result_for(researcher))
    first = evidence_for(researcher, "duplicate")
    second = evidence_for(researcher, "duplicate", summary="Conflicting duplicate")

    with pytest.raises(ResearchEvidenceConflictError):
        coordinator.attach_evidence_batch((first, second))

    assert coordinator.evidence() == ()


def test_invalid_later_evidence_does_not_leave_partial_batch_state():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    coordinator.register_result(result_for(researcher))
    valid = evidence_for(researcher, "valid")
    invalid = evidence_for(researcher, "invalid")
    object.__setattr__(invalid, "task_id", object())
    before = coordinator.inspect()

    with pytest.raises(TypeError, match="task_id"):
        coordinator.attach_evidence_batch((valid, invalid))

    assert coordinator.inspect() == before


def test_invalid_evidence_batch_does_not_leave_partially_registered_result():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    valid = evidence_for(researcher, "valid")
    invalid = evidence_for(researcher, "invalid", mission_id="foreign")

    with pytest.raises(ValueError, match="mission"):
        coordinator.register_result(
            result_for(researcher),
            evidence=(valid, invalid),
        )

    assert coordinator.results() == ()
    assert coordinator.evidence() == ()


def test_conflicting_result_with_new_evidence_mutates_neither_collection():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    original = result_for(researcher, summary="Original")
    coordinator.register_result(original)
    before = coordinator.inspect()

    with pytest.raises(ResearchResultConflictError):
        coordinator.register_result(
            result_for(researcher, summary="Conflict"),
            evidence=(evidence_for(researcher),),
        )

    assert coordinator.inspect() == before


def test_public_inspection_order_is_plan_then_evidence_identity_order():
    mission = make_four_task_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher_1, researcher_2, challenger, judge = mission.plan.tasks

    coordinator.register_result(
        result_for(researcher_2),
        evidence=(evidence_for(researcher_2, "z"), evidence_for(researcher_2, "a")),
    )
    coordinator.register_result(
        result_for(researcher_1),
        evidence=(evidence_for(researcher_1, "m"),),
    )
    coordinator.register_result(result_for(challenger))

    assert [result.task_id for result in coordinator.results()] == [
        researcher_1.task_id,
        researcher_2.task_id,
        challenger.task_id,
    ]
    assert [item.evidence_id for item in coordinator.evidence()] == ["m", "a", "z"]
    assert coordinator.pending_tasks() == (judge,)
    assert coordinator.inspect() == coordinator.inspect()


def test_role_progression_is_enforced_even_if_custom_plan_omits_role_dependency():
    mission = make_mission()
    researcher, challenger, judge = mission.plan.tasks
    challenger = replace(challenger, depends_on=())
    judge = replace(judge, depends_on=(challenger.task_id,))
    mission.plan = ResearchMissionPlan(
        mission_id=mission.mission_id,
        tasks=(researcher, challenger, judge),
    )
    coordinator = ResearchMissionResultCoordinator(mission)

    with pytest.raises(ResearchResultPrerequisiteError):
        coordinator.register_result(result_for(challenger))


def test_state_snapshot_is_frozen_deterministic_and_separates_status_views():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher, challenger, judge = mission.plan.tasks
    failed = result_for(
        researcher,
        status=ResearchTaskResultStatus.FAILED,
        summary="Failed",
    )
    coordinator.register_result(failed)

    state = coordinator.inspect()

    assert isinstance(state, ResearchMissionResultState)
    assert state.results == (failed,)
    assert state.completed_results == ()
    assert state.failed_results == (failed,)
    assert state.blocked_results == ()
    assert state.pending_tasks == (challenger, judge)
    assert state.unsatisfied_tasks == mission.plan.tasks
    assert state.results_sufficient is False
    assert state.completion_eligible is False
    with pytest.raises((AttributeError, TypeError)):
        state.results = ()


def test_pending_task_inspection_does_not_alias_mutable_plan_context():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)

    inspected = coordinator.pending_tasks()
    inspected[0].research_context["caller"] = "mutation"

    assert "caller" not in mission.plan.tasks[0].research_context
    assert "caller" not in coordinator.pending_tasks()[0].research_context


def test_coordinator_preserves_arbitrary_existing_task_context_without_aliasing():
    mission = ResearchMission(
        mission_id="mission-context",
        objective="Preserve the existing task context contract",
        research_context={
            "observed_at": NOW,
            "amount": Decimal("1.25"),
            "tags": {"supplied"},
        },
    )
    mission.plan = ResearchMissionPlanner().plan(mission)
    mission.status = ResearchMissionStatus.ROUTED
    coordinator = ResearchMissionResultCoordinator(mission)

    mission.plan.tasks[0].research_context["tags"].add("source mutation")

    captured = coordinator.pending_tasks()[0].research_context
    assert captured["observed_at"] == NOW
    assert captured["amount"] == Decimal("1.25")
    assert captured["tags"] == {"supplied"}


def test_pending_task_capabilities_do_not_alias_plan_or_coordinator_state():
    mission = ResearchMission(
        mission_id="mission-capabilities",
        objective="Preserve capability identity",
        required_capabilities=frozenset({NodeCapability("network_access")}),
    )
    mission.plan = ResearchMissionPlanner().plan(mission)
    mission.status = ResearchMissionStatus.ROUTED
    coordinator = ResearchMissionResultCoordinator(mission)

    inspected_capability = next(
        iter(coordinator.pending_tasks()[0].required_capabilities)
    )
    object.__setattr__(inspected_capability, "name", "caller-mutation")

    source_capability = next(iter(mission.plan.tasks[0].required_capabilities))
    captured_capability = next(
        iter(coordinator.pending_tasks()[0].required_capabilities)
    )
    assert source_capability.name == "network_access"
    assert captured_capability.name == "network_access"


def test_result_and_evidence_outputs_do_not_alias_coordinator_state():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)

    returned_result = coordinator.register_result(result_for(researcher))
    returned_evidence = coordinator.attach_evidence(evidence_for(researcher))
    object.__setattr__(returned_result, "status", ResearchTaskResultStatus.FAILED)
    object.__setattr__(returned_evidence, "task_id", "unknown-task")

    assert coordinator.get_result(researcher.task_id).status is (
        ResearchTaskResultStatus.COMPLETED
    )
    assert coordinator.evidence()[0].task_id == researcher.task_id

    inspected = coordinator.inspect()
    object.__setattr__(
        inspected.results[0],
        "status",
        ResearchTaskResultStatus.BLOCKED,
    )
    object.__setattr__(inspected.evidence[0], "task_id", "unknown-task")

    assert coordinator.results()[0].status is ResearchTaskResultStatus.COMPLETED
    assert coordinator.evidence()[0].task_id == researcher.task_id


def test_mutated_mission_identity_or_plan_fails_closed_without_state_change():
    mission = make_mission()
    original_mission_id = mission.mission_id
    original_plan = mission.plan
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)

    mission.mission_id = "mutated-mission"
    with pytest.raises(ResearchMissionContractError, match="identity"):
        coordinator.register_result(result_for(researcher))

    mission.mission_id = original_mission_id
    mission.plan = ResearchMissionPlanner().plan(mission)
    with pytest.raises(ResearchMissionContractError, match="plan"):
        coordinator.register_result(result_for(researcher))

    mission.plan = original_plan
    assert coordinator.results() == ()


def test_plan_change_during_result_validation_cannot_commit_partial_state(
    monkeypatch,
):
    mission = make_mission()
    original_plan = mission.plan
    replacement_plan = ResearchMissionPlanner().plan(mission)
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    original_validate = coordinator._validate_evidence_batch_locked

    def validate_then_change_plan(*args, **kwargs):
        original_validate(*args, **kwargs)
        mission.plan = replacement_plan

    monkeypatch.setattr(
        coordinator,
        "_validate_evidence_batch_locked",
        validate_then_change_plan,
    )

    with pytest.raises(ResearchMissionContractError, match="plan"):
        coordinator.register_result(result_for(researcher))

    mission.plan = original_plan
    assert coordinator.results() == ()


def test_malformed_live_mission_status_cannot_claim_completion_eligibility():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    register_completed_chain(coordinator, mission)
    mission.status = "routed"

    with pytest.raises(ResearchMissionContractError, match="status"):
        coordinator.is_completion_eligible()


def test_malformed_custom_and_forged_result_objects_fail_closed():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)

    class DuckResult:
        mission_id = mission.mission_id
        task_id = researcher.task_id
        role = ResearchRole.RESEARCHER

    with pytest.raises(TypeError, match="ResearchTaskResult"):
        coordinator.register_result(DuckResult())

    forged = result_for(researcher)
    object.__setattr__(forged, "status", "completed")
    with pytest.raises(TypeError, match="status"):
        coordinator.register_result(forged)

    assert coordinator.results() == ()


def test_malformed_metadata_forgery_fails_without_mutating_prior_state():
    mission = make_mission()
    coordinator = ResearchMissionResultCoordinator(mission)
    researcher = task_for(mission, ResearchRole.RESEARCHER)
    forged = result_for(researcher)
    object.__setattr__(forged, "metadata", {"opaque": object()})

    with pytest.raises(TypeError, match="metadata"):
        coordinator.register_result(forged)

    assert coordinator.inspect().results == ()


def test_unknown_task_inspection_fails_closed():
    coordinator = ResearchMissionResultCoordinator(make_mission())

    with pytest.raises(ValueError, match="Unknown planned task"):
        coordinator.get_result("unknown")
    with pytest.raises(ValueError, match="Unknown planned task"):
        coordinator.evidence_for_task("unknown")
    with pytest.raises(ValueError, match="Unknown planned task"):
        coordinator.prerequisites_satisfied("unknown")


def test_result_layer_has_no_sports_or_federation_execution_dependency():
    imported_modules = set()
    for module in (result_models_module, result_runtime_module):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)

    assert not any(name == "sports" or name.startswith("sports.") for name in imported_modules)
    assert not {
        "federation.task_request",
        "federation.task_assignment",
        "federation.task_router",
        "federation.task_dispatcher",
    } & imported_modules
    assert not {
        "TaskRequest",
        "TaskAssignment",
        "TaskRouter",
        "TaskDispatchCoordinator",
    } & set(result_runtime_module.__dict__)
    assert not {
        "route",
        "dispatch",
        "create_offer",
    } & set(dir(ResearchMissionResultCoordinator))
