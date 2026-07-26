from decimal import Decimal

from sports.execution.exposure import ExposureCalculator, ExposurePosition
from sports.execution.models import RiskPolicy
from sports.execution.risk import RiskContext, RiskPolicyEngine


def test_risk_engine_blocks_daily_drawdown_and_exposure_limits():
    calculator = ExposureCalculator()
    current_position = ExposurePosition(
        position_id="pos-1",
        league="WNBA",
        team="Liberty",
        player="",
        event_id="event-1",
        market_id="market-1",
        market_type="moneyline",
        outcome_id="outcome-1",
        sportsbook="DraftKings",
        strategy_id="strategy-a",
        model_version="model-v1",
        settlement_horizon="same_day",
        correlated_exposure_group="group-a",
        open_stake=Decimal("40"),
        maximum_possible_loss=Decimal("40"),
        maximum_possible_profit=Decimal("60"),
    )
    proposed = ExposurePosition(
        position_id="pos-2",
        league="WNBA",
        team="Liberty",
        player="",
        event_id="event-1",
        market_id="market-2",
        market_type="moneyline",
        outcome_id="outcome-2",
        sportsbook="FanDuel",
        strategy_id="strategy-a",
        model_version="model-v1",
        settlement_horizon="same_day",
        correlated_exposure_group="group-a",
        open_stake=Decimal("80"),
        maximum_possible_loss=Decimal("80"),
        maximum_possible_profit=Decimal("120"),
    )
    current, projected = calculator.calculate(
        (current_position,),
        available_bankroll=Decimal("100"),
        realized_pl=Decimal("-30"),
        proposed_position=proposed,
    )

    policy = RiskPolicy(
        policy_version="risk-v1",
        maximum_stake_per_position=Decimal("50"),
        maximum_percentage_of_bankroll=Decimal("0.50"),
        event_exposure_limit=Decimal("100"),
        team_exposure_limit=Decimal("100"),
        player_exposure_limit=Decimal("100"),
        league_exposure_limit=Decimal("100"),
        strategy_exposure_limit=Decimal("100"),
        correlated_cluster_limit=Decimal("100"),
        daily_loss_limit=Decimal("20"),
        weekly_drawdown_limit=Decimal("25"),
        maximum_simultaneous_positions=5,
        minimum_available_reserve=Decimal("30"),
    )
    context = RiskContext(
        proposed_position=proposed,
        current_model_status="stable",
        active_position_count=1,
        event_key="event-1",
        team_key="Liberty",
        player_key="",
        league_key="WNBA",
        strategy_key="strategy-a",
        model_version_key="model-v1",
        correlated_group_key="group-a",
    )

    decision = RiskPolicyEngine().evaluate(
        policy, current, projected, context, requested_stake=Decimal("80")
    )

    assert not decision.approved
    assert "maximum stake per position" in decision.blocking_violations
    assert "daily loss limit" in decision.blocking_violations
