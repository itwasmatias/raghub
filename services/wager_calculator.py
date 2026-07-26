from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


MONEY_QUANTUM = Decimal("0.01")
PROBABILITY_QUANTUM = Decimal("0.0001")


class PracticeWagerCalculator:
    """Educational wager math helper. Does not place or persist bets."""

    @classmethod
    def calculate(
        cls,
        *,
        american_odds: Any,
        wager_amount_usd: Any,
        model_probability: Any | None = None,
        market_implied_probability: Any | None = None,
    ) -> dict[str, Decimal | None]:
        odds = cls._parse_odds(american_odds)
        wager = cls._parse_positive_decimal(wager_amount_usd, "wager_amount_usd")
        model_prob = cls._parse_probability(model_probability, "model_probability")
        market_prob = cls._parse_probability(
            market_implied_probability,
            "market_implied_probability",
        )

        if odds > 0:
            potential_profit = wager * odds / Decimal("100")
            break_even = Decimal("100") / (odds + Decimal("100"))
        else:
            potential_profit = wager * Decimal("100") / abs(odds)
            break_even = abs(odds) / (abs(odds) + Decimal("100"))

        total_return = wager + potential_profit

        expected_profit = None
        expected_return_percentage = None
        if model_prob is not None:
            expected_profit = (model_prob * potential_profit) - (
                (Decimal("1") - model_prob) * wager
            )
            expected_return_percentage = (
                expected_profit / wager * Decimal("100") if wager > 0 else None
            )

        return {
            "potential_profit_usd": cls._money(potential_profit),
            "total_return_usd": cls._money(total_return),
            "break_even_probability": cls._probability(break_even),
            "model_probability": cls._probability(model_prob)
            if model_prob is not None
            else None,
            "market_implied_probability": cls._probability(market_prob)
            if market_prob is not None
            else None,
            "expected_profit_usd": cls._money(expected_profit)
            if expected_profit is not None
            else None,
            "expected_return_percentage": cls._money(expected_return_percentage)
            if expected_return_percentage is not None
            else None,
            "win_outcome_usd": cls._money(total_return),
            "loss_outcome_usd": cls._money(-wager),
            "plain_language": cls._plain_language(
                break_even=break_even,
                model_probability=model_prob,
                expected_profit=expected_profit,
            ),
        }

    @staticmethod
    def _parse_odds(value: Any) -> Decimal:
        try:
            odds = Decimal(str(value).strip())
        except (InvalidOperation, ValueError) as error:
            raise ValueError("american_odds must be a valid number") from error
        if odds == 0:
            raise ValueError("american_odds cannot be zero")
        if odds % 1 != 0:
            raise ValueError("american_odds must be a whole number")
        return odds

    @staticmethod
    def _parse_positive_decimal(value: Any, field: str) -> Decimal:
        try:
            amount = Decimal(str(value).strip())
        except (InvalidOperation, ValueError) as error:
            raise ValueError(f"{field} must be a valid decimal number") from error
        if amount <= 0:
            raise ValueError(f"{field} must be greater than zero")
        return amount

    @staticmethod
    def _parse_probability(value: Any | None, field: str) -> Decimal | None:
        if value is None or str(value).strip() == "":
            return None
        try:
            probability = Decimal(str(value).strip())
        except (InvalidOperation, ValueError) as error:
            raise ValueError(f"{field} must be a valid decimal number") from error
        if probability < 0 or probability > 1:
            raise ValueError(f"{field} must be between 0 and 1")
        return probability

    @staticmethod
    def _money(value: Decimal) -> Decimal:
        return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)

    @staticmethod
    def _probability(value: Decimal) -> Decimal:
        return value.quantize(PROBABILITY_QUANTUM, rounding=ROUND_HALF_UP)

    @staticmethod
    def _plain_language(
        *,
        break_even: Decimal,
        model_probability: Decimal | None,
        expected_profit: Decimal | None,
    ) -> str:
        break_even_pct = (break_even * Decimal("100")).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
        if model_probability is None or expected_profit is None:
            return (
                "This is a practice estimate only. "
                f"The break-even win rate is {break_even_pct}%. "
                "Add a model probability to estimate expected profit."
            )

        model_pct = (model_probability * Decimal("100")).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
        direction = "positive" if expected_profit >= 0 else "negative"
        expected_money = abs(expected_profit).quantize(
            MONEY_QUANTUM,
            rounding=ROUND_HALF_UP,
        )
        return (
            "This is a practice estimate only and does not place a wager. "
            f"Break-even is {break_even_pct}%, while the model input is {model_pct}%. "
            f"Expected profit is {direction} at ${expected_money}."
        )
