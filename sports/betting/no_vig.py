from __future__ import annotations

from collections.abc import Sequence
from math import sqrt

from sports.betting.models import NoVigMethod


def remove_vig(
    implied_probabilities: Sequence[float],
    method: NoVigMethod | str = NoVigMethod.PROPORTIONAL,
) -> tuple[float, ...]:
    """Convert a complete book market into probabilities summing to one."""
    values = tuple(float(value) for value in implied_probabilities)
    if len(values) < 2 or any(value <= 0.0 or value >= 1.0 for value in values):
        raise ValueError("a complete market needs at least two probabilities in (0, 1)")
    if sum(values) < 1.0:
        raise ValueError("market probabilities cannot sum to less than one")

    selected = NoVigMethod(method)
    if selected is NoVigMethod.PROPORTIONAL:
        total = sum(values)
        return tuple(value / total for value in values)
    if selected is NoVigMethod.POWER:
        return _power(values)
    return _shin(values)


def _power(values: tuple[float, ...]) -> tuple[float, ...]:
    low, high = 1.0, 100.0
    for _ in range(100):
        exponent = (low + high) / 2.0
        if sum(value**exponent for value in values) > 1.0:
            low = exponent
        else:
            high = exponent
    result = tuple(value ** ((low + high) / 2.0) for value in values)
    total = sum(result)
    return tuple(value / total for value in result)


def _shin(values: tuple[float, ...]) -> tuple[float, ...]:
    # Shin's insider-trading model; solve z numerically and normalize tiny
    # floating-point residue. At zero overround it reduces to the raw market.
    total = sum(values)

    def probabilities(z: float) -> tuple[float, ...]:
        if z >= 1.0:
            return tuple(1.0 / len(values) for _ in values)
        return tuple(
            (sqrt(z * z + 4.0 * (1.0 - z) * value * value / total) - z)
            / (2.0 * (1.0 - z))
            for value in values
        )

    low, high = 0.0, 1.0 - 1e-12
    for _ in range(100):
        z = (low + high) / 2.0
        if sum(probabilities(z)) > 1.0:
            low = z
        else:
            high = z
    result = probabilities((low + high) / 2.0)
    result_total = sum(result)
    return tuple(value / result_total for value in result)
