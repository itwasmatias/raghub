"""Deterministic planning for research mission contracts."""

from research_mission.models import (
    ResearchMission,
    ResearchMissionPlan,
    ResearchRole,
    ResearchTaskSpec,
)


class ResearchMissionPlanner:
    """Create the minimum researcher/challenger/judge mission topology."""

    def plan(self, mission: ResearchMission) -> ResearchMissionPlan:
        if not isinstance(mission, ResearchMission):
            raise TypeError("mission must be a ResearchMission")

        researcher_id = f"{mission.mission_id}-researcher-1"
        challenger_id = f"{mission.mission_id}-challenger-1"
        judge_id = f"{mission.mission_id}-judge-1"
        shared = {
            "mission_id": mission.mission_id,
            "objective": mission.objective,
            "research_context": mission.research_context,
            "authorization_level": mission.authorization_level,
            "approval_required": mission.approval_required,
            "required_capabilities": mission.required_capabilities,
            "preferred_capabilities": mission.preferred_capabilities,
        }
        return ResearchMissionPlan(
            mission_id=mission.mission_id,
            tasks=(
                ResearchTaskSpec(
                    task_id=researcher_id,
                    role=ResearchRole.RESEARCHER,
                    sequence=1,
                    expected_result=(
                        "A structured research finding responsive to the mission "
                        "objective"
                    ),
                    **shared,
                ),
                ResearchTaskSpec(
                    task_id=challenger_id,
                    role=ResearchRole.CHALLENGER,
                    sequence=2,
                    depends_on=(researcher_id,),
                    expected_result=(
                        "A structured challenge identifying weaknesses and alternatives"
                    ),
                    **shared,
                ),
                ResearchTaskSpec(
                    task_id=judge_id,
                    role=ResearchRole.JUDGE,
                    sequence=3,
                    depends_on=(researcher_id, challenger_id),
                    expected_result="A structured judgment over the research and challenge",
                    **shared,
                ),
            ),
        )
