from __future__ import annotations

import logging
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests

from sports.data.repositories.sqlite_lifecycle_repository import (
    SQLiteLifecycleRepository,
)
from sports.data.sources.the_odds_api_moneyline_source import (
    TheOddsApiMoneylineSource,
)
from sports.data.sources.sports_game_odds_moneyline_source import (
    SportsGameOddsMoneylineSource,
)
from sports.intelligence.lifecycle_service import IntelligenceLifecycleService
from sports.personal.config import PersonalEditionSettings
from sports.personal.model import BaselineMoneylineModel, ModelNotTrained
from sports.personal.models import (
    CompleteBookMarket,
    FeedHealth,
    MoneylineForecast,
)
from sports.personal.normalization import MoneylineNormalizer
from sports.personal.qualification import MoneylineQualificationService
from sports.personal.repository import PersonalEditionRepository
from sports.personal.scheduler import PersonalEditionScheduler
from sports.personal.team_history import (
    PregameTeamFeatureBuilder,
    PublicTeamHistorySource,
)


LOGGER = logging.getLogger("sip.personal")


class PersonalEditionService:
    def __init__(
        self,
        settings: PersonalEditionSettings,
        *,
        repository: PersonalEditionRepository | None = None,
        source: TheOddsApiMoneylineSource | Any | None = None,
        clock=None,
        configuration_error: str | None = None,
        history_source: PublicTeamHistorySource | Any | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository or PersonalEditionRepository(
            settings.database_path
        )
        self.repository.migrate()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.configuration_error = configuration_error
        self.history_source = history_source
        self.source = source
        if (
            source is None
            and settings.mode.value == "live"
            and settings.odds_feed_enabled
            and settings.odds_api_key
        ):
            if settings.odds_provider == "sportsgameodds":
                league_names = {
                    "basketball_nba": "NBA",
                    "basketball_wnba": "WNBA",
                    "baseball_mlb": "MLB",
                }
                leagues = tuple(league_names[sport] for sport in settings.odds_sports)
                self.source = SportsGameOddsMoneylineSource(
                    api_key=settings.odds_api_key,
                    leagues=leagues,
                    base_url=settings.odds_base_url,
                    request_timeout_seconds=settings.odds_request_timeout_seconds,
                    max_events_per_request=settings.odds_max_events_per_request,
                )
            else:
                self.source = TheOddsApiMoneylineSource(
                    api_key=settings.odds_api_key,
                    sports=settings.odds_sports,
                    regions=settings.odds_regions,
                )
        self.normalizer = MoneylineNormalizer(
            maximum_age_seconds=settings.odds_max_age_seconds
        )
        self.qualifier = MoneylineQualificationService(
            minimum_complete_books=settings.minimum_complete_books,
            minimum_edge=settings.minimum_model_edge,
            minimum_data_quality=settings.minimum_data_quality,
            forecast_max_age_seconds=settings.forecast_max_age_seconds,
        )
        lifecycle_path = Path(settings.database_path).with_name(
            "sip_personal_lifecycle.db"
        )
        self.lifecycle = IntelligenceLifecycleService(
            SQLiteLifecycleRepository(lifecycle_path)
        )
        self.scheduler = PersonalEditionScheduler(
            self.refresh,
            self.repository,
            interval_seconds=settings.refresh_interval_seconds,
        )

    @classmethod
    def from_environment(cls) -> PersonalEditionService:
        try:
            settings = PersonalEditionSettings.from_environment()
            return cls(settings, history_source=PublicTeamHistorySource())
        except ValueError as error:
            values = dict(os.environ)
            values["ODDS_FEED_ENABLED"] = "false"
            settings = PersonalEditionSettings.from_mapping(values)
            return cls(
                settings,
                configuration_error=str(error),
                history_source=PublicTeamHistorySource(),
            )

    def refresh(self) -> dict[str, Any]:
        now = self.clock().astimezone(timezone.utc)
        correlation_id = f"refresh-{uuid4().hex[:12]}"
        if (
            self.configuration_error
            or not self.settings.odds_feed_enabled
            or self.source is None
        ):
            health = FeedHealth(
                configured=False,
                enabled=self.settings.odds_feed_enabled,
                status="unconfigured",
                last_attempt_at=now.isoformat(),
                last_successful_refresh=None,
                events_received=0,
                complete_books=0,
                freshness="unavailable",
                error=self.configuration_error
                or "Odds feed is disabled or ODDS_API_KEY is missing.",
            )
            self.repository.save_feed_health(health)
            return {
                "status": "unconfigured",
                "correlation_id": correlation_id,
                "health": asdict(health),
                "evaluations": [],
            }
        rows = None
        error = None
        for attempt in range(1, 4):
            try:
                rows = self.source.fetch(correlation_id=correlation_id)
                error = None
                break
            except (requests.Timeout, requests.ConnectionError) as caught:
                error = caught
                LOGGER.warning(
                    "provider_retry",
                    extra={
                        "attempt": attempt,
                        "error_type": type(caught).__name__,
                        "correlation_id": correlation_id,
                    },
                )
        if rows is None:
            prior = self.repository.load_feed_health()
            health = FeedHealth(
                configured=True,
                enabled=True,
                status="unavailable",
                last_attempt_at=now.isoformat(),
                last_successful_refresh=(
                    prior.last_successful_refresh if prior else None
                ),
                events_received=0,
                complete_books=0,
                freshness="stale" if prior else "unavailable",
                error=f"{type(error).__name__}: {error}"[:500],
            )
            self.repository.save_feed_health(health)
            return {
                "status": "unavailable",
                "correlation_id": correlation_id,
                "health": asdict(health),
                "evaluations": [],
            }
        # The evidence cutoff must follow retrieval. Connectors may timestamp
        # observations while the request is in flight.
        now = self.clock().astimezone(timezone.utc)
        normalized = self.normalizer.normalize(rows, as_of=now)
        self.repository.save_market_snapshot(
            events=normalized.events,
            quotes=normalized.quotes,
            retrieved_at=now.isoformat(),
        )
        health = FeedHealth(
            configured=True,
            enabled=True,
            status="healthy",
            last_attempt_at=now.isoformat(),
            last_successful_refresh=now.isoformat(),
            events_received=len(normalized.events),
            complete_books=len(normalized.complete_books),
            freshness="fresh",
            error="; ".join(
                part
                for part in (
                    (
                        f"{len(normalized.rejections)} quote(s) rejected; inspect normalization diagnostics."
                        if normalized.rejections
                        else ""
                    ),
                    "; ".join(
                        getattr(self.source, "last_errors", {}).values()
                    ),
                )
                if part
            ) or None,
        )
        self.repository.save_feed_health(health)
        forecasts = self.repository.list_forecasts()
        generated_forecasts, forecast_warnings = self._forecast_events(
            normalized.events,
            now=now,
        )
        if generated_forecasts:
            forecasts = self.repository.list_forecasts()
        results = self._evaluate(
            normalized.events,
            normalized.complete_books,
            forecasts,
            health,
            now=now,
        )
        LOGGER.info(
            "qualification_batch_complete",
            extra={
                "events": len(normalized.events),
                "qualified": sum(item.qualified for item in results),
                "rejected_quotes": len(normalized.rejections),
                "correlation_id": correlation_id,
            },
        )
        return {
            "status": "healthy",
            "correlation_id": correlation_id,
            "health": asdict(health),
            "events": len(normalized.events),
            "complete_books": len(normalized.complete_books),
            "rejections": [asdict(item) for item in normalized.rejections],
            "forecast_warnings": forecast_warnings,
            "forecasts_generated": len(generated_forecasts),
            "evaluations": [self._result_payload(item) for item in results],
        }

    def _forecast_events(self, events, *, now):
        generated = []
        warnings = []
        for event in events:
            try:
                model = BaselineMoneylineModel.load(
                    self.model_path_for_league(event.league)
                )
                history_path = self.history_path_for_league(event.league)
                games = PublicTeamHistorySource.load(history_path)
                features = PregameTeamFeatureBuilder.upcoming_features(
                    games,
                    season=event.season,
                    home_team=event.home_team_name,
                    away_team=event.away_team_name,
                    event_start=event.start_time,
                )
                generated.extend(model.predict(
                    canonical_event_id=event.canonical_id,
                    league=event.league,
                    features=features,
                    feature_timestamp=now.isoformat(),
                    forecast_timestamp=now.isoformat(),
                ))
            except (ModelNotTrained, FileNotFoundError, ValueError) as error:
                warnings.append(f"{event.canonical_id}: {error}")
        if generated:
            self.repository.save_forecasts(generated)
        return generated, warnings

    def model_path_for_league(self, league: str) -> Path:
        base = Path(self.settings.model_path)
        return base.with_name(f"{base.stem}-{league.lower()}{base.suffix}")

    def history_path_for_league(self, league: str) -> Path:
        return Path("data/training") / f"{league.lower()}_team_games.json"

    def generate_forecasts(
        self, feature_rows: list[dict[str, Any]]
    ) -> list[MoneylineForecast]:
        model = BaselineMoneylineModel.load(self.settings.model_path)
        forecasts: list[MoneylineForecast] = []
        for row in feature_rows:
            event_id = str(row["canonical_event_id"])
            feature_timestamp = str(row["feature_timestamp"])
            values = {
                name: row.get(name)
                for name in BaselineMoneylineModel.FEATURE_NAMES
            }
            generated = model.predict(
                canonical_event_id=event_id,
                league=str(row["league"]),
                features=values,
                feature_timestamp=feature_timestamp,
                forecast_timestamp=self.clock()
                .astimezone(timezone.utc)
                .isoformat(),
            )
            forecasts.extend(generated)
            LOGGER.info(
                "forecast_generated",
                extra={
                    "canonical_event_id": event_id,
                    "model_version": model.metadata.version,
                },
            )
        self.repository.save_forecasts(forecasts)
        return forecasts

    def snapshot(self) -> dict[str, Any]:
        now = self.clock().astimezone(timezone.utc)
        events = [
            item
            for item in self.repository.list_events()
            if datetime.fromisoformat(
                item.start_time.replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            > now
        ]
        quotes = self.repository.list_quotes()
        complete = self._complete_books(quotes)
        health = self.repository.load_feed_health() or FeedHealth(
            configured=False,
            enabled=self.settings.odds_feed_enabled,
            status="not_refreshed",
            last_attempt_at=None,
            last_successful_refresh=None,
            events_received=0,
            complete_books=0,
            freshness="unavailable",
            error=self.configuration_error
            or "Run the first refresh after configuring the provider.",
        )
        evaluations = self.repository.latest_evaluations()
        forecasts = self.repository.list_forecasts()
        league_models = {}
        for league in ("NBA", "WNBA", "MLB"):
            try:
                model = BaselineMoneylineModel.load(
                    self.model_path_for_league(league)
                )
                league_models[league] = asdict(model.metadata)
            except ModelNotTrained as error:
                league_models[league] = {
                    "calibration_status": "not_trained",
                    "status": error.code,
                    "detail": str(error),
                }
            except (ValueError, KeyError, json.JSONDecodeError) as error:
                LOGGER.error(
                    "model_loading_failed",
                    extra={"error_type": type(error).__name__},
                )
                league_models[league] = {
                    "calibration_status": "load_failed",
                    "status": "MODEL_LOAD_FAILED",
                    "detail": "The model artifact is invalid; retrain it before forecasting.",
                }
        calibrated = [
            item for item in league_models.values()
            if item.get("calibration_status") == "calibrated"
        ]
        model_status = {
            "status": (
                "CALIBRATED"
                if len(calibrated) == len(league_models)
                else "PARTIALLY_TRAINED"
                if calibrated
                else "MODEL_NOT_TRAINED"
            ),
            "calibration_status": (
                "calibrated"
                if len(calibrated) == len(league_models)
                else "partial"
                if calibrated
                else "not_trained"
            ),
            "leagues": league_models,
        }
        qualified_choices = [
            item for item in evaluations if item["qualified"]
        ]
        no_bet_results = [
            item for item in evaluations if not item["qualified"]
        ]
        league_summary = {}
        for league in ("NBA", "WNBA", "MLB"):
            prefix = f"{league.lower()}:"
            league_evaluations = [
                item
                for item in evaluations
                if str(item["canonical_event_id"]).lower().startswith(prefix)
            ]
            league_summary[league] = {
                "upcoming_events": sum(
                    1 for item in events if item.league == league
                ),
                "evaluations": len(league_evaluations),
                "qualified_choices": sum(
                    1 for item in league_evaluations if item["qualified"]
                ),
                "no_bet_results": sum(
                    1 for item in league_evaluations if not item["qualified"]
                ),
            }
        event_payloads = []
        for event in events:
            event_quotes = [
                quote
                for quote in quotes
                if quote.canonical_event_id == event.canonical_id
            ]
            event_evaluations = [
                item
                for item in evaluations
                if item["canonical_event_id"] == event.canonical_id
            ]
            event_forecasts = [
                item
                for item in forecasts
                if item.canonical_event_id == event.canonical_id
            ]
            event_payloads.append(
                {
                    **asdict(event),
                    "quotes": [asdict(item) for item in event_quotes],
                    "complete_books": len(
                        {
                            item.sportsbook
                            for item in complete
                            if item.canonical_event_id == event.canonical_id
                        }
                    ),
                    "evaluations": event_evaluations,
                    "forecasts": [asdict(item) for item in event_forecasts],
                    "season_context": {
                        "current_season": (
                            "Available in the persisted forecast factors."
                            if event_forecasts
                            else "Unavailable until sports statistics and features are refreshed."
                        ),
                        "previous_season": (
                            "Available in the persisted forecast factors."
                            if event_forecasts
                            else "Unavailable until historical feature data is loaded."
                        ),
                    },
                }
            )
        return {
            "version": "1.0.0",
            "edition": "Personal Edition",
            "mode": self.settings.mode.value,
            "supported_leagues": ["NBA", "WNBA", "MLB"],
            "supported_market": "pregame full-game moneyline",
            "feed": asdict(health),
            "model": model_status,
            "league_summary": league_summary,
            "events": event_payloads,
            "evaluations": evaluations,
            "qualified_choices": qualified_choices,
            "no_bet_results": no_bet_results,
            "thresholds": {
                "minimum_model_edge": self.settings.minimum_model_edge,
                "minimum_data_quality": self.settings.minimum_data_quality,
                "minimum_complete_books": self.settings.minimum_complete_books,
                "odds_max_age_seconds": self.settings.odds_max_age_seconds,
                "forecast_max_age_seconds": self.settings.forecast_max_age_seconds,
            },
            "scheduler": self.scheduler.status(),
            "research": {
                "player_intelligence_route": "/player",
                "availability": "NBA player intelligence is the primary research path; WNBA and MLB remain optional plugins.",
            },
            "generated_at": now.isoformat(),
        }

    def betting_board(self) -> dict[str, Any]:
        """Expose canonical Personal Edition results through the shared betting UI."""
        snapshot = self.snapshot()
        now = self.clock().astimezone(timezone.utc)
        feed = snapshot["feed"]
        event_ids = {event["canonical_id"] for event in snapshot["events"]}
        quotes = self.repository.list_quotes()
        complete_books = self._complete_books(quotes)
        assessments = []

        for result in snapshot["evaluations"]:
            event_id = str(result["canonical_event_id"])
            if event_id not in event_ids:
                continue
            selection = str(result["selection"])
            latest_quotes = {}
            for quote in quotes:
                if (
                    quote.canonical_event_id == event_id
                    and quote.selection == selection
                ):
                    latest_quotes[quote.sportsbook] = quote
            selection_quotes = list(latest_quotes.values())
            fair_by_book = {
                market.sportsbook: market.fair_probability(selection)
                for market in complete_books
                if market.canonical_event_id == event_id
            }
            book_prices = []
            for quote in selection_quotes:
                observed = datetime.fromisoformat(
                    quote.observed_at.replace("Z", "+00:00")
                ).astimezone(timezone.utc)
                age = max(0.0, (now - observed).total_seconds())
                book_prices.append({
                    "sportsbook": quote.sportsbook,
                    "american_price": quote.american_price,
                    "decimal_price": quote.decimal_price,
                    "fair_probability": fair_by_book.get(
                        quote.sportsbook,
                        quote.implied_probability,
                    ),
                    "age_seconds": age,
                    "stale": age > self.settings.odds_max_age_seconds,
                })
            best_quote = next(
                (
                    quote
                    for quote in selection_quotes
                    if quote.sportsbook == result.get("best_sportsbook")
                    and quote.american_price == result.get("best_price")
                ),
                None,
            )
            diagnostics = result.get("diagnostics") or {}
            confidence = result.get("confidence")
            model_probability = result.get("model_probability")
            assessments.append({
                "qualified": bool(result.get("qualified")),
                "prediction": {
                    "event_id": event_id,
                    "market": "moneyline",
                    "selection": selection,
                    "line": None,
                    "probability": model_probability,
                    "model_version": (
                        diagnostics.get("forecast", {}).get("model_version")
                        or snapshot["model"].get("version")
                        or snapshot["model"].get("status")
                        or "unavailable"
                    ),
                    "generated_at": result.get("evaluated_at"),
                    "reasons": list(result.get("evidence") or ()),
                    "invalidators": list(result.get("risks") or ()),
                },
                "best_quote": (
                    {
                        "sportsbook": best_quote.sportsbook,
                        "american_price": best_quote.american_price,
                        "source_url": best_quote.source_url,
                    }
                    if best_quote is not None
                    else None
                ),
                "book_prices": book_prices,
                "consensus_probability": result.get("market_probability"),
                "probability_edge": result.get("edge"),
                "expected_return": result.get("expected_value"),
                "confidence_adjusted_return": (
                    float(result["expected_value"]) * float(confidence)
                    if result.get("expected_value") is not None
                    and confidence is not None
                    else None
                ),
                "confidence_interval": (
                    (
                        max(0.0, float(model_probability) - 0.05),
                        min(1.0, float(model_probability) + 0.05),
                    )
                    if model_probability is not None
                    else (None, None)
                ),
                "model_agreement": confidence,
                "rejection_reasons": list(result.get("reason_codes") or ()),
                "warnings": list(diagnostics.get("next_actions") or ()),
            })

        classification = snapshot["mode"]
        if classification == "live" and (
            feed.get("status") != "healthy"
            or feed.get("freshness") != "fresh"
        ):
            classification = "delayed"
        model_status = snapshot["model"].get(
            "calibration_status",
            snapshot["model"].get("status", "unavailable"),
        )
        return {
            "data_classification": classification,
            "status": "ready" if assessments else feed.get("status", "unavailable"),
            "assessments": assessments,
            "exclusions": [],
            "source_health": feed,
            "provider": self.settings.odds_provider,
            "generated_at": now.isoformat(),
            "message": (
                f"{len(assessments)} canonical NBA/WNBA/MLB moneyline assessment(s); "
                f"model status: {model_status}."
                if assessments
                else feed.get("error")
                or "Refresh the normalized SIP odds feed to create assessments."
            ),
        }

    def event_detail(self, canonical_event_id: str) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in self.snapshot()["events"]
                if item["canonical_id"] == canonical_event_id
            ),
            None,
        )

    def _evaluate(
        self,
        events,
        complete_books,
        forecasts,
        health,
        *,
        now,
    ):
        results = []
        for event in events:
            for selection in ("home", "away"):
                result = self.qualifier.evaluate(
                    event,
                    complete_books,
                    forecasts,
                    health,
                    selection=selection,
                    as_of=now,
                )
                self.repository.save_evaluation(
                    result, data_mode=self.settings.mode.value
                )
                self._record_lifecycle(result, event)
                results.append(result)
        return results

    def _record_lifecycle(self, result, event) -> None:
        situation_id = (
            f"sip:{event.canonical_id}:moneyline:{result.selection}"
        )
        if self.lifecycle.get_situation(situation_id) is None:
            self.lifecycle.create_situation(
                situation_id=situation_id,
                title=(
                    f"{event.away_team_name} at {event.home_team_name} "
                    f"{result.selection} moneyline"
                ),
                objective="Qualify or reject a pregame full-game moneyline using reproducible evidence.",
                observation="Canonical pregame event entered the Personal Edition pipeline.",
                observed_at=result.evaluated_at,
            )
        self.lifecycle.record_observation(
            situation_id,
            f"Qualification result: {result.status}.",
            occurred_at=result.evaluated_at,
        )
        for index, evidence in enumerate(result.evidence, start=1):
            self.lifecycle.record_evidence(
                situation_id,
                {
                    "id": f"{situation_id}:{result.evaluated_at}:{index}",
                    "claim": evidence,
                    "source": "normalized sportsbook quote",
                    "url": event.source_urls[0] if event.source_urls else "",
                    "retrieved_at": result.evaluated_at,
                },
                occurred_at=result.evaluated_at,
            )
        self.lifecycle.add_recommended_action(
            situation_id,
            (
                "Qualified analytical choice; verify the current price before acting."
                if result.qualified
                else "; ".join(result.diagnostics["next_actions"])
            ),
            occurred_at=result.evaluated_at,
        )
        self.lifecycle.add_monitoring_rule(
            situation_id,
            {
                "signal": "odds_or_forecast_change",
                "action": "Re-run qualification and preserve the new evaluation.",
            },
            occurred_at=result.evaluated_at,
        )

    @staticmethod
    def _complete_books(quotes) -> tuple[CompleteBookMarket, ...]:
        grouped = {}
        for quote in quotes:
            key = (
                quote.canonical_event_id,
                quote.sportsbook,
                quote.observed_at,
            )
            grouped.setdefault(key, {})[quote.selection] = quote
        return tuple(
            CompleteBookMarket(
                canonical_event_id=key[0],
                sportsbook=key[1],
                market="moneyline",
                period="full_game",
                observed_at=key[2],
                home_quote=selections["home"],
                away_quote=selections["away"],
            )
            for key, selections in grouped.items()
            if {"home", "away"} <= set(selections)
        )

    @staticmethod
    def _result_payload(result) -> dict[str, Any]:
        payload = asdict(result)
        payload["reason_codes"] = [
            getattr(item, "value", str(item)) for item in result.reason_codes
        ]
        return payload
