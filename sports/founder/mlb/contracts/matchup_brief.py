from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


def _validate_probability(
    name: str,
    value: Decimal | None,
) -> None:
    if value is None:
        return

    if value < Decimal("0") or value > Decimal("1"):
        raise ValueError(f"{name} must be between 0 and 1")


def _validate_nonnegative_decimal(
    name: str,
    value: Decimal | None,
) -> None:
    if value is not None and value < Decimal("0"):
        raise ValueError(f"{name} cannot be negative")


def _validate_nonnegative_integer(
    name: str,
    value: int,
) -> None:
    if value < 0:
        raise ValueError(f"{name} cannot be negative")


def _validate_aware_datetime(
    name: str,
    value: datetime,
) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class MlbStartingPitcherContext:
    player_id: str
    name: str
    throws: str
    season_era: Decimal | None
    season_whip: Decimal | None
    strikeout_rate: Decimal | None
    walk_rate: Decimal | None
    innings_last_start: Decimal | None
    pitches_last_start: int | None
    days_rest: int | None
    confirmed: bool

    def __post_init__(self) -> None:
        if not self.player_id.strip():
            raise ValueError("player_id is required")

        if not self.name.strip():
            raise ValueError("name is required")

        if self.throws not in {"L", "R"}:
            raise ValueError("throws must be L or R")

        _validate_nonnegative_decimal(
            "season_era",
            self.season_era,
        )
        _validate_nonnegative_decimal(
            "season_whip",
            self.season_whip,
        )
        _validate_probability(
            "strikeout_rate",
            self.strikeout_rate,
        )
        _validate_probability(
            "walk_rate",
            self.walk_rate,
        )
        _validate_nonnegative_decimal(
            "innings_last_start",
            self.innings_last_start,
        )

        if self.pitches_last_start is not None:
            _validate_nonnegative_integer(
                "pitches_last_start",
                self.pitches_last_start,
            )

        if self.days_rest is not None:
            _validate_nonnegative_integer(
                "days_rest",
                self.days_rest,
            )


@dataclass(frozen=True, slots=True)
class MlbBullpenContext:
    innings_last_3_days: Decimal
    pitches_last_3_days: int
    unavailable_pitchers: tuple[str, ...]
    high_leverage_available: bool
    fatigue_score: Decimal

    def __post_init__(self) -> None:
        _validate_nonnegative_decimal(
            "innings_last_3_days",
            self.innings_last_3_days,
        )
        _validate_nonnegative_integer(
            "pitches_last_3_days",
            self.pitches_last_3_days,
        )
        _validate_probability(
            "fatigue_score",
            self.fatigue_score,
        )


@dataclass(frozen=True, slots=True)
class MlbLineupContext:
    confirmed: bool
    projected_runs: Decimal | None
    team_woba: Decimal | None
    team_iso: Decimal | None
    handedness_advantage: Decimal | None
    missing_regulars: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_nonnegative_decimal(
            "projected_runs",
            self.projected_runs,
        )
        _validate_nonnegative_decimal(
            "team_woba",
            self.team_woba,
        )
        _validate_nonnegative_decimal(
            "team_iso",
            self.team_iso,
        )

        if (
            self.handedness_advantage is not None
            and (
                self.handedness_advantage < Decimal("-1")
                or self.handedness_advantage > Decimal("1")
            )
        ):
            raise ValueError(
                "handedness_advantage must be between -1 and 1"
            )


@dataclass(frozen=True, slots=True)
class MlbFounderMatchupBrief:
    brief_id: str
    event_id: str
    home_team: str
    away_team: str
    event_start: datetime
    generated_at: datetime
    home_starting_pitcher: MlbStartingPitcherContext
    away_starting_pitcher: MlbStartingPitcherContext
    home_bullpen: MlbBullpenContext
    away_bullpen: MlbBullpenContext
    home_lineup: MlbLineupContext
    away_lineup: MlbLineupContext
    home_win_probability: Decimal
    market_home_probability: Decimal | None
    no_vig_home_probability: Decimal | None
    projected_home_runs: Decimal
    projected_away_runs: Decimal
    confidence: Decimal
    supporting_evidence: tuple[str, ...]
    contradicting_evidence: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    disclaimer: str

    def __post_init__(self) -> None:
        if not self.brief_id.strip():
            raise ValueError("brief_id is required")

        if not self.event_id.strip():
            raise ValueError("event_id is required")

        if not self.home_team.strip():
            raise ValueError("home_team is required")

        if not self.away_team.strip():
            raise ValueError("away_team is required")

        if self.home_team == self.away_team:
            raise ValueError("home_team and away_team must differ")

        _validate_aware_datetime(
            "event_start",
            self.event_start,
        )
        _validate_aware_datetime(
            "generated_at",
            self.generated_at,
        )

        _validate_probability(
            "home_win_probability",
            self.home_win_probability,
        )
        _validate_probability(
            "market_home_probability",
            self.market_home_probability,
        )
        _validate_probability(
            "no_vig_home_probability",
            self.no_vig_home_probability,
        )
        _validate_probability(
            "confidence",
            self.confidence,
        )
        _validate_nonnegative_decimal(
            "projected_home_runs",
            self.projected_home_runs,
        )
        _validate_nonnegative_decimal(
            "projected_away_runs",
            self.projected_away_runs,
        )

        if not self.disclaimer.strip():
            raise ValueError("disclaimer is required")

    @property
    def probability_difference(self) -> Decimal | None:
        comparison = (
            self.no_vig_home_probability
            if self.no_vig_home_probability is not None
            else self.market_home_probability
        )

        if comparison is None:
            return None

        return self.home_win_probability - comparison

    @property
    def projected_total_runs(self) -> Decimal:
        return (
            self.projected_home_runs
            + self.projected_away_runs
        )
