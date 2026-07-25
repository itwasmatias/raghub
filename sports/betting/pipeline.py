from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from sports.betting.engine import BettingIntelligenceEngine
from sports.betting.models import (
    DataClassification,
    MarketAssessment,
    ModelPrediction,
)
from sports.betting.normalization import MarketNormalizer
from sports.betting.repository import SQLiteBettingRepository
from sports.intelligence.lifecycle_service import IntelligenceLifecycleService
from sports.intelligence.opportunity_ranking import (
    OpportunityRankingEngine,
    RankedOpportunity,
)


@dataclass(frozen=True, slots=True)
class BettingPipelineResult:
    classification: DataClassification
    generated_at: str
    assessments: tuple[MarketAssessment, ...]
    rankings: tuple[RankedOpportunity, ...]
    exclusions: tuple[str, ...]
    situation_ids: tuple[str, ...]
    normalized_quote_count: int
    complete_book_count: int


class BettingIntelligencePipeline:
    """Evidence-gated sportsbook analysis with audit and lifecycle persistence."""

    def __init__(
        self,
        *,
        normalizer: MarketNormalizer,
        repository: SQLiteBettingRepository,
        lifecycle: IntelligenceLifecycleService,
        engine: BettingIntelligenceEngine | None = None,
        ranking_service: OpportunityRankingEngine | None = None,
    ) -> None:
        self.normalizer = normalizer
        self.repository = repository
        self.lifecycle = lifecycle
        self.engine = engine or BettingIntelligenceEngine()
        self.ranking_service = ranking_service or OpportunityRankingEngine()

    def run(
        self,
        *,
        raw_quotes: list[dict[str, Any]],
        predictions: list[ModelPrediction],
        classification: DataClassification,
        as_of: datetime | None = None,
    ) -> BettingPipelineResult:
        now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self._validate_classification(raw_quotes, predictions, classification)
        normalized = self.normalizer.normalize(raw_quotes, as_of=now)
        if normalized.rankable_quotes:
            self.repository.add_quotes(normalized.rankable_quotes)

        assessments = tuple(
            self.engine.assess(
                prediction,
                normalized.rankable_quotes,
                as_of=now,
            )
            for prediction in predictions
        )
        qualified = [item for item in assessments if item.qualified]
        ranking_inputs = [
            {
                "id": self._situation_id(item),
                "assessment": item,
                "data_completeness": 1.0,
                "evidence_quality": 0.9,
                "source_independence": min(1.0, len(item.book_prices) / 3),
                "market_freshness": 1.0,
                "role_stability": 0.75,
                "injury_certainty": 0.75,
                "model_reliability": max(
                    0.0,
                    1.0 - float(item.prediction.similar_bet_brier_score or 1.0),
                ),
                "liquidity": 0.5,
                "positive_factors": item.prediction.reasons,
                "risks": item.warnings,
            }
            for item in qualified
        ]
        rankings = tuple(self.ranking_service.rank(ranking_inputs))
        situation_ids = []
        for assessment in assessments:
            situation_id = self._record_lifecycle(
                assessment,
                classification=classification,
                occurred_at=now.isoformat(),
            )
            situation_ids.append(situation_id)
            if assessment.qualified:
                self.repository.save_recommendation(assessment)

        return BettingPipelineResult(
            classification=classification,
            generated_at=now.isoformat(),
            assessments=assessments,
            rankings=rankings,
            exclusions=tuple(normalized.exclusions),
            situation_ids=tuple(situation_ids),
            normalized_quote_count=normalized.normalized_count,
            complete_book_count=normalized.complete_book_count,
        )

    @staticmethod
    def _validate_classification(
        rows: list[dict[str, Any]],
        predictions: list[ModelPrediction],
        classification: DataClassification,
    ) -> None:
        if classification is DataClassification.LIVE:
            invalid = [
                row
                for row in rows
                if str(row.get("data_classification") or "").lower() != "live"
                or str(row.get("designation") or "current").lower()
                in {"replay", "derived", "simulated"}
            ]
            if invalid:
                raise ValueError(
                    "Live pipeline input must be explicitly live and cannot contain replay or derived quotes."
                )
        if classification is DataClassification.REPLAY and not rows:
            raise ValueError("Replay classification requires replay quote evidence.")
        if classification is DataClassification.MODEL_ONLY and rows:
            raise ValueError("Model-only classification cannot contain sportsbook quotes.")
        if not predictions:
            raise ValueError("At least one model prediction is required.")

    def _record_lifecycle(
        self,
        assessment: MarketAssessment,
        *,
        classification: DataClassification,
        occurred_at: str,
    ) -> str:
        situation_id = self._situation_id(assessment)
        if self.lifecycle.get_situation(situation_id) is None:
            self.lifecycle.create_situation(
                situation_id=situation_id,
                title=(
                    f"{assessment.prediction.market} "
                    f"{assessment.prediction.selection} "
                    f"{assessment.prediction.line}"
                ),
                objective="Determine whether a sportsbook opportunity clears every evidence and risk gate.",
                observation=f"A {classification.value} market entered the betting pipeline.",
                observed_at=occurred_at,
            )
        for index, price in enumerate(assessment.book_prices, start=1):
            self.lifecycle.record_evidence(
                situation_id,
                {
                    "id": f"{situation_id}:quote:{index}:{occurred_at}",
                    "claim": (
                        f"{price.sportsbook} quoted {price.american_price:+d} "
                        f"with fair probability {price.fair_probability:.4f}."
                    ),
                    "source": price.sportsbook,
                    "url": (
                        assessment.best_quote.source_url
                        if assessment.best_quote is not None
                        else ""
                    ),
                    "retrieved_at": occurred_at,
                    "data_classification": classification.value,
                },
                occurred_at=occurred_at,
            )
        self.lifecycle.add_hypothesis(
            situation_id,
            {
                "id": f"{situation_id}:value",
                "explanation": "The calibrated model probability differs from the paired no-vig market.",
                "confidence": assessment.prediction.probability,
                "status": (
                    "supported" if assessment.qualified else "not_supported"
                ),
            },
            occurred_at=occurred_at,
        )
        self.lifecycle.add_forecast(
            situation_id,
            {
                "predicted_event": (
                    f"{assessment.prediction.selection} "
                    f"{assessment.prediction.line}"
                ),
                "probability": assessment.prediction.probability,
                "baseline_probability": (
                    assessment.consensus_probability
                    if assessment.consensus_probability is not None
                    else 0.5
                ),
                "horizon": "through event settlement",
                "model_version": assessment.prediction.model_version,
                "confidence_adjusted_return": assessment.confidence_adjusted_return,
                "decision": assessment.decision.value,
            },
            occurred_at=occurred_at,
        )
        self.lifecycle.add_monitoring_rule(
            situation_id,
            {
                "signal": "quote_or_line_change",
                "action": "Reassess qualification and calculate closing-line value.",
            },
            occurred_at=occurred_at,
        )
        return situation_id

    @staticmethod
    def _situation_id(assessment: MarketAssessment) -> str:
        prediction = assessment.prediction
        player = prediction.canonical_player_id or "market"
        line = str(prediction.line).replace(".", "-")
        return (
            f"bet:{prediction.event_id}:{player}:"
            f"{prediction.market}:{prediction.selection}:{line}"
        )

