from __future__ import annotations

import os
import sqlite3
import time
from math import erf, sqrt
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from xml.etree import ElementTree

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
from sports.data.repositories.sqlite_intelligence_store import (
    SQLiteIntelligenceStore,
)
from sports.data.repositories.sqlite_lifecycle_repository import (
    SQLiteLifecycleRepository,
)
from sports.data.services.nba_refresh_service import NbaRefreshService
from sports.data.services.historical_basketball_loader import (
    HistoricalBasketballLoader,
)
from sports.data.services.release_ingestion_pipeline import (
    ReleaseIngestionPipeline,
)
from sports.data.sources.nba_cdn_source import NbaCdnSource
from sports.data.sources.espn_basketball_source import EspnBasketballSource
from sports.data.sources.nba_stats_http_source import NbaStatsHttpSource
from sports.data.sources.the_odds_api_source import TheOddsApiSource
from sports.betting.engine import BettingIntelligenceEngine
from sports.betting.models import BettingPolicy, ModelPrediction
from sports.betting.normalization import MarketNormalizer
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
from sports.intelligence.lifecycle_service import IntelligenceLifecycleService


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


class ProductionBasketballRuntime:
    DEFAULT_CURRENT_SEASON = "2025-26"
    DEFAULT_PREVIOUS_SEASON = "2024-25"
    DEFAULT_DATABASE_PATH = "data/sip_nba.db"
    SQLITE_LOCK_RETRIES = 4
    SQLITE_LOCK_RETRY_DELAY_SECONDS = 0.1
    PUBLIC_FEED_TTL_SECONDS = 300

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
        intelligence_store: SQLiteIntelligenceStore | None = None,
        odds_source: TheOddsApiSource | None = None,
        betting_engine: BettingIntelligenceEngine | None = None,
        clock=None,
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
        self._public_feed_cache: dict[
            tuple[str, str], tuple[float, dict[str, Any]]
        ] = {}
        self.intelligence_store = intelligence_store or SQLiteIntelligenceStore(
            self.database_path
        )
        odds_provider = os.getenv("ODDS_PROVIDER", "the_odds_api").strip().lower()
        api_key = os.getenv("ODDS_API_KEY", "").strip()
        self.odds_source = odds_source or (
            TheOddsApiSource(api_key=api_key)
            if api_key and odds_provider == "the_odds_api"
            else None
        )
        self.betting_engine = betting_engine or BettingIntelligenceEngine()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

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
        if self.odds_source is not None:
            ReleaseIngestionPipeline(
                self.intelligence_store,
                providers={"sportsbook_markets": self.odds_source},
                required_datasets=("sportsbook_markets",),
                retry_limit=2,
            ).refresh(now=self.clock())

    def get_betting_board(self) -> dict[str, Any]:
        raw_quotes = self.intelligence_store.list_records(
            "sportsbook_markets"
        )
        now = self.clock().astimezone(timezone.utc)
        classifications = {
            str(row.get("data_classification") or "delayed").lower()
            for row in raw_quotes
        }
        if classifications & {"replay", "model-only", "derived", "simulated"}:
            return {
                "data_classification": "delayed",
                "status": "classification_rejected",
                "assessments": [],
                "exclusions": [
                    "Replay, model-only, derived, or simulated records cannot enter the live betting pipeline."
                ],
                "source_health": {"status": "classification_rejected"},
                "generated_at": now.isoformat(),
                "message": "Stored market records failed the production classification gate.",
            }
        board_classification = (
            "live"
            if raw_quotes and classifications == {"live"}
            else "delayed"
        )
        health = self.intelligence_store.source_health().get(
            "sportsbook_markets",
            {
                "status": (
                    "unconfigured"
                    if self.odds_source is None
                    else "not_refreshed"
                ),
                "last_error": (
                    "Configure ODDS_API_KEY for the selected provider and run Refresh."
                    if self.odds_source is None
                    else "Run Refresh to retrieve live odds."
                ),
                "cached_records": len(raw_quotes),
            },
        )
        if not raw_quotes:
            return {
                "data_classification": board_classification,
                "status": health.get("status", "unavailable"),
                "assessments": [],
                "exclusions": [],
                "source_health": health,
                "generated_at": now.isoformat(),
                "message": health.get("last_error")
                or "No normalized sportsbook records are available.",
            }

        logs = self.game_log_repository.list_by_season(
            self.current_season
        ) + self.game_log_repository.list_by_season(self.previous_season)
        player_aliases: dict[str, str] = {}
        player_ids: dict[str, str] = {}
        team_aliases: dict[str, str] = {}
        for row in logs:
            player_name = str(row.get("player_name") or "").strip()
            player_id = str(row.get("player_id") or "").strip()
            if player_name and player_id:
                canonical = f"nba:player:{player_id}"
                player_aliases[player_name.lower()] = canonical
                player_ids[canonical] = player_id
            team_id = str(row.get("team_id") or "").strip()
            canonical_team = f"nba:team:{team_id}" if team_id else ""
            for label in (
                row.get("team_name"),
                row.get("team_abbreviation"),
            ):
                if label and canonical_team:
                    team_aliases[str(label).lower()] = canonical_team
        defaults = MarketNormalizer.release_candidate_defaults(
            players=player_aliases
        )
        normalizer = MarketNormalizer(
            team_aliases={**defaults.team_aliases, **team_aliases},
            player_aliases=player_aliases,
            sportsbook_aliases=defaults.sportsbook_aliases,
            maximum_age_seconds=self.betting_engine.policy.maximum_quote_age_seconds,
        )
        normalized = normalizer.normalize(raw_quotes, as_of=now)
        assessments = []
        targets = {
            (
                quote.event_id,
                quote.canonical_player_id,
                quote.market,
                quote.line,
                quote.outcome_definition,
            )
            for quote in normalized.rankable_quotes
        }
        forecast_quality = IntelligenceLifecycleService(
            SQLiteLifecycleRepository(self.database_path)
        ).evaluate_forecasts()
        evaluated = int(forecast_quality.get("evaluated_forecasts") or 0)
        brier = (
            float(forecast_quality["brier_score"])
            if evaluated >= 30
            else None
        )
        for event_id, canonical_player, market, line, outcome_definition in targets:
            player_id = player_ids.get(canonical_player)
            if not player_id:
                normalized.exclusions.append(
                    f"{canonical_player}: player identity could not be resolved to local game logs"
                )
                continue
            player_rows = [
                row
                for row in logs
                if str(row.get("player_id")) == player_id
            ]
            intelligence = self.player_intelligence_service.analyze(
                player_id,
                player_rows,
                [self.previous_season, self.current_season],
            )
            if intelligence is None:
                normalized.exclusions.append(
                    f"{canonical_player}: player statistics are unavailable"
                )
                continue
            profile = intelligence.profiles.get("NBA:regular") or next(
                iter(intelligence.profiles.values())
            )
            standard_deviation = max(
                1.0, profile.volatility.standard_deviation
            )
            components = tuple(
                self._normal_probability(
                    average.points,
                    float(line),
                    standard_deviation,
                )
                for average in (
                    profile.recent_three,
                    profile.recent_five,
                    profile.recent_ten,
                )
            )
            probability = sum(components) / len(components)
            prediction = ModelPrediction(
                event_id=event_id,
                market=market,
                selection="over",
                line=line,
                probability=probability,
                uncertainty=min(
                    0.2,
                    standard_deviation / max(20.0, float(line) * 3),
                ),
                model_probabilities=components,
                model_version="sip-player-opportunity-v1",
                generated_at=now,
                similar_bet_sample=evaluated,
                similar_bet_brier_score=brier,
                reasons=(
                    f"Expected minutes: {profile.advanced.expected_minutes:.1f}.",
                    f"Recent 3 scoring: {profile.recent_three.points:.1f} PPG.",
                    f"Season scoring: {profile.current.points:.1f} PPG.",
                    f"Normalized trend score: {profile.advanced.normalized_trend_score:.0%}.",
                ),
                invalidators=(
                    "Confirmed lineup or minutes restriction changes.",
                    "Sportsbook line or price moves materially.",
                ),
                canonical_player_id=canonical_player,
                outcome_definition=outcome_definition,
                forecast_type="player_points_over",
                team_id=(
                    f"nba:team:{player_rows[-1].get('team_id')}"
                    if player_rows
                    else ""
                ),
                player_role=(
                    "starter"
                    if player_rows and bool(player_rows[-1].get("starter"))
                    else "rotation"
                ),
            )
            assessments.append(
                self.betting_engine.assess(
                    prediction,
                    normalized.rankable_quotes,
                    as_of=now,
                )
            )
        assessments.sort(
            key=lambda item: (
                item.qualified,
                item.expected_return or float("-inf"),
            ),
            reverse=True,
        )
        return {
            "data_classification": board_classification,
            "status": "ready" if assessments else "not_rankable",
            "assessments": assessments,
            "exclusions": normalized.exclusions,
            "source_health": health,
            "generated_at": now.isoformat(),
            "message": (
                "Qualified opportunities passed every evidence and calibration gate."
                if any(item.qualified for item in assessments)
                else "Markets were analyzed, but no option passed every best-bet gate."
            ),
        }

    def get_betting_assessments(self) -> list[Any]:
        return list(self.get_betting_board().get("assessments") or [])

    @staticmethod
    def _normal_probability(
        expected: float, line: float, standard_deviation: float
    ) -> float:
        z_score = (line - expected) / max(standard_deviation, 1.0)
        return max(
            0.01,
            min(0.99, 0.5 * (1 - erf(z_score / sqrt(2)))),
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

    def get_dashboard_snapshot(
        self,
        league: str | None = None,
        competition: str | None = None,
        research_query: str | None = None,
    ) -> dict[str, Any]:
        try:
            game_logs = self._run_with_lock_retry(
                lambda: self.game_log_repository.list_by_season(self.current_season)
            )
        except Exception:
            game_logs = []

        selected_league = league or None
        selected_competition = competition or None
        trending_players = self.get_trending_players(
            limit=6,
            league=selected_league,
            competition=selected_competition,
        )
        health = self.get_data_health()
        learning = self.get_learning_summary()
        top_hypothesis = self.get_top_hypothesis_update()
        team_form = self._build_team_form(game_logs, trending_players)
        upcoming_games = self._build_upcoming_games(team_form)
        featured_matchup = self._build_featured_matchup(team_form, upcoming_games)
        public_feeds = self._get_public_dashboard_feeds(
            league=selected_league,
            research_query=research_query,
            trending_players=trending_players,
        )

        alerts = []
        for player in trending_players[:3]:
            recent = float(getattr(player, "recent_five_ppg", 0.0) or 0.0)
            season = float(getattr(player, "current_season_ppg", 0.0) or 0.0)
            alerts.append(
                {
                    "title": f"{getattr(player, 'player_name', 'Player')} trending {getattr(player, 'badge', 'stable')}",
                    "body": getattr(
                        player, "explanation", "SIP found a live trend signal."
                    ),
                    "meta": f"{getattr(player, 'latest_team', 'Unknown team')} · {recent - season:+.1f} vs season baseline",
                    "severity": "good" if recent >= season else "warn",
                }
            )

        alerts.extend(public_feeds.get("alerts") or [])

        if top_hypothesis:
            alerts.append(
                {
                    "title": "Top hypothesis update",
                    "body": str(
                        top_hypothesis.get("recommendation")
                        or top_hypothesis.get("hypothesis")
                        or "Research cycle updated."
                    ),
                    "meta": str(top_hypothesis.get("status") or "monitoring"),
                    "severity": "info",
                }
            )

        watchlist = [
            {
                "name": getattr(player, "player_name", "Player"),
                "team": getattr(player, "latest_team", "Unknown team"),
                "tag": str(getattr(player, "badge", "stable")).upper(),
                "summary": getattr(player, "explanation", "Trend under review."),
                "confidence": max(
                    45,
                    min(
                        92,
                        int(
                            round(
                                58
                                + (
                                    float(
                                        getattr(player, "recent_five_ppg", 0.0) or 0.0
                                    )
                                    - float(
                                        getattr(player, "current_season_ppg", 0.0)
                                        or 0.0
                                    )
                                )
                                * 8
                            )
                        ),
                    ),
                ),
            }
            for player in trending_players[:4]
        ]

        hypotheses = [
            {
                "label": f"H{index}",
                "title": item.get("hypothesis")
                or item.get("recommendation")
                or "Research hypothesis",
                "status": item.get("status") or "monitoring",
                "support": item.get("support_rate")
                if item.get("support_rate") is not None
                else 0.0,
            }
            for index, item in enumerate(
                learning.get("hypothesis_updates") or [], start=1
            )
            if isinstance(item, dict)
        ][:3]

        insights = self._build_insights(team_form, trending_players, health, learning)
        confidence = self._build_confidence_payload(
            health, learning, len(trending_players)
        )
        market_board = public_feeds.get("market_board") or self._build_market_board(
            upcoming_games,
            team_form,
        )

        metrics = [
            {
                "label": "Watchlist Alerts",
                "value": len(watchlist),
                "delta": f"{sum(1 for item in watchlist if item['tag'] == 'RISING')} new",
                "tone": "good",
            },
            {
                "label": "Tracked Players",
                "value": len(trending_players),
                "delta": f"{len(team_form)} teams",
                "tone": "info",
            },
            {
                "label": "Active Hypotheses",
                "value": len(hypotheses),
                "delta": f"{int(learning.get('evaluated_alerts') or 0)} evaluated",
                "tone": "warn",
            },
            {
                "label": "Model Accuracy",
                "value": f"{int(float(learning.get('accuracy') or 0.0) * 100)}%",
                "delta": f"Confidence {confidence['overall']}%",
                "tone": "good"
                if float(learning.get("accuracy") or 0.0) >= 0.6
                else "warn",
            },
        ]

        spotlight = None
        if trending_players:
            player = trending_players[0]
            recent = float(getattr(player, "recent_five_ppg", 0.0) or 0.0)
            season = float(getattr(player, "current_season_ppg", 0.0) or 0.0)
            previous = float(getattr(player, "previous_season_ppg", 0.0) or 0.0)
            spotlight = {
                "name": getattr(player, "player_name", "Player"),
                "team": getattr(player, "latest_team", "Unknown team"),
                "badge": getattr(player, "badge", "stable"),
                "trend_score": max(
                    10,
                    min(
                        99,
                        int(
                            round(50 + (recent - season) * 6 + (season - previous) * 2)
                        ),
                    ),
                ),
                "summary": getattr(player, "explanation", "SIP trend signal."),
                "stats": [
                    {"label": "PPG", "value": f"{recent:.1f}"},
                    {"label": "Season", "value": f"{season:.1f}"},
                    {"label": "Prev", "value": f"{previous:.1f}"},
                    {"label": "Delta", "value": f"{recent - season:+.1f}"},
                ],
            }

        return {
            "source_label": str(
                (self.get_load_status_details() or {}).get("source_label")
                or "Basketball data loaded"
            ),
            "health": health,
            "learning": learning,
            "metrics": metrics,
            "featured_matchup": featured_matchup,
            "spotlight": spotlight,
            "alerts": alerts[:4],
            "watchlist": watchlist,
            "team_form": team_form,
            "market_board": market_board,
            "upcoming_games": upcoming_games,
            "hypotheses": hypotheses,
            "insights": insights,
            "public_pulse": public_feeds.get("public_pulse") or {},
            "confidence": confidence,
        }

    def _get_public_dashboard_feeds(
        self,
        *,
        league: str | None,
        research_query: str | None,
        trending_players: list[Any],
    ) -> dict[str, Any]:
        league_key = (league or "NBA").upper()
        social_query = (research_query or "").strip() or self._default_public_query(
            trending_players,
            league_key,
        )
        cache_key = (league_key, social_query.lower())
        now = time.time()
        cached = self._public_feed_cache.get(cache_key)
        if cached and (now - cached[0]) <= self.PUBLIC_FEED_TTL_SECONDS:
            return cached[1]

        payload: dict[str, Any] = {"alerts": [], "market_board": [], "public_pulse": {}}
        try:
            payload = self._fetch_public_dashboard_feeds(
                league=league_key,
                social_query=social_query,
            )
        except Exception:
            payload = {"alerts": [], "market_board": [], "public_pulse": {}}

        self._public_feed_cache[cache_key] = (now, payload)
        return payload

    def _fetch_public_dashboard_feeds(
        self,
        *,
        league: str,
        social_query: str,
    ) -> dict[str, Any]:
        scoreboard = self._fetch_json(
            f"https://site.api.espn.com/apis/site/v2/sports/basketball/{league.lower()}/scoreboard"
        )
        league_news = self._fetch_json(
            f"https://site.api.espn.com/apis/site/v2/sports/basketball/{league.lower()}/news"
        )
        summaries = []
        for event in (scoreboard.get("events") or [])[:3]:
            event_id = str(event.get("id") or "").strip()
            if not event_id:
                continue
            try:
                summary = self._fetch_json(
                    f"https://site.api.espn.com/apis/site/v2/sports/basketball/{league.lower()}/summary?event={event_id}"
                )
            except Exception:
                continue
            summaries.append(summary)

        alerts = self._build_public_alerts(league_news, summaries)
        market_board = self._build_external_market_board(scoreboard, summaries)
        public_pulse = self._build_public_pulse(social_query)
        return {
            "alerts": alerts,
            "market_board": market_board,
            "public_pulse": public_pulse,
        }

    @staticmethod
    def _default_public_query(
        trending_players: list[Any],
        league_key: str,
    ) -> str:
        if trending_players:
            return str(getattr(trending_players[0], "player_name", "") or league_key)
        return league_key

    @staticmethod
    def _fetch_json(url: str) -> dict[str, Any]:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=12,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _fetch_text(url: str) -> str:
        response = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/xml,text/xml,*/*",
            },
            timeout=12,
        )
        response.raise_for_status()
        return response.text

    def _build_public_alerts(
        self,
        league_news: dict[str, Any],
        summaries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        seen_titles: set[str] = set()

        for article in (league_news.get("articles") or [])[:3]:
            title = str(article.get("headline") or "").strip()
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            alerts.append(
                {
                    "title": title,
                    "body": str(article.get("description") or "ESPN headline"),
                    "meta": "League news",
                    "severity": "info",
                    "url": self._article_url(article),
                    "published": self._format_timestamp(
                        article.get("published") or article.get("lastModified")
                    ),
                }
            )

        for summary in summaries:
            for injury in (summary.get("injuries") or [])[:2]:
                athlete = injury.get("athlete") or {}
                team = injury.get("team") or {}
                status = injury.get("status") or {}
                title = str(athlete.get("displayName") or "").strip()
                if not title:
                    continue
                headline = f"{title} injury update"
                if headline in seen_titles:
                    continue
                seen_titles.add(headline)
                alerts.append(
                    {
                        "title": headline,
                        "body": str(
                            status.get("description")
                            or injury.get("details")
                            or "Listed on the current ESPN injury report."
                        ),
                        "meta": f"{team.get('abbreviation', 'TEAM')} · injury report",
                        "severity": "warn",
                        "url": self._team_link(team),
                        "published": "Live summary",
                    }
                )
            news = summary.get("news") or {}
            if isinstance(news, dict):
                for article in (news.get("articles") or [])[:1]:
                    title = str(article.get("headline") or "").strip()
                    if not title or title in seen_titles:
                        continue
                    seen_titles.add(title)
                    alerts.append(
                        {
                            "title": title,
                            "body": str(
                                article.get("description") or "Game-linked coverage."
                            ),
                            "meta": "Game summary news",
                            "severity": "info",
                            "url": self._article_url(article),
                            "published": self._format_timestamp(
                                article.get("published") or article.get("lastModified")
                            ),
                        }
                    )
        return alerts[:6]

    def _build_external_market_board(
        self,
        scoreboard: dict[str, Any],
        summaries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        event_map = {
            str(event.get("id") or ""): event
            for event in (scoreboard.get("events") or [])
        }
        rows: list[dict[str, Any]] = []
        for summary in summaries[:4]:
            header = summary.get("header") or {}
            competitions = header.get("competitions") or []
            competition = competitions[0] if competitions else {}
            event_id = str(header.get("id") or competition.get("id") or "").strip()
            event = event_map.get(event_id) or {}
            competitors = (
                competition.get("competitors") or event.get("competitions") or [{}]
            )[0]
            teams = competition.get("competitors") or []
            if len(teams) >= 2:
                away = teams[0].get("team", {})
                home = teams[1].get("team", {})
                matchup = f"{away.get('abbreviation', 'AWY')} @ {home.get('abbreviation', 'HME')}"
            else:
                matchup = str(
                    header.get("shortName") or event.get("shortName") or "Tracked game"
                )

            odds_entries = summary.get("odds") or []
            pickcenter_entries = summary.get("pickcenter") or []
            ats_entries = summary.get("againstTheSpread") or []
            odds_line = self._extract_market_line(odds_entries, pickcenter_entries)
            ats_text = self._extract_ats_text(ats_entries)
            signal = odds_line[2] or ats_text or "ESPN summary"
            rows.append(
                {
                    "matchup": matchup,
                    "spread": odds_line[0] or ats_text or "N/A",
                    "total": odds_line[1] or "No total posted",
                    "signal": signal,
                }
            )
        return rows

    @staticmethod
    def _extract_market_line(
        odds_entries: list[Any],
        pickcenter_entries: list[Any],
    ) -> tuple[str, str, str]:
        source_entries = odds_entries or pickcenter_entries
        if not source_entries:
            return "", "", ""
        entry = source_entries[0] if isinstance(source_entries[0], dict) else {}
        details = str(entry.get("details") or entry.get("displayValue") or "").strip()
        over_under = str(entry.get("overUnder") or entry.get("overunder") or "").strip()
        provider = str(
            (entry.get("provider") or {}).get("name") or entry.get("provider") or ""
        ).strip()
        return details, over_under, provider

    @staticmethod
    def _extract_ats_text(ats_entries: list[Any]) -> str:
        labels = []
        for entry in ats_entries[:2]:
            if not isinstance(entry, dict):
                continue
            team = entry.get("team") or {}
            records = entry.get("records") or []
            summary = ""
            if records and isinstance(records[0], dict):
                summary = str(
                    records[0].get("summary") or records[0].get("displayValue") or ""
                ).strip()
            abbreviation = str(
                team.get("abbreviation") or team.get("displayName") or "TEAM"
            ).strip()
            if abbreviation and summary:
                labels.append(f"{abbreviation} {summary}")
        return " ATS ".join(labels)

    def _build_public_pulse(self, query: str) -> dict[str, Any]:
        rss_url = (
            "https://news.google.com/rss/search?"
            f"q={requests.utils.quote(query)}&hl=en-US&gl=US&ceid=US:en"
        )
        try:
            xml_text = self._fetch_text(rss_url)
        except Exception:
            return {}

        root = ElementTree.fromstring(xml_text)
        items = []
        scores = []
        for entry in root.findall("./channel/item")[:4]:
            title = (entry.findtext("title") or "").strip()
            link = (entry.findtext("link") or "").strip()
            pub_date = (entry.findtext("pubDate") or "").strip()
            sentiment = self._headline_sentiment(title)
            scores.append(sentiment)
            label = (
                "Positive"
                if sentiment > 0
                else "Negative"
                if sentiment < 0
                else "Mixed"
            )
            items.append(
                {
                    "title": title,
                    "url": link,
                    "source": "Google News",
                    "label": label,
                    "summary": pub_date,
                }
            )

        if not items:
            return {}

        average = sum(scores) / len(scores)
        outlook = (
            "Bullish" if average > 0.1 else "Cautious" if average < -0.1 else "Balanced"
        )
        return {
            "summary": f"{outlook} public headline pulse for {query}",
            "items": items,
        }

    @staticmethod
    def _headline_sentiment(title: str) -> float:
        positive = {
            "win",
            "wins",
            "rising",
            "surge",
            "breakout",
            "dominant",
            "strong",
            "boost",
            "return",
            "healthy",
            "signs",
            "hot",
            "improve",
            "improves",
        }
        negative = {
            "injury",
            "injured",
            "out",
            "doubtful",
            "lose",
            "loss",
            "slump",
            "concern",
            "trade",
            "problem",
            "suspend",
            "setback",
            "cuts",
            "bad",
        }
        words = {token.strip(".,:;!?()[]\"'").lower() for token in title.split()}
        score = sum(1 for word in words if word in positive) - sum(
            1 for word in words if word in negative
        )
        return max(-1.0, min(1.0, score / 3))

    @staticmethod
    def _article_url(article: dict[str, Any]) -> str:
        links = article.get("links") or {}
        mobile = links.get("mobile") if isinstance(links, dict) else None
        web = links.get("web") if isinstance(links, dict) else None
        candidates = [
            (mobile or {}).get("href") if isinstance(mobile, dict) else None,
            (web or {}).get("href") if isinstance(web, dict) else None,
            article.get("link"),
        ]
        for candidate in candidates:
            if candidate:
                return str(candidate)
        return ""

    @staticmethod
    def _team_link(team: dict[str, Any]) -> str:
        for link in team.get("links") or []:
            href = link.get("href") if isinstance(link, dict) else None
            if href:
                return str(href)
        return ""

    @staticmethod
    def _format_timestamp(value: Any) -> str:
        if not value:
            return ""
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def _build_team_form(
        self,
        game_logs: list[dict[str, Any]],
        trending_players: list[Any],
    ) -> list[dict[str, Any]]:
        team_totals: dict[str, dict[str, Any]] = {}
        for row in game_logs:
            game_id = str(row.get("game_id") or "").strip()
            team_code = str(row.get("team_abbreviation") or "").strip()
            if not game_id or not team_code:
                continue
            key = f"{game_id}:{team_code}"
            entry = team_totals.setdefault(
                key,
                {
                    "game_id": game_id,
                    "team": team_code,
                    "team_name": row.get("team_name") or team_code,
                    "game_date": str(row.get("game_date") or ""),
                    "points": 0.0,
                },
            )
            entry["points"] += float(row.get("pts") or 0.0)

        games: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for entry in team_totals.values():
            games[str(entry["game_id"])].append(entry)

        team_results: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for sides in games.values():
            if len(sides) != 2:
                continue
            first, second = sorted(sides, key=lambda item: str(item.get("team") or ""))
            first_margin = float(first["points"]) - float(second["points"])
            second_margin = -first_margin
            team_results[str(first["team"] or "")].append(
                {
                    "won": first_margin > 0,
                    "margin": first_margin,
                    "game_date": str(first.get("game_date") or ""),
                    "team_name": first.get("team_name") or first["team"],
                }
            )
            team_results[str(second["team"] or "")].append(
                {
                    "won": second_margin > 0,
                    "margin": second_margin,
                    "game_date": str(second.get("game_date") or ""),
                    "team_name": second.get("team_name") or second["team"],
                }
            )

        trending_count: dict[str, int] = defaultdict(int)
        for player in trending_players:
            team_code = str(getattr(player, "team_abbreviation", "") or "")
            latest_team = str(getattr(player, "latest_team", "") or "")
            if team_code:
                trending_count[team_code] += 1
            elif latest_team:
                trending_count[latest_team[:3].upper()] += 1

        rows = []
        for team, results in team_results.items():
            recent = sorted(
                results, key=lambda item: str(item.get("game_date") or ""), reverse=True
            )[:10]
            wins = sum(1 for item in recent if item["won"])
            losses = len(recent) - wins
            avg_margin = sum(float(item["margin"]) for item in recent) / max(
                1, len(recent)
            )
            rows.append(
                {
                    "team": team,
                    "code": team,
                    "record": f"{wins}-{losses}",
                    "trend_score": avg_margin,
                    "rising_players": trending_count.get(team, 0),
                    "avg_recent": avg_margin,
                    "avg_season": 0.0,
                    "confidence": max(
                        45,
                        min(
                            90,
                            int(
                                round(
                                    55
                                    + avg_margin * 2
                                    + trending_count.get(team, 0) * 5
                                )
                            ),
                        ),
                    ),
                }
            )
        rows.sort(
            key=lambda item: (item["confidence"], item["trend_score"]), reverse=True
        )
        return rows[:5]

    def _build_upcoming_games(
        self, team_form: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        games = []
        try:
            schedule = self.cdn_source._get_json(self.cdn_source.schedule_url)
        except Exception:
            schedule = {}

        game_dates = schedule.get("leagueSchedule", {}).get("gameDates", [])
        for date_entry in game_dates:
            for game in date_entry.get("games", []):
                if game.get("gameStatus") == 3:
                    continue
                away = game.get("awayTeam", {})
                home = game.get("homeTeam", {})
                start_time = str(
                    game.get("gameDateTimeUTC")
                    or game.get("gameTimeUTC")
                    or date_entry.get("gameDate")
                    or ""
                )
                games.append(
                    {
                        "matchup": f"{away.get('teamTricode', 'TBD')} @ {home.get('teamTricode', 'TBD')}",
                        "time": start_time[:16].replace("T", " "),
                        "edge": "Live schedule",
                    }
                )
                if len(games) >= 4:
                    return games

        if not games and team_form:
            for row in team_form[:4]:
                games.append(
                    {
                        "matchup": f"{row.get('code', 'TBD')} follow-up",
                        "time": "Tracked window",
                        "edge": f"{int(row.get('confidence', 50))}% confidence",
                    }
                )
        return games[:4]

    @staticmethod
    def _build_featured_matchup(
        team_form: list[dict[str, Any]],
        upcoming_games: list[dict[str, Any]],
    ) -> dict[str, Any]:
        away_code = "TBD"
        home_code = "TBD"
        tipoff = "Live monitored slate"
        if upcoming_games:
            matchup = str(upcoming_games[0].get("matchup") or "")
            if "@" in matchup:
                away_code, home_code = [
                    part.strip() for part in matchup.split("@", maxsplit=1)
                ]
            tipoff = str(upcoming_games[0].get("time") or tipoff)

        lookup = {str(item.get("code") or ""): item for item in team_form}
        away = lookup.get(away_code) or (team_form[0] if team_form else {})
        home = lookup.get(home_code) or (team_form[1] if len(team_form) > 1 else {})
        away_probability = max(5, min(95, int(away.get("confidence", 50))))
        return {
            "away_code": away_code or str(away.get("code") or "TBD"),
            "away_team": away.get("team") or away_code or "Away",
            "away_record": away.get("record") or "Tracked",
            "home_code": home_code or str(home.get("code") or "TBD"),
            "home_team": home.get("team") or home_code or "Home",
            "home_record": home.get("record") or "Tracked",
            "tipoff": tipoff,
            "venue": "NBA CDN + SIP form model",
            "away_probability": away_probability,
            "home_probability": 100 - away_probability,
        }

    @staticmethod
    def _build_market_board(
        upcoming_games: list[dict[str, Any]],
        team_form: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return []

    @staticmethod
    def _build_insights(
        team_form: list[dict[str, Any]],
        trending_players: list[Any],
        health: dict[str, Any],
        learning: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rows = []
        if team_form:
            leader = team_form[0]
            rows.append(
                {
                    "title": f"{leader.get('team')} leads the monitored form board.",
                    "body": f"Tracked form {leader.get('record')} with confidence {leader.get('confidence')}.",
                }
            )
        if trending_players:
            player = trending_players[0]
            recent = float(getattr(player, "recent_five_ppg", 0.0) or 0.0)
            weighted = float(getattr(player, "weighted_two_season_ppg", 0.0) or 0.0)
            rows.append(
                {
                    "title": f"{getattr(player, 'player_name', 'Player')} is {recent - weighted:+.1f} above the weighted baseline.",
                    "body": getattr(
                        player, "explanation", "SIP found a meaningful trend change."
                    ),
                }
            )
        rows.append(
            {
                "title": f"Learning system has evaluated {int(learning.get('evaluated_alerts') or 0)} alerts.",
                "body": f"Confidence readiness is {'strong' if health.get('confidence_ready') else 'still provisional'}.",
            }
        )
        return rows[:3]

    @staticmethod
    def _build_confidence_payload(
        health: dict[str, Any],
        learning: dict[str, Any],
        trending_count: int,
    ) -> dict[str, Any]:
        data_quality = 85 if health.get("confidence_ready") else 56
        model_performance = int(round(float(learning.get("accuracy") or 0.0) * 100))
        sample_size = (
            min(100, 35 + int(health.get("games") or 0) // 4)
            if health
            else max(30, trending_count * 8)
        )
        market_efficiency = max(35, min(90, 55 + trending_count * 4))
        overall = int(
            round(
                (data_quality + model_performance + sample_size + market_efficiency) / 4
            )
        )
        return {
            "overall": overall,
            "label": "Good"
            if overall >= 65
            else "Fair"
            if overall >= 50
            else "Developing",
            "breakdown": [
                {
                    "label": "Data Quality",
                    "value": data_quality,
                    "status": "High" if data_quality >= 75 else "Fair",
                },
                {
                    "label": "Model Performance",
                    "value": model_performance,
                    "status": "Good" if model_performance >= 60 else "Fair",
                },
                {
                    "label": "Sample Size",
                    "value": sample_size,
                    "status": "Good" if sample_size >= 60 else "Low",
                },
                {
                    "label": "Market Efficiency",
                    "value": market_efficiency,
                    "status": "Good" if market_efficiency >= 70 else "Fair",
                },
            ],
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
            calibration_detail_provider = staticmethod(runtime.get_calibration_details)
            top_hypothesis_provider = staticmethod(runtime.get_top_hypothesis_update)
            dashboard_snapshot_provider = staticmethod(runtime.get_dashboard_snapshot)

        LiveNbaFeatureUIHandler.__name__ = "LiveNbaFeatureUIHandler"
        return LiveNbaFeatureUIHandler

    @staticmethod
    def _get_env_value(keys: list[str], default: str) -> str:
        for key in keys:
            value = os.getenv(key)
            if value:
                return value
        return default

