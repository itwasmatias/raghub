from __future__ import annotations

from typing import Protocol

from sports.intelligence.models.autonomy import AlertOutcome, IntelligenceAlert
from sports.intelligence.models.calibration import (
    CalibrationAlertDetail,
    CalibrationBucketDetail,
    CalibrationDrilldown,
)


class CalibrationRepository(Protocol):
    def list_alerts(self) -> list[IntelligenceAlert]: ...

    def list_outcomes(self) -> list[AlertOutcome]: ...


class CalibrationDrilldownService:
    """Builds detailed confidence-calibration evidence for API and UI consumers."""

    def __init__(self, repository: CalibrationRepository) -> None:
        self.repository = repository

    def build(
        self,
        bucket_count: int = 10,
        bucket_index: int | None = None,
    ) -> CalibrationDrilldown:
        if bucket_count < 1:
            raise ValueError("bucket_count must be at least 1")
        if bucket_index is not None and not 0 <= bucket_index < bucket_count:
            raise ValueError("bucket_index must identify a requested bucket")

        outcomes = {
            outcome.alert_id: outcome for outcome in self.repository.list_outcomes()
        }
        pairs: list[tuple[IntelligenceAlert, AlertOutcome]] = []
        for alert in self.repository.list_alerts():
            alert_id = int(alert.id or 0)
            outcome = outcomes.get(alert_id)
            if alert.id is not None and outcome is not None:
                pairs.append((alert, outcome))

        grouped: list[list[tuple[IntelligenceAlert, AlertOutcome]]] = [
            [] for _ in range(bucket_count)
        ]
        for alert, outcome in pairs:
            confidence = min(1.0, max(0.0, alert.confidence))
            index = min(bucket_count - 1, int(confidence * bucket_count))
            grouped[index].append((alert, outcome))

        buckets = tuple(
            self._build_bucket(index, bucket_count, grouped[index])
            for index in range(bucket_count)
            if bucket_index is None or index == bucket_index
        )
        correct_alerts = sum(1 for _, outcome in pairs if outcome.correct)
        total_confidence = sum(alert.confidence for alert, _ in pairs)
        total = len(pairs)
        weighted_error = sum(
            bucket.calibration_error * bucket.count
            for bucket in (
                self._build_bucket(index, bucket_count, group)
                for index, group in enumerate(grouped)
            )
        )
        return CalibrationDrilldown(
            bucket_count=bucket_count,
            evaluated_alerts=total,
            correct_alerts=correct_alerts,
            accuracy=correct_alerts / total if total else 0.0,
            average_confidence=total_confidence / total if total else 0.0,
            calibration_error=weighted_error / total if total else 0.0,
            buckets=buckets,
        )

    @staticmethod
    def _build_bucket(
        index: int,
        bucket_count: int,
        pairs: list[tuple[IntelligenceAlert, AlertOutcome]],
    ) -> CalibrationBucketDetail:
        lower = index / bucket_count
        upper = (index + 1) / bucket_count
        count = len(pairs)
        correct_count = sum(1 for _, outcome in pairs if outcome.correct)
        average_confidence = (
            sum(alert.confidence for alert, _ in pairs) / count if count else 0.0
        )
        accuracy = correct_count / count if count else 0.0
        details = tuple(
            CalibrationAlertDetail(
                alert_id=int(alert.id or 0),
                player_id=alert.player_id,
                signal=alert.signal,
                created_at=alert.created_at,
                confidence=alert.confidence,
                correct=outcome.correct,
                confidence_error=outcome.confidence_error,
                baseline_value=alert.baseline_value,
                observed_value=alert.observed_value,
                continued=outcome.continued,
                role_grew=outcome.role_grew,
                evaluated_at=outcome.evaluated_at,
                evidence=tuple(alert.evidence),
                outcome_notes=outcome.notes,
            )
            for alert, outcome in pairs
        )
        return CalibrationBucketDetail(
            index=index,
            lower=lower,
            upper=upper,
            count=count,
            correct_count=correct_count,
            accuracy=accuracy,
            average_confidence=average_confidence,
            calibration_error=abs(average_confidence - accuracy) if count else 0.0,
            alerts=details,
        )
