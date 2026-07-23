from sports.features.player_features import (
    calculate_ewma,
    calculate_variance
)


def test_ewma_matches_known_baseline():

    values = [10, 12, 14, 16]

    result = calculate_ewma(values, alpha=0.5)

    assert result == 14.25


def test_variance_matches_known_baseline():

    values = [10, 12, 14, 16]

    result = calculate_variance(values)

    assert result == 5