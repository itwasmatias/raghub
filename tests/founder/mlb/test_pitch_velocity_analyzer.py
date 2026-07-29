from __future__ import annotations

from decimal import Decimal

import pytest

from sports.founder.mlb.analyzers.pitch_velocity import (
    MlbPitchVelocityAnalyzer,
    MlbPitchVelocitySample,
)


def make_sample(
    **overrides: object,
) -> MlbPitchVelocitySample:
    values: dict[str, object] = {
        "pitcher_id": "pitcher-001",
        "pitcher_name": "Test Starter",
        "baseline_velocity": Decimal("95.0"),
        "recent_velocity": Decimal("95.6"),
        "maximum_velocity": Decimal("97.8"),
        "early_inning_velocity": Decimal("95.8"),
        "late_inning_velocity": Decimal("95.1"),
        "pitches_observed": 180,
        "games_observed": 5,
    }
    values.update(overrides)
    return MlbPitchVelocitySample(**values)


def test_sample_is_immutable() -> None:
    sample = make_sample()

    with pytest.raises(AttributeError):
        sample.pitcher_name = "Changed"  # type: ignore[misc]


def test_sample_rejects_negative_velocity() -> None:
    with pytest.raises(ValueError, match="baseline_velocity"):
        make_sample(baseline_velocity=Decimal("-1"))


def test_sample_rejects_negative_observation_counts() -> None:
    with pytest.raises(ValueError, match="pitches_observed"):
        make_sample(pitches_observed=-1)


def test_analyzer_rewards_velocity_gain_relative_to_baseline() -> None:
    analyzer = MlbPitchVelocityAnalyzer()

    gaining = analyzer.analyze(
        make_sample(
            baseline_velocity=Decimal("94.5"),
            recent_velocity=Decimal("96.0"),
        )
    )
    declining = analyzer.analyze(
        make_sample(
            baseline_velocity=Decimal("96.0"),
            recent_velocity=Decimal("94.5"),
        )
    )

    assert gaining.velocity_score > declining.velocity_score
    assert gaining.velocity_change == Decimal("1.500")
    assert declining.velocity_change == Decimal("-1.500")


def test_analyzer_penalizes_late_inning_velocity_fade() -> None:
    analyzer = MlbPitchVelocityAnalyzer()

    stable = analyzer.analyze(
        make_sample(
            early_inning_velocity=Decimal("95.8"),
            late_inning_velocity=Decimal("95.4"),
        )
    )
    fading = analyzer.analyze(
        make_sample(
            early_inning_velocity=Decimal("95.8"),
            late_inning_velocity=Decimal("92.9"),
        )
    )

    assert stable.velocity_retention_score > fading.velocity_retention_score
    assert any(
        "late-inning" in warning.lower()
        for warning in fading.warnings
    )


def test_small_sample_reduces_reliability_and_pulls_toward_neutral() -> None:
    analyzer = MlbPitchVelocityAnalyzer()

    established = analyzer.analyze(
        make_sample(
            pitches_observed=240,
            games_observed=7,
            recent_velocity=Decimal("97.0"),
        )
    )
    limited = analyzer.analyze(
        make_sample(
            pitches_observed=12,
            games_observed=1,
            recent_velocity=Decimal("97.0"),
        )
    )

    assert established.reliability_score > limited.reliability_score
    assert abs(limited.overall_score - Decimal("0.500")) < abs(
        established.overall_score - Decimal("0.500")
    )
    assert any(
        "sample" in warning.lower()
        for warning in limited.warnings
    )


def test_missing_velocity_data_returns_neutral_low_reliability_result() -> None:
    analyzer = MlbPitchVelocityAnalyzer()

    assessment = analyzer.analyze(
        make_sample(
            baseline_velocity=None,
            recent_velocity=None,
            maximum_velocity=None,
            early_inning_velocity=None,
            late_inning_velocity=None,
            pitches_observed=0,
            games_observed=0,
        )
    )

    assert assessment.velocity_score == Decimal("0.500")
    assert assessment.velocity_retention_score == Decimal("0.500")
    assert assessment.overall_score == Decimal("0.500")
    assert assessment.reliability_score == Decimal("0.000")
    assert assessment.data_quality == "LOW"
    assert assessment.warnings


def test_assessment_is_bounded_and_explainable() -> None:
    analyzer = MlbPitchVelocityAnalyzer()

    assessment = analyzer.analyze(make_sample())

    assert assessment.pitcher_id == "pitcher-001"
    assert assessment.pitcher_name == "Test Starter"
    assert Decimal("0") <= assessment.velocity_score <= Decimal("1")
    assert Decimal("0") <= assessment.velocity_retention_score <= Decimal("1")
    assert Decimal("0") <= assessment.raw_score <= Decimal("1")
    assert Decimal("0") <= assessment.overall_score <= Decimal("1")
    assert Decimal("0") <= assessment.reliability_score <= Decimal("1")
    assert assessment.data_quality in {"LOW", "MEDIUM", "HIGH"}
    assert assessment.evidence
