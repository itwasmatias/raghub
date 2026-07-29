from __future__ import annotations

from decimal import Decimal

import pytest

from sports.founder.mlb.analyzers.pitch_power import (
    MlbPitchPowerAnalyzer,
    MlbPitchPowerSample,
)


def make_sample(
    **overrides: object,
) -> MlbPitchPowerSample:
    values: dict[str, object] = {
        "pitcher_id": "pitcher-001",
        "pitcher_name": "Test Starter",
        "average_fastball_velocity": Decimal("95.2"),
        "maximum_fastball_velocity": Decimal("98.4"),
        "strikeout_rate": Decimal("0.280"),
        "swinging_strike_rate": Decimal("0.135"),
        "hard_hit_rate_allowed": Decimal("0.340"),
        "barrel_rate_allowed": Decimal("0.065"),
        "pitches_observed": 240,
        "games_observed": 6,
    }
    values.update(overrides)
    return MlbPitchPowerSample(**values)


def test_sample_is_immutable() -> None:
    sample = make_sample()

    with pytest.raises(AttributeError):
        sample.pitcher_name = "Changed"  # type: ignore[misc]


def test_sample_rejects_negative_velocity() -> None:
    with pytest.raises(
        ValueError,
        match="average_fastball_velocity",
    ):
        make_sample(
            average_fastball_velocity=Decimal("-1"),
        )


def test_sample_rejects_probability_above_one() -> None:
    with pytest.raises(ValueError, match="strikeout_rate"):
        make_sample(
            strikeout_rate=Decimal("1.01"),
        )


def test_sample_rejects_negative_observation_counts() -> None:
    with pytest.raises(ValueError, match="pitches_observed"):
        make_sample(pitches_observed=-1)


def test_analyzer_rewards_premium_fastball_velocity() -> None:
    analyzer = MlbPitchPowerAnalyzer()

    premium = analyzer.analyze(
        make_sample(
            average_fastball_velocity=Decimal("97.0"),
            maximum_fastball_velocity=Decimal("100.2"),
        )
    )
    modest = analyzer.analyze(
        make_sample(
            average_fastball_velocity=Decimal("91.0"),
            maximum_fastball_velocity=Decimal("94.0"),
        )
    )

    assert premium.velocity_power_score > modest.velocity_power_score
    assert any("velocity" in item.lower() for item in premium.evidence)


def test_analyzer_rewards_bat_missing_ability() -> None:
    analyzer = MlbPitchPowerAnalyzer()

    dominant = analyzer.analyze(
        make_sample(
            strikeout_rate=Decimal("0.340"),
            swinging_strike_rate=Decimal("0.160"),
        )
    )
    limited = analyzer.analyze(
        make_sample(
            strikeout_rate=Decimal("0.160"),
            swinging_strike_rate=Decimal("0.070"),
        )
    )

    assert dominant.bat_miss_score > limited.bat_miss_score


def test_analyzer_penalizes_damaging_contact() -> None:
    analyzer = MlbPitchPowerAnalyzer()

    suppressing = analyzer.analyze(
        make_sample(
            hard_hit_rate_allowed=Decimal("0.280"),
            barrel_rate_allowed=Decimal("0.040"),
        )
    )
    vulnerable = analyzer.analyze(
        make_sample(
            hard_hit_rate_allowed=Decimal("0.460"),
            barrel_rate_allowed=Decimal("0.120"),
        )
    )

    assert suppressing.contact_suppression_score > vulnerable.contact_suppression_score
    assert any(
        "contact" in warning.lower() or "barrel" in warning.lower()
        for warning in vulnerable.warnings
    )


def test_small_sample_reduces_reliability_and_extremity() -> None:
    analyzer = MlbPitchPowerAnalyzer()

    established = analyzer.analyze(
        make_sample(
            average_fastball_velocity=Decimal("98.0"),
            maximum_fastball_velocity=Decimal("101.0"),
            strikeout_rate=Decimal("0.360"),
            swinging_strike_rate=Decimal("0.170"),
            pitches_observed=320,
            games_observed=8,
        )
    )
    limited = analyzer.analyze(
        make_sample(
            average_fastball_velocity=Decimal("98.0"),
            maximum_fastball_velocity=Decimal("101.0"),
            strikeout_rate=Decimal("0.360"),
            swinging_strike_rate=Decimal("0.170"),
            pitches_observed=12,
            games_observed=1,
        )
    )

    assert established.reliability_score > limited.reliability_score
    assert abs(limited.overall_score - Decimal("0.500")) < abs(
        established.overall_score - Decimal("0.500")
    )
    assert any("sample" in warning.lower() for warning in limited.warnings)


def test_missing_data_returns_neutral_low_reliability_result() -> None:
    analyzer = MlbPitchPowerAnalyzer()

    assessment = analyzer.analyze(
        make_sample(
            average_fastball_velocity=None,
            maximum_fastball_velocity=None,
            strikeout_rate=None,
            swinging_strike_rate=None,
            hard_hit_rate_allowed=None,
            barrel_rate_allowed=None,
            pitches_observed=0,
            games_observed=0,
        )
    )

    assert assessment.velocity_power_score == Decimal("0.500")
    assert assessment.bat_miss_score == Decimal("0.500")
    assert assessment.contact_suppression_score == Decimal("0.500")
    assert assessment.raw_score == Decimal("0.500")
    assert assessment.overall_score == Decimal("0.500")
    assert assessment.reliability_score == Decimal("0.000")
    assert assessment.data_quality == "LOW"
    assert assessment.warnings


def test_assessment_is_bounded_and_explainable() -> None:
    analyzer = MlbPitchPowerAnalyzer()

    assessment = analyzer.analyze(make_sample())

    assert assessment.pitcher_id == "pitcher-001"
    assert assessment.pitcher_name == "Test Starter"
    assert Decimal("0") <= assessment.velocity_power_score <= Decimal("1")
    assert Decimal("0") <= assessment.bat_miss_score <= Decimal("1")
    assert Decimal("0") <= assessment.contact_suppression_score <= Decimal("1")
    assert Decimal("0") <= assessment.raw_score <= Decimal("1")
    assert Decimal("0") <= assessment.overall_score <= Decimal("1")
    assert Decimal("0") <= assessment.reliability_score <= Decimal("1")
    assert assessment.data_quality in {"LOW", "MEDIUM", "HIGH"}
    assert assessment.evidence
