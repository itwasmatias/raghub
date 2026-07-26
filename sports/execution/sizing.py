from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sports.execution.models import SizingPolicy, SizingResult


@dataclass(frozen=True, slots=True)
class SizingContext:
    reconciled_probability: Decimal
    decimal_profit_multiple: Decimal
    bankroll: Decimal
    per_position_limit: Decimal
    event_limit: Decimal
    team_limit: Decimal
    player_limit: Decimal
    correlated_limit: Decimal
    strategy_limit: Decimal
    daily_risk_budget: Decimal
    available_bankroll: Decimal
    experimental_model: bool = False


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


class KellySizer:
    def size(self, policy: SizingPolicy, context: SizingContext) -> SizingResult:
        if context.decimal_profit_multiple <= 0:
            return SizingResult(
                stake=Decimal("0"),
                kelly_fraction=Decimal("0"),
                kelly_amount=Decimal("0"),
                limiting_factor="non_positive_profit_multiple",
                calculation_audit={"policy_version": policy.policy_version},
            )

        probability = context.reconciled_probability
        loss_probability = Decimal("1") - probability
        kelly_fraction = (
            (context.decimal_profit_multiple * probability) - loss_probability
        ) / context.decimal_profit_multiple
        if kelly_fraction <= 0:
            return SizingResult(
                stake=Decimal("0"),
                kelly_fraction=kelly_fraction,
                kelly_amount=Decimal("0"),
                limiting_factor="negative_kelly",
                calculation_audit={
                    "probability": str(probability),
                    "decimal_profit_multiple": str(context.decimal_profit_multiple),
                    "loss_probability": str(loss_probability),
                    "policy_version": policy.policy_version,
                },
            )

        kelly_amount = (
            context.bankroll * kelly_fraction * policy.fractional_kelly_multiplier
        )
        if context.experimental_model:
            kelly_amount *= policy.experimental_model_multiplier

        caps = {
            "per_position_limit": context.per_position_limit,
            "event_limit": context.event_limit,
            "team_limit": context.team_limit,
            "player_limit": context.player_limit,
            "correlated_limit": context.correlated_limit,
            "strategy_limit": context.strategy_limit,
            "daily_risk_budget": context.daily_risk_budget,
            "available_bankroll": context.available_bankroll,
        }
        limiting_factor, stake = min(caps.items(), key=lambda item: item[1])
        stake = min(stake, kelly_amount)
        limiting_factor = "kelly" if stake == kelly_amount else limiting_factor

        audit = {
            "probability": str(probability),
            "decimal_profit_multiple": str(context.decimal_profit_multiple),
            "loss_probability": str(loss_probability),
            "kelly_fraction": str(kelly_fraction),
            "kelly_amount": str(kelly_amount),
            "caps": {key: str(value) for key, value in caps.items()},
            "policy_version": policy.policy_version,
            "experimental_model": context.experimental_model,
            "fractional_kelly_multiplier": str(policy.fractional_kelly_multiplier),
        }
        return SizingResult(
            stake=_quantize(max(Decimal("0"), stake)),
            kelly_fraction=kelly_fraction,
            kelly_amount=_quantize(kelly_amount),
            limiting_factor=limiting_factor,
            calculation_audit=audit,
        )
