from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class BriefMetadata:
    report_id: str
    generated_at: str
    slate_date: str
    league: str
    data_freshness: str
    methodology_version: str


@dataclass(frozen=True, slots=True)
class BriefSummary:
    games_analyzed: int
    markets_available: tuple[str, ...]
    books_represented: tuple[str, ...]
    stale_or_incomplete_markets: tuple[str, ...]
    highest_priority_research_items: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BriefQuoteObservation:
    sportsbook: str
    selection: str
    american_price: int
    observed_at: str
    provider: str
    provider_event_id: str
    provider_quote_id: str
    source_url: str
    stale: bool
    age_minutes: float


@dataclass(frozen=True, slots=True)
class BriefProbabilityView:
    market_probability: float | None
    market_probability_reason: str | None
    model_probability: float | None
    model_probability_reason: str | None
    probability_difference: float | None
    probability_difference_reason: str | None
    confidence: float | None
    confidence_reason: str | None
    research_selection: str | None


@dataclass(frozen=True, slots=True)
class BriefGameCard:
    canonical_game_id: str
    teams: str
    scheduled_start: str
    available_sportsbook_quotes: tuple[BriefQuoteObservation, ...]
    probabilities: BriefProbabilityView
    confidence_label: str
    data_quality_status: str
    evidence_references: tuple[str, ...]
    contradictions: tuple[str, ...]
    risk_flags: tuple[str, ...]
    verdict: str
    verdict_detail: str
    stale_warning: str | None = None


@dataclass(frozen=True, slots=True)
class BriefAuditEntry:
    source_provider: str
    observed_timestamp: str
    provider_event_id: str
    provider_quote_id: str
    canonical_game_id: str
    source_url: str


@dataclass(frozen=True, slots=True)
class BriefDisclosure:
    audit_entries: tuple[BriefAuditEntry, ...]
    generated_timestamp: str
    model_limitations: tuple[str, ...]
    no_guarantee_language: str
    no_automation_language: str


@dataclass(frozen=True, slots=True)
class MlbIntelligenceBrief:
    metadata: BriefMetadata
    slate_summary: BriefSummary
    game_cards: tuple[BriefGameCard, ...]
    disclosure: BriefDisclosure
    unavailable_reason: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
