from pathlib import Path

from sports.data.repositories.sqlite_player_game_log_repository import (
    SQLitePlayerGameLogRepository,
)
from sports.data.services.historical_basketball_loader import (
    HistoricalBasketballLoader,
)


class FakeSource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def fetch_player_game_logs(
        self,
        season: str,
        league_id: str,
        season_type: str,
    ) -> list[dict[str, object]]:
        self.calls.append((season, league_id, season_type))
        if season == "2026" and league_id == "10":
            return []
        return [
            {
                "player_id": "p1",
                "player_name": "Player One",
                "team_id": 1,
                "team_abbreviation": "TST",
                "team_name": "Test Team",
                "game_id": "shared-game",
                "game_date": "2025-01-01",
                "minutes": "10:00",
                "pts": 10,
                "reb": 2,
                "ast": 3,
                "fgm": 4,
                "fga": 8,
                "fg3m": 1,
                "fg3a": 3,
                "ftm": 1,
                "fta": 2,
            }
        ]


def test_preload_requests_full_competition_matrix_and_reports_empty(
    tmp_path: Path,
) -> None:
    source = FakeSource()
    repository = SQLitePlayerGameLogRepository(tmp_path / "logs.db")

    summary = HistoricalBasketballLoader(source, repository).preload()

    assert len(source.calls) == 14
    assert source.calls.count(("2023-24", "00", "Regular Season")) == 1
    assert source.calls.count(("2023-24", "00", "Playoffs")) == 1
    assert source.calls.count(("2024", "10", "Regular Season")) == 1
    assert source.calls.count(("2024", "10", "Playoffs")) == 1
    assert source.calls.count(("2024", "15", "Regular Season")) == 1
    assert summary.datasets_requested == 14
    assert summary.datasets_loaded == 13
    assert summary.games_loaded == 13
    assert len(summary.empty_datasets) == 1
    assert summary.empty_datasets[0].league == "WNBA"
    assert summary.empty_datasets[0].season == "2026"


def test_preload_keeps_same_game_identity_across_competitions_and_is_idempotent(
    tmp_path: Path,
) -> None:
    source = FakeSource()
    repository = SQLitePlayerGameLogRepository(tmp_path / "logs.db")
    loader = HistoricalBasketballLoader(source, repository)

    loader.preload()
    loader.preload()

    logs = repository.list_by_season("2024")
    assert len(logs) == 3
    assert {log["league"] for log in logs} == {"NBA", "WNBA"}
    assert {log["competition"] for log in logs} == {
        "regular",
        "playoffs",
        "summer_league",
    }
    assert all(log["source"] == "nba_api" for log in logs)
    assert all(log["loaded_at"] for log in logs)


class UnsupportedSource:
    def fetch_player_game_logs(
        self,
        season: str,
        league_id: str,
        season_type: str,
    ) -> list[dict[str, object]]:
        raise NotImplementedError


def test_preload_reports_unsupported_datasets(tmp_path: Path) -> None:
    repository = SQLitePlayerGameLogRepository(tmp_path / "logs.db")

    summary = HistoricalBasketballLoader(
        UnsupportedSource(),
        repository,
    ).preload()

    assert summary.datasets_loaded == 0
    assert len(summary.unsupported_datasets) == 14
