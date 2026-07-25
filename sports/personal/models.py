from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class DataMode(StrEnum):
    LIVE = "live"
    REPLAY = "replay"


@dataclass(frozen=True, slots=True)
class FeedHealth:
    configured: bool
    enabled: bool
    status: str
    last_attempt_at: str | None
    last_successful_refresh: str | None
    events_received: int
    complete_books: int
    freshness: str
    error: str | None = None

    @classmethod
    def configured_success(
        cls, refreshed_at: str, events: int, complete_books: int
    ) -> FeedHealth:
        return cls(
            configured=True,
            enabled=True,
            status="healthy",
            last_attempt_at=refreshed_at,
            last_successful_refresh=refreshed_at,
            events_received=events,
            complete_books=complete_books,
            freshness="fresh",
        )


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    canonical_id: str
    league: str
    season: str
    start_time: str
    home_team_id: str
    home_team_name: str
    away_team_id: str
    away_team_name: str
    venue: str | None
    status: str
    provider_event_ids: tuple[str, ...]
    source_urls: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NormalizedMoneylineQuote:
    canonical_event_id: str
    provider_event_id: str
    league: str
    season: str
    event_start: str
    home_team_id: str
    away_team_id: str
    sportsbook: str
    market: str
    period: str
    selection: str
    selection_team_id: str
    line: float | None
    american_price: int
    observed_at: str
    source: str
    source_url: str
    data_mode: str

    @property
    def decimal_price(self) -> float:
        if self.american_price > 0:
            return 1 + self.american_price / 100
        return 1 + 100 / abs(self.american_price)

    @property
    def implied_probability(self) -> float:
        return 1 / self.decimal_price


@dataclass(frozen=True, slots=True)
class CompleteBookMarket:
    canonical_event_id: str
    sportsbook: str
    market: str
    period: str
    observed_at: str
    home_quote: NormalizedMoneylineQuote
    away_quote: NormalizedMoneylineQuote

    def fair_probability(self, selection: str) -> float:
        home = self.home_quote.implied_probability
        away = self.away_quote.implied_probability
        total = home + away
        return (home if selection == "home" else away) / total


@dataclass(frozen=True, slots=True)
class NormalizationRejection:
    code: str
    detail: str
    sportsbook: str | None = None


@dataclass(frozen=True, slots=True)
class MoneylineNormalizationResult:
    events: tuple[CanonicalEvent, ...]
    quotes: tuple[NormalizedMoneylineQuote, ...]
    complete_books: tuple[CompleteBookMarket, ...]
    rejections: tuple[NormalizationRejection, ...]


@dataclass(frozen=True, slots=True)
class ModelMetadata:
    model_name: str
    version: str
    training_date: str
    training_period: tuple[str, str]
    validation_period: tuple[str, str]
    feature_version: str
    training_examples: int
    calibration_method: str
    brier_score: float | None
    log_loss: float | None
    calibration_status: str
    validation_examples: int | None = None
    baseline_brier_score: float | None = None
    baseline_log_loss: float | None = None


@dataclass(frozen=True, slots=True)
class MoneylineForecast:
    canonical_event_id: str
    league: str
    market: str
    period: str
    selection: str
    raw_probability: float
    calibrated_probability: float
    model_version: str
    feature_version: str
    feature_timestamp: str
    forecast_timestamp: str
    calibration_status: str
    contributing_factors: tuple[str, ...]
    missing_feature_warnings: tuple[str, ...]
    metadata: ModelMetadata


@dataclass(frozen=True, slots=True)
class QualificationResult:
    canonical_event_id: str
    selection: str
    qualified: bool
    status: str
    reason_codes: tuple[Any, ...]
    diagnostics: dict[str, Any]
    best_sportsbook: str | None = None
    best_price: int | None = None
    consensus_price: int | None = None
    raw_implied_probability: float | None = None
    market_probability: float | None = None
    model_probability: float | None = None
    edge: float | None = None
    expected_value: float | None = None
    confidence: float | None = None
    data_quality: float = 0.0
    evaluated_at: str = ""
    evidence: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
