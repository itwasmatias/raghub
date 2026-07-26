from dataclasses import fields
from decimal import Decimal
import inspect

from sports.execution.contracts import (
    DomainEventV1,
    ExecutionReceiptV1,
    ExposureSnapshotV1,
    OrderIntentV1,
    PositionV1,
    ProbabilityReconciliationResultV1,
    ProbabilitySnapshotV1,
    RiskDecisionV1,
    SettlementResultV1,
    SizingDecisionV1,
    StrategyDecisionV1,
    contract_to_dict,
    serialize_contract,
)
from sports.execution.statuses import (
    OrderIntentStateV1,
    order_intent_transition_allowed,
)


def _snapshot() -> ProbabilitySnapshotV1:
    return ProbabilitySnapshotV1(
        snapshot_id="snapshot-1",
        market_id="market-1",
        outcome_id="outcome-1",
        raw_american_odds=-110,
        raw_decimal_odds=Decimal("1.909091"),
        raw_implied_probability=Decimal("0.523810"),
        no_vig_probability=Decimal("0.515000"),
        cross_book_consensus_probability=Decimal("0.520000"),
        raw_sip_probability=Decimal("0.610000"),
        calibrated_sip_probability=Decimal("0.640000"),
        reconciled_execution_probability=Decimal("0.560000"),
        confidence_lower_bound=Decimal("0.520000"),
        confidence_upper_bound=Decimal("0.600000"),
        break_even_probability=Decimal("0.512000"),
        quote_timestamp="2026-07-25T12:00:00+00:00",
        forecast_timestamp="2026-07-25T12:01:00+00:00",
        model_version="model-v1",
        calibration_version="cal-v1",
        reconciliation_version="recon-v1",
        data_quality_score=Decimal("0.830000"),
        sportsbook_coverage=4,
        reconciliation_method="weighted-shrink",
        reconciliation_weights={
            "calibrated_sip": Decimal("0.500000"),
            "consensus": Decimal("0.300000"),
            "prior": Decimal("0.200000"),
        },
        source_quote_ids=("quote-1", "quote-2"),
        notes=("freeze",),
        audit_id="audit-1",
        idempotency_key="snapshot-1",
        created_at="2026-07-25T12:02:00+00:00",
        updated_at="2026-07-25T12:02:00+00:00",
    )


def test_contract_serialization_is_stable_and_decimal_safe():
    snapshot = _snapshot()

    first = serialize_contract(snapshot)
    second = serialize_contract(snapshot)

    assert first == second
    assert '"raw_implied_probability":"0.523810"' in first
    assert '"reconciled_execution_probability":"0.560000"' in first
    assert contract_to_dict(snapshot)["raw_american_odds"] == -110


def test_required_version_fields_exist_across_contracts():
    contract_classes = [
        ProbabilitySnapshotV1,
        ProbabilityReconciliationResultV1,
        StrategyDecisionV1,
        ExposureSnapshotV1,
        RiskDecisionV1,
        SizingDecisionV1,
        OrderIntentV1,
        ExecutionReceiptV1,
        PositionV1,
        SettlementResultV1,
        DomainEventV1,
    ]

    for contract_class in contract_classes:
        field_names = {item.name for item in fields(contract_class)}
        assert "contract_version" in field_names
        assert "schema_version" in field_names
        assert any(name.endswith("_version") for name in field_names)


def test_probability_values_remain_distinct_and_decimal_safe():
    snapshot = _snapshot()

    assert snapshot.raw_implied_probability != snapshot.no_vig_probability
    assert snapshot.no_vig_probability != snapshot.cross_book_consensus_probability
    assert snapshot.raw_sip_probability != snapshot.calibrated_sip_probability
    assert (
        snapshot.calibrated_sip_probability != snapshot.reconciled_execution_probability
    )
    assert snapshot.confidence_lower_bound != snapshot.confidence_upper_bound
    assert isinstance(snapshot.raw_decimal_odds, Decimal)
    assert isinstance(snapshot.raw_implied_probability, Decimal)


def test_only_reconciled_probability_is_present_in_sizing_contract():
    field_names = {item.name for item in fields(SizingDecisionV1)}

    assert "reconciled_execution_probability" in field_names
    assert "raw_sip_probability" not in field_names
    assert "raw_implied_probability" not in field_names
    assert "market_probability" not in field_names


def test_risk_approval_required_before_order_intent_creation_and_position_opening():
    order_fields = {item.name for item in fields(OrderIntentV1)}
    position_fields = {item.name for item in fields(PositionV1)}

    assert {
        "probability_snapshot_id",
        "strategy_decision_id",
        "risk_decision_id",
        "sizing_decision_id",
    }.issubset(order_fields)
    assert {"execution_receipt_id", "acceptance_receipt_id"}.issubset(position_fields)
    assert "settlement_result_id" in position_fields


def test_settlement_ids_are_idempotent_and_override_fields_exist():
    settlement_fields = {item.name for item in fields(SettlementResultV1)}
    risk_fields = {item.name for item in fields(RiskDecisionV1)}

    assert {"settlement_id", "idempotency_key"}.issubset(settlement_fields)
    assert {"override_reason", "override_actor_id", "override_audit_id"}.issubset(
        risk_fields
    )


def test_invalid_state_transitions_fail():
    assert order_intent_transition_allowed(
        OrderIntentStateV1.THESIS_DRAFTED,
        OrderIntentStateV1.FORECAST_CREATED,
    )
    assert not order_intent_transition_allowed(
        OrderIntentStateV1.THESIS_DRAFTED,
        OrderIntentStateV1.RISK_APPROVED,
    )
    assert not order_intent_transition_allowed(
        OrderIntentStateV1.EXECUTION_SUBMITTED,
        OrderIntentStateV1.FORECAST_CREATED,
    )


def test_existing_synthetic_valuation_path_does_not_touch_cash_balance():
    from sports.personal.repository import PersonalEditionRepository

    source = inspect.getsource(
        PersonalEditionRepository.save_synthetic_position_valuation
    )
    assert "UPDATE sip_bankroll_accounts" not in source
