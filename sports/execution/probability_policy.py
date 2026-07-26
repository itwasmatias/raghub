from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from sports.execution.contracts import PROBABILITY_PRECISION


@dataclass(frozen=True, slots=True)
class ProbabilityReconciliationPolicyV1:
    policy_version: str
    reconciliation_version: str
    reconciliation_method_version: str
    confidence_interval_method_version: str
    base_sip_weight: Decimal = Decimal("0.600000")
    sip_weight_floor: Decimal = Decimal("0.050000")
    sip_weight_ceiling: Decimal = Decimal("0.750000")
    anchor_consensus_weight: Decimal = Decimal("0.700000")
    anchor_prior_weight: Decimal = Decimal("0.300000")
    hard_minimum_data_quality: Decimal = Decimal("0.350000")
    maximum_quote_age_seconds: int = 1800
    dispersion_scale: Decimal = Decimal("0.120000")
    supported_leagues: tuple[str, ...] = ("wnba", "nba", "nfl", "mlb", "nhl")
    supported_market_types: tuple[str, ...] = ("moneyline", "spread", "total")
    supported_periods: tuple[str, ...] = ("full_game", "first_half", "first_quarter")
    supported_outcome_schemas: tuple[str, ...] = ("home_away", "over_under")
    active_model_versions: tuple[str, ...] = ("model-v1",)
    active_calibration_versions: tuple[str, ...] = ("cal-v1",)

    def quantize_probability(self, value: Decimal) -> Decimal:
        return value.quantize(PROBABILITY_PRECISION, rounding=ROUND_HALF_UP)
