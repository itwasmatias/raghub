from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from time import perf_counter
from typing import Any

from sports.intelligence.lifecycle_service import IntelligenceLifecycleService


class PlannerPermission(StrEnum):
    OBSERVE = "observe"
    RECOMMEND = "recommend"
    APPROVAL_REQUIRED = "approval_required"
    BOUNDED_AUTONOMY = "bounded_autonomy"


@dataclass(frozen=True, slots=True)
class PlannerLimits:
    max_actions: int = 10
    max_api_calls: int = 20
    retry_limit: int = 1
    max_cycle_seconds: float = 60.0
    allowed_actions: frozenset[str] = frozenset()
    external_actions: frozenset[str] = frozenset()
    stop_on_error: bool = True
    emergency_disabled: bool = False

    def __post_init__(self) -> None:
        if self.max_actions < 1:
            raise ValueError("max_actions must be positive")
        if self.retry_limit < 0:
            raise ValueError("retry_limit cannot be negative")
        if self.max_api_calls < 0:
            raise ValueError("max_api_calls cannot be negative")
        if self.max_cycle_seconds <= 0:
            raise ValueError("max_cycle_seconds must be positive")


@dataclass(frozen=True, slots=True)
class PlannerResult:
    objective: str
    permission: PlannerPermission
    status: str
    stopping_reason: str | None
    approval_queue: tuple[dict[str, str], ...] = ()
    audit_trail: tuple[dict[str, Any], ...] = ()
    outputs: dict[str, Any] = field(default_factory=dict)
    actions_consumed: int = 0
    api_calls_consumed: int = 0
    retries_consumed: int = 0
    duplicates_prevented: int = 0
    last_successful_action: str | None = None
    last_failure: str | None = None


class BoundedIntelligencePlanner:
    """Execute injected actions only within explicit permission and budget limits."""

    OBSERVATION_ACTIONS = frozenset(
        {
            "check_schedule",
            "retrieve_availability",
            "retrieve_lineups",
            "retrieve_sportsbooks",
            "retrieve_player_context",
        }
    )

    def __init__(
        self,
        lifecycle: IntelligenceLifecycleService,
        *,
        actions: dict[str, Callable[[dict[str, Any]], Any]],
    ) -> None:
        self.lifecycle = lifecycle
        self.actions = dict(actions)

    def run(
        self,
        *,
        situation_id: str,
        objective: str,
        action_names: list[str],
        permission: PlannerPermission,
        limits: PlannerLimits,
        approved_actions: frozenset[str] = frozenset(),
        context: dict[str, Any] | None = None,
    ) -> PlannerResult:
        execution_context = dict(context or {})
        started_at = perf_counter()
        audit: list[dict[str, Any]] = []
        approvals: list[dict[str, str]] = []
        outputs: dict[str, Any] = {}
        executable: list[str] = []
        seen_actions: set[str] = set()
        duplicates_prevented = 0
        if limits.emergency_disabled:
            return PlannerResult(
                objective=objective,
                permission=permission,
                status="disabled",
                stopping_reason="emergency disable switch is active",
            )

        for action_name in action_names:
            if action_name in seen_actions:
                duplicates_prevented += 1
                audit.append(
                    {
                        "action": action_name,
                        "status": "skipped",
                        "reason": "duplicate action prevented",
                    }
                )
                continue
            seen_actions.add(action_name)
            if action_name not in self.actions:
                audit.append(
                    {
                        "action": action_name,
                        "status": "rejected",
                        "reason": "action is not registered",
                    }
                )
                continue
            if limits.allowed_actions and action_name not in limits.allowed_actions:
                audit.append(
                    {
                        "action": action_name,
                        "status": "rejected",
                        "reason": "action is outside the allowed-action boundary",
                    }
                )
                continue
            if permission is PlannerPermission.RECOMMEND:
                approvals.append(
                    {
                        "action": action_name,
                        "reason": f"Proposed for objective: {objective}",
                    }
                )
                continue
            if (
                permission is PlannerPermission.APPROVAL_REQUIRED
                and action_name not in approved_actions
            ):
                approvals.append(
                    {
                        "action": action_name,
                        "reason": "Explicit approval is required.",
                    }
                )
                continue
            if (
                action_name in limits.external_actions
                and action_name not in approved_actions
            ):
                approvals.append(
                    {
                        "action": action_name,
                        "reason": "External or costly actions require human approval.",
                    }
                )
                continue
            if (
                permission is PlannerPermission.OBSERVE
                and action_name not in self.OBSERVATION_ACTIONS
            ):
                audit.append(
                    {
                        "action": action_name,
                        "status": "rejected",
                        "reason": "observe permission cannot execute this action",
                    }
                )
                continue
            executable.append(action_name)

        if approvals and not executable:
            activity = {
                "objective": objective,
                "status": "awaiting_approval",
                "approval_queue": approvals,
            }
            self.lifecycle.record_planner_activity(situation_id, activity)
            return PlannerResult(
                objective=objective,
                permission=permission,
                status="awaiting_approval",
                stopping_reason="approval required",
                approval_queue=tuple(approvals),
                audit_trail=(activity,),
                duplicates_prevented=duplicates_prevented,
            )

        stopping_reason = None
        api_calls = 0
        retries = 0
        last_successful_action = None
        last_failure = None
        for action_name in executable:
            if perf_counter() - started_at >= limits.max_cycle_seconds:
                stopping_reason = "maximum cycle duration reached"
                break
            if len(outputs) >= limits.max_actions:
                stopping_reason = "action budget exhausted"
                break
            is_api_action = action_name.startswith(("retrieve_", "check_"))
            if is_api_action and api_calls >= limits.max_api_calls:
                stopping_reason = "API call budget exhausted"
                break
            attempts = 0
            while True:
                attempts += 1
                if is_api_action:
                    api_calls += 1
                try:
                    output = self.actions[action_name](execution_context)
                    outputs[action_name] = output
                    execution_context[action_name] = output
                    activity = {
                        "objective": objective,
                        "action": action_name,
                        "status": "completed",
                        "attempts": attempts,
                        "result": output,
                    }
                    audit.append(activity)
                    last_successful_action = action_name
                    self.lifecycle.record_planner_activity(
                        situation_id, activity
                    )
                    break
                except Exception as error:
                    if attempts <= limits.retry_limit:
                        retries += 1
                        continue
                    last_failure = str(error)
                    activity = {
                        "objective": objective,
                        "action": action_name,
                        "status": "failed",
                        "attempts": attempts,
                        "error": str(error),
                    }
                    audit.append(activity)
                    self.lifecycle.record_planner_activity(
                        situation_id, activity
                    )
                    if limits.stop_on_error:
                        stopping_reason = "action failed"
                    break
            if stopping_reason:
                break

        if (
            stopping_reason is None
            and len(outputs) >= limits.max_actions
            and len(executable) > len(outputs)
        ):
            stopping_reason = "action budget exhausted"
        status = "stopped" if stopping_reason else "completed"
        return PlannerResult(
            objective=objective,
            permission=permission,
            status=status,
            stopping_reason=stopping_reason,
            approval_queue=tuple(approvals),
            audit_trail=tuple(audit),
            outputs=outputs,
            actions_consumed=len(outputs),
            api_calls_consumed=api_calls,
            retries_consumed=retries,
            duplicates_prevented=duplicates_prevented,
            last_successful_action=last_successful_action,
            last_failure=last_failure,
        )
