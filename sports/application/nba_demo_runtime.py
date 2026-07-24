from __future__ import annotations

import os
import sqlite3
import time
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import requests

from sports.data.models.player_season_stats import PlayerSeasonStats
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
from sports.data.sources.nba_cdn_source import NbaCdnSource
from sports.data.sources.espn_basketball_source import EspnBasketballSource
from sports.data.sources.nba_stats_http_source import NbaStatsHttpSource
from sports.intelligence.trending_player_service import TrendingPlayerService


class PlayerGameLogSource(Protocol):
    def fetch_player_game_logs(self, season: str) -> list[dict[str, Any]]: ...


class OfficialSampleSource(Protocol):
    def fetch_recent_game_logs(
        self,
        limit: int = 40,
    ) -> list[dict[str, Any]]: ...


class NbaStatsHttpSeasonSource:
    """Adapts NbaStatsHttpSource to NbaRefreshService's source protocol."""

    def __init__(
        self,
        *,
        http_source: NbaStatsHttpSource | None = None,
        league_id: str = "00",
        season_type: str = "Regular Season",
        league_label: str = "NBA",
        competition_label: str = "Regular Season",
    ) -> None:
        self.http_source = http_source or NbaStatsHttpSource()
        self.league_id = league_id
        self.season_type = season_type
        self.league_label = league_label
        self.competition_label = competition_label

    def fetch_player_game_logs(self, season: str) -> list[dict[str, Any]]:
        rows = self.http_source.fetch_player_game_logs(
            season=season,
            league_id=self.league_id,
            season_type=self.season_type,
        )
        return [
            {
                **row,
                "league": self.league_label,
                "competition": self.competition_label,
                "season_type": self.season_type,
                "source": "nba-stats-http",
            }
            for row in rows
        ]


class BasketballDemoRuntime:
    DEFAULT_CURRENT_SEASON = "2025-26"
    DEFAULT_PREVIOUS_SEASON = "2024-25"
    DEFAULT_DATABASE_PATH = "data/sip_nba.db"
    SQLITE_LOCK_RETRIES = 4
    SQLITE_LOCK_RETRY_DELAY_SECONDS = 0.1

    def __init__(
        self,
        *,
        current_season: str | None = None,
        previous_season: str | None = None,
        database_path: str | Path | None = None,
        source: PlayerGameLogSource | None = None,
        cdn_source: OfficialSampleSource | None = None,
        espn_source: OfficialSampleSource | None = None,
        stats_repository: SQLitePlayerStatsRepository | None = None,
        game_log_repository: SQLitePlayerGameLogRepository | None = None,
        state_repository: SQLiteRefreshStateRepository | None = None,
        refresh_service: NbaRefreshService | None = None,
        trending_service: TrendingPlayerService | None = None,
    ) -> None:
        self.current_season = current_season or self._get_env_value(
            ["SIP_NBA_CURRENT_SEASON", "NBA_CURRENT_SEASON"],
            self.DEFAULT_CURRENT_SEASON,
        )
        self.previous_season = previous_season or self._get_env_value(
            ["SIP_NBA_PREVIOUS_SEASON", "NBA_PREVIOUS_SEASON"],
            self.DEFAULT_PREVIOUS_SEASON,
        )
        database_value = database_path or self._get_env_value(
            ["SIP_NBA_DATABASE", "NBA_DATABASE"],
            self.DEFAULT_DATABASE_PATH,
        )
        self.database_path = Path(database_value)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

        league_id = self._get_env_value(
            ["SIP_NBA_LEAGUE_ID", "NBA_LEAGUE_ID"],
            "00",
        )
        season_type = self._get_env_value(
            ["SIP_NBA_SEASON_TYPE", "NBA_SEASON_TYPE"],
            "Regular Season",
        )
        league_label = self._get_env_value(
            ["SIP_NBA_LEAGUE_LABEL", "NBA_LEAGUE_LABEL"],
            "NBA",
        )
        competition_label = self._get_env_value(
            ["SIP_NBA_COMPETITION_LABEL", "NBA_COMPETITION_LABEL"],
            season_type,
        )

        self.source = source or NbaStatsHttpSeasonSource(
            league_id=league_id,
            season_type=season_type,
            league_label=league_label,
            competition_label=competition_label,
        )
        self.cdn_source = cdn_source or NbaCdnSource()
        self.espn_source = espn_source or EspnBasketballSource()
        self.stats_repository = stats_repository or SQLitePlayerStatsRepository(
            self.database_path
        )
        self.game_log_repository = game_log_repository or SQLitePlayerGameLogRepository(
            self.database_path
        )
        self.state_repository = state_repository or SQLiteRefreshStateRepository(
            self.database_path
        )
        self.refresh_service = refresh_service or NbaRefreshService(
            self.source,
            self.stats_repository,
            self.state_repository,
            self.game_log_repository,
        )
        self.trending_service = trending_service or TrendingPlayerService(
            self.stats_repository
        )
        self.last_load_error_message: str | None = None
        self.last_load_status_message: str | None = None
        self.last_load_status_details: dict[str, Any] | None = None

    def refresh(self) -> None:
        previous_stats = self._run_with_lock_retry(
            lambda: self.stats_repository.list_by_season(self.previous_season)
        )
        if not previous_stats:
            self._run_with_lock_retry(
                lambda: self.refresh_service.refresh_season(self.previous_season)
            )

        self._run_with_lock_retry(
            lambda: self.refresh_service.refresh_incremental(self.current_season)
        )

    def get_trending_players(self, limit: int = 10) -> list[Any]:
        try:
            game_logs = self._run_with_lock_retry(
                lambda: self.game_log_repository.list_by_season(self.current_season)
            )
        except Exception as error:  # pragma: no cover - integration guard
            self.last_load_error_message = (
                "Unable to read historical basketball cache. "
                "Please load historical data again. "
                f"Details: {error}"
            )
            return []

        if not game_logs:
            return []

        return self.trending_service.rank(
            current_season=self.current_season,
            previous_season=self.previous_season,
            game_logs=game_logs,
            limit=limit,
        )

    def load_history(self) -> None:
        had_error = self.last_load_error_message is not None
        try:
            self.refresh()
        except Exception as error:  # pragma: no cover - integration guard
            if self._is_timeout(error):
                cdn_error: Exception | None = None
                try:
                    cdn_load = self.load_recent_official_sample()
                except Exception as fallback_error:
                    cdn_error = fallback_error
                    cdn_load = {
                        "records_loaded": 0,
                        "games_loaded": 0,
                        "players_loaded": 0,
                    }

                if cdn_load["records_loaded"] > 0:
                    self.last_load_error_message = None
                    self.last_load_status_message = (
                        "Loaded live basketball cache from Official NBA CDN "
                        f"with {cdn_load['games_loaded']} games and "
                        f"{cdn_load['players_loaded']} players."
                    )
                    self.last_load_status_details = {
                        "success": True,
                        "source_label": "Official NBA CDN",
                        **cdn_load,
                        "message": self.last_load_status_message,
                    }
                    return

                try:
                    espn_load = self.load_recent_espn_sample()
                except Exception as espn_error:
                    self.last_load_error_message = (
                        "Could not load historical basketball data from "
                        "Official NBA CDN or ESPN public web data. "
                        f"CDN details: {cdn_error or 'zero normalized rows'}. "
                        f"ESPN details: {espn_error}"
                    )
                    self.last_load_status_message = None
                    self.last_load_status_details = {
                        "success": False,
                        "source_label": None,
                        "records_loaded": 0,
                        "games_loaded": 0,
                        "players_loaded": 0,
                        "message": self.last_load_error_message,
                    }
                    return

                if espn_load["records_loaded"] > 0:
                    self.last_load_error_message = None
                    self.last_load_status_message = (
                        "Loaded live basketball cache from ESPN public web "
                        f"data with {espn_load['games_loaded']} games and "
                        f"{espn_load['players_loaded']} players."
                    )
                    self.last_load_status_details = {
                        "success": True,
                        "source_label": "ESPN public web data",
                        **espn_load,
                        "message": self.last_load_status_message,
                    }
                    return

                self.last_load_error_message = (
                    "Could not load historical basketball data. No real "
                    "player records were cached from Official NBA CDN or "
                    "ESPN public web data."
                )
                self.last_load_status_message = None
                self.last_load_status_details = {
                    "success": False,
                    "source_label": None,
                    "records_loaded": 0,
                    "games_loaded": 0,
                    "players_loaded": 0,
                    "message": self.last_load_error_message,
                }
                return

            self.last_load_error_message = (
                f"Could not load historical basketball data. Details: {error}"
            )
            self.last_load_status_message = None
            self.last_load_status_details = {
                "success": False,
                "source_label": None,
                "records_loaded": 0,
                "games_loaded": 0,
                "players_loaded": 0,
                "message": self.last_load_error_message,
            }
            return

        self.last_load_error_message = None
        self.last_load_status_message = (
            None if had_error else "Historical NBA Stats source succeeded."
        )
        self.last_load_status_details = (
            None
            if had_error
            else {
                "success": True,
                "source_label": None,
                "records_loaded": 0,
                "games_loaded": 0,
                "players_loaded": 0,
                "message": self.last_load_status_message,
            }
        )

    def get_load_status_message(self) -> str | None:
        return self.last_load_error_message or self.last_load_status_message

    def get_load_status_details(self) -> dict[str, Any] | None:
        return self.last_load_status_details

    def load_recent_official_sample(self) -> dict[str, int]:
        rows = self.cdn_source.fetch_recent_game_logs(limit=40)
        real_rows = self._real_rows(rows)
        summary = self._summarize_rows(real_rows)
        if summary["records_loaded"] <= 0:
            return summary

        self._persist_sample(real_rows, source="nba-cdn")
        return summary

    def load_recent_espn_sample(self) -> dict[str, int]:
        rows = self.espn_source.fetch_recent_game_logs()
        real_rows = self._real_rows(rows)
        summary = self._summarize_rows(real_rows)
        if summary["records_loaded"] <= 0:
            return summary
        self._persist_sample(real_rows, source="espn_web")
        return summary

    @staticmethod
    def _real_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            row
            for row in rows
            if str(row.get("player_id") or "").strip()
            and str(row.get("player_name") or "").strip()
            and str(row.get("game_id") or "").strip()
        ]

    @staticmethod
    def _summarize_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
        game_ids = {
            str(row.get("game_id") or "").strip()
            for row in rows
            if str(row.get("game_id") or "").strip()
        }
        player_ids = {
            str(row.get("player_id") or "").strip()
            for row in rows
            if str(row.get("player_id") or "").strip()
        }
        return {
            "records_loaded": len(rows),
            "games_loaded": len(game_ids),
            "players_loaded": len(player_ids),
        }

    def _persist_sample(
        self,
        rows: list[dict[str, Any]],
        *,
        source: str,
    ) -> None:
        loaded_at = datetime.now(timezone.utc).isoformat()
        cached_rows = [
            {
                **row,
                "league": row.get("league", "NBA"),
                "competition": "regular",
                "season": self.current_season,
                "season_type": "Regular Season",
                "source": source,
                "loaded_at": loaded_at,
            }
            for row in rows
        ]
        self._run_with_lock_retry(
            lambda: self.game_log_repository.save_many(cached_rows)
        )
        self._save_sample_stats(rows)

    def _save_sample_stats(
        self,
        rows: list[dict[str, Any]],
    ) -> None:
        by_player: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            by_player[str(row["player_id"])].append(row)

        for player_id, player_rows in by_player.items():
            first = player_rows[0]
            self._run_with_lock_retry(
                lambda: self.stats_repository.save(
                    PlayerSeasonStats(
                        player_id=player_id,
                        player_name=str(first.get("player_name") or ""),
                        season=self.current_season,
                        games_played=len(player_rows),
                        points=self._sum(player_rows, "pts"),
                        rebounds=self._sum(player_rows, "reb"),
                        assists=self._sum(player_rows, "ast"),
                        minutes=sum(
                            self._minutes(row.get("minutes")) for row in player_rows
                        ),
                        field_goals_made=self._sum(player_rows, "fgm"),
                        field_goals_attempted=self._sum(player_rows, "fga"),
                        three_points_made=self._sum(player_rows, "fg3m"),
                        three_points_attempted=self._sum(player_rows, "fg3a"),
                        free_throws_made=self._sum(player_rows, "ftm"),
                        free_throws_attempted=self._sum(player_rows, "fta"),
                    )
                )
            )

    @staticmethod
    def _sum(rows: list[Mapping[str, Any]], key: str) -> float:
        return sum(float(row.get(key) or 0) for row in rows)

    @staticmethod
    def _minutes(value: Any) -> float:
        if value is None:
            return 0.0
        if isinstance(value, str) and ":" in value:
            minutes, seconds = value.split(":", maxsplit=1)
            return float(minutes) + float(seconds) / 60
        return float(value)

    @staticmethod
    def _is_timeout(error: BaseException) -> bool:
        current: BaseException | None = error
        while current is not None:
            if isinstance(current, requests.Timeout):
                return True
            if "timed out" in str(current).lower():
                return True
            current = current.__cause__
        return False

    @staticmethod
    def _is_sqlite_locked(error: BaseException) -> bool:
        current: BaseException | None = error
        while current is not None:
            if isinstance(current, sqlite3.OperationalError):
                message = str(current).lower()
                if (
                    "database is locked" in message
                    or "database table is locked" in message
                ):
                    return True
            current = current.__cause__
        return False

    def _run_with_lock_retry(self, operation):
        attempts = self.SQLITE_LOCK_RETRIES + 1
        for attempt in range(attempts):
            try:
                return operation()
            except Exception as error:
                if not self._is_sqlite_locked(error) or attempt >= attempts - 1:
                    raise
                delay = self.SQLITE_LOCK_RETRY_DELAY_SECONDS * (attempt + 1)
                time.sleep(delay)

    def build_feature_ui_handler(self, base_handler_class: type[Any]) -> type[Any]:
        runtime = self

        class LiveNbaFeatureUIHandler(base_handler_class):
            trending_player_provider = staticmethod(runtime.get_trending_players)
            refresh_callback = staticmethod(runtime.refresh)

        LiveNbaFeatureUIHandler.__name__ = "LiveNbaFeatureUIHandler"
        return LiveNbaFeatureUIHandler

    @staticmethod
    def _get_env_value(keys: list[str], default: str) -> str:
        for key in keys:
            value = os.getenv(key)
            if value:
                return value
        return default


# Backward-compatible alias.
NbaDemoRuntime = BasketballDemoRuntime
