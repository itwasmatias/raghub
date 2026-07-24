from pathlib import Path

import pytest

from sports.data.models.basketball_dataset import BasketballDataset
from sports.data.repositories.sqlite_player_game_log_repository import (
    SQLitePlayerGameLogRepository,
)
from sports.intelligence.data_health_service import DataHealthService
from sports.intelligence.player_intelligence_service import (
    PlayerIntelligenceService,
)


def log(
    season: str,
    game: int,
    points: float,
    *,
    competition: str = "regular",
    minutes: float = 20,
    attempts: float = 10,
) -> dict[str, object]:
    return {
        "league": "NBA",
        "competition": competition,
        "season": season,
        "season_type": (
            "Playoffs" if competition == "playoffs" else "Regular Season"
        ),
        "player_id": "p1",
        "player_name": "Player One",
        "team_id": 1,
        "team_abbreviation": "TST",
        "team_name": "Test Team",
        "game_id": f"{season}-{competition}-{game}",
        "game_date": f"2025-01-{game:02d}",
        "minutes": minutes,
        "pts": points,
        "reb": 6,
        "ast": 4,
        "fgm": attempts / 2,
        "fga": attempts,
        "fg3m": 1,
        "fg3a": 3,
        "ftm": 2,
        "fta": 2,
        "source": "test",
        "loaded_at": "2026-01-01T00:00:00+00:00",
    }


def test_data_health_reports_loaded_missing_and_failures(
    tmp_path: Path,
) -> None:
    repository = SQLitePlayerGameLogRepository(tmp_path / "logs.db")
    repository.save_many([log("2024-25", 1, 10)])
    expected = [
        BasketballDataset("NBA", "regular", "2024-25", "Regular Season"),
        BasketballDataset("NBA", "playoffs", "2024-25", "Playoffs"),
    ]

    report = DataHealthService(repository, expected).inspect(
        failed_sources=["WNBA"],
        timed_out_sources=["NBA Stats"],
    )

    assert len(report.loaded_datasets) == 1
    assert len(report.missing_datasets) == 1
    assert report.games == 1
    assert report.players == 1
    assert report.last_successful_refresh is not None
    assert report.confidence_ready is False


def test_three_season_profiles_never_mix_competitions() -> None:
    rows = [
        *[log("2023-24", index, 10) for index in range(1, 4)],
        *[log("2024-25", index, 20) for index in range(4, 7)],
        *[
            log("2025-26", index, 30, minutes=30, attempts=16)
            for index in range(7, 13)
        ],
        *[
            log("2025-26", index, 15, competition="playoffs")
            for index in range(13, 16)
        ],
        *[
            log("2025-26", index, 50, competition="summer_league")
            for index in range(16, 19)
        ],
    ]

    result = PlayerIntelligenceService().analyze(
        "p1",
        rows,
        ["2023-24", "2024-25", "2025-26"],
    )

    assert result is not None
    regular = result.profiles["NBA:regular"]
    assert regular.baseline.games == 12
    assert regular.baseline.points == 22.5
    assert regular.current.points == 30
    assert regular.previous is not None
    assert regular.previous.points == 20
    assert regular.recent_five.points == 30
    assert regular.recent_ten.games == 10
    assert regular.baseline.field_goal_percentage == pytest.approx(0.5)
    assert result.profiles["NBA:playoffs"].baseline.points == 15
    assert result.profiles["NBA:summer_league"].baseline.points == 50
    assert result.playoff_vs_regular_ppg == pytest.approx(-7.5)
    assert regular.volatility.consistency_score < 100
    assert regular.role_change.minutes_change > 0
    assert regular.role_change.usage_change > 0
