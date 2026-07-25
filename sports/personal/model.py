from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime, timezone
from math import exp, log, sqrt
from pathlib import Path
from typing import Any

from sports.personal.models import ModelMetadata, MoneylineForecast


class InsufficientCalibrationData(ValueError):
    code = "INSUFFICIENT_CALIBRATION_DATA"


class ModelNotTrained(FileNotFoundError):
    code = "MODEL_NOT_TRAINED"


class BaselineMoneylineModel:
    FEATURE_NAMES = (
        "home_season_win_pct",
        "away_season_win_pct",
        "home_recent_win_pct",
        "away_recent_win_pct",
        "home_season_score_diff",
        "away_season_score_diff",
        "home_recent_score_diff",
        "away_recent_score_diff",
        "home_rest_days",
        "away_rest_days",
        "home_advantage",
    )
    FEATURE_VERSION = "moneyline-v1"

    def __init__(
        self,
        *,
        weights: list[float],
        intercept: float,
        calibration_a: float,
        calibration_b: float,
        metadata: ModelMetadata,
    ) -> None:
        self.weights = [float(value) for value in weights]
        self.intercept = float(intercept)
        self.calibration_a = float(calibration_a)
        self.calibration_b = float(calibration_b)
        self.metadata = metadata

    @classmethod
    def train(
        cls,
        rows: list[dict[str, Any]],
        *,
        minimum_examples: int = 50,
        version: str = "1.0.0",
        trained_at: datetime | None = None,
    ) -> BaselineMoneylineModel:
        ordered = sorted(rows, key=lambda row: str(row["event_date"]))
        if len(ordered) < minimum_examples:
            raise InsufficientCalibrationData(
                f"INSUFFICIENT_CALIBRATION_DATA: need at least {minimum_examples} chronological examples; received {len(ordered)}"
            )
        matrix = [
            [float(row[name]) for name in cls.FEATURE_NAMES]
            for row in ordered
        ]
        targets = [float(row["home_win"]) for row in ordered]
        split = max(1, min(len(ordered) - 10, int(len(ordered) * 0.8)))
        train_x, validation_x = matrix[:split], matrix[split:]
        train_y, validation_y = targets[:split], targets[split:]
        if len(validation_y) < 10 or len(set(validation_y)) < 2:
            raise InsufficientCalibrationData(
                "INSUFFICIENT_CALIBRATION_DATA: validation requires at least 10 examples containing both outcomes"
            )
        means = [
            sum(row[index] for row in train_x) / len(train_x)
            for index in range(len(cls.FEATURE_NAMES))
        ]
        scales = []
        for index, mean in enumerate(means):
            variance = sum(
                (row[index] - mean) ** 2 for row in train_x
            ) / len(train_x)
            scales.append(sqrt(variance) or 1.0)
        train_scaled = [
            [(value - means[index]) / scales[index] for index, value in enumerate(row)]
            for row in train_x
        ]
        validation_scaled = [
            [(value - means[index]) / scales[index] for index, value in enumerate(row)]
            for row in validation_x
        ]
        weights, intercept = cls._fit_logistic(train_scaled, train_y)
        raw_validation = [
            cls._sigmoid(cls._dot(row, weights) + intercept)
            for row in validation_scaled
        ]
        logits = [
            log(max(value, 1e-6) / max(1 - value, 1e-6))
            for value in raw_validation
        ]
        calibration_a, calibration_b = cls._fit_platt(logits, validation_y)
        calibrated = [
            cls._sigmoid(calibration_a * value + calibration_b)
            for value in logits
        ]
        brier = sum(
            (probability - target) ** 2
            for probability, target in zip(calibrated, validation_y)
        ) / len(validation_y)
        log_loss = -sum(
            target * log(max(probability, 1e-9))
            + (1 - target) * log(max(1 - probability, 1e-9))
            for probability, target in zip(calibrated, validation_y)
        ) / len(validation_y)
        validation_rate = sum(validation_y) / len(validation_y)
        baseline_brier = sum(
            (validation_rate - target) ** 2 for target in validation_y
        ) / len(validation_y)
        baseline_log_loss = -sum(
            target * log(max(validation_rate, 1e-9))
            + (1 - target) * log(max(1 - validation_rate, 1e-9))
            for target in validation_y
        ) / len(validation_y)
        calibration_status = (
            "calibrated"
            if brier < baseline_brier and log_loss < baseline_log_loss
            else "validation_failed"
        )
        current = (trained_at or datetime.now(timezone.utc)).astimezone(
            timezone.utc
        )
        metadata = ModelMetadata(
            model_name="sip-moneyline-baseline",
            version=version,
            training_date=current.isoformat(),
            training_period=(
                str(ordered[0]["event_date"]),
                str(ordered[split - 1]["event_date"]),
            ),
            validation_period=(
                str(ordered[split]["event_date"]),
                str(ordered[-1]["event_date"]),
            ),
            feature_version=cls.FEATURE_VERSION,
            training_examples=split,
            calibration_method="platt",
            brier_score=brier,
            log_loss=log_loss,
            calibration_status=calibration_status,
            validation_examples=len(validation_y),
            baseline_brier_score=baseline_brier,
            baseline_log_loss=baseline_log_loss,
        )
        model = cls(
            weights=weights,
            intercept=intercept,
            calibration_a=calibration_a,
            calibration_b=calibration_b,
            metadata=metadata,
        )
        model.means = means
        model.scales = scales
        return model

    @classmethod
    def train_csv(
        cls, path: str | Path, **kwargs: Any
    ) -> BaselineMoneylineModel:
        with Path(path).open(newline="", encoding="utf-8") as handle:
            return cls.train(list(csv.DictReader(handle)), **kwargs)

    def predict(
        self,
        *,
        canonical_event_id: str,
        league: str,
        features: dict[str, Any],
        feature_timestamp: str,
        forecast_timestamp: str | None = None,
    ) -> tuple[MoneylineForecast, MoneylineForecast]:
        missing = tuple(
            name for name in self.FEATURE_NAMES if features.get(name) is None
        )
        if missing:
            raise ValueError(
                "INSUFFICIENT_FEATURE_DATA: " + ", ".join(missing)
            )
        vector = [float(features[name]) for name in self.FEATURE_NAMES]
        means = getattr(self, "means", [0.0] * len(vector))
        scales = getattr(self, "scales", [1.0] * len(vector))
        standardized = [
            (value - means[index]) / scales[index]
            for index, value in enumerate(vector)
        ]
        score = self._dot(standardized, self.weights) + self.intercept
        raw_home = self._sigmoid(score)
        raw_logit = log(
            max(raw_home, 1e-9) / max(1 - raw_home, 1e-9)
        )
        calibrated_home = self._sigmoid(
            self.calibration_a * raw_logit + self.calibration_b
        )
        current = forecast_timestamp or datetime.now(timezone.utc).isoformat()
        factors = tuple(
            f"{name}: {features[name]}"
            for name in self.FEATURE_NAMES
        )
        common = {
            "canonical_event_id": canonical_event_id,
            "league": league,
            "market": "moneyline",
            "period": "full_game",
            "model_version": self.metadata.version,
            "feature_version": self.metadata.feature_version,
            "feature_timestamp": feature_timestamp,
            "forecast_timestamp": current,
            "calibration_status": self.metadata.calibration_status,
            "contributing_factors": factors,
            "missing_feature_warnings": (),
            "metadata": self.metadata,
        }
        return (
            MoneylineForecast(
                selection="home",
                raw_probability=raw_home,
                calibrated_probability=calibrated_home,
                **common,
            ),
            MoneylineForecast(
                selection="away",
                raw_probability=1 - raw_home,
                calibrated_probability=1 - calibrated_home,
                **common,
            ),
        )

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "weights": self.weights,
            "intercept": self.intercept,
            "calibration_a": self.calibration_a,
            "calibration_b": self.calibration_b,
            "means": getattr(self, "means", [0.0] * len(self.weights)),
            "scales": getattr(self, "scales", [1.0] * len(self.weights)),
            "metadata": asdict(self.metadata),
        }
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> BaselineMoneylineModel:
        target = Path(path)
        if not target.exists():
            raise ModelNotTrained(f"MODEL_NOT_TRAINED: {target} does not exist")
        payload = json.loads(target.read_text(encoding="utf-8"))
        metadata = payload["metadata"]
        metadata["training_period"] = tuple(metadata["training_period"])
        metadata["validation_period"] = tuple(metadata["validation_period"])
        model = cls(
            weights=payload["weights"],
            intercept=payload["intercept"],
            calibration_a=payload["calibration_a"],
            calibration_b=payload["calibration_b"],
            metadata=ModelMetadata(**metadata),
        )
        model.means = [float(value) for value in payload["means"]]
        model.scales = [float(value) for value in payload["scales"]]
        return model

    @staticmethod
    def _fit_logistic(
        matrix: list[list[float]], targets: list[float]
    ) -> tuple[list[float], float]:
        weights = [0.0] * len(matrix[0])
        intercept = 0.0
        prior_loss = None
        for iteration in range(250):
            probabilities = [
                BaselineMoneylineModel._sigmoid(
                    BaselineMoneylineModel._dot(row, weights) + intercept
                )
                for row in matrix
            ]
            errors = [
                probability - target
                for probability, target in zip(probabilities, targets)
            ]
            for index in range(len(weights)):
                gradient = sum(
                    row[index] * error
                    for row, error in zip(matrix, errors)
                ) / len(targets)
                weights[index] -= 0.08 * (
                    gradient + 0.001 * weights[index]
                )
            intercept -= 0.08 * sum(errors) / len(errors)
            if iteration % 25 == 0:
                loss = sum(error * error for error in errors) / len(errors)
                if prior_loss is not None and abs(prior_loss - loss) < 1e-10:
                    break
                prior_loss = loss
        return weights, intercept

    @staticmethod
    def _fit_platt(
        logits: list[float], targets: list[float]
    ) -> tuple[float, float]:
        a, b = 1.0, 0.0
        for _ in range(200):
            probabilities = [
                BaselineMoneylineModel._sigmoid(a * value + b)
                for value in logits
            ]
            errors = [
                probability - target
                for probability, target in zip(probabilities, targets)
            ]
            a -= 0.04 * sum(
                error * value for error, value in zip(errors, logits)
            ) / len(errors)
            b -= 0.04 * sum(errors) / len(errors)
        return a, b

    @staticmethod
    def _sigmoid(value: float) -> float:
        clipped = max(-35.0, min(35.0, float(value)))
        return 1 / (1 + exp(-clipped))

    @staticmethod
    def _dot(left: list[float], right: list[float]) -> float:
        return sum(a * b for a, b in zip(left, right))
