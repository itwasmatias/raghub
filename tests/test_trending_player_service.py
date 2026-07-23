from pathlib import Path

import pytest

from sports.data.models.player_season_stats import PlayerSeasonStats
from sports.data.repositories.sqlite_player_stats_repository import (
    SQLitePlayerStatsRepository,
)
from sports.intelligence.trending_player_service import TrendingPlayerService


def season_stats(
    player_id: str,
    player_name: str,
    season: str,
    games: int,
    points: float,
) -> PlayerSeasonStats:
    return PlayerSeasonStats(
        player_id=player_id,
        player_name=player_name,
        season=season,
        games_played=games,
        points=points,
        rebounds=0,
        assists=0,
        minutes=0,
        field_goals_made=0,
        field_goals_attempted=0,
        three_points_made=0,
        three_points_attempted=0,
        free_throws_made=0,
        free_throws_attempted=0,
    )


def game_log(
    player_id: str,
    date: str,
    points: float,
    team: str,
) -> dict[str, object]:
    return {
        "player_id": player_id,
        "game_date": date,
        "pts": points,
        "team_name": team,
    }


def test_rank_calculates_rates_from_totals_and_latest_games(
    tmp_path: Path,
) -> None:
    repository = SQLitePlayerStatsRepository(tmp_path / "stats.db")
    repository.save(season_stats("p1", "Player One", "2024-25", 10, 100))
    repository.save(season_stats("p1", "Player One", "2023-24", 30, 600))
    logs = [
        game_log("p1", "2025-01-06", 20, "New Team"),
        game_log("p1", "2025-01-01", 1, "Old Team"),
        game_log("p1", "2025-01-05", 18, "New Team"),
        game_log("p1", "2025-01-04", 16, "New Team"),
        game_log("p1", "2025-01-03", 14, "Old Team"),
        game_log("p1", "2025-01-02", 100, "Old Team"),
    ]

    result = TrendingPlayerService(repository).rank(
        "2024-25",
        "2023-24",
        logs,
    )[0]

    assert result.player_name == "Player One"
    assert result.latest_team == "New Team"
    assert result.recent_five_ppg == pytest.approx(33.6)
    assert result.current_season_ppg == 10.0
    assert result.previous_season_ppg == 20.0
    assert result.weighted_two_season_ppg == 17.5
    assert result.trend_score == pytest.approx(23.6)
    assert result.badge == "rising"
    assert result.explanation


def test_rank_orders_by_score_applies_badges_and_limit(
    tmp_path: Path,
) -> None:
    repository = SQLitePlayerStatsRepository(tmp_path / "stats.db")
    repository.save(season_stats("rise", "Rising", "current", 2, 20))
    repository.save(season_stats("stable", "Stable", "current", 2, 20))
    repository.save(season_stats("fall", "Falling", "current", 2, 20))
    logs = [
        game_log("fall", "2025-01-01", 8, "F"),
        game_log("stable", "2025-01-01", 10.5, "S"),
        game_log("rise", "2025-01-01", 12, "R"),
    ]

    results = TrendingPlayerService(repository).rank(
        "current",
        "previous",
        logs,
        limit=2,
    )

    assert [item.player_id for item in results] == ["rise", "stable"]
    assert [item.badge for item in results] == ["rising", "stable"]
    assert results[0].previous_season_ppg is None
    assert results[0].weighted_two_season_ppg == 10.0
