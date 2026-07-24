from __future__ import annotations

from dataclasses import dataclass

from sports.data.repositories.sqlite_autonomy_repository import (
    SQLiteAutonomyRepository,
)
from sports.intelligence.models.autonomy import (
    AlertOutcome,
    IntelligenceAlert,
    ResearchLifecycle,
)


@dataclass(frozen=True, slots=True)
class CalibrationBucket:
    lower: float
    upper: float
    count: int
    avg_predicted_confidence: float
    empirical_accuracy: float


@dataclass(frozen=True, slots=True)
class HypothesisUpdate:
    lifecycle_id: int | None
    hypothesis: str
    status: str
    support_rate: float
    recommendation: str


class LearningIntelligenceService:
    """Builds reusable learning signals from historical alerts/outcomes."""

    def __init__(self, repository: SQLiteAutonomyRepository) -> None:
        self.repository = repository

    def evaluate_past_alerts(self) -> dict[str, object]:
        alerts = self.repository.list_alerts()
        outcomes = {
            outcome.alert_id: outcome for outcome in self.repository.list_outcomes()
        }
        paired = [
            (alert, outcomes.get(int(alert.id or 0)))
            for alert in alerts
            if int(alert.id or 0) in outcomes
        ]
        if not paired:
            return {
                "evaluated_alerts": 0,
                "accuracy": 0.0,
                "avg_confidence": 0.0,
                "avg_confidence_error": 0.0,
                "by_signal": {},
            }

        correct = [1.0 if outcome.correct else 0.0 for _, outcome in paired if outcome]
        confidences = [alert.confidence for alert, _ in paired]
        confidence_errors = [
            outcome.confidence_error for _, outcome in paired if outcome
        ]
        by_signal: dict[str, dict[str, float]] = {}
        for alert, outcome in paired:
            if outcome is None:
                continue
            signal = alert.signal
            group = by_signal.setdefault(
                signal,
                {
                    "count": 0.0,
                    "accuracy": 0.0,
                    "avg_confidence": 0.0,
                    "avg_confidence_error": 0.0,
                },
            )
            group["count"] += 1
            group["accuracy"] += 1.0 if outcome.correct else 0.0
            group["avg_confidence"] += alert.confidence
            group["avg_confidence_error"] += outcome.confidence_error

        for values in by_signal.values():
            count = values["count"] or 1.0
            values["accuracy"] /= count
            values["avg_confidence"] /= count
            values["avg_confidence_error"] /= count

        return {
            "evaluated_alerts": len(paired),
            "accuracy": sum(correct) / len(correct),
            "avg_confidence": sum(confidences) / len(confidences),
            "avg_confidence_error": sum(confidence_errors) / len(confidence_errors),
            "by_signal": by_signal,
        }

    def calibrate_confidence(
        self,
        bucket_count: int = 5,
    ) -> list[CalibrationBucket]:
        alerts = self.repository.list_alerts()
        outcomes = {
            outcome.alert_id: outcome for outcome in self.repository.list_outcomes()
        }
        pairs = [
            (alert, outcomes.get(int(alert.id or 0)))
            for alert in alerts
            if int(alert.id or 0) in outcomes
        ]
        if not pairs:
            return []

        bucket_count = max(1, bucket_count)
        bucket_width = 1.0 / bucket_count
        grouped: list[list[tuple[IntelligenceAlert, AlertOutcome | None]]] = [
            [] for _ in range(bucket_count)
        ]
        for pair in pairs:
            confidence = min(0.999999, max(0.0, pair[0].confidence))
            index = int(confidence / bucket_width)
            grouped[index].append(pair)

        buckets: list[CalibrationBucket] = []
        for index, group in enumerate(grouped):
            if not group:
                continue
            lower = index * bucket_width
            upper = lower + bucket_width
            predicted = [alert.confidence for alert, _ in group]
            observed = [
                1.0 if outcome and outcome.correct else 0.0 for _, outcome in group
            ]
            buckets.append(
                CalibrationBucket(
                    lower=lower,
                    upper=upper,
                    count=len(group),
                    avg_predicted_confidence=sum(predicted) / len(predicted),
                    empirical_accuracy=sum(observed) / len(observed),
                )
            )
        return buckets

    def propose_hypothesis_updates(self) -> list[HypothesisUpdate]:
        lifecycles = self.repository.list_lifecycles()
        summary = self.evaluate_past_alerts()
        baseline_accuracy = float(summary["accuracy"])
        updates: list[HypothesisUpdate] = []

        for lifecycle in lifecycles:
            if lifecycle.outcome is None:
                status = "pending"
                support_rate = baseline_accuracy
                recommendation = (
                    "Keep collecting evidence before updating this hypothesis."
                )
            else:
                positive_tokens = (
                    "remained",
                    "sustained",
                    "improved",
                    "predictive",
                    "above",
                )
                negative_tokens = ("declined", "rejected", "failed", "below", "noise")
                outcome_text = lifecycle.outcome.lower()
                pos = sum(token in outcome_text for token in positive_tokens)
                neg = sum(token in outcome_text for token in negative_tokens)
                support_rate = max(
                    0.0, min(1.0, baseline_accuracy + (pos - neg) * 0.05)
                )
                if support_rate >= 0.65:
                    status = "strengthen"
                    recommendation = "Increase confidence and reuse this hypothesis in similar alerts."
                elif support_rate <= 0.4:
                    status = "revise"
                    recommendation = (
                        "Revise the hypothesis conditions before reusing it."
                    )
                else:
                    status = "monitor"
                    recommendation = "Retain as a candidate and gather additional validation samples."

            updates.append(
                HypothesisUpdate(
                    lifecycle_id=lifecycle.id,
                    hypothesis=lifecycle.hypothesis,
                    status=status,
                    support_rate=support_rate,
                    recommendation=recommendation,
                )
            )

        return updates

    def build_reusable_knowledge(self, limit: int = 10) -> list[str]:
        limit = max(1, limit)
        summary = self.evaluate_past_alerts()
        by_signal = summary.get("by_signal", {})
        knowledge: list[str] = []

        if isinstance(by_signal, dict):
            for signal, values in by_signal.items():
                accuracy = float(values.get("accuracy", 0.0))
                avg_error = float(values.get("avg_confidence_error", 0.0))
                knowledge.append(
                    f"Signal '{signal}' accuracy {accuracy:.0%} with confidence error {avg_error:.2f}."
                )

        for lifecycle in self.repository.list_lifecycles():
            if lifecycle.learned_knowledge:
                knowledge.append(lifecycle.learned_knowledge.strip())

        unique: list[str] = []
        seen: set[str] = set()
        for item in knowledge:
            normalized = item.strip()
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(normalized)

        scored = sorted(unique, key=lambda value: (-len(value), value.lower()))
        return scored[:limit]

    def confidence_calibration_error(self, bucket_count: int = 5) -> float:
        buckets = self.calibrate_confidence(bucket_count=bucket_count)
        if not buckets:
            return 0.0
        weighted_errors = [
            abs(bucket.avg_predicted_confidence - bucket.empirical_accuracy)
            * bucket.count
            for bucket in buckets
        ]
        total = sum(bucket.count for bucket in buckets)
        return sum(weighted_errors) / max(1, total)

    def update_hypothesis_text(
        self,
        lifecycle: ResearchLifecycle,
        confidence_score: float,
    ) -> ResearchLifecycle:
        label = (
            "high-confidence"
            if confidence_score >= 0.65
            else "low-confidence"
            if confidence_score <= 0.4
            else "medium-confidence"
        )
        updated = ResearchLifecycle(
            id=lifecycle.id,
            observation=lifecycle.observation,
            hypothesis=f"[{label}] {lifecycle.hypothesis}",
            experiment=lifecycle.experiment,
            outcome=lifecycle.outcome,
            learned_knowledge=lifecycle.learned_knowledge,
        )
        return self.repository.save_lifecycle(updated)
