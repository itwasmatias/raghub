from pathlib import Path

from sports.application.basketball_demo_runtime import BasketballDemoRuntime
from sports.data.models.bootstrap_summary import BootstrapSummary
from sports.data.models.refresh_summary import RefreshSummary


class FakeLoader:
    def __init__(self) -> None:
        self.calls = 0

    def preload(self) -> BootstrapSummary:
        self.calls += 1
        return BootstrapSummary(14, 14, 14)


class FakeRefreshService:
    def __init__(self) -> None:
        self.seasons: list[str] = []

    def refresh_incremental(self, season: str) -> RefreshSummary:
        self.seasons.append(season)
        return RefreshSummary(season, 1, 1)


class FakeTrendingService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[dict], int]] = []

    def rank(
        self,
        current_season: str,
        previous_season: str,
        game_logs: list[dict],
        limit: int = 10,
    ) -> list[str]:
        self.calls.append(
            (current_season, previous_season, game_logs, limit)
        )
        return ["cached-player"]


def cached_log(season: str) -> dict[str, object]:
    return {
        "league": "NBA",
        "competition": "regular",
        "season": season,
        "season_type": "Regular Season",
        "player_id": "p1",
        "player_name": "Player One",
        "team_id": 1,
        "team_abbreviation": "TST",
        "team_name": "Test Team",
        "game_id": f"game-{season}",
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
        "source": "test",
        "loaded_at": "2025-01-01T00:00:00+00:00",
    }


def test_runtime_creates_database_directory_and_loads_history(
    tmp_path: Path,
) -> None:
    loader = FakeLoader()
    database_path = tmp_path / "nested" / "basketball.db"

    runtime = BasketballDemoRuntime(
        database_path=database_path,
        historical_loader=loader,
    )
    summary = runtime.load_history()

    assert database_path.parent.is_dir()
    assert loader.calls == 1
    assert summary.datasets_requested == 14


def test_refresh_incrementally_updates_only_cached_seasons(
    tmp_path: Path,
) -> None:
    refresh_service = FakeRefreshService()
    runtime = BasketballDemoRuntime(
        database_path=tmp_path / "basketball.db",
        refresh_service=refresh_service,
    )
    runtime.game_log_repository.save_many(
        [cached_log("2024-25"), cached_log("2025-26")]
    )

    summaries = runtime.refresh()

    assert refresh_service.seasons == ["2024-25", "2025-26"]
    assert len(summaries) == 2


def test_get_trending_players_uses_cache_and_has_no_fallback(
    tmp_path: Path,
) -> None:
    trending_service = FakeTrendingService()
    runtime = BasketballDemoRuntime(
        database_path=tmp_path / "basketball.db",
        trending_service=trending_service,
    )

    assert runtime.get_trending_players("2025-26", "2024-25") == []
    assert trending_service.calls == []

    runtime.game_log_repository.save_many([cached_log("2025-26")])
    result = runtime.get_trending_players(
        "2025-26",
        "2024-25",
        limit=5,
    )

    assert result == ["cached-player"]
    assert trending_service.calls[0][0:2] == ("2025-26", "2024-25")
    assert trending_service.calls[0][3] == 5
