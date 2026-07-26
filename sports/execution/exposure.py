from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from sports.execution.models import ExposureSnapshot


@dataclass(frozen=True, slots=True)
class ExposurePosition:
    position_id: str
    league: str
    team: str
    player: str
    event_id: str
    market_id: str
    market_type: str
    outcome_id: str
    sportsbook: str
    strategy_id: str
    model_version: str
    settlement_horizon: str
    correlated_exposure_group: str
    open_stake: Decimal
    maximum_possible_loss: Decimal
    maximum_possible_profit: Decimal
    realized_pl: Decimal = Decimal("0")
    estimated_open_position_value: Decimal = Decimal("0")
    legs: tuple["ExposurePosition", ...] = ()


def _zero_map() -> dict[str, Decimal]:
    return {}


def _add(target: dict[str, Decimal], key: str, amount: Decimal) -> None:
    target[key] = target.get(key, Decimal("0")) + amount


def _aggregate(
    position: ExposurePosition, totals: dict[str, dict[str, Decimal]], stake: Decimal
) -> None:
    _add(totals["league"], position.league, stake)
    _add(totals["team"], position.team, stake)
    _add(totals["player"], position.player, stake)
    _add(totals["event"], position.event_id, stake)
    _add(totals["market"], position.market_id, stake)
    _add(totals["market_type"], position.market_type, stake)
    _add(totals["outcome"], position.outcome_id, stake)
    _add(totals["sportsbook"], position.sportsbook, stake)
    _add(totals["strategy"], position.strategy_id, stake)
    _add(totals["model_version"], position.model_version, stake)
    _add(totals["settlement_horizon"], position.settlement_horizon, stake)
    _add(totals["correlated_group"], position.correlated_exposure_group, stake)

    for leg in position.legs:
        _aggregate(leg, totals, stake)


def _snapshot(
    totals: dict[str, dict[str, Decimal]],
    *,
    available_bankroll: Decimal,
    reserved_bankroll: Decimal,
    open_stake: Decimal,
    realized_pl: Decimal,
) -> ExposureSnapshot:
    max_loss = max(
        (sum(map(abs, values.values()), Decimal("0")) for values in totals.values()),
        default=Decimal("0"),
    )
    max_profit = open_stake + reserved_bankroll
    open_value = open_stake - reserved_bankroll + realized_pl
    denominator = max(open_stake, Decimal("1"))
    return ExposureSnapshot(
        exposure_by_league=totals["league"],
        exposure_by_team=totals["team"],
        exposure_by_player=totals["player"],
        exposure_by_event=totals["event"],
        exposure_by_market=totals["market"],
        exposure_by_market_type=totals["market_type"],
        exposure_by_outcome=totals["outcome"],
        exposure_by_sportsbook=totals["sportsbook"],
        exposure_by_strategy=totals["strategy"],
        exposure_by_model_version=totals["model_version"],
        exposure_by_settlement_horizon=totals["settlement_horizon"],
        exposure_by_correlated_group=totals["correlated_group"],
        available_bankroll=available_bankroll,
        reserved_bankroll=reserved_bankroll,
        open_stake=open_stake,
        maximum_possible_loss=max_loss,
        maximum_possible_profit=max_profit,
        realized_pl=realized_pl,
        estimated_open_position_value=open_value,
        event_concentration=(
            max(totals["event"].values(), default=Decimal("0")) / denominator
        ),
        team_concentration=(
            max(totals["team"].values(), default=Decimal("0")) / denominator
        ),
        strategy_concentration=(
            max(totals["strategy"].values(), default=Decimal("0")) / denominator
        ),
        model_concentration=(
            max(totals["model_version"].values(), default=Decimal("0")) / denominator
        ),
        daily_drawdown=max(Decimal("0"), -realized_pl),
        weekly_drawdown=max(Decimal("0"), -realized_pl),
    )


class ExposureCalculator:
    def calculate(
        self,
        positions: tuple[ExposurePosition, ...] | list[ExposurePosition],
        *,
        available_bankroll: Decimal,
        reserved_bankroll: Decimal = Decimal("0"),
        realized_pl: Decimal = Decimal("0"),
        proposed_position: ExposurePosition | None = None,
    ) -> tuple[ExposureSnapshot, ExposureSnapshot]:
        def _build(
            source_positions: list[ExposurePosition],
            bankroll: Decimal,
            reserved: Decimal,
        ) -> ExposureSnapshot:
            totals = {
                "league": _zero_map(),
                "team": _zero_map(),
                "player": _zero_map(),
                "event": _zero_map(),
                "market": _zero_map(),
                "market_type": _zero_map(),
                "outcome": _zero_map(),
                "sportsbook": _zero_map(),
                "strategy": _zero_map(),
                "model_version": _zero_map(),
                "settlement_horizon": _zero_map(),
                "correlated_group": _zero_map(),
            }
            open_stake = Decimal("0")
            for position in source_positions:
                open_stake += position.open_stake
                _aggregate(position, totals, position.open_stake)
            return _snapshot(
                totals,
                available_bankroll=bankroll,
                reserved_bankroll=reserved,
                open_stake=open_stake,
                realized_pl=realized_pl,
            )

        current_positions = list(positions)
        current = _build(current_positions, available_bankroll, reserved_bankroll)
        if proposed_position is None:
            return current, current
        projected_positions = current_positions + [proposed_position]
        projected = _build(
            projected_positions,
            available_bankroll - proposed_position.open_stake,
            reserved_bankroll + proposed_position.open_stake,
        )
        return current, projected
