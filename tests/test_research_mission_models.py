"""Comprehensive tests for research mission domain contracts."""

import pytest

from federation import AuthorizationLevel, NodeCapability
from research_mission import (
    ResearchMission,
    ResearchMissionPlan,
    ResearchMissionPlanner,
    ResearchMissionStatus,
    ResearchRole,
    ResearchTaskSpec,
)


# ResearchRole tests


def test_research_role_values():
    """Test that ResearchRole has exactly the required values."""
    assert ResearchRole.RESEARCHER.value == "researcher"
    assert ResearchRole.CHALLENGER.value == "challenger"
    assert ResearchRole.JUDGE.value == "judge"
    assert len(list(ResearchRole)) == 3


def test_research_role_is_string_enum():
    """Test that ResearchRole is a string enum."""
    assert isinstance(ResearchRole.RESEARCHER, str)
    assert ResearchRole.RESEARCHER == "researcher"


# ResearchMissionStatus tests


def test_research_mission_status_values():
    """Test that ResearchMissionStatus has required lifecycle states."""
    assert ResearchMissionStatus.CREATED.value == "created"
    assert ResearchMissionStatus.PLANNED.value == "planned"
    assert ResearchMissionStatus.ROUTED.value == "routed"
    assert ResearchMissionStatus.DISPATCHED.value == "dispatched"
    assert ResearchMissionStatus.COMPLETED.value == "completed"
    assert ResearchMissionStatus.FAILED.value == "failed"
    assert ResearchMissionStatus.CANCELLED.value == "cancelled"


def test_research_mission_status_is_string_enum():
    """Test that ResearchMissionStatus is a string enum."""
    assert isinstance(ResearchMissionStatus.CREATED, str)
    assert ResearchMissionStatus.CREATED == "created"


# ResearchTaskSpec tests


def test_research_task_spec_minimal():
    """Test creating a minimal research task spec."""
    spec = ResearchTaskSpec(
        task_id="task-1",
        mission_id="mission-1",
        role=ResearchRole.RESEARCHER,
        sequence=1,
        objective="Find information",
        research_context={},
        expected_result="Results",
        authorization_level=AuthorizationLevel.INTERNAL,
        approval_required=False,
    )

    assert spec.task_id == "task-1"
    assert spec.mission_id == "mission-1"
    assert spec.role is ResearchRole.RESEARCHER
    assert spec.sequence == 1
    assert spec.objective == "Find information"
    assert spec.research_context == {}
    assert spec.expected_result == "Results"
    assert spec.authorization_level is AuthorizationLevel.INTERNAL
    assert spec.approval_required is False
    assert spec.required_capabilities == frozenset()
    assert spec.preferred_capabilities == frozenset()
    assert spec.depends_on == ()


def test_research_task_spec_requires_non_empty_task_id():
    """Test that task_id must be non-empty."""
    with pytest.raises(ValueError, match="task_id"):
        ResearchTaskSpec(
            task_id="",
            mission_id="mission-1",
            role=ResearchRole.RESEARCHER,
            sequence=1,
            objective="Test",
            research_context={},
            expected_result="Result",
            authorization_level=AuthorizationLevel.INTERNAL,
            approval_required=False,
        )


def test_research_task_spec_requires_non_empty_mission_id():
    """Test that mission_id must be non-empty."""
    with pytest.raises(ValueError, match="mission_id"):
        ResearchTaskSpec(
            task_id="task-1",
            mission_id="  ",
            role=ResearchRole.RESEARCHER,
            sequence=1,
            objective="Test",
            research_context={},
            expected_result="Result",
            authorization_level=AuthorizationLevel.INTERNAL,
            approval_required=False,
        )


def test_research_task_spec_requires_valid_role():
    """Test that role must be a ResearchRole."""
    with pytest.raises(TypeError, match="role"):
        ResearchTaskSpec(
            task_id="task-1",
            mission_id="mission-1",
            role="researcher",
            sequence=1,
            objective="Test",
            research_context={},
            expected_result="Result",
            authorization_level=AuthorizationLevel.INTERNAL,
            approval_required=False,
        )


def test_research_task_spec_requires_positive_sequence():
    """Test that sequence must be a positive integer."""
    with pytest.raises(ValueError, match="sequence"):
        ResearchTaskSpec(
            task_id="task-1",
            mission_id="mission-1",
            role=ResearchRole.RESEARCHER,
            sequence=0,
            objective="Test",
            research_context={},
            expected_result="Result",
            authorization_level=AuthorizationLevel.INTERNAL,
            approval_required=False,
        )


def test_research_task_spec_rejects_negative_sequence():
    """Test that sequence cannot be negative."""
    with pytest.raises(ValueError, match="sequence"):
        ResearchTaskSpec(
            task_id="task-1",
            mission_id="mission-1",
            role=ResearchRole.RESEARCHER,
            sequence=-1,
            objective="Test",
            research_context={},
            expected_result="Result",
            authorization_level=AuthorizationLevel.INTERNAL,
            approval_required=False,
        )


def test_research_task_spec_requires_non_empty_objective():
    """Test that objective must be non-empty."""
    with pytest.raises(ValueError, match="objective"):
        ResearchTaskSpec(
            task_id="task-1",
            mission_id="mission-1",
            role=ResearchRole.RESEARCHER,
            sequence=1,
            objective="  ",
            research_context={},
            expected_result="Result",
            authorization_level=AuthorizationLevel.INTERNAL,
            approval_required=False,
        )


def test_research_task_spec_requires_dict_research_context():
    """Test that research_context must be a dict."""
    with pytest.raises(TypeError, match="research_context"):
        ResearchTaskSpec(
            task_id="task-1",
            mission_id="mission-1",
            role=ResearchRole.RESEARCHER,
            sequence=1,
            objective="Test",
            research_context="not a dict",
            expected_result="Result",
            authorization_level=AuthorizationLevel.INTERNAL,
            approval_required=False,
        )


def test_research_task_spec_requires_non_empty_expected_result():
    """Test that expected_result must be non-empty."""
    with pytest.raises(ValueError, match="expected_result"):
        ResearchTaskSpec(
            task_id="task-1",
            mission_id="mission-1",
            role=ResearchRole.RESEARCHER,
            sequence=1,
            objective="Test",
            research_context={},
            expected_result="",
            authorization_level=AuthorizationLevel.INTERNAL,
            approval_required=False,
        )


def test_research_task_spec_requires_authorization_level():
    """Test that authorization_level must be an AuthorizationLevel."""
    with pytest.raises(TypeError, match="authorization_level"):
        ResearchTaskSpec(
            task_id="task-1",
            mission_id="mission-1",
            role=ResearchRole.RESEARCHER,
            sequence=1,
            objective="Test",
            research_context={},
            expected_result="Result",
            authorization_level="internal",
            approval_required=False,
        )


def test_research_task_spec_requires_boolean_approval():
    """Test that approval_required must be a boolean."""
    with pytest.raises(TypeError, match="approval_required"):
        ResearchTaskSpec(
            task_id="task-1",
            mission_id="mission-1",
            role=ResearchRole.RESEARCHER,
            sequence=1,
            objective="Test",
            research_context={},
            expected_result="Result",
            authorization_level=AuthorizationLevel.INTERNAL,
            approval_required="true",
        )


def test_research_task_spec_rejects_overlapping_capabilities():
    """Test that required and preferred capabilities must not overlap."""
    cap = NodeCapability("python_execution")

    with pytest.raises(ValueError, match="must not overlap"):
        ResearchTaskSpec(
            task_id="task-1",
            mission_id="mission-1",
            role=ResearchRole.RESEARCHER,
            sequence=1,
            objective="Test",
            research_context={},
            expected_result="Result",
            authorization_level=AuthorizationLevel.INTERNAL,
            approval_required=False,
            required_capabilities={cap},
            preferred_capabilities={cap},
        )


def test_research_task_spec_converts_capabilities_to_frozenset():
    """Test that capabilities are converted to frozenset."""
    spec = ResearchTaskSpec(
        task_id="task-1",
        mission_id="mission-1",
        role=ResearchRole.RESEARCHER,
        sequence=1,
        objective="Test",
        research_context={},
        expected_result="Result",
        authorization_level=AuthorizationLevel.INTERNAL,
        approval_required=False,
        required_capabilities=[NodeCapability("python_execution")],
        preferred_capabilities=[NodeCapability("gpu_available")],
    )

    assert isinstance(spec.required_capabilities, frozenset)
    assert isinstance(spec.preferred_capabilities, frozenset)


def test_research_task_spec_validates_dependencies():
    """Test that depends_on entries must be valid identifiers."""
    with pytest.raises(ValueError, match="depends_on"):
        ResearchTaskSpec(
            task_id="task-1",
            mission_id="mission-1",
            role=ResearchRole.RESEARCHER,
            sequence=1,
            objective="Test",
            research_context={},
            expected_result="Result",
            authorization_level=AuthorizationLevel.INTERNAL,
            approval_required=False,
            depends_on=("",),
        )


def test_research_task_spec_preserves_mission_context():
    """Test that task spec preserves all mission governance metadata."""
    spec = ResearchTaskSpec(
        task_id="task-1",
        mission_id="mission-1",
        role=ResearchRole.RESEARCHER,
        sequence=1,
        objective="Test objective",
        research_context={"key": "value"},
        expected_result="Result",
        authorization_level=AuthorizationLevel.RESTRICTED,
        approval_required=True,
        required_capabilities={NodeCapability("python_execution")},
        preferred_capabilities={NodeCapability("gpu_available")},
        depends_on=("task-0",),
    )

    request = spec.to_task_request()

    assert request.task_id == "task-1"
    assert request.mission_id == "mission-1"
    assert request.authorization_level is AuthorizationLevel.RESTRICTED
    assert request.approval_required is True
    assert request.required_capabilities == {NodeCapability("python_execution")}
    assert request.preferred_capabilities == {NodeCapability("gpu_available")}
    assert request.expected_result == "Result"
    assert request.input_data["research_mission"]["mission_id"] == "mission-1"
    assert request.input_data["research_mission"]["objective"] == "Test objective"
    assert request.input_data["research_mission"]["role"] == "researcher"
    assert request.input_data["research_mission"]["sequence"] == 1
    assert request.input_data["research_mission"]["depends_on"] == ["task-0"]
    assert request.input_data["research_mission"]["context"] == {"key": "value"}


# ResearchMissionPlan tests


def test_research_mission_plan_requires_all_roles():
    """Test that a plan must include researcher, challenger, and judge."""
    with pytest.raises(ValueError, match="researcher, challenger, and judge"):
        ResearchMissionPlan(
            mission_id="mission-1",
            tasks=(
                ResearchTaskSpec(
                    task_id="task-1",
                    mission_id="mission-1",
                    role=ResearchRole.RESEARCHER,
                    sequence=1,
                    objective="Test",
                    research_context={},
                    expected_result="Result",
                    authorization_level=AuthorizationLevel.INTERNAL,
                    approval_required=False,
                ),
            ),
        )


def test_research_mission_plan_rejects_empty_tasks():
    """Test that a plan cannot have empty tasks."""
    with pytest.raises(ValueError, match="must not be empty"):
        ResearchMissionPlan(mission_id="mission-1", tasks=())


def test_research_mission_plan_requires_matching_mission_ids():
    """Test that all tasks must belong to the plan mission_id."""
    with pytest.raises(ValueError, match="belong to the plan mission_id"):
        ResearchMissionPlan(
            mission_id="mission-1",
            tasks=(
                ResearchTaskSpec(
                    task_id="task-1",
                    mission_id="mission-2",
                    role=ResearchRole.RESEARCHER,
                    sequence=1,
                    objective="Test",
                    research_context={},
                    expected_result="Result",
                    authorization_level=AuthorizationLevel.INTERNAL,
                    approval_required=False,
                ),
                ResearchTaskSpec(
                    task_id="task-2",
                    mission_id="mission-2",
                    role=ResearchRole.CHALLENGER,
                    sequence=2,
                    objective="Test",
                    research_context={},
                    expected_result="Result",
                    authorization_level=AuthorizationLevel.INTERNAL,
                    approval_required=False,
                ),
                ResearchTaskSpec(
                    task_id="task-3",
                    mission_id="mission-2",
                    role=ResearchRole.JUDGE,
                    sequence=3,
                    objective="Test",
                    research_context={},
                    expected_result="Result",
                    authorization_level=AuthorizationLevel.INTERNAL,
                    approval_required=False,
                ),
            ),
        )


def test_research_mission_plan_requires_unique_task_ids():
    """Test that task_id values must be unique within a plan."""
    with pytest.raises(ValueError, match="unique"):
        ResearchMissionPlan(
            mission_id="mission-1",
            tasks=(
                ResearchTaskSpec(
                    task_id="task-1",
                    mission_id="mission-1",
                    role=ResearchRole.RESEARCHER,
                    sequence=1,
                    objective="Test",
                    research_context={},
                    expected_result="Result",
                    authorization_level=AuthorizationLevel.INTERNAL,
                    approval_required=False,
                ),
                ResearchTaskSpec(
                    task_id="task-1",
                    mission_id="mission-1",
                    role=ResearchRole.CHALLENGER,
                    sequence=2,
                    objective="Test",
                    research_context={},
                    expected_result="Result",
                    authorization_level=AuthorizationLevel.INTERNAL,
                    approval_required=False,
                ),
                ResearchTaskSpec(
                    task_id="task-3",
                    mission_id="mission-1",
                    role=ResearchRole.JUDGE,
                    sequence=3,
                    objective="Test",
                    research_context={},
                    expected_result="Result",
                    authorization_level=AuthorizationLevel.INTERNAL,
                    approval_required=False,
                ),
            ),
        )


def test_research_mission_plan_requires_dependency_ordering():
    """Dependencies must name earlier tasks in deterministic plan order."""
    mission = ResearchMission(mission_id="mission-1", objective="Test objective")
    tasks = ResearchMissionPlanner().plan(mission).tasks

    with pytest.raises(ValueError, match="deterministic plan order"):
        ResearchMissionPlan(
            mission_id=mission.mission_id,
            tasks=(tasks[1], tasks[0], tasks[2]),
        )


def test_research_mission_plan_rejects_unknown_dependencies():
    """A plan must fail closed when a dependency is outside the plan."""
    mission = ResearchMission(mission_id="mission-1", objective="Test objective")
    researcher, challenger, judge = ResearchMissionPlanner().plan(mission).tasks
    challenger = ResearchTaskSpec(
        task_id=challenger.task_id,
        mission_id=challenger.mission_id,
        role=challenger.role,
        sequence=challenger.sequence,
        objective=challenger.objective,
        research_context=challenger.research_context,
        expected_result=challenger.expected_result,
        authorization_level=challenger.authorization_level,
        approval_required=challenger.approval_required,
        required_capabilities=challenger.required_capabilities,
        preferred_capabilities=challenger.preferred_capabilities,
        depends_on=("foreign-task",),
    )

    with pytest.raises(ValueError, match="earlier task"):
        ResearchMissionPlan(
            mission_id=mission.mission_id,
            tasks=(researcher, challenger, judge),
        )


# ResearchMissionPlanner tests


def test_planner_creates_exactly_one_researcher():
    """Test that planner creates at least one researcher task."""
    mission = ResearchMission(
        mission_id="mission-1",
        objective="Test objective",
    )
    plan = ResearchMissionPlanner().plan(mission)

    researcher_tasks = [task for task in plan.tasks if task.role is ResearchRole.RESEARCHER]
    assert len(researcher_tasks) >= 1


def test_planner_creates_exactly_one_challenger():
    """Test that planner creates exactly one challenger task."""
    mission = ResearchMission(
        mission_id="mission-1",
        objective="Test objective",
    )
    plan = ResearchMissionPlanner().plan(mission)

    challenger_tasks = [task for task in plan.tasks if task.role is ResearchRole.CHALLENGER]
    assert len(challenger_tasks) == 1


def test_planner_creates_exactly_one_judge():
    """Test that planner creates exactly one judge task."""
    mission = ResearchMission(
        mission_id="mission-1",
        objective="Test objective",
    )
    plan = ResearchMissionPlanner().plan(mission)

    judge_tasks = [task for task in plan.tasks if task.role is ResearchRole.JUDGE]
    assert len(judge_tasks) == 1


def test_planner_creates_deterministic_task_ids():
    """Test that planner creates deterministic task IDs (no random UUIDs)."""
    mission1 = ResearchMission(
        mission_id="mission-1",
        objective="Test objective",
    )
    mission2 = ResearchMission(
        mission_id="mission-1",
        objective="Test objective",
    )

    plan1 = ResearchMissionPlanner().plan(mission1)
    plan2 = ResearchMissionPlanner().plan(mission2)

    assert [task.task_id for task in plan1.tasks] == [task.task_id for task in plan2.tasks]


def test_planner_task_ids_derive_from_mission_identity():
    """Test that task IDs are derived from mission identity and role."""
    mission = ResearchMission(
        mission_id="mission-climate-analysis",
        objective="Test objective",
    )
    plan = ResearchMissionPlanner().plan(mission)

    for task in plan.tasks:
        assert task.task_id.startswith("mission-climate-analysis-")
        assert task.role.value in task.task_id


def test_planner_preserves_authorization_level():
    """Test that planner preserves authorization level."""
    mission = ResearchMission(
        mission_id="mission-1",
        objective="Test objective",
        authorization_level=AuthorizationLevel.CONFIDENTIAL,
    )
    plan = ResearchMissionPlanner().plan(mission)

    assert all(
        task.authorization_level is AuthorizationLevel.CONFIDENTIAL for task in plan.tasks
    )


def test_planner_preserves_approval_requirement():
    """Test that planner preserves approval requirement."""
    mission = ResearchMission(
        mission_id="mission-1",
        objective="Test objective",
        approval_required=True,
    )
    plan = ResearchMissionPlanner().plan(mission)

    assert all(task.approval_required is True for task in plan.tasks)


def test_planner_preserves_required_capabilities():
    """Test that planner preserves required capabilities."""
    mission = ResearchMission(
        mission_id="mission-1",
        objective="Test objective",
        required_capabilities={NodeCapability("python_execution")},
    )
    plan = ResearchMissionPlanner().plan(mission)

    assert all(
        NodeCapability("python_execution") in task.required_capabilities
        for task in plan.tasks
    )


def test_planner_preserves_preferred_capabilities():
    """Test that planner preserves preferred capabilities."""
    mission = ResearchMission(
        mission_id="mission-1",
        objective="Test objective",
        preferred_capabilities={NodeCapability("gpu_available")},
    )
    plan = ResearchMissionPlanner().plan(mission)

    assert all(
        NodeCapability("gpu_available") in task.preferred_capabilities for task in plan.tasks
    )


def test_planner_establishes_dependency_ordering():
    """Test that planner establishes researcher -> challenger -> judge dependency."""
    mission = ResearchMission(
        mission_id="mission-1",
        objective="Test objective",
    )
    plan = ResearchMissionPlanner().plan(mission)

    researcher_id = next(task.task_id for task in plan.tasks if task.role is ResearchRole.RESEARCHER)
    challenger_id = next(task.task_id for task in plan.tasks if task.role is ResearchRole.CHALLENGER)
    judge_id = next(task.task_id for task in plan.tasks if task.role is ResearchRole.JUDGE)

    challenger_task = next(task for task in plan.tasks if task.role is ResearchRole.CHALLENGER)
    judge_task = next(task for task in plan.tasks if task.role is ResearchRole.JUDGE)

    assert researcher_id in challenger_task.depends_on
    assert researcher_id in judge_task.depends_on
    assert challenger_id in judge_task.depends_on


# ResearchMission tests


def test_research_mission_minimal():
    """Test creating a minimal research mission."""
    mission = ResearchMission(
        mission_id="mission-1",
        objective="Test objective",
    )

    assert mission.mission_id == "mission-1"
    assert mission.objective == "Test objective"
    assert mission.research_context == {}
    assert mission.authorization_level is AuthorizationLevel.INTERNAL
    assert mission.approval_required is False
    assert mission.required_capabilities == frozenset()
    assert mission.preferred_capabilities == frozenset()
    assert mission.status is ResearchMissionStatus.CREATED
    assert mission.plan is None
    assert mission.routing_decisions == ()
    assert mission.dispatch_offers == ()


def test_research_mission_rejects_overlapping_capabilities():
    """Test that mission rejects overlapping required and preferred capabilities."""
    cap = NodeCapability("python_execution")

    with pytest.raises(ValueError, match="must not overlap"):
        ResearchMission(
            mission_id="mission-1",
            objective="Test objective",
            required_capabilities={cap},
            preferred_capabilities={cap},
        )
