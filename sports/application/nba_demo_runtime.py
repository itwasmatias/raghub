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
from sports.data.repositories.sqlite_autonomy_repository import (
    SQLiteAutonomyRepository,
)
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
from sports.data.services.historical_basketball_loader import (
    HistoricalBasketballLoader,
)
from sports.data.sources.nba_cdn_source import NbaCdnSource
from sports.data.sources.espn_basketball_source import EspnBasketballSource
from sports.data.sources.nba_stats_http_source import NbaStatsHttpSource
from sports.intelligence.trending_player_service import TrendingPlayerService
from sports.intelligence.autonomy_service import AutonomyService
from sports.intelligence.data_health_service import DataHealthService
from sports.intelligence.player_detail_service import PlayerDetailService
from sports.intelligence.player_intelligence_service import (
    PlayerIntelligenceService,
)
from sports.intelligence.learning_intelligence_service import (
    LearningIntelligenceService,
)
from sports.intelligence.calibration_drilldown_service import (
    CalibrationDrilldownService,
)


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
        autonomy_repository: SQLiteAutonomyRepository | None = None,
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
        self.player_intelligence_service = PlayerIntelligenceService()
        self.player_detail_service = PlayerDetailService(
            self.game_log_repository,
            self.player_intelligence_service,
        )
        self.data_health_service = DataHealthService(
            self.game_log_repository,
            HistoricalBasketballLoader.DATASETS,
        )
        self.autonomy_repository = autonomy_repository or SQLiteAutonomyRepository(
            self.database_path
        )
        self.autonomy_service = AutonomyService(self.autonomy_repository)
        self.learning_service = LearningIntelligenceService(self.autonomy_repository)
        self.calibration_drilldown_service = CalibrationDrilldownService(
            self.autonomy_repository
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

    def get_trending_players(
        self,
        limit: int = 10,
        league: str | None = None,
        competition: str | None = None,
    ) -> list[Any]:
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
        if league is not None:
            game_logs = [row for row in game_logs if row.get("league") == league]
        if competition is not None:
            game_logs = [
                row for row in game_logs if row.get("competition") == competition
            ]
        if not game_logs:
            return []

        return self.trending_service.rank(
            current_season=self.current_season,
            previous_season=self.previous_season,
            game_logs=game_logs,
            limit=limit,
        )

    def get_data_health(self) -> dict[str, Any]:
        failed: list[str] = []
        timed_out: list[str] = []
        message = self.get_load_status_message() or ""
        if self.last_load_error_message:
            failed.append(self.last_load_error_message)
        if "timed out" in message.lower():
            timed_out.append("NBA Stats")
        report = self.data_health_service.inspect(failed, timed_out)
        return {
            "confidence_ready": report.confidence_ready,
            "loaded_datasets": len(report.loaded_datasets),
            "missing_datasets": [
                {
                    "league": item.dataset.league,
                    "competition": item.dataset.competition,
                    "season": item.dataset.season,
                }
                for item in report.missing_datasets
            ],
            "games": report.games,
            "players": report.players,
            "sources": sorted(
                {item.source for item in report.loaded_datasets if item.source}
            ),
            "last_successful_refresh": report.last_successful_refresh,
            "failed_sources": failed,
            "timed_out_sources": timed_out,
        }

    def get_player_detail(self, player_id: str) -> dict[str, Any] | None:
        return self.player_detail_service.get(
            player_id,
            [
                self._prior_season(self.previous_season),
                self.previous_season,
                self.current_season,
            ],
        )

    def get_learning_summary(self) -> dict[str, Any]:
        summary = self.learning_service.evaluate_past_alerts()
        calibration = self.learning_service.calibrate_confidence(bucket_count=5)
        updates = self.learning_service.propose_hypothesis_updates()
        knowledge = self.learning_service.build_reusable_knowledge(limit=5)
        return {
            "evaluated_alerts": int(summary.get("evaluated_alerts") or 0),
            "accuracy": float(summary.get("accuracy") or 0.0),
            "avg_confidence": float(summary.get("avg_confidence") or 0.0),
            "avg_confidence_error": float(summary.get("avg_confidence_error") or 0.0),
            "calibration_error": self.learning_service.confidence_calibration_error(
                bucket_count=5
            ),
            "calibration_buckets": [
                {
                    "range": f"{bucket.lower:.2f}-{bucket.upper:.2f}",
                    "count": bucket.count,
                    "predicted": bucket.avg_predicted_confidence,
                    "observed": bucket.empirical_accuracy,
                }
                for bucket in calibration
            ],
            "hypothesis_updates": [
                {
                    "lifecycle_id": update.lifecycle_id,
                    "hypothesis": update.hypothesis,
                    "status": update.status,
                    "support_rate": update.support_rate,
                    "recommendation": update.recommendation,
                }
                for update in updates[:5]
            ],
            "reusable_knowledge": knowledge,
        }

    def get_reusable_knowledge(self, limit: int = 10) -> list[str]:
        return self.learning_service.build_reusable_knowledge(limit=limit)

    def get_calibration_details(
        self,
        bucket_count: int = 10,
        bucket_index: int | None = None,
    ) -> dict[str, Any]:
        report = self.calibration_drilldown_service.build(
            bucket_count=bucket_count,
            bucket_index=bucket_index,
        )
        return {
            "bucket_count": report.bucket_count,
            "evaluated_alerts": report.evaluated_alerts,
            "correct_alerts": report.correct_alerts,
            "accuracy": report.accuracy,
            "average_confidence": report.average_confidence,
            "calibration_error": report.calibration_error,
            "buckets": [
                {
                    "index": bucket.index,
                    "range": f"{bucket.lower:.2f}-{bucket.upper:.2f}",
                    "lower": bucket.lower,
                    "upper": bucket.upper,
                    "count": bucket.count,
                    "correct_count": bucket.correct_count,
                    "predicted": bucket.average_confidence,
                    "observed": bucket.accuracy,
                    "error": bucket.calibration_error,
                    "alerts": [
                        {
                            "alert_id": alert.alert_id,
                            "player_id": alert.player_id,
                            "signal": alert.signal,
                            "created_at": alert.created_at,
                            "confidence": alert.confidence,
                            "correct": alert.correct,
                            "confidence_error": alert.confidence_error,
                            "baseline_value": alert.baseline_value,
                            "observed_value": alert.observed_value,
                            "continued": alert.continued,
                            "role_grew": alert.role_grew,
                            "evaluated_at": alert.evaluated_at,
                            "evidence": list(alert.evidence),
                            "outcome_notes": alert.outcome_notes,
                        }
                        for alert in bucket.alerts
                    ],
                }
                for bucket in report.buckets
            ],
        }

    def get_top_hypothesis_update(
        self,
        player_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the strongest research update available for a detail view.

        Research lifecycles are currently global, so ``player_id`` is accepted as
        a forward-compatible filter but does not alter selection.
        """
        del player_id
        updates = self.learning_service.propose_hypothesis_updates()
        if not updates:
            return None
        priority = {"strengthen": 3, "revise": 2, "monitor": 1, "pending": 0}
        update = max(
            updates,
            key=lambda item: (
                priority.get(item.status, 0),
                item.support_rate,
                int(item.lifecycle_id or 0),
            ),
        )
        return {
            "lifecycle_id": update.lifecycle_id,
            "hypothesis": update.hypothesis,
            "status": update.status,
            "support_rate": update.support_rate,
            "recommendation": update.recommendation,
        }

    def add_watch(
        self,
        target_type: str,
        target_value: str,
        condition: str,
    ):
        return self.autonomy_service.add_watch(
            target_type,
            target_value,
            condition,
        )

    def run_watchlists(self):
        def candidates():
            logs = self.game_log_repository.list_by_season(self.current_season)
            player_ids = {
                str(row["player_id"])
                for row in logs
                if row.get("player_id") is not None
            }
            profiles = [self.get_player_detail(player_id) for player_id in player_ids]
            return [detail["intelligence"] for detail in profiles if detail is not None]

        return self.autonomy_service.run_once(self.refresh, candidates)

    @staticmethod
    def _prior_season(season: str) -> str:
        if "-" not in season:
            try:
                return str(int(season) - 1)
            except ValueError:
                return season
        start, end = season.split("-", maxsplit=1)
        return f"{int(start) - 1}-{int(end) - 1:02d}"

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
            player_detail_provider = staticmethod(runtime.get_player_detail)
            data_health_provider = staticmethod(runtime.get_data_health)
            learning_summary_provider = staticmethod(runtime.get_learning_summary)
            calibration_detail_provider = staticmethod(
                runtime.get_calibration_details
            )
            top_hypothesis_provider = staticmethod(
                runtime.get_top_hypothesis_update
            )

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
