from __future__ import annotations

from decimal import Decimal

import pytest

from sports.founder.mlb.analyzers.starting_pitcher import (
    MlbStartingPitcherAnalyzer,
    MlbStartingPitcherSample,
)


def make_sample(
    **overrides: object,
) -> MlbStartingPitcherSample:
    values: dict[str, object] = {
        "pitcher_id": "pitcher-001",
        "pitcher_name": "Test Starter",
        "throws": "R",
        "starts_in_sample": 5,
        "innings_in_sample": Decimal("31.0"),
        "recent_era": Decimal("2.90"),
        "recent_fip": Decimal("3.10"),
        "recent_whip": Decimal("1.08"),
        "strikeout_rate": Decimal("0.29"),
        "walk_rate": Decimal("0.07"),
        "ground_ball_rate": Decimal("0.46"),
        "hard_hit_rate": Decimal("0.33"),
        "average_fastball_velocity": Decimal("95.2"),
        "fastball_velocity_change": Decimal("0.4"),
        "average_pitch_count": Decimal("94"),
        "pitch_count_change": Decimal("2"),
        "days_rest": 5,
        "opponent_woba_vs_hand": Decimal("0.305"),
        "opponent_strikeout_rate_vs_hand": Decimal("0.25"),
        "confirmed_starter": True,
    }
    values.update(overrides)
    return MlbStartingPitcherSample(**values)


def test_sample_is_immutable() -> None:
    sample = make_sample()

    with pytest.raises(AttributeError):
        sample.pitcher_name = "Changed"  # type: ignore[misc]


def test_sample_rejects_invalid_throwing_hand() -> None:
    with pytest.raises(ValueError, match="throws"):
        make_sample(throws="X")


def test_sample_rejects_rate_above_one() -> None:
    with pytest.raises(ValueError, match="strikeout_rate"):
        make_sample(strikeout_rate=Decimal("1.01"))


def test_analyzer_rewards_strikeout_minus_walk_strength() -> None:
    analyzer = MlbStartingPitcherAnalyzer()

    strong = analyzer.analyze(
        make_sample(
            strikeout_rate=Decimal("0.31"),
            walk_rate=Decimal("0.05"),
        )
    )
    weak = analyzer.analyze(
        make_sample(
            strikeout_rate=Decimal("0.18"),
            walk_rate=Decimal("0.11"),
        )
    )

    assert strong.command_score > weak.command_score
    assert strong.overall_score > weak.overall_score


def test_analyzer_penalizes_velocity_decline() -> None:
    analyzer = MlbStartingPitcherAnalyzer()

    stable = analyzer.analyze(
        make_sample(
            fastball_velocity_change=Decimal("0.2"),
        )
    )
    declining = analyzer.analyze(
        make_sample(
            fastball_velocity_change=Decimal("-2.1"),
        )
    )

    assert stable.workload_score > declining.workload_score
    assert any(
        "velocity" in warning.lower()
        for warning in declining.warnings
    )


def test_analyzer_penalizes_short_rest() -> None:
    analyzer = MlbStartingPitcherAnalyzer()

    normal_rest = analyzer.analyze(
        make_sample(days_rest=5)
    )
    short_rest = analyzer.analyze(
        make_sample(days_rest=3)
    )

    assert normal_rest.workload_score > short_rest.workload_score
    assert any(
        "rest" in warning.lower()
        for warning in short_rest.warnings
    )


def test_analyzer_reduces_reliability_for_small_sample() -> None:
    analyzer = MlbStartingPitcherAnalyzer()

    established = analyzer.analyze(
        make_sample(
            starts_in_sample=6,
            innings_in_sample=Decimal("36"),
        )
    )
    limited = analyzer.analyze(
        make_sample(
            starts_in_sample=2,
            innings_in_sample=Decimal("8"),
        )
    )

    assert established.reliability_score > limited.reliability_score
    assert limited.data_quality == "LOW"
    assert any(
        "sample" in warning.lower()
        for warning in limited.warnings
    )


def test_unconfirmed_starter_cannot_receive_high_reliability() -> None:
    analyzer = MlbStartingPitcherAnalyzer()

    assessment = analyzer.analyze(
        make_sample(confirmed_starter=False)
    )

    assert assessment.reliability_score <= Decimal("0.45")
    assert assessment.data_quality != "HIGH"
    assert any(
        "not confirmed" in warning.lower()
        for warning in assessment.warnings
    )


def test_favorable_opponent_matchup_increases_matchup_score() -> None:
    analyzer = MlbStartingPitcherAnalyzer()

    favorable = analyzer.analyze(
        make_sample(
            opponent_woba_vs_hand=Decimal("0.292"),
            opponent_strikeout_rate_vs_hand=Decimal("0.28"),
        )
    )
    difficult = analyzer.analyze(
        make_sample(
            opponent_woba_vs_hand=Decimal("0.350"),
            opponent_strikeout_rate_vs_hand=Decimal("0.18"),
        )
    )

    assert favorable.matchup_score > difficult.matchup_score


def test_assessment_contains_explainable_evidence() -> None:
    analyzer = MlbStartingPitcherAnalyzer()

    assessment = analyzer.analyze(make_sample())

    assert assessment.pitcher_id == "pitcher-001"
    assert assessment.pitcher_name == "Test Starter"
    assert Decimal("0") <= assessment.overall_score <= Decimal("1")
    assert Decimal("0") <= assessment.performance_score <= Decimal("1")
    assert Decimal("0") <= assessment.command_score <= Decimal("1")
    assert Decimal("0") <= assessment.matchup_score <= Decimal("1")
    assert Decimal("0") <= assessment.workload_score <= Decimal("1")
    assert Decimal("0") <= assessment.reliability_score <= Decimal("1")
    assert assessment.evidence
    assert assessment.data_quality in {"LOW", "MEDIUM", "HIGH"}


def test_overall_score_is_reliability_adjusted() -> None:
    analyzer = MlbStartingPitcherAnalyzer()

    high_reliability = analyzer.analyze(
        make_sample(
            starts_in_sample=7,
            innings_in_sample=Decimal("42"),
            confirmed_starter=True,
        )
    )
    low_reliability = analyzer.analyze(
        make_sample(
            starts_in_sample=2,
            innings_in_sample=Decimal("8"),
            confirmed_starter=False,
        )
    )

    assert high_reliability.raw_score > Decimal("0")
    assert low_reliability.raw_score > Decimal("0")
    assert high_reliability.overall_score > low_reliability.overall_score
