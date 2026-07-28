from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from sports.founder.mlb.contracts.matchup_brief import (
    MlbBullpenContext,
    MlbFounderMatchupBrief,
    MlbLineupContext,
    MlbStartingPitcherContext,
)


NOW = datetime(
    2026,
    7,
    28,
    18,
    0,
    tzinfo=timezone.utc,
)


def make_pitcher(
    *,
    player_id: str = "pitcher-001",
    name: str = "Test Starter",
) -> MlbStartingPitcherContext:
    return MlbStartingPitcherContext(
        player_id=player_id,
        name=name,
        throws="R",
        season_era=Decimal("3.42"),
        season_whip=Decimal("1.18"),
        strikeout_rate=Decimal("0.27"),
        walk_rate=Decimal("0.07"),
        innings_last_start=Decimal("6.0"),
        pitches_last_start=94,
        days_rest=5,
        confirmed=True,
    )


def make_bullpen() -> MlbBullpenContext:
    return MlbBullpenContext(
        innings_last_3_days=Decimal("9.2"),
        pitches_last_3_days=147,
        unavailable_pitchers=("Reliever One",),
        high_leverage_available=True,
        fatigue_score=Decimal("0.38"),
    )


def make_lineup() -> MlbLineupContext:
    return MlbLineupContext(
        confirmed=True,
        projected_runs=Decimal("4.7"),
        team_woba=Decimal("0.326"),
        team_iso=Decimal("0.171"),
        handedness_advantage=Decimal("0.04"),
        missing_regulars=("Starting Catcher",),
    )


def test_starting_pitcher_context_is_immutable() -> None:
    pitcher = make_pitcher()

    with pytest.raises(AttributeError):
        pitcher.name = "Changed"  # type: ignore[misc]


def test_starting_pitcher_requires_valid_hand() -> None:
    with pytest.raises(ValueError, match="throws"):
        MlbStartingPitcherContext(
            player_id="pitcher-001",
            name="Test Starter",
            throws="X",
            season_era=Decimal("3.42"),
            season_whip=Decimal("1.18"),
            strikeout_rate=Decimal("0.27"),
            walk_rate=Decimal("0.07"),
            innings_last_start=Decimal("6.0"),
            pitches_last_start=94,
            days_rest=5,
            confirmed=True,
        )


def test_bullpen_fatigue_score_must_be_probability() -> None:
    with pytest.raises(ValueError, match="fatigue_score"):
        MlbBullpenContext(
            innings_last_3_days=Decimal("9.2"),
            pitches_last_3_days=147,
            unavailable_pitchers=(),
            high_leverage_available=True,
            fatigue_score=Decimal("1.10"),
        )


def test_lineup_handedness_advantage_allows_negative_values() -> None:
    lineup = MlbLineupContext(
        confirmed=False,
        projected_runs=Decimal("4.1"),
        team_woba=Decimal("0.311"),
        team_iso=Decimal("0.155"),
        handedness_advantage=Decimal("-0.06"),
        missing_regulars=(),
    )

    assert lineup.handedness_advantage == Decimal("-0.06")


def test_matchup_brief_preserves_baseball_context() -> None:
    brief = MlbFounderMatchupBrief(
        brief_id="mlb-brief-001",
        event_id="mlb-event-001",
        home_team="Chicago Cubs",
        away_team="Milwaukee Brewers",
        event_start=NOW,
        generated_at=NOW,
        home_starting_pitcher=make_pitcher(
            player_id="home-pitcher",
            name="Home Starter",
        ),
        away_starting_pitcher=make_pitcher(
            player_id="away-pitcher",
            name="Away Starter",
        ),
        home_bullpen=make_bullpen(),
        away_bullpen=make_bullpen(),
        home_lineup=make_lineup(),
        away_lineup=make_lineup(),
        home_win_probability=Decimal("0.56"),
        market_home_probability=Decimal("0.52"),
        no_vig_home_probability=Decimal("0.51"),
        projected_home_runs=Decimal("4.8"),
        projected_away_runs=Decimal("4.2"),
        confidence=Decimal("0.67"),
        supporting_evidence=(
            "Home starter owns a strikeout advantage",
        ),
        contradicting_evidence=(
            "Home bullpen worked heavily yesterday",
        ),
        invalidation_conditions=(
            "Starting pitcher changes",
            "Weather postponement",
        ),
        disclaimer=(
            "Research and decision-support only. "
            "Baseball outcomes are uncertain."
        ),
    )

    assert brief.home_starting_pitcher.name == "Home Starter"
    assert brief.away_starting_pitcher.name == "Away Starter"
    assert brief.home_win_probability == Decimal("0.56")
    assert brief.probability_difference == Decimal("0.05")
    assert brief.projected_total_runs == Decimal("9.0")


def test_matchup_brief_requires_different_teams() -> None:
    with pytest.raises(ValueError, match="must differ"):
        MlbFounderMatchupBrief(
            brief_id="mlb-brief-001",
            event_id="mlb-event-001",
            home_team="Chicago Cubs",
            away_team="Chicago Cubs",
            event_start=NOW,
            generated_at=NOW,
            home_starting_pitcher=make_pitcher(),
            away_starting_pitcher=make_pitcher(
                player_id="pitcher-002",
            ),
            home_bullpen=make_bullpen(),
            away_bullpen=make_bullpen(),
            home_lineup=make_lineup(),
            away_lineup=make_lineup(),
            home_win_probability=Decimal("0.56"),
            market_home_probability=Decimal("0.52"),
            no_vig_home_probability=Decimal("0.51"),
            projected_home_runs=Decimal("4.8"),
            projected_away_runs=Decimal("4.2"),
            confidence=Decimal("0.67"),
            supporting_evidence=(),
            contradicting_evidence=(),
            invalidation_conditions=(),
            disclaimer="Research only.",
        )
