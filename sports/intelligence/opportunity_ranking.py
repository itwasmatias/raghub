from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sports.betting.models import MarketAssessment


@dataclass(frozen=True, slots=True)
class RankedOpportunity:
    id: str
    rank: int
    score: float
    explanation: tuple[str, ...]
    risks: tuple[str, ...]
    component_scores: dict[str, float]


class OpportunityRankingEngine:
    """Rank qualified opportunities with evidence and reliability guardrails."""

    def rank(self, opportunities: list[dict[str, Any]]) -> list[RankedOpportunity]:
        scored = [self._score(item) for item in opportunities]
        ordered = sorted(scored, key=lambda item: item.score, reverse=True)
        return [
            RankedOpportunity(
                id=item.id,
                rank=index,
                score=item.score,
                explanation=item.explanation,
                risks=item.risks,
                component_scores=item.component_scores,
            )
            for index, item in enumerate(ordered, start=1)
        ]

    def _score(self, item: dict[str, Any]) -> RankedOpportunity:
        assessment = item["assessment"]
        if not isinstance(assessment, MarketAssessment):
            raise TypeError("assessment must be a MarketAssessment")
        expected_value = max(0.0, float(assessment.expected_return or 0.0))
        confidence = float(assessment.prediction.probability)
        factors = {
            "expected_value": min(1.0, expected_value),
            "forecast_confidence": confidence,
            "data_completeness": self._unit(item, "data_completeness"),
            "evidence_quality": self._unit(item, "evidence_quality"),
            "source_independence": self._unit(item, "source_independence"),
            "market_freshness": self._unit(item, "market_freshness"),
            "role_stability": self._unit(item, "role_stability"),
            "injury_certainty": self._unit(item, "injury_certainty"),
            "model_reliability": self._unit(item, "model_reliability"),
            "liquidity": self._unit(item, "liquidity"),
        }
        quality = (
            factors["forecast_confidence"]
            * factors["data_completeness"]
            * factors["evidence_quality"]
            * factors["market_freshness"]
            * factors["model_reliability"]
        )
        supporting = (
            factors["source_independence"]
            + factors["role_stability"]
            + factors["injury_certainty"]
            + factors["liquidity"]
        ) / 4
        uncertainty_penalty = min(
            1.0, float(assessment.prediction.uncertainty) * 2.5
        )
        score = 100 * max(
            0.0,
            factors["expected_value"] * quality * (0.7 + 0.3 * supporting)
            - 0.05 * uncertainty_penalty,
        )
        explanation = tuple(str(value) for value in item.get("positive_factors", ()))
        risks = tuple(str(value) for value in item.get("risks", ()))
        if assessment.rejection_reasons:
            risks = (*risks, *assessment.rejection_reasons)
        return RankedOpportunity(
            id=str(item["id"]),
            rank=0,
            score=round(score, 2),
            explanation=explanation,
            risks=risks,
            component_scores={**factors, "uncertainty_penalty": uncertainty_penalty},
        )

    @staticmethod
    def _unit(item: dict[str, Any], key: str) -> float:
        value = float(item.get(key, 0.0))
        if not 0 <= value <= 1:
            raise ValueError(f"{key} must be between 0 and 1")
        return value

