from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CalibrationAlertDetail:
    alert_id: int
    player_id: str
    signal: str
    created_at: str
    confidence: float
    correct: bool
    confidence_error: float
    baseline_value: float
    observed_value: float
    continued: bool
    role_grew: bool
    evaluated_at: str
    evidence: tuple[str, ...]
    outcome_notes: str


@dataclass(frozen=True, slots=True)
class CalibrationBucketDetail:
    index: int
    lower: float
    upper: float
    count: int
    correct_count: int
    accuracy: float
    average_confidence: float
    calibration_error: float
    alerts: tuple[CalibrationAlertDetail, ...]


@dataclass(frozen=True, slots=True)
class CalibrationDrilldown:
    bucket_count: int
    evaluated_alerts: int
    correct_alerts: int
    accuracy: float
    average_confidence: float
    calibration_error: float
    buckets: tuple[CalibrationBucketDetail, ...]
