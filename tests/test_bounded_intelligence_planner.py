from pathlib import Path

from sports.data.repositories.sqlite_lifecycle_repository import (
    SQLiteLifecycleRepository,
)
from sports.intelligence.bounded_planner import (
    BoundedIntelligencePlanner,
    PlannerLimits,
    PlannerPermission,
)
from sports.intelligence.lifecycle_service import IntelligenceLifecycleService


def _lifecycle(tmp_path: Path) -> tuple[IntelligenceLifecycleService, str]:
    service = IntelligenceLifecycleService(
        SQLiteLifecycleRepository(tmp_path / "lifecycle.db")
    )
    situation = service.create_situation(
        situation_id="nba:planner:1",
        title="NBA opportunity scan",
        objective="Identify meaningful NBA player opportunities in 24 hours.",
        observation="Planning objective received.",
    )
    return service, situation.id


def test_recommend_permission_builds_approval_queue_without_execution(
    tmp_path: Path,
) -> None:
    lifecycle, situation_id = _lifecycle(tmp_path)
    calls: list[str] = []
    planner = BoundedIntelligencePlanner(
        lifecycle,
        actions={"retrieve_availability": lambda _context: calls.append("called")},
    )

    result = planner.run(
        situation_id=situation_id,
        objective="Identify meaningful NBA player opportunities in 24 hours.",
        action_names=["retrieve_availability"],
        permission=PlannerPermission.RECOMMEND,
        limits=PlannerLimits(max_actions=3, retry_limit=1),
    )

    assert calls == []
    assert result.status == "awaiting_approval"
    assert result.approval_queue[0]["action"] == "retrieve_availability"
    assert result.audit_trail


def test_bounded_permission_enforces_action_budget_and_allowed_actions(
    tmp_path: Path,
) -> None:
    lifecycle, situation_id = _lifecycle(tmp_path)
    calls: list[str] = []
    planner = BoundedIntelligencePlanner(
        lifecycle,
        actions={
            "check_schedule": lambda _context: calls.append("schedule") or {"games": 4},
            "retrieve_availability": lambda _context: calls.append("status") or {"reports": 2},
            "retrieve_sportsbooks": lambda _context: calls.append("odds") or {"markets": 3},
        },
    )

    result = planner.run(
        situation_id=situation_id,
        objective="Identify meaningful NBA player opportunities in 24 hours.",
        action_names=[
            "check_schedule",
            "retrieve_availability",
            "retrieve_sportsbooks",
        ],
        permission=PlannerPermission.BOUNDED_AUTONOMY,
        limits=PlannerLimits(
            max_actions=2,
            retry_limit=0,
            allowed_actions=frozenset(
                {"check_schedule", "retrieve_availability", "retrieve_sportsbooks"}
            ),
        ),
    )

    assert calls == ["schedule", "status"]
    assert result.status == "stopped"
    assert result.stopping_reason == "action budget exhausted"
    assert len(result.audit_trail) == 2
    assert len(lifecycle.timeline(situation_id)) == 3


def test_planner_enforces_duplicate_api_budget_and_emergency_disable(
    tmp_path: Path,
) -> None:
    lifecycle, situation_id = _lifecycle(tmp_path)
    calls: list[str] = []
    planner = BoundedIntelligencePlanner(
        lifecycle,
        actions={
            "retrieve_availability": lambda _context: calls.append("status")
        },
    )

    result = planner.run(
        situation_id=situation_id,
        objective="Monitor availability.",
        action_names=[
            "retrieve_availability",
            "retrieve_availability",
        ],
        permission=PlannerPermission.BOUNDED_AUTONOMY,
        limits=PlannerLimits(
            max_actions=4,
            max_api_calls=1,
            retry_limit=0,
            allowed_actions=frozenset({"retrieve_availability"}),
        ),
    )
    disabled = planner.run(
        situation_id=situation_id,
        objective="Monitor availability.",
        action_names=["retrieve_availability"],
        permission=PlannerPermission.BOUNDED_AUTONOMY,
        limits=PlannerLimits(emergency_disabled=True),
    )

    assert calls == ["status"]
    assert result.api_calls_consumed == 1
    assert result.duplicates_prevented == 1
    assert disabled.status == "disabled"
    assert disabled.stopping_reason == "emergency disable switch is active"
