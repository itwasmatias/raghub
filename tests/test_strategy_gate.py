from decimal import Decimal

from sports.execution.models import StrategyEligibilityPolicy
from sports.execution.strategy_gate import StrategyEligibilityEngine, StrategySignal


def test_strategy_gate_blocks_insufficient_coverage_and_negative_ev():
    policy = StrategyEligibilityPolicy(
        strategy_id="value-v1",
        strategy_version="1",
        minimum_book_count=3,
        approved_market_types=("moneyline",),
        approved_model_versions=("model-v1",),
    )
    signal = StrategySignal(
        market_type="moneyline",
        sportsbook_count=2,
        quote_age_minutes=5,
        data_quality_score=Decimal("0.88"),
        model_version="model-v1",
        reconciled_probability=Decimal("0.54"),
        break_even_probability=Decimal("0.51"),
        expected_value_after_vig=Decimal("-0.01"),
        uncertainty_width=Decimal("0.08"),
        reconciled_edge=Decimal("0.03"),
        market_open=True,
        unresolved_injury_or_lineup_blocker=False,
    )

    decision = StrategyEligibilityEngine().evaluate(policy, signal)

    assert not decision.eligible
    assert "minimum sportsbook count" in decision.failed_rules
    assert "positive expected value after vig" in decision.failed_rules


def test_strategy_gate_approves_supported_market_and_model():
    policy = StrategyEligibilityPolicy(
        strategy_id="value-v1",
        strategy_version="1",
        minimum_book_count=2,
        approved_market_types=("moneyline",),
        approved_model_versions=("model-v1",),
    )
    signal = StrategySignal(
        market_type="moneyline",
        sportsbook_count=3,
        quote_age_minutes=5,
        data_quality_score=Decimal("0.88"),
        model_version="model-v1",
        reconciled_probability=Decimal("0.58"),
        break_even_probability=Decimal("0.50"),
        expected_value_after_vig=Decimal("0.03"),
        uncertainty_width=Decimal("0.05"),
        reconciled_edge=Decimal("0.08"),
        market_open=True,
        unresolved_injury_or_lineup_blocker=False,
    )

    decision = StrategyEligibilityEngine().evaluate(policy, signal)

    assert decision.eligible
    assert "approved market type" in decision.matched_rules
    assert decision.ranking_score > Decimal("0")
