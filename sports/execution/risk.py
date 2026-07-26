from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sports.execution.exposure import ExposurePosition
from sports.execution.models import ExposureSnapshot, RiskDecision, RiskPolicy


@dataclass(frozen=True, slots=True)
class RiskContext:
    proposed_position: ExposurePosition
    current_model_status: str
    active_position_count: int
    event_key: str
    team_key: str
    player_key: str
    league_key: str
    strategy_key: str
    model_version_key: str
    correlated_group_key: str


def _value(snapshot: ExposureSnapshot, mapping_name: str, key: str) -> Decimal:
    mapping = getattr(snapshot, mapping_name)
    return mapping.get(key, Decimal("0"))


class RiskPolicyEngine:
    def evaluate(
        self,
        policy: RiskPolicy,
        exposure_before: ExposureSnapshot,
        projected_exposure: ExposureSnapshot,
        context: RiskContext,
        *,
        requested_stake: Decimal,
    ) -> RiskDecision:
        violations: list[str] = []
        warnings: list[str] = []

        if requested_stake > policy.maximum_stake_per_position:
            violations.append("maximum stake per position")
        if (
            requested_stake
            > exposure_before.available_bankroll * policy.maximum_percentage_of_bankroll
        ):
            violations.append("maximum percentage of bankroll")
        if (
            _value(projected_exposure, "exposure_by_event", context.event_key)
            > policy.event_exposure_limit
        ):
            violations.append("event exposure limit")
        if (
            _value(projected_exposure, "exposure_by_team", context.team_key)
            > policy.team_exposure_limit
        ):
            violations.append("team exposure limit")
        if (
            _value(projected_exposure, "exposure_by_player", context.player_key)
            > policy.player_exposure_limit
        ):
            violations.append("player exposure limit")
        if (
            _value(projected_exposure, "exposure_by_league", context.league_key)
            > policy.league_exposure_limit
        ):
            violations.append("league exposure limit")
        if (
            _value(projected_exposure, "exposure_by_strategy", context.strategy_key)
            > policy.strategy_exposure_limit
        ):
            violations.append("strategy exposure limit")
        if (
            _value(
                projected_exposure,
                "exposure_by_correlated_group",
                context.correlated_group_key,
            )
            > policy.correlated_cluster_limit
        ):
            violations.append("correlated-cluster limit")
        if projected_exposure.daily_drawdown > policy.daily_loss_limit:
            violations.append("daily loss limit")
        if projected_exposure.weekly_drawdown > policy.weekly_drawdown_limit:
            violations.append("weekly drawdown limit")
        if context.active_position_count + 1 > policy.maximum_simultaneous_positions:
            violations.append("maximum simultaneous positions")
        if projected_exposure.available_bankroll < policy.minimum_available_reserve:
            violations.append("minimum available reserve")
        if (
            policy.model_status_restrictions
            and context.current_model_status in policy.model_status_restrictions
            and not policy.override_policy
        ):
            violations.append("model-status restriction")

        if context.current_model_status.startswith("experimental"):
            warnings.append("experimental model multiplier applied")

        approved_stake = requested_stake
        if context.current_model_status.startswith("experimental"):
            approved_stake = min(
                approved_stake, requested_stake * policy.experimental_model_multiplier
            )
        approved = not violations
        if violations and policy.override_policy:
            warnings.append(
                "override policy available; violations must be explicitly reasoned"
            )

        explanation = "approved" if approved else "blocked: " + ", ".join(violations)
        return RiskDecision(
            approved=approved,
            approved_stake=approved_stake if approved else Decimal("0"),
            requested_stake=requested_stake,
            blocking_violations=tuple(violations),
            warnings=tuple(warnings),
            exposure_before=exposure_before,
            projected_exposure=projected_exposure,
            policy_version=policy.policy_version,
            explanation=explanation,
        )
