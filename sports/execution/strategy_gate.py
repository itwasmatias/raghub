from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sports.execution.models import StrategyDecision, StrategyEligibilityPolicy


@dataclass(frozen=True, slots=True)
class StrategySignal:
    market_type: str
    sportsbook_count: int
    quote_age_minutes: int
    data_quality_score: Decimal
    model_version: str
    reconciled_probability: Decimal
    break_even_probability: Decimal
    expected_value_after_vig: Decimal
    uncertainty_width: Decimal
    reconciled_edge: Decimal
    market_open: bool
    unresolved_injury_or_lineup_blocker: bool


def _rounded_rank(edge: Decimal, quality: Decimal, coverage_bonus: Decimal) -> Decimal:
    score = edge * Decimal("100") * quality + coverage_bonus
    return score.quantize(Decimal("0.0001"))


class StrategyEligibilityEngine:
    def evaluate(
        self,
        policy: StrategyEligibilityPolicy,
        signal: StrategySignal,
    ) -> StrategyDecision:
        matched_rules: list[str] = []
        failed_rules: list[str] = []
        warnings: list[str] = []

        if signal.sportsbook_count >= policy.minimum_book_count:
            matched_rules.append("minimum sportsbook count")
        else:
            failed_rules.append("minimum sportsbook count")

        if signal.quote_age_minutes <= policy.maximum_quote_age_minutes:
            matched_rules.append("maximum quote age")
        else:
            failed_rules.append("maximum quote age")

        if signal.data_quality_score >= policy.minimum_data_quality:
            matched_rules.append("minimum data quality")
        else:
            failed_rules.append("minimum data quality")

        if (
            not policy.approved_market_types
            or signal.market_type in policy.approved_market_types
        ):
            matched_rules.append("approved market type")
        else:
            failed_rules.append("approved market type")

        if (
            not policy.approved_model_versions
            or signal.model_version in policy.approved_model_versions
        ):
            matched_rules.append("approved model version")
        else:
            failed_rules.append("approved model version")

        if signal.reconciled_edge >= policy.minimum_reconciled_edge:
            matched_rules.append("minimum reconciled edge")
        else:
            failed_rules.append("minimum reconciled edge")

        if signal.expected_value_after_vig > policy.minimum_expected_value_after_vig:
            matched_rules.append("positive expected value after vig")
        else:
            failed_rules.append("positive expected value after vig")

        if signal.uncertainty_width <= policy.maximum_uncertainty_width:
            matched_rules.append("acceptable uncertainty")
        else:
            failed_rules.append("acceptable uncertainty")

        if not policy.require_market_open or signal.market_open:
            matched_rules.append("market open")
        else:
            failed_rules.append("market open")

        if (
            not policy.require_no_unresolved_injury_or_lineup_blocker
            or not signal.unresolved_injury_or_lineup_blocker
        ):
            matched_rules.append("no unresolved injury or lineup blocker")
        else:
            failed_rules.append("no unresolved injury or lineup blocker")

        if signal.data_quality_score < Decimal("0.70"):
            warnings.append("reduced ranking due to weak data quality")

        eligible = not failed_rules
        ranking_score = _rounded_rank(
            signal.reconciled_edge,
            signal.data_quality_score,
            Decimal(signal.sportsbook_count) / Decimal("10"),
        )
        explanation = "eligible" if eligible else "blocked: " + ", ".join(failed_rules)
        return StrategyDecision(
            eligible=eligible,
            strategy_id=policy.strategy_id,
            strategy_version=policy.strategy_version,
            matched_rules=tuple(matched_rules),
            failed_rules=tuple(failed_rules),
            warnings=tuple(warnings),
            ranking_score=ranking_score,
            explanation=explanation,
        )
