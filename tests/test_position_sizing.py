from decimal import Decimal

from sports.execution.models import SizingPolicy
from sports.execution.sizing import KellySizer, SizingContext


def test_fractional_kelly_uses_reconciled_probability_and_caps_the_stake():
    policy = SizingPolicy(
        policy_version="size-v1",
        fractional_kelly_multiplier=Decimal("0.25"),
        experimental_model_multiplier=Decimal("0.50"),
    )
    context = SizingContext(
        reconciled_probability=Decimal("0.58"),
        decimal_profit_multiple=Decimal("1.20"),
        bankroll=Decimal("1000"),
        per_position_limit=Decimal("30"),
        event_limit=Decimal("40"),
        team_limit=Decimal("40"),
        player_limit=Decimal("40"),
        correlated_limit=Decimal("40"),
        strategy_limit=Decimal("40"),
        daily_risk_budget=Decimal("40"),
        available_bankroll=Decimal("1000"),
    )

    result = KellySizer().size(policy, context)

    assert result.kelly_fraction > Decimal("0")
    assert result.stake <= Decimal("30")
    assert result.calculation_audit["probability"] == "0.58"


def test_negative_kelly_produces_no_position_and_experimental_models_are_reduced():
    policy = SizingPolicy(
        policy_version="size-v1",
        fractional_kelly_multiplier=Decimal("0.25"),
        experimental_model_multiplier=Decimal("0.50"),
    )
    negative = SizingContext(
        reconciled_probability=Decimal("0.40"),
        decimal_profit_multiple=Decimal("1.00"),
        bankroll=Decimal("1000"),
        per_position_limit=Decimal("30"),
        event_limit=Decimal("40"),
        team_limit=Decimal("40"),
        player_limit=Decimal("40"),
        correlated_limit=Decimal("40"),
        strategy_limit=Decimal("40"),
        daily_risk_budget=Decimal("40"),
        available_bankroll=Decimal("1000"),
    )
    experimental = SizingContext(
        reconciled_probability=Decimal("0.58"),
        decimal_profit_multiple=Decimal("1.20"),
        bankroll=Decimal("1000"),
        per_position_limit=Decimal("30"),
        event_limit=Decimal("40"),
        team_limit=Decimal("40"),
        player_limit=Decimal("40"),
        correlated_limit=Decimal("40"),
        strategy_limit=Decimal("40"),
        daily_risk_budget=Decimal("40"),
        available_bankroll=Decimal("1000"),
        experimental_model=True,
    )

    negative_result = KellySizer().size(policy, negative)
    experimental_result = KellySizer().size(policy, experimental)

    assert negative_result.stake == Decimal("0")
    assert negative_result.limiting_factor == "negative_kelly"
    assert experimental_result.stake <= Decimal("30")
