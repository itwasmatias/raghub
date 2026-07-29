from __future__ import annotations

from decimal import Decimal

import pytest

from sports.founder.mlb.analyzers.pitch_movement import (
    MlbPitchMovementAnalyzer,
    MlbPitchMovementSample,
)


def make_sample(
    **overrides: object,
) -> MlbPitchMovementSample:
    values: dict[str, object] = {
        "pitcher_id": "pitcher-001",
        "pitcher_name": "Test Starter",
        "pitch_type": "FOUR_SEAM",
        "baseline_spin_rate": Decimal("2350"),
        "recent_spin_rate": Decimal("2425"),
        "horizontal_movement": Decimal("8.5"),
        "induced_vertical_break": Decimal("17.2"),
        "release_point_variance": Decimal("0.08"),
        "extension": Decimal("6.7"),
        "pitches_observed": 120,
        "games_observed": 5,
    }
    values.update(overrides)
    return MlbPitchMovementSample(**values)


def test_sample_is_immutable() -> None:
    sample = make_sample()

    with pytest.raises(AttributeError):
        sample.pitcher_name = "Changed"  # type: ignore[misc]


def test_sample_rejects_unsupported_pitch_type() -> None:
    with pytest.raises(ValueError, match="pitch_type"):
        make_sample(pitch_type="MAGIC_BALL")


def test_sample_rejects_negative_spin_rate() -> None:
    with pytest.raises(ValueError, match="baseline_spin_rate"):
        make_sample(baseline_spin_rate=Decimal("-1"))


def test_analyzer_rewards_positive_spin_trend() -> None:
    analyzer = MlbPitchMovementAnalyzer()

    improving = analyzer.analyze(
        make_sample(
            baseline_spin_rate=Decimal("2300"),
            recent_spin_rate=Decimal("2450"),
        )
    )
    declining = analyzer.analyze(
        make_sample(
            baseline_spin_rate=Decimal("2450"),
            recent_spin_rate=Decimal("2250"),
        )
    )

    assert improving.spin_score > declining.spin_score
    assert improving.spin_change == Decimal("150.000")
    assert declining.spin_change == Decimal("-200.000")


def test_analyzer_uses_pitch_specific_movement_expectations() -> None:
    analyzer = MlbPitchMovementAnalyzer()

    strong_four_seam = analyzer.analyze(
        make_sample(
            pitch_type="FOUR_SEAM",
            horizontal_movement=Decimal("7.5"),
            induced_vertical_break=Decimal("18.5"),
        )
    )
    weak_four_seam = analyzer.analyze(
        make_sample(
            pitch_type="FOUR_SEAM",
            horizontal_movement=Decimal("7.5"),
            induced_vertical_break=Decimal("10.0"),
        )
    )

    assert strong_four_seam.movement_score > weak_four_seam.movement_score


def test_release_inconsistency_reduces_command_support() -> None:
    analyzer = MlbPitchMovementAnalyzer()

    consistent = analyzer.analyze(
        make_sample(release_point_variance=Decimal("0.05"))
    )
    inconsistent = analyzer.analyze(
        make_sample(release_point_variance=Decimal("0.55"))
    )

    assert consistent.release_consistency_score > (
        inconsistent.release_consistency_score
    )
    assert any(
        "release" in warning.lower()
        for warning in inconsistent.warnings
    )


def test_small_sample_reduces_reliability_and_pulls_toward_neutral() -> None:
    analyzer = MlbPitchMovementAnalyzer()

    established = analyzer.analyze(
        make_sample(
            pitches_observed=180,
            games_observed=7,
            induced_vertical_break=Decimal("19.0"),
        )
    )
    limited = analyzer.analyze(
        make_sample(
            pitches_observed=10,
            games_observed=1,
            induced_vertical_break=Decimal("19.0"),
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


def test_missing_data_returns_neutral_low_reliability_result() -> None:
    analyzer = MlbPitchMovementAnalyzer()

    assessment = analyzer.analyze(
        make_sample(
            baseline_spin_rate=None,
            recent_spin_rate=None,
            horizontal_movement=None,
            induced_vertical_break=None,
            release_point_variance=None,
            extension=None,
            pitches_observed=0,
            games_observed=0,
        )
    )

    assert assessment.spin_score == Decimal("0.500")
    assert assessment.movement_score == Decimal("0.500")
    assert assessment.release_consistency_score == Decimal("0.500")
    assert assessment.overall_score == Decimal("0.500")
    assert assessment.reliability_score == Decimal("0.000")
    assert assessment.data_quality == "LOW"
    assert assessment.warnings


def test_assessment_is_bounded_and_explainable() -> None:
    analyzer = MlbPitchMovementAnalyzer()

    assessment = analyzer.analyze(make_sample())

    assert assessment.pitcher_id == "pitcher-001"
    assert assessment.pitcher_name == "Test Starter"
    assert assessment.pitch_type == "FOUR_SEAM"
    assert Decimal("0") <= assessment.spin_score <= Decimal("1")
    assert Decimal("0") <= assessment.movement_score <= Decimal("1")
    assert Decimal("0") <= assessment.release_consistency_score <= Decimal("1")
    assert Decimal("0") <= assessment.extension_score <= Decimal("1")
    assert Decimal("0") <= assessment.raw_score <= Decimal("1")
    assert Decimal("0") <= assessment.overall_score <= Decimal("1")
    assert Decimal("0") <= assessment.reliability_score <= Decimal("1")
    assert assessment.data_quality in {"LOW", "MEDIUM", "HIGH"}
    assert assessment.evidence
