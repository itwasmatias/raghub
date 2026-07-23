from pathlib import Path

from sports.application.nba_demo_runtime import NbaDemoRuntime


class FakeStatsRepository:
    def __init__(self, by_season=None):
        self.by_season = by_season or {}

    def list_by_season(self, season: str):
        return list(self.by_season.get(season, []))


class FakeGameLogRepository:
    def __init__(self, logs=None):
        self.logs = list(logs or [])
        self.calls = []

    def list_by_season(self, season: str):
        self.calls.append(season)
        return list(self.logs)


class FakeRefreshService:
    def __init__(self):
        self.refresh_season_calls = []
        self.refresh_incremental_calls = []

    def refresh_season(self, season: str):
        self.refresh_season_calls.append(season)

    def refresh_incremental(self, season: str):
        self.refresh_incremental_calls.append(season)


class FakeTrendingPlayerService:
    def __init__(self, players=None):
        self.players = list(players or [])
        self.calls = []

    def rank(self, *, current_season, previous_season, game_logs, limit):
        self.calls.append(
            {
                "current_season": current_season,
                "previous_season": previous_season,
                "game_logs": list(game_logs),
                "limit": limit,
            }
        )
        return list(self.players)


def test_defaults_and_database_parent_directory_created(tmp_path: Path):
    database_path = tmp_path / "nested" / "sip_nba.db"

    runtime = NbaDemoRuntime(
        database_path=database_path,
        source=object(),
        stats_repository=FakeStatsRepository(),
        game_log_repository=FakeGameLogRepository(),
        state_repository=object(),
        refresh_service=FakeRefreshService(),
        trending_service=FakeTrendingPlayerService(),
    )

    assert runtime.current_season == "2025-26"
    assert runtime.previous_season == "2024-25"
    assert runtime.database_path == database_path
    assert database_path.parent.exists()


def test_environment_overrides(monkeypatch, tmp_path: Path):
    database_path = tmp_path / "env-data" / "demo.db"
    monkeypatch.setenv("SIP_NBA_CURRENT_SEASON", "2030-31")
    monkeypatch.setenv("SIP_NBA_PREVIOUS_SEASON", "2029-30")
    monkeypatch.setenv("SIP_NBA_DATABASE", str(database_path))

    runtime = NbaDemoRuntime(
        source=object(),
        stats_repository=FakeStatsRepository(),
        game_log_repository=FakeGameLogRepository(),
        state_repository=object(),
        refresh_service=FakeRefreshService(),
        trending_service=FakeTrendingPlayerService(),
    )

    assert runtime.current_season == "2030-31"
    assert runtime.previous_season == "2029-30"
    assert runtime.database_path == database_path
    assert database_path.parent.exists()


def test_refresh_loads_previous_only_when_absent_and_refreshes_current_incrementally():
    stats_repository = FakeStatsRepository(by_season={})
    refresh_service = FakeRefreshService()

    runtime = NbaDemoRuntime(
        current_season="2025-26",
        previous_season="2024-25",
        database_path="data/test_runtime.db",
        source=object(),
        stats_repository=stats_repository,
        game_log_repository=FakeGameLogRepository(),
        state_repository=object(),
        refresh_service=refresh_service,
        trending_service=FakeTrendingPlayerService(),
    )

    runtime.refresh()

    assert refresh_service.refresh_season_calls == ["2024-25"]
    assert refresh_service.refresh_incremental_calls == ["2025-26"]

    stats_repository.by_season["2024-25"] = [object()]
    runtime.refresh()

    assert refresh_service.refresh_season_calls == ["2024-25"]
    assert refresh_service.refresh_incremental_calls == [
        "2025-26",
        "2025-26",
    ]


def test_get_trending_players_uses_current_cached_logs_and_ranking():
    logs = [
        {
            "player_id": "1",
            "player_name": "Player One",
            "season": "2025-26",
            "game_id": "game-1",
            "game_date": "2026-01-01",
            "pts": 25,
        }
    ]
    game_log_repository = FakeGameLogRepository(logs=logs)
    trending_service = FakeTrendingPlayerService(players=["ranked-player"])

    runtime = NbaDemoRuntime(
        current_season="2025-26",
        previous_season="2024-25",
        database_path="data/test_runtime.db",
        source=object(),
        stats_repository=FakeStatsRepository(),
        game_log_repository=game_log_repository,
        state_repository=object(),
        refresh_service=FakeRefreshService(),
        trending_service=trending_service,
    )

    players = runtime.get_trending_players(limit=7)

    assert players == ["ranked-player"]
    assert game_log_repository.calls == ["2025-26"]
    assert trending_service.calls == [
        {
            "current_season": "2025-26",
            "previous_season": "2024-25",
            "game_logs": logs,
            "limit": 7,
        }
    ]


def test_get_trending_players_returns_empty_when_no_cached_logs():
    trending_service = FakeTrendingPlayerService(players=["unused"])

    runtime = NbaDemoRuntime(
        current_season="2025-26",
        previous_season="2024-25",
        database_path="data/test_runtime.db",
        source=object(),
        stats_repository=FakeStatsRepository(),
        game_log_repository=FakeGameLogRepository(logs=[]),
        state_repository=object(),
        refresh_service=FakeRefreshService(),
        trending_service=trending_service,
    )

    players = runtime.get_trending_players()

    assert players == []
    assert trending_service.calls == []


def test_build_feature_ui_handler_returns_configured_subclass():
    runtime = NbaDemoRuntime(
        source=object(),
        stats_repository=FakeStatsRepository(),
        game_log_repository=FakeGameLogRepository(),
        state_repository=object(),
        refresh_service=FakeRefreshService(),
        trending_service=FakeTrendingPlayerService(),
    )

    class BaseHandler:
        pass

    configured = runtime.build_feature_ui_handler(BaseHandler)

    assert issubclass(configured, BaseHandler)
    assert configured.__name__ == "LiveNbaFeatureUIHandler"

    # These callbacks are class-level runtime bindings consumed by FeatureUIHandler.
    assert callable(configured.trending_player_provider)
    assert callable(configured.refresh_callback)
