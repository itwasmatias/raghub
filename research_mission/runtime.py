"""Lifecycle coordination over existing federation routing and dispatch."""

from federation.routing_decision import RoutingOutcome

from research_mission.models import (
    ResearchMission,
    ResearchMissionPlan,
    ResearchMissionStatus,
)
from research_mission.planner import ResearchMissionPlanner


class InvalidMissionTransition(RuntimeError):
    """Raised when an operation is invalid for the current mission state."""


class ResearchMissionRuntime:
    """Coordinate mission contracts without executing or synthesizing research."""

    def __init__(self, task_router, *, dispatch_coordinator=None, planner=None):
        if not callable(getattr(task_router, "route", None)):
            raise TypeError("task_router must provide route(TaskRequest)")
        if dispatch_coordinator is not None and not callable(
            getattr(dispatch_coordinator, "create_offer", None)
        ):
            raise TypeError("dispatch_coordinator must provide create_offer(...) or be None")
        if planner is not None and not callable(getattr(planner, "plan", None)):
            raise TypeError("planner must provide plan(ResearchMission)")
        self._task_router = task_router
        self._dispatch_coordinator = dispatch_coordinator
        self._planner = planner or ResearchMissionPlanner()

    def plan(self, mission: ResearchMission):
        self._require(mission, ResearchMissionStatus.CREATED, "plan")
        plan = self._planner.plan(mission)
        if not isinstance(plan, ResearchMissionPlan):
            raise TypeError("planner must return a ResearchMissionPlan")
        mission.plan = plan
        mission.status = ResearchMissionStatus.PLANNED
        return mission.plan

    def route(self, mission: ResearchMission):
        self._require(mission, ResearchMissionStatus.PLANNED, "route")
        decisions = tuple(
            self._task_router.route(request)
            for request in mission.plan.to_task_requests()
        )
        mission.routing_decisions = decisions
        mission.status = (
            ResearchMissionStatus.ROUTED
            if all(decision.outcome is RoutingOutcome.SUCCESS for decision in decisions)
            else ResearchMissionStatus.FAILED
        )
        return decisions

    def dispatch(self, mission: ResearchMission, *, actor_node_id, expires_at):
        self._require(mission, ResearchMissionStatus.ROUTED, "dispatch")
        if self._dispatch_coordinator is None:
            raise RuntimeError("a dispatch coordinator is required to dispatch a mission")
        assignment_ids = tuple(
            decision.assignment_id for decision in mission.routing_decisions
        )
        if any(
            not isinstance(assignment_id, str) or not assignment_id.strip()
            for assignment_id in assignment_ids
        ):
            raise RuntimeError(
                "every routing decision requires a durable assignment_id before dispatch"
            )
        offers = []
        for assignment_id in assignment_ids:
            offer = self._dispatch_coordinator.create_offer(
                assignment_id=assignment_id,
                actor_node_id=actor_node_id,
                expires_at=expires_at,
            )
            offers.append(offer)
            mission.dispatch_offers = tuple(offers)
        mission.status = ResearchMissionStatus.DISPATCHED
        return mission.dispatch_offers

    def complete(self, mission: ResearchMission) -> None:
        self._require_one_of(
            mission,
            (ResearchMissionStatus.ROUTED, ResearchMissionStatus.DISPATCHED),
            "complete",
        )
        mission.status = ResearchMissionStatus.COMPLETED

    def cancel(self, mission: ResearchMission) -> None:
        self._require_one_of(
            mission,
            (
                ResearchMissionStatus.CREATED,
                ResearchMissionStatus.PLANNED,
                ResearchMissionStatus.ROUTED,
                ResearchMissionStatus.DISPATCHED,
            ),
            "cancel",
        )
        mission.status = ResearchMissionStatus.CANCELLED

    @staticmethod
    def _require(mission, expected, operation):
        ResearchMissionRuntime._require_one_of(mission, (expected,), operation)

    @staticmethod
    def _require_one_of(mission, expected, operation):
        if not isinstance(mission, ResearchMission):
            raise TypeError("mission must be a ResearchMission")
        if mission.status not in expected:
            raise InvalidMissionTransition(
                f"mission in {mission.status.value} status cannot {operation}"
            )
