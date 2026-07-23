from sports.features.player_features import (
    calculate_ewma,
    calculate_variance
)


def test_empty_values_return_zero():

    assert calculate_ewma([]) == 0.0

    assert calculate_variance([]) == 0.0


def test_single_value_has_zero_variance():

    values = [20]

    assert calculate_ewma(values) == 20

    assert calculate_variance(values) == 0.0


def test_constant_values_have_zero_variance():

    values = [10, 10, 10, 10]

    assert calculate_variance(values) == 0.0