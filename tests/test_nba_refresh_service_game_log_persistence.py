from pathlib import Path

from sports.data.repositories.sqlite_player_game_log_repository import (
    SQLitePlayerGameLogRepository,
)
from sports.data.repositories.sqlite_player_stats_repository import (
    SQLitePlayerStatsRepository,
)
from sports.data.repositories.sqlite_refresh_state_repository import (
    SQLiteRefreshStateRepository,
)
from sports.data.services.nba_refresh_service import NbaRefreshService


class FakeSource:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def fetch_player_game_logs(
        self,
        season: str,
    ) -> list[dict[str, object]]:
        return list(self.rows)


def make_log(game_id: str, game_date: str) -> dict[str, object]:
    return {
        "player_id": "player-1",
        "player_name": "Test Player",
        "team_id": 1,
        "team_abbreviation": "TST",
        "team_name": "Test Team",
        "game_id": game_id,
        "game_date": game_date,
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


def test_refresh_season_persists_all_logs_without_duplicates(
    tmp_path: Path,
) -> None:
    season = "2024-25"
    source = FakeSource(
        [
            make_log("game-1", "2025-01-01"),
            make_log("game-2", "2025-01-02"),
        ]
    )
    stats = SQLitePlayerStatsRepository(tmp_path / "stats.db")
    logs = SQLitePlayerGameLogRepository(tmp_path / "logs.db")
    service = NbaRefreshService(
        source,
        stats,
        game_log_repository=logs,
    )

    service.refresh_season(season)
    service.refresh_season(season)

    stored = logs.list_by_season(season)
    assert [row["game_id"] for row in stored] == ["game-1", "game-2"]
    assert all(row["season"] == season for row in stored)


def test_incremental_refresh_persists_only_new_logs(
    tmp_path: Path,
) -> None:
    season = "2024-25"
    source = FakeSource([make_log("game-1", "2025-01-01")])
    stats = SQLitePlayerStatsRepository(tmp_path / "stats.db")
    state = SQLiteRefreshStateRepository(tmp_path / "state.db")
    logs = SQLitePlayerGameLogRepository(tmp_path / "logs.db")
    service = NbaRefreshService(
        source,
        stats,
        state,
        game_log_repository=logs,
    )

    service.refresh_incremental(season)
    source.rows.append(make_log("game-2", "2025-01-02"))
    service.refresh_incremental(season)
    service.refresh_incremental(season)

    stored = logs.list_by_season(season)
    assert [row["game_id"] for row in stored] == ["game-1", "game-2"]
