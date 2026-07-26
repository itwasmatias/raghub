from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol


class ReconciliationStatus(StrEnum):
    RECONCILED = "reconciled"
    UNAVAILABLE = "unavailable"


class OrderIntentState(StrEnum):
    THESIS_DRAFTED = "thesis_drafted"
    FORECAST_CREATED = "forecast_created"
    PROBABILITIES_RECONCILED = "probabilities_reconciled"
    STRATEGY_MATCHED = "strategy_matched"
    RISK_EVALUATED = "risk_evaluated"
    RISK_APPROVED = "risk_approved"
    ORDER_INTENT_CREATED = "order_intent_created"
    USER_CONFIRMED = "user_confirmed"
    EXECUTION_SUBMITTED = "execution_submitted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    POSITION_OPEN = "position_open"
    SETTLEMENT_PENDING = "settlement_pending"
    SETTLED = "settled"
    REVIEWED = "reviewed"


class ExecutionReceiptType(StrEnum):
    QUOTE = "quote"
    CONFIRMATION = "confirmation"
    ACCEPTANCE = "acceptance"
    REJECTION = "rejection"
    FAILURE = "failure"


@dataclass(frozen=True, slots=True)
class ProbabilityRecord:
    market_id: str
    outcome_id: str
    raw_implied_probability: Decimal
    no_vig_probability: Decimal
    cross_book_consensus_probability: Decimal
    raw_sip_probability: Decimal
    calibrated_sip_probability: Decimal
    reconciled_execution_probability: Decimal
    confidence_interval: tuple[Decimal, Decimal]
    break_even_probability: Decimal
    quote_timestamp: str
    forecast_timestamp: str
    model_version: str
    calibration_version: str
    data_quality_score: Decimal
    sportsbook_coverage: int
    reconciliation_method: str
    reconciliation_weights: dict[str, Decimal] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProbabilityReconciliationPolicy:
    policy_version: str
    reconciliation_version: str
    maximum_quote_age_minutes: int = 30
    minimum_data_quality: Decimal = Decimal("0.35")
    minimum_sportsbook_coverage: int = 2
    consensus_pull_floor: Decimal = Decimal("0.30")
    consensus_pull_ceiling: Decimal = Decimal("0.85")


@dataclass(frozen=True, slots=True)
class ReconciledProbabilityResult:
    probability: Decimal
    lower_bound: Decimal
    upper_bound: Decimal
    component_weights: dict[str, Decimal]
    adjustment_reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    model_version: str
    reconciliation_version: str
    status: ReconciliationStatus
    raw_component_probabilities: dict[str, Decimal]
    explanation: str


@dataclass(frozen=True, slots=True)
class StrategyEligibilityPolicy:
    strategy_id: str
    strategy_version: str
    minimum_book_count: int = 2
    maximum_quote_age_minutes: int = 30
    minimum_data_quality: Decimal = Decimal("0.50")
    approved_model_versions: tuple[str, ...] = ()
    approved_market_types: tuple[str, ...] = ()
    minimum_reconciled_edge: Decimal = Decimal("0.01")
    minimum_expected_value_after_vig: Decimal = Decimal("0.00")
    maximum_uncertainty_width: Decimal = Decimal("0.12")
    require_market_open: bool = True
    require_no_unresolved_injury_or_lineup_blocker: bool = True


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    eligible: bool
    strategy_id: str
    strategy_version: str
    matched_rules: tuple[str, ...]
    failed_rules: tuple[str, ...]
    warnings: tuple[str, ...]
    ranking_score: Decimal
    explanation: str


@dataclass(frozen=True, slots=True)
class ExposureSnapshot:
    exposure_by_league: dict[str, Decimal]
    exposure_by_team: dict[str, Decimal]
    exposure_by_player: dict[str, Decimal]
    exposure_by_event: dict[str, Decimal]
    exposure_by_market: dict[str, Decimal]
    exposure_by_market_type: dict[str, Decimal]
    exposure_by_outcome: dict[str, Decimal]
    exposure_by_sportsbook: dict[str, Decimal]
    exposure_by_strategy: dict[str, Decimal]
    exposure_by_model_version: dict[str, Decimal]
    exposure_by_settlement_horizon: dict[str, Decimal]
    exposure_by_correlated_group: dict[str, Decimal]
    available_bankroll: Decimal
    reserved_bankroll: Decimal
    open_stake: Decimal
    maximum_possible_loss: Decimal
    maximum_possible_profit: Decimal
    realized_pl: Decimal
    estimated_open_position_value: Decimal
    event_concentration: Decimal
    team_concentration: Decimal
    strategy_concentration: Decimal
    model_concentration: Decimal
    daily_drawdown: Decimal
    weekly_drawdown: Decimal


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    policy_version: str
    maximum_stake_per_position: Decimal
    maximum_percentage_of_bankroll: Decimal
    event_exposure_limit: Decimal
    team_exposure_limit: Decimal
    player_exposure_limit: Decimal
    league_exposure_limit: Decimal
    strategy_exposure_limit: Decimal
    correlated_cluster_limit: Decimal
    daily_loss_limit: Decimal
    weekly_drawdown_limit: Decimal
    maximum_simultaneous_positions: int
    minimum_available_reserve: Decimal
    model_status_restrictions: tuple[str, ...] = ()
    experimental_model_multiplier: Decimal = Decimal("0.50")
    override_policy: bool = False


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    approved_stake: Decimal
    requested_stake: Decimal
    blocking_violations: tuple[str, ...]
    warnings: tuple[str, ...]
    exposure_before: ExposureSnapshot
    projected_exposure: ExposureSnapshot
    policy_version: str
    explanation: str


@dataclass(frozen=True, slots=True)
class SizingPolicy:
    policy_version: str
    fractional_kelly_multiplier: Decimal = Decimal("0.25")
    experimental_model_multiplier: Decimal = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class SizingResult:
    stake: Decimal
    kelly_fraction: Decimal
    kelly_amount: Decimal
    limiting_factor: str
    calculation_audit: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OrderIntentEnvelope:
    intent_id: str
    market_id: str
    outcome_id: str
    state: OrderIntentState
    execution_id: str | None = None
    requested_odds: int | None = None
    current_quote_odds: int | None = None
    acceptance_receipt_id: str | None = None
    submission_ids: tuple[str, ...] = ()
    quote_revision: int = 0
    requires_revalidation: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    receipt_id: str
    execution_id: str
    receipt_type: ExecutionReceiptType
    created_at: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    aggregate_id: str
    event_type: str
    version: int
    timestamp: str
    actor: str
    correlation_id: str
    causation_id: str | None
    payload: dict[str, Any]
    payload_hash: str
    previous_event_hash: str | None
    event_hash: str
    model_version: str | None
    strategy_version: str | None
    risk_policy_version: str | None


class ExecutionAdapter(Protocol):
    def request_quote(self, order_intent: OrderIntentEnvelope) -> dict[str, Any]: ...

    def validate_availability(
        self, order_intent: OrderIntentEnvelope
    ) -> dict[str, Any]: ...

    def submit(
        self,
        order_intent: OrderIntentEnvelope,
        confirmation: dict[str, Any],
    ) -> dict[str, Any]: ...

    def get_status(self, execution_id: str) -> dict[str, Any]: ...

    def cancel(self, execution_id: str) -> dict[str, Any]: ...

    def fetch_receipt(self, execution_id: str) -> ExecutionReceipt | None: ...
