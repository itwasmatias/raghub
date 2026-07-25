from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from math import isfinite


class NoVigMethod(StrEnum):
    PROPORTIONAL = "proportional"
    POWER = "power"
    SHIN = "shin"


class Decision(StrEnum):
    QUALIFIED = "qualified"
    NO_BET = "no_bet"


class DataClassification(StrEnum):
    LIVE = "live"
    DELAYED = "delayed"
    REPLAY = "replay"
    MODEL_ONLY = "model-only"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class OddsQuote:
    event_id: str
    market: str
    selection: str
    sportsbook: str
    line: float | None
    american_price: int
    event_start: datetime
    fetched_at: datetime
    source: str
    designation: str = "current"
    source_url: str = ""
    canonical_player_id: str = ""
    outcome_definition: str = ""
    status: str = "active"
    liquidity: float | None = None

    def __post_init__(self) -> None:
        if not all((self.event_id, self.market, self.selection, self.sportsbook, self.source)):
            raise ValueError("quote identifiers and source are required")
        if self.american_price == 0 or -100 < self.american_price < 100:
            raise ValueError("American price must be <= -100 or >= +100")
        if self.designation not in {"opening", "current", "closing"}:
            raise ValueError("designation must be opening, current, or closing")
        if self.status not in {"active", "suspended", "closed"}:
            raise ValueError("status must be active, suspended, or closed")
        if self.liquidity is not None and self.liquidity < 0:
            raise ValueError("liquidity cannot be negative")
        object.__setattr__(self, "event_start", _utc(self.event_start))
        object.__setattr__(self, "fetched_at", _utc(self.fetched_at))

    @property
    def decimal_price(self) -> float:
        if self.american_price > 0:
            return 1.0 + self.american_price / 100.0
        return 1.0 + 100.0 / abs(self.american_price)

    @property
    def implied_probability(self) -> float:
        return 1.0 / self.decimal_price


@dataclass(frozen=True, slots=True)
class ModelPrediction:
    event_id: str
    market: str
    selection: str
    line: float | None
    probability: float
    uncertainty: float
    model_probabilities: tuple[float, ...]
    model_version: str
    generated_at: datetime
    similar_bet_sample: int = 0
    similar_bet_brier_score: float | None = None
    reasons: tuple[str, ...] = ()
    invalidators: tuple[str, ...] = ()
    canonical_player_id: str = ""
    outcome_definition: str = ""
    forecast_type: str = ""
    team_id: str = ""
    player_role: str = ""

    def __post_init__(self) -> None:
        values = (self.probability, *self.model_probabilities)
        if not all(isfinite(value) and 0.0 <= value <= 1.0 for value in values):
            raise ValueError("probabilities must be finite values between 0 and 1")
        if not isfinite(self.uncertainty) or self.uncertainty < 0:
            raise ValueError("uncertainty must be a non-negative finite value")
        if not self.model_probabilities:
            raise ValueError("at least one component model probability is required")
        object.__setattr__(self, "generated_at", _utc(self.generated_at))


@dataclass(frozen=True, slots=True)
class BettingPolicy:
    no_vig_method: NoVigMethod = NoVigMethod.PROPORTIONAL
    minimum_edge: float = 0.025
    minimum_expected_return: float = 0.02
    minimum_confidence_adjusted_return: float = 0.0
    maximum_uncertainty: float = 0.08
    maximum_quote_age_seconds: int = 180
    minimum_books: int = 2
    minimum_model_agreement: float = 0.67
    minimum_similar_sample: int = 30
    maximum_similar_brier_score: float = 0.25


@dataclass(frozen=True, slots=True)
class BookPrice:
    sportsbook: str
    american_price: int
    decimal_price: float
    fair_probability: float
    age_seconds: float
    stale: bool


@dataclass(frozen=True, slots=True)
class MarketAssessment:
    decision: Decision
    prediction: ModelPrediction
    best_quote: OddsQuote | None
    book_prices: tuple[BookPrice, ...]
    consensus_probability: float | None
    probability_edge: float | None
    expected_return: float | None
    confidence_adjusted_return: float | None
    confidence_interval: tuple[float, float]
    model_agreement: float
    rejection_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def qualified(self) -> bool:
        return self.decision is Decision.QUALIFIED


@dataclass(frozen=True, slots=True)
class ClosingEvaluation:
    recommendation_id: int
    beat_closing_price: bool | None
    suggested_implied_probability: float
    closing_implied_probability: float | None
    closing_line_delta: float | None
    won: bool | None
    realized_return: float | None


@dataclass(frozen=True, slots=True)
class StoredRecommendation:
    id: int | None
    assessment: MarketAssessment
    created_at: datetime
    metadata: dict[str, str] = field(default_factory=dict)
