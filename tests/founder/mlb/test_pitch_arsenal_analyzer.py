from __future__ import annotations

from decimal import Decimal

import pytest

from sports.founder.mlb.analyzers.pitch_arsenal import (
    MlbPitchArsenalAnalyzer,
    MlbPitchArsenalSample,
    MlbPitchProfile,
)


def make_pitch(
    **overrides: object,
) -> MlbPitchProfile:
    values: dict[str, object] = {
        "pitch_type": "FOUR_SEAM",
        "usage_rate": Decimal("0.55"),
        "previous_usage_rate": Decimal("0.53"),
        "average_velocity": Decimal("95.4"),
        "maximum_velocity": Decimal("98.0"),
        "spin_rate": Decimal("2400"),
        "horizontal_movement": Decimal("8.0"),
        "induced_vertical_break": Decimal("17.5"),
        "whiff_rate": Decimal("0.29"),
        "chase_rate": Decimal("0.31"),
        "csw_rate": Decimal("0.32"),
        "ground_ball_rate": Decimal("0.39"),
        "hard_hit_rate": Decimal("0.34"),
        "barrel_rate": Decimal("0.06"),
        "batting_average_allowed": Decimal("0.225"),
        "slugging_allowed": Decimal("0.375"),
        "xwoba": Decimal("0.290"),
        "run_value": Decimal("-5.0"),
        "pitches_observed": 220,
    }
    values.update(overrides)
    return MlbPitchProfile(**values)


def make_sample(
    **overrides: object,
) -> MlbPitchArsenalSample:
    values: dict[str, object] = {
        "pitcher_id": "pitcher-001",
        "pitcher_name": "Test Starter",
        "pitches": (
            make_pitch(),
            make_pitch(
                pitch_type="SLIDER",
                usage_rate=Decimal("0.30"),
                previous_usage_rate=Decimal("0.31"),
                average_velocity=Decimal("86.8"),
                maximum_velocity=Decimal("89.5"),
                spin_rate=Decimal("2550"),
                horizontal_movement=Decimal("9.5"),
                induced_vertical_break=Decimal("-1.0"),
                whiff_rate=Decimal("0.38"),
                chase_rate=Decimal("0.36"),
                csw_rate=Decimal("0.35"),
                ground_ball_rate=Decimal("0.43"),
                hard_hit_rate=Decimal("0.29"),
                barrel_rate=Decimal("0.04"),
                batting_average_allowed=Decimal("0.190"),
                slugging_allowed=Decimal("0.310"),
                xwoba=Decimal("0.255"),
                run_value=Decimal("-7.0"),
                pitches_observed=130,
            ),
            make_pitch(
                pitch_type="CHANGEUP",
                usage_rate=Decimal("0.15"),
                previous_usage_rate=Decimal("0.16"),
                average_velocity=Decimal("87.0"),
                maximum_velocity=Decimal("89.0"),
                spin_rate=Decimal("1750"),
                horizontal_movement=Decimal("14.0"),
                induced_vertical_break=Decimal("8.0"),
                whiff_rate=Decimal("0.32"),
                chase_rate=Decimal("0.33"),
                csw_rate=Decimal("0.30"),
                ground_ball_rate=Decimal("0.48"),
                hard_hit_rate=Decimal("0.31"),
                barrel_rate=Decimal("0.05"),
                batting_average_allowed=Decimal("0.215"),
                slugging_allowed=Decimal("0.350"),
                xwoba=Decimal("0.275"),
                run_value=Decimal("-3.0"),
                pitches_observed=70,
            ),
        ),
        "total_pitches_observed": 420,
        "games_observed": 7,
    }
    values.update(overrides)
    return MlbPitchArsenalSample(**values)


def test_pitch_profile_is_immutable() -> None:
    pitch = make_pitch()

    with pytest.raises(AttributeError):
        pitch.pitch_type = "SLIDER"  # type: ignore[misc]


def test_pitch_type_aliases_are_normalized() -> None:
    four_seam = make_pitch(pitch_type="FF")
    two_seam = make_pitch(pitch_type="2-SEAM")
    curveball = make_pitch(pitch_type="CURVE")
    mystery = make_pitch(pitch_type="MYSTERY_PITCH")

    assert four_seam.pitch_type == "FOUR_SEAM"
    assert two_seam.pitch_type == "TWO_SEAM"
    assert curveball.pitch_type == "CURVEBALL"
    assert mystery.pitch_type == "UNKNOWN"


def test_pitch_profile_rejects_rate_above_one() -> None:
    with pytest.raises(ValueError, match="whiff_rate"):
        make_pitch(whiff_rate=Decimal("1.01"))


def test_deeper_usable_arsenal_scores_above_one_pitch_arsenal() -> None:
    analyzer = MlbPitchArsenalAnalyzer()

    deep = analyzer.analyze(make_sample())
    shallow = analyzer.analyze(
        make_sample(
            pitches=(
                make_pitch(
                    usage_rate=Decimal("1.0"),
                    previous_usage_rate=Decimal("1.0"),
                    pitches_observed=420,
                ),
            )
        )
    )

    assert deep.effective_pitch_count > shallow.effective_pitch_count
    assert deep.arsenal_depth_score > shallow.arsenal_depth_score


def test_pitch_quality_is_weighted_by_actual_usage() -> None:
    analyzer = MlbPitchArsenalAnalyzer()

    elite_pitch = make_pitch(
        pitch_type="SLIDER",
        usage_rate=Decimal("0.70"),
        previous_usage_rate=Decimal("0.70"),
        whiff_rate=Decimal("0.45"),
        chase_rate=Decimal("0.40"),
        csw_rate=Decimal("0.38"),
        hard_hit_rate=Decimal("0.25"),
        barrel_rate=Decimal("0.03"),
        xwoba=Decimal("0.235"),
        run_value=Decimal("-10"),
    )
    weak_pitch = make_pitch(
        pitch_type="FOUR_SEAM",
        usage_rate=Decimal("0.30"),
        previous_usage_rate=Decimal("0.30"),
        whiff_rate=Decimal("0.15"),
        chase_rate=Decimal("0.20"),
        csw_rate=Decimal("0.22"),
        hard_hit_rate=Decimal("0.48"),
        barrel_rate=Decimal("0.12"),
        xwoba=Decimal("0.390"),
        run_value=Decimal("8"),
    )

    elite_heavy = analyzer.analyze(
        make_sample(pitches=(elite_pitch, weak_pitch))
    )
    weak_heavy = analyzer.analyze(
        make_sample(
            pitches=(
                make_pitch(
                    pitch_type="SLIDER",
                    usage_rate=Decimal("0.30"),
                    previous_usage_rate=Decimal("0.30"),
                    whiff_rate=Decimal("0.45"),
                    chase_rate=Decimal("0.40"),
                    csw_rate=Decimal("0.38"),
                    hard_hit_rate=Decimal("0.25"),
                    barrel_rate=Decimal("0.03"),
                    xwoba=Decimal("0.235"),
                    run_value=Decimal("-10"),
                ),
                make_pitch(
                    pitch_type="FOUR_SEAM",
                    usage_rate=Decimal("0.70"),
                    previous_usage_rate=Decimal("0.70"),
                    whiff_rate=Decimal("0.15"),
                    chase_rate=Decimal("0.20"),
                    csw_rate=Decimal("0.22"),
                    hard_hit_rate=Decimal("0.48"),
                    barrel_rate=Decimal("0.12"),
                    xwoba=Decimal("0.390"),
                    run_value=Decimal("8"),
                ),
            )
        )
    )

    assert elite_heavy.pitch_quality_score > weak_heavy.pitch_quality_score


def test_stable_pitch_mix_scores_above_large_usage_change() -> None:
    analyzer = MlbPitchArsenalAnalyzer()

    stable = analyzer.analyze(make_sample())
    volatile = analyzer.analyze(
        make_sample(
            pitches=(
                make_pitch(
                    usage_rate=Decimal("0.80"),
                    previous_usage_rate=Decimal("0.40"),
                ),
                make_pitch(
                    pitch_type="SLIDER",
                    usage_rate=Decimal("0.20"),
                    previous_usage_rate=Decimal("0.60"),
                ),
            )
        )
    )

    assert stable.pitch_mix_stability_score > (
        volatile.pitch_mix_stability_score
    )
    assert any(
        "mix" in warning.lower()
        for warning in volatile.warnings
    )


def test_small_sample_reduces_reliability_and_neutralizes_score() -> None:
    analyzer = MlbPitchArsenalAnalyzer()

    established = analyzer.analyze(make_sample())
    limited = analyzer.analyze(
        make_sample(
            pitches=(
                make_pitch(
                    usage_rate=Decimal("1.0"),
                    previous_usage_rate=Decimal("1.0"),
                    pitches_observed=10,
                ),
            ),
            total_pitches_observed=10,
            games_observed=1,
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


def test_missing_quality_data_returns_neutral_low_reliability_result() -> None:
    analyzer = MlbPitchArsenalAnalyzer()

    missing_pitch = make_pitch(
        usage_rate=Decimal("1.0"),
        previous_usage_rate=None,
        average_velocity=None,
        maximum_velocity=None,
        spin_rate=None,
        horizontal_movement=None,
        induced_vertical_break=None,
        whiff_rate=None,
        chase_rate=None,
        csw_rate=None,
        ground_ball_rate=None,
        hard_hit_rate=None,
        barrel_rate=None,
        batting_average_allowed=None,
        slugging_allowed=None,
        xwoba=None,
        run_value=None,
        pitches_observed=0,
    )

    assessment = analyzer.analyze(
        make_sample(
            pitches=(missing_pitch,),
            total_pitches_observed=0,
            games_observed=0,
        )
    )

    assert assessment.pitch_quality_score == Decimal("0.500")
    assert assessment.overall_score == Decimal("0.500")
    assert assessment.reliability_score == Decimal("0.000")
    assert assessment.data_quality == "LOW"
    assert assessment.warnings


def test_assessment_is_bounded_and_explainable() -> None:
    analyzer = MlbPitchArsenalAnalyzer()

    assessment = analyzer.analyze(make_sample())

    assert assessment.pitcher_id == "pitcher-001"
    assert assessment.pitcher_name == "Test Starter"
    assert assessment.normalized_pitch_types == (
        "FOUR_SEAM",
        "SLIDER",
        "CHANGEUP",
    )
    assert Decimal("0") <= assessment.arsenal_depth_score <= Decimal("1")
    assert Decimal("0") <= assessment.pitch_quality_score <= Decimal("1")
    assert (
        Decimal("0")
        <= assessment.pitch_mix_stability_score
        <= Decimal("1")
    )
    assert Decimal("0") <= assessment.raw_score <= Decimal("1")
    assert Decimal("0") <= assessment.overall_score <= Decimal("1")
    assert Decimal("0") <= assessment.reliability_score <= Decimal("1")
    assert assessment.data_quality in {"LOW", "MEDIUM", "HIGH"}
    assert assessment.evidence
