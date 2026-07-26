from decimal import Decimal

from sports.execution.exposure import ExposureCalculator, ExposurePosition


def test_exposure_engine_counts_correlated_positions_and_parlay_legs():
    calculator = ExposureCalculator()
    parlay_leg_one = ExposurePosition(
        position_id="leg-1",
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
        open_stake=Decimal("10"),
        maximum_possible_loss=Decimal("10"),
        maximum_possible_profit=Decimal("12"),
    )
    parlay_leg_two = ExposurePosition(
        position_id="leg-2",
        league="WNBA",
        team="Aces",
        player="",
        event_id="event-2",
        market_id="market-2",
        market_type="spread",
        outcome_id="outcome-2",
        sportsbook="FanDuel",
        strategy_id="strategy-a",
        model_version="model-v1",
        settlement_horizon="same_day",
        correlated_exposure_group="group-a",
        open_stake=Decimal("10"),
        maximum_possible_loss=Decimal("10"),
        maximum_possible_profit=Decimal("15"),
    )
    parlay = ExposurePosition(
        position_id="parlay-1",
        league="WNBA",
        team="Liberty",
        player="",
        event_id="event-parlay",
        market_id="market-parlay",
        market_type="parlay",
        outcome_id="outcome-parlay",
        sportsbook="DraftKings",
        strategy_id="strategy-a",
        model_version="model-v1",
        settlement_horizon="same_day",
        correlated_exposure_group="group-a",
        open_stake=Decimal("10"),
        maximum_possible_loss=Decimal("10"),
        maximum_possible_profit=Decimal("22"),
        legs=(parlay_leg_one, parlay_leg_two),
    )

    current, projected = calculator.calculate(
        (parlay_leg_one,),
        available_bankroll=Decimal("1000"),
        proposed_position=parlay,
    )

    assert current.exposure_by_event["event-1"] == Decimal("10")
    assert projected.exposure_by_event["event-1"] == Decimal("20")
    assert projected.exposure_by_event["event-2"] == Decimal("10")
    assert projected.exposure_by_correlated_group["group-a"] >= Decimal("20")
