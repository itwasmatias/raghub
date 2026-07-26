from decimal import Decimal

import pytest

from services.wager_calculator import PracticeWagerCalculator


def test_positive_american_odds_payout():
    result = PracticeWagerCalculator.calculate(
        american_odds=150,
        wager_amount_usd="20",
    )
    assert result["potential_profit_usd"] == Decimal("30.00")


def test_negative_american_odds_payout():
    result = PracticeWagerCalculator.calculate(
        american_odds=-200,
        wager_amount_usd="20",
    )
    assert result["potential_profit_usd"] == Decimal("10.00")


def test_total_return_includes_original_wager():
    result = PracticeWagerCalculator.calculate(
        american_odds=120,
        wager_amount_usd="50",
    )
    assert result["total_return_usd"] == Decimal("110.00")


def test_break_even_probability_positive_and_negative():
    positive = PracticeWagerCalculator.calculate(
        american_odds=150, wager_amount_usd="10"
    )
    negative = PracticeWagerCalculator.calculate(
        american_odds=-200, wager_amount_usd="10"
    )

    assert positive["break_even_probability"] == Decimal("0.4000")
    assert negative["break_even_probability"] == Decimal("0.6667")


def test_expected_profit_and_return_percentage_with_model_probability():
    result = PracticeWagerCalculator.calculate(
        american_odds=110,
        wager_amount_usd="100",
        model_probability="0.58",
    )

    assert result["expected_profit_usd"] == Decimal("21.80")
    assert result["expected_return_percentage"] == Decimal("21.80")


def test_zero_odds_rejected():
    with pytest.raises(ValueError, match="cannot be zero"):
        PracticeWagerCalculator.calculate(american_odds=0, wager_amount_usd="25")


def test_negative_wager_rejected():
    with pytest.raises(ValueError, match="greater than zero"):
        PracticeWagerCalculator.calculate(american_odds=120, wager_amount_usd="-5")


def test_missing_model_probability_is_allowed():
    result = PracticeWagerCalculator.calculate(american_odds=120, wager_amount_usd="25")
    assert result["expected_profit_usd"] is None
    assert result["expected_return_percentage"] is None


def test_probabilities_outside_zero_to_one_rejected():
    with pytest.raises(ValueError, match="between 0 and 1"):
        PracticeWagerCalculator.calculate(
            american_odds=120,
            wager_amount_usd="25",
            model_probability="1.2",
        )


def test_decimal_precision_and_rounding():
    result = PracticeWagerCalculator.calculate(
        american_odds=-135,
        wager_amount_usd=Decimal("17.23"),
        model_probability=Decimal("0.5123"),
    )
    assert result["potential_profit_usd"] == Decimal("12.76")
    assert result["total_return_usd"] == Decimal("29.99")
    assert result["break_even_probability"] == Decimal("0.5745")


def test_market_probability_optional_validation():
    with pytest.raises(ValueError, match="between 0 and 1"):
        PracticeWagerCalculator.calculate(
            american_odds=120,
            wager_amount_usd="25",
            market_implied_probability="-0.1",
        )
