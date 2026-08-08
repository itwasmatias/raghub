from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from federation import (
    AuthorizationLevel,
    NodeCapability,
    RoutingOutcome,
    TaskRequest,
)
from research_mission import (
    InvalidMissionTransition,
    ResearchMission,
    ResearchMissionPlanner,
    ResearchMissionRuntime,
    ResearchMissionStatus,
    ResearchRole,
)


def make_mission(**overrides):
    values = {
        "mission_id": "mission-climate-1",
        "objective": "Assess a disputed technical claim",
        "research_context": {
            "claim": "The intervention reduces energy use",
            "constraints": ["Use supplied evidence only"],
        },
        "authorization_level": AuthorizationLevel.RESTRICTED,
        "approval_required": True,
        "required_capabilities": {NodeCapability("python_execution")},
        "preferred_capabilities": {NodeCapability("persistent_storage")},
    }
    values.update(overrides)
    return ResearchMission(**values)


def test_planner_creates_deterministic_research_challenge_and_judgment_plan():
    mission = make_mission()
    planner = ResearchMissionPlanner()

    first = planner.plan(mission)
    second = planner.plan(mission)

    assert first == second
    assert first.mission_id == mission.mission_id
    assert [task.role for task in first.tasks] == [
        ResearchRole.RESEARCHER,
        ResearchRole.CHALLENGER,
        ResearchRole.JUDGE,
    ]
    assert [task.task_id for task in first.tasks] == [
        "mission-climate-1-researcher-1",
        "mission-climate-1-challenger-1",
        "mission-climate-1-judge-1",
    ]
    assert first.tasks[1].depends_on == (first.tasks[0].task_id,)
    assert first.tasks[2].depends_on == (
        first.tasks[0].task_id,
        first.tasks[1].task_id,
    )


def test_planned_tasks_convert_to_existing_task_request_without_losing_governance():
    mission = make_mission()
    plan = ResearchMissionPlanner().plan(mission)

    requests = plan.to_task_requests()

    assert all(type(request) is TaskRequest for request in requests)
    assert [request.task_id for request in requests] == [
        task.task_id for task in plan.tasks
    ]
    for task, request in zip(plan.tasks, requests, strict=True):
        assert request.mission_id == mission.mission_id
        assert request.authorization_level is AuthorizationLevel.RESTRICTED
        assert request.approval_required is True
        assert request.required_capabilities == {NodeCapability("python_execution")}
        assert request.preferred_capabilities == {
            NodeCapability("persistent_storage")
        }
        assert request.expected_result == task.expected_result
        assert request.input_data == {
            "research_mission": {
                "mission_id": mission.mission_id,
                "objective": mission.objective,
                "role": task.role.value,
                "sequence": task.sequence,
                "depends_on": list(task.depends_on),
                "context": mission.research_context,
            }
        }


def test_models_defensively_copy_mutable_context_and_capabilities():
    context = {"nested": {"items": ["original"]}}
    required = {NodeCapability("python_execution")}
    mission = make_mission(research_context=context, required_capabilities=required)

    context["nested"]["items"].append("mutation")
    required.add(NodeCapability("network_access"))
    request = ResearchMissionPlanner().plan(mission).to_task_requests()[0]
    request.input_data["research_mission"]["context"]["nested"]["items"].append(
        "request mutation"
    )

    assert mission.research_context == {"nested": {"items": ["original"]}}
    assert mission.required_capabilities == frozenset(
        {NodeCapability("python_execution")}
    )
    assert mission.research_context == {"nested": {"items": ["original"]}}


@dataclass
class StubDecision:
    outcome: RoutingOutcome
    assignment_id: str | None


class RecordingRouter:
    def __init__(self, outcomes=None):
        self.requests = []
        self.outcomes = outcomes or [RoutingOutcome.SUCCESS] * 3

    def route(self, request):
        self.requests.append(request)
        index = len(self.requests)
        outcome = self.outcomes[index - 1]
        return StubDecision(
            outcome=outcome,
            assignment_id=f"assignment-{index}" if outcome is RoutingOutcome.SUCCESS else None,
        )


class RecordingDispatcher:
    def __init__(self):
        self.calls = []

    def create_offer(self, **kwargs):
        self.calls.append(kwargs)
        return {"offer": kwargs["assignment_id"]}


class FailingDispatcher(RecordingDispatcher):
    def create_offer(self, **kwargs):
        offer = super().create_offer(**kwargs)
        if len(self.calls) == 2:
            raise RuntimeError("second offer failed")
        return offer


def test_runtime_lifecycle_delegates_routing_and_dispatch_in_plan_order():
    router = RecordingRouter()
    dispatcher = RecordingDispatcher()
    runtime = ResearchMissionRuntime(router, dispatch_coordinator=dispatcher)
    mission = make_mission()

    plan = runtime.plan(mission)
    decisions = runtime.route(mission)
    expires_at = datetime(2026, 8, 8, tzinfo=timezone.utc)
    offers = runtime.dispatch(
        mission,
        actor_node_id="coordinator-1",
        expires_at=expires_at,
    )

    assert mission.status is ResearchMissionStatus.DISPATCHED
    assert mission.plan is plan
    assert tuple(router.requests) == plan.to_task_requests()
    assert mission.routing_decisions == decisions
    assert offers == tuple(mission.dispatch_offers)
    assert dispatcher.calls == [
        {
            "assignment_id": f"assignment-{index}",
            "actor_node_id": "coordinator-1",
            "expires_at": expires_at,
        }
        for index in range(1, 4)
    ]


def test_runtime_requires_explicit_valid_lifecycle_transitions():
    runtime = ResearchMissionRuntime(RecordingRouter())
    mission = make_mission()

    with pytest.raises(InvalidMissionTransition, match="created.*route"):
        runtime.route(mission)
    with pytest.raises(InvalidMissionTransition, match="created.*complete"):
        runtime.complete(mission)

    runtime.plan(mission)
    with pytest.raises(InvalidMissionTransition, match="planned.*plan"):
        runtime.plan(mission)
    runtime.route(mission)
    with pytest.raises(RuntimeError, match="dispatch coordinator"):
        runtime.dispatch(
            mission,
            actor_node_id="coordinator-1",
            expires_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        )


def test_runtime_rejects_invalid_planner_output_without_advancing_lifecycle():
    class InvalidPlanner:
        def plan(self, mission):
            return object()

    mission = make_mission()
    runtime = ResearchMissionRuntime(RecordingRouter(), planner=InvalidPlanner())

    with pytest.raises(TypeError, match="ResearchMissionPlan"):
        runtime.plan(mission)

    assert mission.plan is None
    assert mission.status is ResearchMissionStatus.CREATED


def test_failed_routing_marks_mission_failed_and_prevents_dispatch():
    runtime = ResearchMissionRuntime(
        RecordingRouter(
            [
                RoutingOutcome.SUCCESS,
                RoutingOutcome.NO_ELIGIBLE_NODES,
                RoutingOutcome.SUCCESS,
            ]
        ),
        dispatch_coordinator=RecordingDispatcher(),
    )
    mission = make_mission()
    runtime.plan(mission)

    decisions = runtime.route(mission)

    assert len(decisions) == 3
    assert mission.status is ResearchMissionStatus.FAILED
    with pytest.raises(InvalidMissionTransition, match="failed.*dispatch"):
        runtime.dispatch(
            mission,
            actor_node_id="coordinator-1",
            expires_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        )


def test_dispatch_fails_before_creating_offers_without_durable_assignment_ids():
    dispatcher = RecordingDispatcher()
    runtime = ResearchMissionRuntime(
        RecordingRouter(),
        dispatch_coordinator=dispatcher,
    )
    mission = make_mission()
    runtime.plan(mission)
    runtime.route(mission)
    mission.routing_decisions[1].assignment_id = None

    with pytest.raises(RuntimeError, match="durable assignment_id"):
        runtime.dispatch(
            mission,
            actor_node_id="coordinator-1",
            expires_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        )

    assert dispatcher.calls == []
    assert mission.dispatch_offers == ()
    assert mission.status is ResearchMissionStatus.ROUTED


def test_dispatch_preserves_successful_offers_when_a_later_offer_fails():
    dispatcher = FailingDispatcher()
    runtime = ResearchMissionRuntime(
        RecordingRouter(),
        dispatch_coordinator=dispatcher,
    )
    mission = make_mission()
    runtime.plan(mission)
    runtime.route(mission)

    with pytest.raises(RuntimeError, match="second offer failed"):
        runtime.dispatch(
            mission,
            actor_node_id="coordinator-1",
            expires_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        )

    assert mission.dispatch_offers == ({"offer": "assignment-1"},)
    assert mission.status is ResearchMissionStatus.ROUTED


def test_terminal_transitions_are_explicit_and_do_not_execute_work():
    runtime = ResearchMissionRuntime(RecordingRouter())

    completed = make_mission(mission_id="mission-complete")
    runtime.plan(completed)
    runtime.route(completed)
    runtime.complete(completed)
    assert completed.status is ResearchMissionStatus.COMPLETED

    cancelled = make_mission(mission_id="mission-cancel")
    runtime.cancel(cancelled)
    assert cancelled.status is ResearchMissionStatus.CANCELLED
    with pytest.raises(InvalidMissionTransition):
        runtime.plan(cancelled)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("mission_id", "", ValueError),
        ("objective", "  ", ValueError),
        ("research_context", [], TypeError),
        ("approval_required", "yes", TypeError),
        ("required_capabilities", {"python_execution"}, TypeError),
    ],
)
def test_mission_rejects_invalid_contract_values(field, value, error):
    with pytest.raises(error):
        make_mission(**{field: value})
