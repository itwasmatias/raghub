from datetime import datetime, timedelta, timezone

import pytest

from sports.personal.model import (
    BaselineMoneylineModel,
    InsufficientCalibrationData,
    ModelNotTrained,
)


def rows(count=80):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    values = []
    for index in range(count):
        stronger_home = index % 3 != 0
        values.append({
            "event_date": (start + timedelta(days=index)).date().isoformat(),
            "home_win": 1 if stronger_home else 0,
            "home_season_win_pct": 0.65 if stronger_home else 0.40,
            "away_season_win_pct": 0.42 if stronger_home else 0.62,
            "home_recent_win_pct": 0.60 if stronger_home else 0.35,
            "away_recent_win_pct": 0.40 if stronger_home else 0.65,
            "home_season_score_diff": 4.5 if stronger_home else -2.5,
            "away_season_score_diff": -3.0 if stronger_home else 4.0,
            "home_recent_score_diff": 5.0 if stronger_home else -3.0,
            "away_recent_score_diff": -3.5 if stronger_home else 4.5,
            "home_rest_days": 2,
            "away_rest_days": 1,
            "home_advantage": 1,
        })
    return values


def test_training_is_chronological_calibrated_and_persisted(tmp_path):
    model = BaselineMoneylineModel.train(
        rows(), trained_at=datetime(2026, 7, 24, tzinfo=timezone.utc)
    )
    path = tmp_path / "model.json"
    model.save(path)
    loaded = BaselineMoneylineModel.load(path)
    forecasts = loaded.predict(
        canonical_event_id="wnba:game",
        league="WNBA",
        features=rows()[0],
        feature_timestamp="2026-07-24T23:00:00+00:00",
        forecast_timestamp="2026-07-24T23:30:00+00:00",
    )

    assert loaded.metadata.calibration_status == "calibrated"
    assert loaded.metadata.calibration_method == "platt"
    assert loaded.metadata.validation_period[0] > loaded.metadata.training_period[1]
    assert loaded.metadata.brier_score is not None
    assert loaded.metadata.log_loss is not None
    assert forecasts[0].calibrated_probability + forecasts[1].calibrated_probability == pytest.approx(1)


def test_model_fails_honestly_without_training_or_calibration_data(tmp_path):
    with pytest.raises(InsufficientCalibrationData, match="INSUFFICIENT_CALIBRATION_DATA"):
        BaselineMoneylineModel.train(rows(20))
    with pytest.raises(ModelNotTrained, match="MODEL_NOT_TRAINED"):
        BaselineMoneylineModel.load(tmp_path / "missing.json")
