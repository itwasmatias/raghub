from pathlib import Path
import sqlite3

from sports.application.nba_demo_runtime import (
    BasketballDemoRuntime,
    NbaDemoRuntime,
)


def test_nba_demo_runtime_alias_is_preserved():
    assert NbaDemoRuntime is BasketballDemoRuntime


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


class FailingGameLogRepository:
    def list_by_season(self, season: str):
        raise RuntimeError("no such column: league")


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

    runtime = BasketballDemoRuntime(
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

    runtime = BasketballDemoRuntime(
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

    runtime = BasketballDemoRuntime(
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

    runtime = BasketballDemoRuntime(
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

    runtime = BasketballDemoRuntime(
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


def test_get_trending_players_catches_cache_read_errors():
    runtime = BasketballDemoRuntime(
        current_season="2025-26",
        previous_season="2024-25",
        database_path="data/test_runtime.db",
        source=object(),
        stats_repository=FakeStatsRepository(),
        game_log_repository=FailingGameLogRepository(),
        state_repository=object(),
        refresh_service=FakeRefreshService(),
        trending_service=FakeTrendingPlayerService(),
    )

    players = runtime.get_trending_players()

    assert players == []
    message = runtime.get_load_status_message()
    assert message is not None
    assert "Unable to read historical basketball cache" in message
    assert "no such column: league" in message


def test_build_feature_ui_handler_returns_configured_subclass():
    runtime = BasketballDemoRuntime(
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
    assert callable(configured.learning_summary_provider)


def test_get_learning_summary_returns_default_zero_metrics(tmp_path: Path):
    runtime = BasketballDemoRuntime(
        current_season="2025-26",
        previous_season="2024-25",
        database_path=tmp_path / "learning_summary.db",
        source=object(),
        stats_repository=FakeStatsRepository(),
        game_log_repository=FakeGameLogRepository(),
        state_repository=object(),
        refresh_service=FakeRefreshService(),
        trending_service=FakeTrendingPlayerService(),
    )

    summary = runtime.get_learning_summary()

    assert summary["evaluated_alerts"] == 0
    assert summary["accuracy"] == 0.0
    assert summary["avg_confidence"] == 0.0
    assert summary["avg_confidence_error"] == 0.0
    assert summary["calibration_error"] == 0.0
    assert summary["calibration_buckets"] == []
    assert summary["hypothesis_updates"] == []
    assert summary["reusable_knowledge"] == []


class FakeSource:
    def __init__(self, should_fail: bool):
        self.should_fail = should_fail
        self.calls = []

    def fetch_player_game_logs(self, season: str):
        self.calls.append(season)
        if self.should_fail:
            raise RuntimeError("simulated source failure")
        return []


def test_load_history_catches_errors_and_sets_readable_status(tmp_path: Path):
    runtime = BasketballDemoRuntime(
        current_season="2025-26",
        previous_season="2024-25",
        database_path=tmp_path / "runtime.db",
        source=FakeSource(should_fail=True),
    )

    runtime.load_history()

    message = runtime.get_load_status_message()
    assert message is not None
    assert "Could not load historical basketball data" in message
    assert "simulated source failure" in message


def test_load_history_clears_error_after_successful_retry(tmp_path: Path):
    source = FakeSource(should_fail=True)
    runtime = BasketballDemoRuntime(
        current_season="2025-26",
        previous_season="2024-25",
        database_path=tmp_path / "runtime.db",
        source=source,
    )

    runtime.load_history()
    assert runtime.get_load_status_message() is not None

    source.should_fail = False
    runtime.load_history()

    assert runtime.get_load_status_message() is None


def test_load_history_uses_espn_when_cdn_returns_no_real_rows(
    tmp_path: Path,
) -> None:
    class TimedOutStats:
        def fetch_player_game_logs(self, season: str) -> list[dict]:
            raise RuntimeError("timed out while loading stats")

    class EmptyCdnRows:
        def fetch_recent_game_logs(self, limit: int = 40) -> list[dict]:
            return [
                {
                    "player_id": "",
                    "player_name": "",
                    "game_id": "",
                }
            ]

    class EspnRows:
        def fetch_recent_game_logs(self) -> list[dict]:
            return [
                {
                    "player_id": "42",
                    "player_name": "Real Player",
                    "team_id": "1",
                    "team_abbreviation": "TST",
                    "team_name": "Test Team",
                    "game_id": "espn-game-1",
                    "game_date": "2026-01-05",
                    "minutes": "10:00",
                    "pts": 10,
                    "reb": 2,
                    "ast": 1,
                    "fgm": 4,
                    "fga": 8,
                    "fg3m": 1,
                    "fg3a": 2,
                    "ftm": 1,
                    "fta": 2,
                }
            ]

    runtime = BasketballDemoRuntime(
        database_path=tmp_path / "runtime.db",
        source=TimedOutStats(),
        cdn_source=EmptyCdnRows(),
        espn_source=EspnRows(),
    )

    runtime.load_history()

    details = runtime.get_load_status_details()
    assert details is not None
    assert details["success"] is True
    assert details["source_label"] == "ESPN public web data"
    assert details["games_loaded"] == 1
    assert details["players_loaded"] == 1


def test_load_history_preserves_empty_cache_message_when_all_sources_fail(
    tmp_path: Path,
) -> None:
    class TimedOutStats:
        def fetch_player_game_logs(self, season: str) -> list[dict]:
            raise RuntimeError("timed out while loading stats")

    class EmptyCdnRows:
        def fetch_recent_game_logs(self, limit: int = 40) -> list[dict]:
            return []

    class EmptyEspnRows:
        def fetch_recent_game_logs(self) -> list[dict]:
            return []

    runtime = BasketballDemoRuntime(
        database_path=tmp_path / "runtime.db",
        source=TimedOutStats(),
        cdn_source=EmptyCdnRows(),
        espn_source=EmptyEspnRows(),
    )

    runtime.load_history()

    message = runtime.get_load_status_message()
    assert message is not None
    assert "No real player records were cached" in message
    assert "Official NBA CDN" in message
    assert "ESPN public web data" in message

    details = runtime.get_load_status_details()
    assert details is not None
    assert details["success"] is False
    assert details["records_loaded"] == 0


def test_refresh_retries_when_database_is_locked(tmp_path: Path) -> None:
    class LockedThenOkStatsRepository:
        def __init__(self) -> None:
            self.calls = 0

        def list_by_season(self, season: str):
            self.calls += 1
            if self.calls == 1:
                raise sqlite3.OperationalError("database is locked")
            return [object()]

    refresh_service = FakeRefreshService()
    runtime = BasketballDemoRuntime(
        current_season="2025-26",
        previous_season="2024-25",
        database_path=tmp_path / "runtime.db",
        source=object(),
        stats_repository=LockedThenOkStatsRepository(),
        game_log_repository=FakeGameLogRepository(),
        state_repository=object(),
        refresh_service=refresh_service,
        trending_service=FakeTrendingPlayerService(),
    )
    runtime.SQLITE_LOCK_RETRY_DELAY_SECONDS = 0

    runtime.refresh()

    assert refresh_service.refresh_season_calls == []
    assert refresh_service.refresh_incremental_calls == ["2025-26"]


def test_get_trending_players_retries_when_database_is_locked(
    tmp_path: Path,
) -> None:
    class LockedThenOkGameLogRepository:
        def __init__(self) -> None:
            self.calls = 0

        def list_by_season(self, season: str):
            self.calls += 1
            if self.calls == 1:
                raise sqlite3.OperationalError("database is locked")
            return [
                {
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
            ]

    trending_service = FakeTrendingPlayerService(players=["ranked-player"])
    runtime = BasketballDemoRuntime(
        current_season="2025-26",
        previous_season="2024-25",
        database_path=tmp_path / "runtime.db",
        source=object(),
        stats_repository=FakeStatsRepository(),
        game_log_repository=LockedThenOkGameLogRepository(),
        state_repository=object(),
        refresh_service=FakeRefreshService(),
        trending_service=trending_service,
    )
    runtime.SQLITE_LOCK_RETRY_DELAY_SECONDS = 0

    players = runtime.get_trending_players(limit=3)

    assert players == ["ranked-player"]
    assert len(trending_service.calls) == 1
    assert trending_service.calls[0]["limit"] == 3
