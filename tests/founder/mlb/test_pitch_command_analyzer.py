from __future__ import annotations

from decimal import Decimal

import pytest

from sports.founder.mlb.analyzers.pitch_command import (
    MlbPitchCommandAnalyzer,
    MlbPitchCommandSample,
)


def make_sample(
    **overrides: object,
) -> MlbPitchCommandSample:
    values: dict[str, object] = {
        "pitcher_id": "pitcher-001",
        "pitcher_name": "Test Starter",
        "strike_rate": Decimal("0.67"),
        "walk_rate": Decimal("0.065"),
        "first_pitch_strike_rate": Decimal("0.64"),
        "zone_rate": Decimal("0.49"),
        "edge_rate": Decimal("0.43"),
        "heart_rate": Decimal("0.21"),
        "mistake_rate": Decimal("0.055"),
        "location_quality": Decimal("0.72"),
        "pitches_observed": 420,
        "games_observed": 7,
    }
    values.update(overrides)
    return MlbPitchCommandSample(**values)


def test_sample_is_immutable() -> None:
    sample = make_sample()

    with pytest.raises(AttributeError):
        sample.pitcher_name = "Changed"  # type: ignore[misc]


def test_sample_rejects_rate_above_one() -> None:
    with pytest.raises(ValueError, match="strike_rate"):
        make_sample(strike_rate=Decimal("1.01"))


def test_control_rewards_strikes_and_low_walk_rate() -> None:
    analyzer = MlbPitchCommandAnalyzer()

    strong_control = analyzer.analyze(
        make_sample(
            strike_rate=Decimal("0.70"),
            walk_rate=Decimal("0.045"),
            first_pitch_strike_rate=Decimal("0.68"),
        )
    )
    weak_control = analyzer.analyze(
        make_sample(
            strike_rate=Decimal("0.59"),
            walk_rate=Decimal("0.125"),
            first_pitch_strike_rate=Decimal("0.51"),
        )
    )

    assert strong_control.control_score > weak_control.control_score


def test_command_rewards_edges_and_location_quality() -> None:
    analyzer = MlbPitchCommandAnalyzer()

    precise = analyzer.analyze(
        make_sample(
            edge_rate=Decimal("0.48"),
            heart_rate=Decimal("0.17"),
            mistake_rate=Decimal("0.035"),
            location_quality=Decimal("0.82"),
        )
    )
    imprecise = analyzer.analyze(
        make_sample(
            edge_rate=Decimal("0.28"),
            heart_rate=Decimal("0.35"),
            mistake_rate=Decimal("0.14"),
            location_quality=Decimal("0.38"),
        )
    )

    assert precise.command_score > imprecise.command_score


def test_throwing_strikes_does_not_equal_elite_command() -> None:
    analyzer = MlbPitchCommandAnalyzer()

    strike_thrower = analyzer.analyze(
        make_sample(
            strike_rate=Decimal("0.71"),
            walk_rate=Decimal("0.04"),
            first_pitch_strike_rate=Decimal("0.69"),
            edge_rate=Decimal("0.25"),
            heart_rate=Decimal("0.39"),
            mistake_rate=Decimal("0.16"),
            location_quality=Decimal("0.35"),
        )
    )

    assert strike_thrower.control_score >= Decimal("0.750")
    assert strike_thrower.command_score < Decimal("0.500")
    assert strike_thrower.control_score > strike_thrower.command_score
    assert any(
        "command" in warning.lower()
        or "heart" in warning.lower()
        or "mistake" in warning.lower()
        for warning in strike_thrower.warnings
    )


def test_high_mistake_rate_penalizes_command() -> None:
    analyzer = MlbPitchCommandAnalyzer()

    safe = analyzer.analyze(
        make_sample(mistake_rate=Decimal("0.03"))
    )
    mistake_prone = analyzer.analyze(
        make_sample(mistake_rate=Decimal("0.18"))
    )

    assert safe.command_score > mistake_prone.command_score
    assert any(
        "mistake" in warning.lower()
        for warning in mistake_prone.warnings
    )


def test_small_sample_reduces_reliability_and_pulls_toward_neutral() -> None:
    analyzer = MlbPitchCommandAnalyzer()

    established = analyzer.analyze(
        make_sample(
            pitches_observed=500,
            games_observed=8,
            location_quality=Decimal("0.90"),
        )
    )
    limited = analyzer.analyze(
        make_sample(
            pitches_observed=18,
            games_observed=1,
            location_quality=Decimal("0.90"),
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
    analyzer = MlbPitchCommandAnalyzer()

    assessment = analyzer.analyze(
        make_sample(
            strike_rate=None,
            walk_rate=None,
            first_pitch_strike_rate=None,
            zone_rate=None,
            edge_rate=None,
            heart_rate=None,
            mistake_rate=None,
            location_quality=None,
            pitches_observed=0,
            games_observed=0,
        )
    )

    assert assessment.control_score == Decimal("0.500")
    assert assessment.command_score == Decimal("0.500")
    assert assessment.raw_score == Decimal("0.500")
    assert assessment.overall_score == Decimal("0.500")
    assert assessment.reliability_score == Decimal("0.000")
    assert assessment.data_quality == "LOW"
    assert assessment.warnings


def test_assessment_is_bounded_and_explainable() -> None:
    analyzer = MlbPitchCommandAnalyzer()

    assessment = analyzer.analyze(make_sample())

    assert assessment.pitcher_id == "pitcher-001"
    assert assessment.pitcher_name == "Test Starter"
    assert Decimal("0") <= assessment.control_score <= Decimal("1")
    assert Decimal("0") <= assessment.command_score <= Decimal("1")
    assert Decimal("0") <= assessment.raw_score <= Decimal("1")
    assert Decimal("0") <= assessment.overall_score <= Decimal("1")
    assert Decimal("0") <= assessment.reliability_score <= Decimal("1")
    assert assessment.data_quality in {"LOW", "MEDIUM", "HIGH"}
    assert assessment.evidence
