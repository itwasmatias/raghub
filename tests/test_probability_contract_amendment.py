from decimal import Decimal

import pytest

from sports.execution.contracts import (
    DecisionReasonV1,
    ProbabilityReconciliationResultV1,
    ProbabilitySnapshotV1,
    serialize_contract,
)
from sports.execution.statuses import (
    CalibrationApprovalStatusV1,
    CalibrationStatusV1,
    DecisionReasonCategoryV1,
    DecisionReasonSeverityV1,
    ModelApprovalStatusV1,
    ModelStatusV1,
    ProbabilityReconciliationStatusV1,
    VersionApprovalStatusV1,
)


def _snapshot(**overrides: object) -> ProbabilitySnapshotV1:
    values: dict[str, object] = {
        "snapshot_id": "snapshot-1",
        "league": "wnba",
        "event_id": "event-1",
        "market_id": "market-1",
        "market_type": "moneyline",
        "period": "full_game",
        "outcome_id": "outcome-1",
        "selection_id": "selection-1",
        "outcome_schema": "home_away",
        "event_start_time": "2026-07-25T20:00:00+00:00",
        "as_of": "2026-07-25T12:05:00+00:00",
        "raw_american_odds": -110,
        "raw_decimal_odds": Decimal("1.909091"),
        "raw_implied_probability": Decimal("0.523809"),
        "no_vig_probability": Decimal("0.515000"),
        "cross_book_consensus_probability": Decimal("0.520000"),
        "raw_sip_probability": Decimal("0.610000"),
        "calibrated_sip_probability": Decimal("0.640000"),
        "reconciled_execution_probability": None,
        "confidence_lower_bound": Decimal("0.520000"),
        "confidence_upper_bound": Decimal("0.680000"),
        "break_even_probability": Decimal("0.523809"),
        "historical_prior_probability": Decimal("0.530000"),
        "historical_prior_source": "wnba-prior-dataset",
        "historical_prior_version": "prior-v1",
        "historical_prior_timestamp": "2026-07-24T00:00:00+00:00",
        "historical_prior_sample_scope": "league_last_2000_games",
        "historical_prior_missing": False,
        "execution_quote_id": "quote-1",
        "execution_sportsbook_id": "book-1",
        "execution_quote_timestamp": "2026-07-25T12:00:00+00:00",
        "consensus_constituent_quote_ids": ("quote-1", "quote-2"),
        "constituent_sportsbook_ids": ("book-1", "book-2"),
        "oldest_constituent_timestamp": "2026-07-25T11:59:00+00:00",
        "consensus_calculated_at": "2026-07-25T12:01:00+00:00",
        "complete_fresh_sportsbook_count": 2,
        "market_dispersion": Decimal("0.010000"),
        "canonical_input_hash": "",
        "source_quote_set_id": "quote-set-1",
        "quote_timestamp": "2026-07-25T12:00:00+00:00",
        "forecast_timestamp": "2026-07-25T12:01:00+00:00",
        "model_version": "model-v1",
        "calibration_version": "cal-v1",
        "reconciliation_version": "recon-v1",
        "reconciliation_policy_version": "policy-v1",
        "reconciliation_method_version": "weighted-shrink-v1",
        "confidence_interval_method_version": "normal-approx-v1",
        "model_status": ModelStatusV1.APPROVED,
        "calibration_status": CalibrationStatusV1.APPROVED,
        "model_approval_status": ModelApprovalStatusV1.APPROVED,
        "calibration_approval_status": CalibrationApprovalStatusV1.APPROVED,
        "policy_approval_status": VersionApprovalStatusV1.APPROVED,
        "reconciliation_approval_status": VersionApprovalStatusV1.APPROVED,
        "data_quality_score": Decimal("0.830000"),
        "sportsbook_coverage": 4,
        "reconciliation_method": "weighted-shrink",
        "strategy_version": "strategy-v1",
        "policy_version": "policy-v1",
        "reconciliation_weights": {
            "calibrated_sip": Decimal("0.500000"),
            "consensus": Decimal("0.300000"),
            "prior": Decimal("0.200000"),
        },
        "trust_factors": {
            "data_quality": Decimal("0.830000"),
            "coverage": Decimal("0.500000"),
        },
        "source_quote_ids": ("quote-1", "quote-2"),
    }
    values.update(overrides)
    return ProbabilitySnapshotV1(**values)


def _reason(code: str = "CP001") -> DecisionReasonV1:
    return DecisionReasonV1(
        code=code,
        severity=DecisionReasonSeverityV1.ERROR,
        category=DecisionReasonCategoryV1.POLICY,
        message="blocked by policy",
        observed_value="0",
        threshold="1",
        source_reference="policy-v1",
    )


def _result(**overrides: object) -> ProbabilityReconciliationResultV1:
    values: dict[str, object] = {
        "reconciliation_result_id": "recon-result-1",
        "snapshot_id": "snapshot-1",
        "market_id": "market-1",
        "outcome_id": "outcome-1",
        "reconciled_execution_probability": Decimal("0.560000"),
        "lower_bound": Decimal("0.520000"),
        "upper_bound": Decimal("0.600000"),
        "break_even_probability": Decimal("0.523809"),
        "component_probabilities": {
            "calibrated_sip": Decimal("0.640000"),
            "consensus": Decimal("0.520000"),
            "prior": Decimal("0.530000"),
        },
        "component_weights": {
            "calibrated_sip": Decimal("0.500000"),
            "consensus": Decimal("0.300000"),
            "prior": Decimal("0.200000"),
        },
        "effective_component_weights": {
            "calibrated_sip": Decimal("0.500000"),
            "consensus": Decimal("0.300000"),
            "prior": Decimal("0.200000"),
        },
        "raw_component_probabilities": {
            "raw_sip": Decimal("0.610000"),
            "raw_implied": Decimal("0.523809"),
        },
        "model_version": "model-v1",
        "calibration_version": "cal-v1",
        "reconciliation_version": "recon-v1",
        "reconciliation_policy_version": "policy-v1",
        "reconciliation_method_version": "weighted-shrink-v1",
        "confidence_interval_method_version": "normal-approx-v1",
        "policy_version": "policy-v1",
        "model_status": ModelStatusV1.APPROVED,
        "calibration_status": CalibrationStatusV1.APPROVED,
        "model_approval_status": ModelApprovalStatusV1.APPROVED,
        "calibration_approval_status": CalibrationApprovalStatusV1.APPROVED,
        "policy_approval_status": VersionApprovalStatusV1.APPROVED,
        "reconciliation_approval_status": VersionApprovalStatusV1.APPROVED,
        "required_input_evidence_present": True,
        "is_stale": False,
        "data_quality_score": Decimal("0.830000"),
        "quote_age_seconds": 180,
        "sportsbook_coverage": 4,
        "complete_fresh_sportsbook_count": 2,
        "quote_freshness_as_of": "2026-07-25T12:05:00+00:00",
        "as_of": "2026-07-25T12:05:00+00:00",
        "canonical_input_hash": "hash-1",
        "source_quote_set_id": "quote-set-1",
    }
    values.update(overrides)
    return ProbabilityReconciliationResultV1(**values)


def test_historical_prior_is_explicit_and_optional_without_ambiguity():
    missing_prior = _snapshot(
        historical_prior_probability=None,
        historical_prior_source="",
        historical_prior_version="",
        historical_prior_timestamp="",
        historical_prior_sample_scope="",
        historical_prior_missing=True,
    )
    assert missing_prior.historical_prior_missing
    assert missing_prior.historical_prior_probability is None


def test_identity_fields_persist_in_serialization():
    serialized = serialize_contract(_snapshot())
    assert '"league":"wnba"' in serialized
    assert '"event_id":"event-1"' in serialized
    assert '"market_type":"moneyline"' in serialized
    assert '"period":"full_game"' in serialized
    assert '"selection_id":"selection-1"' in serialized
    assert '"outcome_schema":"home_away"' in serialized


def test_naive_timestamps_reject():
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        _snapshot(as_of="2026-07-25T12:05:00")


def test_future_quote_timestamp_rejects():
    with pytest.raises(ValueError, match="cannot be after as_of"):
        _snapshot(execution_quote_timestamp="2026-07-25T12:06:00+00:00")


def test_structured_blocking_violations_serialize_deterministically():
    blocked = _result(
        status=ProbabilityReconciliationStatusV1.REJECTED,
        blocking_violations=(_reason("CP900"),),
    )
    first = serialize_contract(blocked)
    second = serialize_contract(blocked)
    assert first == second
    assert '"code":"CP900"' in first


def test_numeric_blocked_results_are_not_execution_eligible():
    blocked = _result(
        status=ProbabilityReconciliationStatusV1.REJECTED,
        blocking_violations=(_reason(),),
    )
    assert blocked.execution_eligible is False


def test_only_reconciled_result_with_no_blockers_can_be_eligible():
    eligible = _result()
    assert eligible.execution_eligible is True


def test_missing_model_approval_blocks_eligibility():
    blocked = _result(model_approval_status=ModelApprovalStatusV1.VERSION_UNAPPROVED)
    assert blocked.execution_eligible is False


def test_missing_calibration_approval_blocks_eligibility():
    blocked = _result(
        calibration_approval_status=CalibrationApprovalStatusV1.VERSION_UNAPPROVED
    )
    assert blocked.execution_eligible is False


def test_experimental_models_are_not_eligible_by_default():
    blocked = _result(model_status=ModelStatusV1.EXPERIMENTAL)
    assert blocked.execution_eligible is False


def test_probability_bounds_validate():
    with pytest.raises(ValueError, match="lower_bound must be <= upper_bound"):
        _result(lower_bound=Decimal("0.700000"), upper_bound=Decimal("0.600000"))


def test_confidence_bounds_must_contain_reconciled_probability():
    with pytest.raises(
        ValueError,
        match="reconciled_execution_probability must be inside bounds",
    ):
        _result(lower_bound=Decimal("0.570000"), upper_bound=Decimal("0.600000"))


def test_effective_weights_sum_exactly_to_one():
    _result(
        effective_component_weights={
            "calibrated_sip": Decimal("0.333333"),
            "consensus": Decimal("0.333333"),
            "prior": Decimal("0.333334"),
        }
    )


def test_coverage_is_nonnegative_and_distinct_fresh_count_is_preserved():
    snapshot = _snapshot(complete_fresh_sportsbook_count=3, sportsbook_coverage=5)
    assert snapshot.complete_fresh_sportsbook_count == 3
    assert snapshot.sportsbook_coverage == 5


def test_canonical_input_hash_is_deterministic():
    snapshot = _snapshot()
    assert snapshot.canonical_input_hash == snapshot.compute_canonical_input_hash()


def test_reordered_source_evidence_does_not_change_canonical_identity():
    first = _snapshot(
        consensus_constituent_quote_ids=("quote-1", "quote-2"),
        constituent_sportsbook_ids=("book-1", "book-2"),
    )
    second = _snapshot(
        consensus_constituent_quote_ids=("quote-2", "quote-1"),
        constituent_sportsbook_ids=("book-2", "book-1"),
    )
    assert first.canonical_input_hash == second.canonical_input_hash


def test_mutable_input_mappings_do_not_mutate_frozen_contract_state():
    weights = {
        "calibrated_sip": Decimal("0.500000"),
        "consensus": Decimal("0.300000"),
        "prior": Decimal("0.200000"),
    }
    snapshot = _snapshot(reconciliation_weights=weights)
    weights["prior"] = Decimal("0.900000")
    assert snapshot.reconciliation_weights["prior"] == Decimal("0.200000")


def test_compatibility_probability_alias_matches_authoritative_field():
    result = _result(probability=Decimal("0.560000"))
    assert result.probability == result.reconciled_execution_probability


def test_compatibility_alias_rejects_mismatch():
    with pytest.raises(ValueError, match="compatibility alias"):
        _result(probability=Decimal("0.550000"))
