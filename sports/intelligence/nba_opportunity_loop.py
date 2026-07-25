from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sports.betting.engine import BettingIntelligenceEngine
from sports.betting.models import Decision, MarketAssessment, ModelPrediction, OddsQuote
from sports.intelligence.lifecycle_service import IntelligenceLifecycleService


class NBAPlayerOpportunityLoop:
    """Run one evidence-gated NBA opportunity from observation to monitoring."""

    def __init__(
        self,
        lifecycle: IntelligenceLifecycleService,
        betting_engine: BettingIntelligenceEngine,
    ) -> None:
        self.lifecycle = lifecycle
        self.betting_engine = betting_engine

    def run(
        self,
        *,
        situation_id: str,
        player_id: str,
        player_name: str,
        status_signal: dict[str, Any],
        player_context: dict[str, Any],
        prediction: ModelPrediction,
        quotes: list[OddsQuote],
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        current = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
        situation = self.lifecycle.create_situation(
            situation_id=situation_id,
            title=f"{player_name} player opportunity",
            objective=(
                f"Determine whether {player_name} has a measurable next-game "
                "performance opportunity supported by player context and at least "
                "two complete sportsbook markets."
            ),
            observation=str(status_signal["report"]),
            observed_at=str(status_signal["reported_at"]),
        )
        status_evidence_id = "player-status"
        self.lifecycle.record_evidence(
            situation.id,
            {
                "id": status_evidence_id,
                "claim": str(status_signal["report"]),
                "source": str(status_signal["source"]),
                "url": str(status_signal["url"]),
                "retrieved_at": str(status_signal["reported_at"]),
                "reliability": float(status_signal.get("reliability", 0.8)),
                "freshness": "current",
                "source_type": "primary",
                "independent_source_count": 1,
                "conflicting_reports": list(
                    status_signal.get("conflicting_reports", [])
                ),
            },
            occurred_at=str(status_signal["reported_at"]),
        )
        context_evidence_id = "player-context"
        context_claim = (
            f"{player_name} projects from {float(player_context['current_minutes']):.1f} "
            f"to {float(player_context['projected_minutes']):.1f} minutes and "
            f"{float(player_context['current_usage_rate']):.1%} to "
            f"{float(player_context['projected_usage_rate']):.1%} usage."
        )
        self.lifecycle.record_evidence(
            situation.id,
            {
                "id": context_evidence_id,
                "claim": context_claim,
                "source": str(player_context["source"]),
                "url": str(player_context["url"]),
                "retrieved_at": str(player_context["retrieved_at"]),
                "reliability": float(player_context.get("reliability", 0.85)),
                "freshness": "current",
                "source_type": "primary",
                "independent_source_count": 1,
                "conflicting_reports": [],
                "player_id": player_id,
                "recent_points_average": float(
                    player_context["recent_points_average"]
                ),
                "season_points_average": float(
                    player_context["season_points_average"]
                ),
                "stable_rotation_minutes": bool(
                    player_context["stable_rotation_minutes"]
                ),
                "coaching_context": str(
                    player_context.get("coaching_context") or ""
                ),
            },
            occurred_at=str(player_context["retrieved_at"]),
        )

        books = {quote.sportsbook for quote in quotes}
        quote_evidence_ids = []
        for index, quote in enumerate(quotes, start=1):
            if not quote.source_url:
                self.lifecycle.record_evidence_gap(
                    situation.id,
                    (
                        f"{quote.sportsbook} quote lacks an accessible source URL "
                        "and was excluded from the evidence graph."
                    ),
                    occurred_at=quote.fetched_at.isoformat(),
                )
                continue
            evidence_id = f"quote-{index}"
            quote_evidence_ids.append(evidence_id)
            self.lifecycle.record_evidence(
                situation.id,
                {
                    "id": evidence_id,
                    "claim": (
                        f"{quote.sportsbook} listed {quote.selection} "
                        f"{quote.line} at {quote.american_price:+d}."
                    ),
                    "source": quote.source,
                    "url": quote.source_url,
                    "retrieved_at": quote.fetched_at.isoformat(),
                    "reliability": 0.9,
                    "freshness": (
                        "current"
                        if (
                            current - quote.fetched_at
                        ).total_seconds()
                        <= self.betting_engine.policy.maximum_quote_age_seconds
                        else "stale"
                    ),
                    "source_type": "primary",
                    "independent_source_count": len(books),
                    "conflicting_reports": [],
                    "sportsbook": quote.sportsbook,
                    "designation": quote.designation,
                },
                occurred_at=quote.fetched_at.isoformat(),
            )

        hypothesis_id = "expanded-role"
        self.lifecycle.add_hypothesis(
            situation.id,
            {
                "id": hypothesis_id,
                "explanation": (
                    f"{player_name} receives more minutes and offensive "
                    "responsibility after the availability change."
                ),
                "supporting_evidence": [
                    status_evidence_id,
                    context_evidence_id,
                ],
                "contradicting_evidence": [],
                "competing_explanations": [
                    "The team distributes replacement minutes across a committee.",
                    "The player receives more minutes without more shot attempts.",
                ],
                "expected_indicators": [
                    "Confirmed lineup includes the player.",
                    "Projected minutes and usage remain elevated.",
                ],
                "falsification_criteria": list(prediction.invalidators),
                "confidence": prediction.probability,
                "confidence_history": [
                    {
                        "value": prediction.probability,
                        "reason": "Player context and availability evidence",
                        "recorded_at": prediction.generated_at.isoformat(),
                    }
                ],
                "status": "active",
            },
            occurred_at=prediction.generated_at.isoformat(),
        )
        forecast_id = (
            f"{prediction.event_id}:{prediction.market}:"
            f"{prediction.selection}:{prediction.line}"
        )
        self.lifecycle.add_forecast(
            situation.id,
            {
                "id": forecast_id,
                "hypothesis_ids": [hypothesis_id],
                "predicted_event": (
                    f"{player_name} {prediction.selection} {prediction.line} "
                    f"{prediction.market.replace('_', ' ')}."
                ),
                "probability": prediction.probability,
                "baseline_probability": 0.5,
                "horizon": "next game",
                "supporting_factors": list(prediction.reasons),
                "invalidation_factors": list(prediction.invalidators),
                "confidence_level": self._confidence_level(prediction.probability),
                "model_version": prediction.model_version,
                "market": prediction.market,
                "forecast_type": (
                    prediction.forecast_type or prediction.market
                ),
                "team_id": prediction.team_id,
                "player_role": prediction.player_role,
                "revision_history": [],
                "market_evidence_ids": quote_evidence_ids,
            },
            occurred_at=prediction.generated_at.isoformat(),
        )

        assessment = self.betting_engine.assess(
            prediction, quotes, as_of=current
        )
        self._record_decision(situation.id, assessment)
        self.lifecycle.add_monitoring_rule(
            situation.id,
            {
                "signal": "confirmed lineup or sportsbook line movement",
                "condition": (
                    "Re-evaluate on lineup change, invalidator, or a line move "
                    "of at least 1.0 point."
                ),
                "sources": [
                    str(status_signal["source"]),
                    *sorted(books),
                ],
                "expected_update_frequency": "until game start",
                "significance_threshold": 1.0,
                "escalation_rule": "Append evidence and revise the forecast.",
            },
        )
        restored = self.lifecycle.get_situation(situation.id)
        if restored is None:  # pragma: no cover - persistence contract guard
            raise RuntimeError("NBA opportunity situation was not persisted.")
        return {
            "situation": restored,
            "assessment": assessment,
            "claim_graph": self.lifecycle.claim_graph(situation.id),
        }

    def _record_decision(
        self, situation_id: str, assessment: MarketAssessment
    ) -> None:
        if assessment.decision is Decision.QUALIFIED and assessment.best_quote:
            quote = assessment.best_quote
            self.lifecycle.add_recommended_action(
                situation_id,
                (
                    f"Qualified research opportunity: {quote.sportsbook} "
                    f"{quote.selection} {quote.line} at "
                    f"{quote.american_price:+d}. Continue monitoring; this is "
                    "not a guaranteed result."
                ),
            )
            return
        reasons = "; ".join(assessment.rejection_reasons)
        self.lifecycle.record_evidence_gap(
            situation_id,
            (
                "No actionable sportsbook comparison: complete sportsbook markets "
                f"or validation requirements are missing. {reasons}"
            ),
        )
        self.lifecycle.add_recommended_action(
            situation_id,
            "Do not rank this opportunity until the listed evidence gaps are resolved.",
        )

    @staticmethod
    def _confidence_level(probability: float) -> str:
        if probability >= 0.75:
            return "high"
        if probability >= 0.6:
            return "medium"
        return "low"
