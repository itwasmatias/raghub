from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, is_dataclass
from decimal import Decimal
from typing import Any

from sports.execution.statuses import (
    DomainEventStatusV1,
    ExecutionReceiptStatusV1,
    ExposureSnapshotStatusV1,
    OrderIntentStateV1,
    PositionStatusV1,
    ProbabilityReconciliationStatusV1,
    ProbabilitySnapshotStatusV1,
    RiskDecisionStatusV1,
    SettlementResultStatusV1,
    SizingDecisionStatusV1,
    StrategyDecisionStatusV1,
)


MONEY_PRECISION = Decimal("0.01")
PROBABILITY_PRECISION = Decimal("0.000001")
UTC_TIMESTAMP_EXAMPLE = "2026-07-25T00:00:00+00:00"
CONTRACT_VERSION_V1 = "v1"
SCHEMA_VERSION_V1 = "v1"


def _serialize_value(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _serialize_value(item) for key, item in asdict(value).items()}
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_serialize_value(item) for item in value]
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    return value


def contract_to_dict(contract: Any) -> dict[str, Any]:
    if not is_dataclass(contract):
        raise TypeError("contract_to_dict expects a dataclass instance")
    return _serialize_value(contract)


def serialize_contract(contract: Any) -> str:
    return json.dumps(contract_to_dict(contract), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ProbabilitySnapshotV1:
    snapshot_id: str
    market_id: str
    outcome_id: str
    raw_american_odds: int
    raw_decimal_odds: Decimal
    raw_implied_probability: Decimal
    no_vig_probability: Decimal
    cross_book_consensus_probability: Decimal
    raw_sip_probability: Decimal
    calibrated_sip_probability: Decimal
    reconciled_execution_probability: Decimal
    confidence_lower_bound: Decimal
    confidence_upper_bound: Decimal
    break_even_probability: Decimal
    quote_timestamp: str
    forecast_timestamp: str
    model_version: str
    calibration_version: str
    reconciliation_version: str
    data_quality_score: Decimal
    sportsbook_coverage: int
    reconciliation_method: str
    strategy_version: str = ""
    policy_version: str = ""
    reconciliation_weights: dict[str, Decimal] = field(default_factory=dict)
    source_quote_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    status: ProbabilitySnapshotStatusV1 = ProbabilitySnapshotStatusV1.AVAILABLE
    contract_version: str = CONTRACT_VERSION_V1
    schema_version: str = SCHEMA_VERSION_V1
    created_at: str = UTC_TIMESTAMP_EXAMPLE
    updated_at: str = UTC_TIMESTAMP_EXAMPLE
    idempotency_key: str = ""
    audit_id: str = ""


@dataclass(frozen=True, slots=True)
class ProbabilityReconciliationResultV1:
    reconciliation_result_id: str
    snapshot_id: str
    market_id: str
    outcome_id: str
    probability: Decimal
    lower_bound: Decimal
    upper_bound: Decimal
    component_weights: dict[str, Decimal] = field(default_factory=dict)
    adjustment_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    raw_component_probabilities: dict[str, Decimal] = field(default_factory=dict)
    explanation: str = ""
    model_version: str = ""
    strategy_version: str = ""
    reconciliation_version: str = ""
    policy_version: str = ""
    status: ProbabilityReconciliationStatusV1 = (
        ProbabilityReconciliationStatusV1.RECONCILED
    )
    contract_version: str = CONTRACT_VERSION_V1
    schema_version: str = SCHEMA_VERSION_V1
    created_at: str = UTC_TIMESTAMP_EXAMPLE
    updated_at: str = UTC_TIMESTAMP_EXAMPLE
    idempotency_key: str = ""
    audit_id: str = ""


@dataclass(frozen=True, slots=True)
class StrategyDecisionV1:
    strategy_decision_id: str
    snapshot_id: str
    market_id: str
    outcome_id: str
    strategy_id: str
    strategy_version: str
    model_version: str
    policy_version: str
    eligible: bool
    matched_rules: tuple[str, ...]
    failed_rules: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    ranking_score: Decimal = Decimal("0")
    explanation: str = ""
    status: StrategyDecisionStatusV1 = StrategyDecisionStatusV1.BLOCKED
    contract_version: str = CONTRACT_VERSION_V1
    schema_version: str = SCHEMA_VERSION_V1
    created_at: str = UTC_TIMESTAMP_EXAMPLE
    updated_at: str = UTC_TIMESTAMP_EXAMPLE
    idempotency_key: str = ""
    audit_id: str = ""


@dataclass(frozen=True, slots=True)
class ExposureSnapshotV1:
    exposure_snapshot_id: str
    account_id: str
    as_of: str
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
    model_version: str = ""
    strategy_version: str = ""
    policy_version: str = ""
    status: ExposureSnapshotStatusV1 = ExposureSnapshotStatusV1.AVAILABLE
    contract_version: str = CONTRACT_VERSION_V1
    schema_version: str = SCHEMA_VERSION_V1
    created_at: str = UTC_TIMESTAMP_EXAMPLE
    updated_at: str = UTC_TIMESTAMP_EXAMPLE
    idempotency_key: str = ""
    audit_id: str = ""


@dataclass(frozen=True, slots=True)
class RiskDecisionV1:
    risk_decision_id: str
    snapshot_id: str
    strategy_decision_id: str
    exposure_snapshot_id: str
    policy_version: str
    model_version: str
    strategy_version: str
    approved: bool
    approved_stake: Decimal
    requested_stake: Decimal
    blocking_violations: tuple[str, ...]
    warnings: tuple[str, ...]
    exposure_before_reference: str
    projected_exposure_reference: str
    explanation: str
    status: RiskDecisionStatusV1 = RiskDecisionStatusV1.REJECTED
    override_reason: str | None = None
    override_actor_id: str | None = None
    override_audit_id: str | None = None
    contract_version: str = CONTRACT_VERSION_V1
    schema_version: str = SCHEMA_VERSION_V1
    created_at: str = UTC_TIMESTAMP_EXAMPLE
    updated_at: str = UTC_TIMESTAMP_EXAMPLE
    idempotency_key: str = ""
    audit_id: str = ""


@dataclass(frozen=True, slots=True)
class SizingDecisionV1:
    sizing_decision_id: str
    risk_decision_id: str
    probability_snapshot_id: str
    reconciled_execution_probability: Decimal
    decimal_profit_multiple: Decimal
    bankroll: Decimal
    kelly_fraction: Decimal
    kelly_amount: Decimal
    approved_stake: Decimal
    limiting_factor: str
    caps: dict[str, Decimal] = field(default_factory=dict)
    calculation_audit: dict[str, Any] = field(default_factory=dict)
    model_version: str = ""
    strategy_version: str = ""
    policy_version: str = ""
    status: SizingDecisionStatusV1 = SizingDecisionStatusV1.NO_POSITION
    contract_version: str = CONTRACT_VERSION_V1
    schema_version: str = SCHEMA_VERSION_V1
    created_at: str = UTC_TIMESTAMP_EXAMPLE
    updated_at: str = UTC_TIMESTAMP_EXAMPLE
    idempotency_key: str = ""
    audit_id: str = ""


@dataclass(frozen=True, slots=True)
class OrderIntentV1:
    order_intent_id: str
    thesis_id: str
    forecast_id: str
    probability_snapshot_id: str
    strategy_decision_id: str
    risk_decision_id: str
    sizing_decision_id: str
    market_id: str
    outcome_id: str
    requested_odds: int
    accepted_odds: int
    stake: Decimal
    execution_mode: str
    state: OrderIntentStateV1
    requires_confirmation: bool
    price_movement_tolerance: Decimal
    quote_timestamp: str
    current_quote_timestamp: str | None = None
    revalidation_required: bool = False
    execution_id: str | None = None
    acceptance_receipt_id: str | None = None
    submission_ids: tuple[str, ...] = ()
    model_version: str = ""
    strategy_version: str = ""
    policy_version: str = ""
    contract_version: str = CONTRACT_VERSION_V1
    schema_version: str = SCHEMA_VERSION_V1
    created_at: str = UTC_TIMESTAMP_EXAMPLE
    updated_at: str = UTC_TIMESTAMP_EXAMPLE
    idempotency_key: str = ""
    audit_id: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionReceiptV1:
    receipt_id: str
    order_intent_id: str
    execution_id: str
    receipt_type: str
    status: ExecutionReceiptStatusV1
    actor_type: str
    actor_id: str
    payload: dict[str, Any]
    created_at: str
    model_version: str = ""
    strategy_version: str = ""
    policy_version: str = ""
    provider_reference: str | None = None
    external_reference: str | None = None
    contract_version: str = CONTRACT_VERSION_V1
    schema_version: str = SCHEMA_VERSION_V1
    idempotency_key: str = ""
    audit_id: str = ""


@dataclass(frozen=True, slots=True)
class PositionV1:
    position_id: str
    order_intent_id: str
    execution_receipt_id: str
    acceptance_receipt_id: str
    account_id: str
    market_id: str
    outcome_id: str
    strategy_id: str
    strategy_version: str
    model_version: str
    policy_version: str
    stake: Decimal
    entry_odds: int
    entry_probability: Decimal
    reconciled_execution_probability: Decimal
    settlement_horizon: str
    correlated_exposure_group: str
    open_time: str
    monitoring_started_at: str
    exposure_before: dict[str, Decimal]
    exposure_after: dict[str, Decimal]
    state: PositionStatusV1 = PositionStatusV1.OPEN
    settled_at: str | None = None
    settlement_result_id: str | None = None
    contract_version: str = CONTRACT_VERSION_V1
    schema_version: str = SCHEMA_VERSION_V1
    idempotency_key: str = ""
    audit_id: str = ""


@dataclass(frozen=True, slots=True)
class SettlementResultV1:
    settlement_id: str
    position_id: str
    order_intent_id: str
    result: str
    payout_amount: Decimal
    profit_amount: Decimal
    settled_at: str
    settlement_source: str
    status: SettlementResultStatusV1 = SettlementResultStatusV1.PENDING
    model_version: str = ""
    strategy_version: str = ""
    policy_version: str = ""
    contract_version: str = CONTRACT_VERSION_V1
    schema_version: str = SCHEMA_VERSION_V1
    idempotency_key: str = ""
    audit_id: str = ""


@dataclass(frozen=True, slots=True)
class DomainEventV1:
    event_id: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    event_version: int
    occurred_at: str
    recorded_at: str
    actor_type: str
    actor_id: str
    correlation_id: str
    causation_id: str | None
    payload: dict[str, Any]
    payload_hash: str
    previous_event_hash: str | None
    event_hash: str
    model_version: str | None
    strategy_version: str | None
    policy_version: str | None
    status: DomainEventStatusV1 = DomainEventStatusV1.RECORDED
    contract_version: str = CONTRACT_VERSION_V1
    schema_version: str = SCHEMA_VERSION_V1
    idempotency_key: str = ""
    audit_id: str = ""
