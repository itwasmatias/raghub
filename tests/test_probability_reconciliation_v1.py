from __future__ import annotations

from decimal import Decimal, getcontext

import pytest

from sports.execution import LegacyProbabilityReconciler, ProbabilityReconciler
from sports.execution.contracts import (
    ConsensusQuoteEvidenceV1,
    ProbabilitySnapshotV1,
    serialize_contract,
)
from sports.execution.probability_adapters import LegacyProbabilityAdapterV1
from sports.execution.probability_policy import ProbabilityReconciliationPolicyV1
from sports.execution.statuses import (
    CalibrationApprovalStatusV1,
    CalibrationStatusV1,
    ModelApprovalStatusV1,
    ModelStatusV1,
    ProbabilityReconciliationStatusV1,
    VersionApprovalStatusV1,
)


def _policy(**overrides: object) -> ProbabilityReconciliationPolicyV1:
    payload: dict[str, object] = {
        "policy_version": "policy-v1",
        "reconciliation_version": "recon-v1",
        "reconciliation_method_version": "recon-method-v1",
        "confidence_interval_method_version": "interval-method-v1",
        "active_model_versions": ("model-v1",),
        "active_calibration_versions": ("cal-v1",),
    }
    payload.update(overrides)
    return ProbabilityReconciliationPolicyV1(**payload)


def _evidence(
    quote_id: str, sportsbook_id: str, observed_at: str, **overrides: object
) -> ConsensusQuoteEvidenceV1:
    payload: dict[str, object] = {
        "quote_id": quote_id,
        "canonical_sportsbook_id": sportsbook_id,
        "provider_id": f"provider-{quote_id}",
        "league": "wnba",
        "event_id": "event-1",
        "market_id": "market-1",
        "market_type": "moneyline",
        "selection_id": "selection-1",
        "outcome_id": "outcome-1",
        "period": "full_game",
        "outcome_schema": "home_away",
        "observed_at": observed_at,
        "active": True,
        "complete_market": True,
        "included_in_consensus": True,
        "no_vig_probability": Decimal("0.520000"),
    }
    payload.update(overrides)
    return ConsensusQuoteEvidenceV1(**payload)


def _snapshot(**overrides: object) -> ProbabilitySnapshotV1:
    payload: dict[str, object] = {
        "snapshot_id": "snapshot-1",
        "league": "wnba",
        "event_id": "event-1",
        "market_id": "market-1",
        "market_type": "moneyline",
        "period": "full_game",
        "outcome_id": "outcome-1",
        "selection_id": "selection-1",
        "outcome_schema": "home_away",
        "event_start_time": "2026-07-26T20:00:00+00:00",
        "as_of": "2026-07-26T12:05:00+00:00",
        "raw_american_odds": -110,
        "raw_decimal_odds": Decimal("1.909091"),
        "raw_implied_probability": Decimal("0.523809"),
        "no_vig_probability": Decimal("0.515000"),
        "cross_book_consensus_probability": Decimal("0.520000"),
        "raw_sip_probability": Decimal("0.210000"),
        "calibrated_sip_probability": Decimal("0.640000"),
        "reconciled_execution_probability": None,
        "confidence_lower_bound": Decimal("0.510000"),
        "confidence_upper_bound": Decimal("0.690000"),
        "break_even_probability": Decimal("0.523809"),
        "historical_prior_probability": Decimal("0.530000"),
        "historical_prior_source": "prior-source",
        "historical_prior_version": "prior-v1",
        "historical_prior_timestamp": "2026-07-25T00:00:00+00:00",
        "historical_prior_sample_scope": "league_last_2000_games",
        "historical_prior_missing": False,
        "execution_quote_id": "quote-1",
        "execution_sportsbook_id": "book-1",
        "execution_quote_timestamp": "2026-07-26T12:00:00+00:00",
        "consensus_constituent_quote_ids": ("quote-1", "quote-2"),
        "constituent_sportsbook_ids": ("book-1", "book-2"),
        "oldest_constituent_timestamp": "2026-07-26T11:58:00+00:00",
        "consensus_calculated_at": "2026-07-26T12:01:00+00:00",
        "complete_fresh_sportsbook_count": 2,
        "market_dispersion": Decimal("0.010000"),
        "canonical_input_hash": "",
        "source_quote_set_id": "quote-set-1",
        "quote_timestamp": "2026-07-26T12:00:00+00:00",
        "forecast_timestamp": "2026-07-26T12:01:00+00:00",
        "model_version": "model-v1",
        "calibration_version": "cal-v1",
        "reconciliation_version": "recon-v1",
        "reconciliation_policy_version": "policy-v1",
        "reconciliation_method_version": "recon-method-v1",
        "confidence_interval_method_version": "interval-method-v1",
        "model_status": ModelStatusV1.APPROVED,
        "calibration_status": CalibrationStatusV1.APPROVED,
        "model_approval_status": ModelApprovalStatusV1.APPROVED,
        "calibration_approval_status": CalibrationApprovalStatusV1.APPROVED,
        "policy_approval_status": VersionApprovalStatusV1.APPROVED,
        "reconciliation_approval_status": VersionApprovalStatusV1.APPROVED,
        "data_quality_score": Decimal("0.820000"),
        "sportsbook_coverage": 2,
        "reconciliation_method": "weighted-shrink",
        "strategy_version": "strategy-v1",
        "policy_version": "policy-v1",
        "reconciliation_weights": {
            "calibrated_sip": Decimal("0.500000"),
            "consensus": Decimal("0.300000"),
            "prior": Decimal("0.200000"),
        },
        "trust_factors": {"model_stability": Decimal("0.900000")},
        "source_quote_ids": ("quote-1", "quote-2"),
    }
    payload.update(overrides)
    return ProbabilitySnapshotV1(**payload)


def _service() -> ProbabilityReconciler:
    return ProbabilityReconciler(_policy())


def _default_evidence() -> tuple[ConsensusQuoteEvidenceV1, ...]:
    return (
        _evidence("quote-1", "book-1", "2026-07-26T12:00:00+00:00"),
        _evidence("quote-2", "book-2", "2026-07-26T12:01:00+00:00"),
    )


def test_exact_manual_formula_result_from_fixture():
    service = _service()
    snapshot = _snapshot()
    result = service.reconcile(
        snapshot,
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=_default_evidence(),
    )

    freshness = Decimal("0.833333")
    data_quality = Decimal("0.723077")
    coverage = Decimal("0.350000")
    calibration = Decimal("1.000000")
    stability = Decimal("0.900000")
    market_agreement = Decimal("0.916667")
    model_status = Decimal("1.000000")
    trust = Decimal("0.173990")
    effective_sip = Decimal("0.104394")
    anchor = Decimal("0.524000")
    expected = anchor + effective_sip * (Decimal("0.640000") - anchor)
    expected = expected.quantize(Decimal("0.000001"))
    assert result.reconciled_execution_probability == expected


def test_missing_historical_prior_supported_with_warning():
    service = _service()
    snapshot = _snapshot(
        historical_prior_missing=True,
        historical_prior_probability=None,
        historical_prior_source="",
        historical_prior_version="",
        historical_prior_timestamp="",
        historical_prior_sample_scope="",
    )
    result = service.reconcile(
        snapshot,
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=_default_evidence(),
    )
    assert any(
        reason.code == "PRW_MISSING_HISTORICAL_PRIOR"
        for reason in result.warning_reasons
    )


def test_quote_age_at_boundary_allowed():
    service = _service()
    result = service.reconcile(
        _snapshot(execution_quote_timestamp="2026-07-26T11:35:00+00:00"),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=(
            _evidence("quote-1", "book-1", "2026-07-26T11:35:00+00:00"),
            _evidence("quote-2", "book-2", "2026-07-26T11:36:00+00:00"),
        ),
    )
    assert result.status != ProbabilityReconciliationStatusV1.STALE


def test_quote_age_one_second_beyond_boundary_blocks():
    service = _service()
    result = service.reconcile(
        _snapshot(execution_quote_timestamp="2026-07-26T11:34:59+00:00"),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=(
            _evidence("quote-1", "book-1", "2026-07-26T11:34:59+00:00"),
            _evidence("quote-2", "book-2", "2026-07-26T11:35:00+00:00"),
        ),
    )
    assert any(
        reason.code == "PRB_STALE_EXECUTION_QUOTE"
        for reason in result.blocking_violations
    )


def test_reconciliation_version_mismatch_blocks():
    service = _service()
    result = service.reconcile(
        _snapshot(reconciliation_version="recon-vX"),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=_default_evidence(),
    )
    assert any(
        reason.code == "PRB_RECONCILIATION_VERSION_MISMATCH"
        for reason in result.blocking_violations
    )


def test_reconciliation_method_version_mismatch_blocks():
    service = _service()
    result = service.reconcile(
        _snapshot(reconciliation_method_version="method-vX"),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=_default_evidence(),
    )
    assert any(
        reason.code == "PRB_RECONCILIATION_METHOD_VERSION_MISMATCH"
        for reason in result.blocking_violations
    )


def test_interval_method_version_mismatch_blocks():
    service = _service()
    result = service.reconcile(
        _snapshot(confidence_interval_method_version="interval-vX"),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=_default_evidence(),
    )
    assert any(
        reason.code == "PRB_INTERVAL_METHOD_VERSION_MISMATCH"
        for reason in result.blocking_violations
    )


def test_unapproved_model_version_blocks():
    service = _service()
    result = service.reconcile(
        _snapshot(model_version="model-vX"),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=_default_evidence(),
    )
    assert any(
        reason.code == "PRB_MODEL_VERSION_UNAPPROVED"
        for reason in result.blocking_violations
    )


def test_unapproved_calibration_version_blocks():
    service = _service()
    result = service.reconcile(
        _snapshot(calibration_version="cal-vX"),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=_default_evidence(),
    )
    assert any(
        reason.code == "PRB_CALIBRATION_VERSION_UNAPPROVED"
        for reason in result.blocking_violations
    )


def test_two_provider_aliases_same_sportsbook_count_once():
    service = _service()
    evidence = (
        _evidence("quote-1", "book-1", "2026-07-26T12:00:00+00:00", provider_id="A"),
        _evidence("quote-2", "book-1", "2026-07-26T12:00:10+00:00", provider_id="B"),
        _evidence("quote-3", "book-2", "2026-07-26T12:00:20+00:00", provider_id="C"),
    )
    result = service.reconcile(
        _snapshot(
            complete_fresh_sportsbook_count=2,
            consensus_constituent_quote_ids=("quote-1", "quote-2", "quote-3"),
        ),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert result.complete_fresh_sportsbook_count == 2


def test_multiple_quotes_same_sportsbook_count_once():
    service = _service()
    evidence = (
        _evidence("quote-1", "book-1", "2026-07-26T11:59:00+00:00"),
        _evidence("quote-2", "book-1", "2026-07-26T12:00:00+00:00"),
        _evidence("quote-3", "book-2", "2026-07-26T12:00:00+00:00"),
    )
    result = service.reconcile(
        _snapshot(
            complete_fresh_sportsbook_count=2,
            consensus_constituent_quote_ids=("quote-1", "quote-2", "quote-3"),
        ),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert result.complete_fresh_sportsbook_count == 2


def test_deterministic_latest_quote_selection():
    service = _service()
    older = _evidence(
        "quote-A",
        "book-1",
        "2026-07-26T12:00:00+00:00",
        no_vig_probability=Decimal("0.490000"),
    )
    newer = _evidence(
        "quote-B",
        "book-1",
        "2026-07-26T12:01:00+00:00",
        no_vig_probability=Decimal("0.550000"),
    )
    evidence = (
        older,
        newer,
        _evidence("quote-2", "book-2", "2026-07-26T12:00:00+00:00"),
    )
    snapshot = _snapshot(
        complete_fresh_sportsbook_count=2,
        consensus_constituent_quote_ids=("quote-A", "quote-B", "quote-2"),
    )
    result1 = service.reconcile(
        snapshot, as_of="2026-07-26T12:05:00+00:00", consensus_evidence=evidence
    )
    result2 = service.reconcile(
        snapshot,
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=tuple(reversed(evidence)),
    )
    assert serialize_contract(result1) == serialize_contract(result2)


def test_suspended_quote_does_not_count():
    service = _service()
    evidence = (
        _evidence("quote-1", "book-1", "2026-07-26T12:00:00+00:00", active=False),
        _evidence("quote-2", "book-2", "2026-07-26T12:00:00+00:00"),
    )
    result = service.reconcile(
        _snapshot(complete_fresh_sportsbook_count=1),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert any(
        reason.code == "PRB_INSUFFICIENT_COVERAGE"
        for reason in result.blocking_violations
    )


def test_incomplete_quote_does_not_count():
    service = _service()
    evidence = (
        _evidence(
            "quote-1", "book-1", "2026-07-26T12:00:00+00:00", complete_market=False
        ),
        _evidence("quote-2", "book-2", "2026-07-26T12:00:00+00:00"),
    )
    result = service.reconcile(
        _snapshot(complete_fresh_sportsbook_count=1),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert any(
        reason.code == "PRB_INSUFFICIENT_COVERAGE"
        for reason in result.blocking_violations
    )


def test_stale_constituent_quote_does_not_count():
    service = _service()
    evidence = (
        _evidence("quote-1", "book-1", "2026-07-26T11:20:00+00:00"),
        _evidence("quote-2", "book-2", "2026-07-26T12:00:00+00:00"),
    )
    result = service.reconcile(
        _snapshot(complete_fresh_sportsbook_count=1),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert any(
        reason.code == "PRB_INSUFFICIENT_COVERAGE"
        for reason in result.blocking_violations
    )


def test_excluded_quote_does_not_count():
    service = _service()
    evidence = (
        _evidence(
            "quote-1",
            "book-1",
            "2026-07-26T12:00:00+00:00",
            included_in_consensus=False,
        ),
        _evidence("quote-2", "book-2", "2026-07-26T12:00:00+00:00"),
    )
    result = service.reconcile(
        _snapshot(complete_fresh_sportsbook_count=1),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert any(
        reason.code == "PRB_INSUFFICIENT_COVERAGE"
        for reason in result.blocking_violations
    )


def test_claimed_vs_derived_coverage_mismatch_blocks():
    service = _service()
    result = service.reconcile(
        _snapshot(complete_fresh_sportsbook_count=4),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=_default_evidence(),
    )
    assert any(
        reason.code == "PRB_COVERAGE_EVIDENCE_MISMATCH"
        for reason in result.blocking_violations
    )


def test_canonical_hash_changes_on_material_input_change():
    service = _service()
    a = service.reconcile(
        _snapshot(data_quality_score=Decimal("0.820000")),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=_default_evidence(),
    )
    b = service.reconcile(
        _snapshot(data_quality_score=Decimal("0.830000")),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=_default_evidence(),
    )
    assert a.canonical_input_hash != b.canonical_input_hash


def test_forged_caller_supplied_canonical_hash_rejects():
    service = _service()
    snapshot = _snapshot(canonical_input_hash="forged")
    result = service.reconcile(
        snapshot,
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=_default_evidence(),
    )
    assert any(
        reason.code == "PRB_CANONICAL_HASH_MISMATCH"
        for reason in result.blocking_violations
    )


def test_reordered_equivalent_quote_evidence_same_hash():
    service = _service()
    evidence = _default_evidence()
    first = service.reconcile(
        _snapshot(), as_of="2026-07-26T12:05:00+00:00", consensus_evidence=evidence
    )
    second = service.reconcile(
        _snapshot(),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=tuple(reversed(evidence)),
    )
    assert first.canonical_input_hash == second.canonical_input_hash


def test_rounding_independent_of_global_decimal_context():
    service = _service()
    evidence = _default_evidence()
    original_prec = getcontext().prec
    original_rounding = getcontext().rounding

    baseline = service.reconcile(
        _snapshot(), as_of="2026-07-26T12:05:00+00:00", consensus_evidence=evidence
    )
    getcontext().prec = 9
    getcontext().rounding = "ROUND_FLOOR"
    mutated = service.reconcile(
        _snapshot(), as_of="2026-07-26T12:05:00+00:00", consensus_evidence=evidence
    )

    getcontext().prec = original_prec
    getcontext().rounding = original_rounding
    assert serialize_contract(baseline) == serialize_contract(mutated)


def test_missing_consensus_yields_structured_blocker_not_exception():
    service = _service()
    result = service.reconcile(
        _snapshot(
            cross_book_consensus_probability=None,
            complete_fresh_sportsbook_count=0,
            consensus_constituent_quote_ids=(),
            source_quote_ids=(),
            constituent_sportsbook_ids=(),
            sportsbook_coverage=0,
        ),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=(),
    )
    assert any(
        reason.code == "PRB_MISSING_CONSENSUS" for reason in result.blocking_violations
    )
    assert result.execution_eligible is False


def test_same_admitted_evidence_submitted_consensus_001_blocks():
    service = _service()
    result = service.reconcile(
        _snapshot(cross_book_consensus_probability=Decimal("0.010000")),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=_default_evidence(),
    )
    assert any(
        reason.code == "PRB_CONSENSUS_EVIDENCE_MISMATCH"
        for reason in result.blocking_violations
    )
    assert result.execution_eligible is False


def test_same_admitted_evidence_submitted_consensus_099_blocks():
    service = _service()
    result = service.reconcile(
        _snapshot(cross_book_consensus_probability=Decimal("0.990000")),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=_default_evidence(),
    )
    assert any(
        reason.code == "PRB_CONSENSUS_EVIDENCE_MISMATCH"
        for reason in result.blocking_violations
    )
    assert result.execution_eligible is False


def test_correct_submitted_consensus_remains_eligible():
    service = _service()
    result = service.reconcile(
        _snapshot(cross_book_consensus_probability=Decimal("0.520000")),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=_default_evidence(),
    )
    assert not any(
        reason.code == "PRB_CONSENSUS_EVIDENCE_MISMATCH"
        for reason in result.blocking_violations
    )
    assert result.execution_eligible is True


def test_missing_submitted_consensus_uses_admitted_evidence_when_available():
    service = _service()
    result = service.reconcile(
        _snapshot(cross_book_consensus_probability=None),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=_default_evidence(),
    )
    assert not any(
        reason.code == "PRB_MISSING_CONSENSUS" for reason in result.blocking_violations
    )
    assert result.execution_eligible is True


def test_stale_declared_constituent_blocks():
    service = _service()
    evidence = (
        _evidence("quote-stale", "book-1", "2026-07-26T11:20:00+00:00"),
        _evidence("quote-2", "book-2", "2026-07-26T12:00:00+00:00"),
        _evidence("quote-3", "book-3", "2026-07-26T12:00:00+00:00"),
    )
    result = service.reconcile(
        _snapshot(
            complete_fresh_sportsbook_count=2,
            consensus_constituent_quote_ids=("quote-stale", "quote-2", "quote-3"),
            source_quote_ids=("quote-stale", "quote-2", "quote-3"),
            constituent_sportsbook_ids=("book-1", "book-2", "book-3"),
            sportsbook_coverage=2,
        ),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert any(
        reason.code == "PRB_CONSENSUS_CONSTITUENT_MISMATCH"
        for reason in result.blocking_violations
    )


def test_inactive_declared_constituent_blocks():
    service = _service()
    evidence = (
        _evidence(
            "quote-inactive", "book-1", "2026-07-26T12:00:00+00:00", active=False
        ),
        _evidence("quote-2", "book-2", "2026-07-26T12:00:00+00:00"),
        _evidence("quote-3", "book-3", "2026-07-26T12:00:00+00:00"),
    )
    result = service.reconcile(
        _snapshot(
            complete_fresh_sportsbook_count=2,
            consensus_constituent_quote_ids=("quote-inactive", "quote-2", "quote-3"),
            source_quote_ids=("quote-inactive", "quote-2", "quote-3"),
            constituent_sportsbook_ids=("book-1", "book-2", "book-3"),
            sportsbook_coverage=2,
        ),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert any(
        reason.code == "PRB_CONSENSUS_CONSTITUENT_MISMATCH"
        for reason in result.blocking_violations
    )


def test_incomplete_declared_constituent_blocks():
    service = _service()
    evidence = (
        _evidence(
            "quote-incomplete",
            "book-1",
            "2026-07-26T12:00:00+00:00",
            complete_market=False,
        ),
        _evidence("quote-2", "book-2", "2026-07-26T12:00:00+00:00"),
        _evidence("quote-3", "book-3", "2026-07-26T12:00:00+00:00"),
    )
    result = service.reconcile(
        _snapshot(
            complete_fresh_sportsbook_count=2,
            consensus_constituent_quote_ids=(
                "quote-incomplete",
                "quote-2",
                "quote-3",
            ),
            source_quote_ids=("quote-incomplete", "quote-2", "quote-3"),
            constituent_sportsbook_ids=("book-1", "book-2", "book-3"),
            sportsbook_coverage=2,
        ),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert any(
        reason.code == "PRB_CONSENSUS_CONSTITUENT_MISMATCH"
        for reason in result.blocking_violations
    )


def test_excluded_declared_constituent_blocks():
    service = _service()
    evidence = (
        _evidence(
            "quote-excluded",
            "book-1",
            "2026-07-26T12:00:00+00:00",
            included_in_consensus=False,
        ),
        _evidence("quote-2", "book-2", "2026-07-26T12:00:00+00:00"),
        _evidence("quote-3", "book-3", "2026-07-26T12:00:00+00:00"),
    )
    result = service.reconcile(
        _snapshot(
            complete_fresh_sportsbook_count=2,
            consensus_constituent_quote_ids=("quote-excluded", "quote-2", "quote-3"),
            source_quote_ids=("quote-excluded", "quote-2", "quote-3"),
            constituent_sportsbook_ids=("book-1", "book-2", "book-3"),
            sportsbook_coverage=2,
        ),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert any(
        reason.code == "PRB_CONSENSUS_CONSTITUENT_MISMATCH"
        for reason in result.blocking_violations
    )


def test_superseded_duplicate_constituent_blocks():
    service = _service()
    evidence = (
        _evidence("quote-old", "book-1", "2026-07-26T12:00:00+00:00"),
        _evidence("quote-new", "book-1", "2026-07-26T12:01:00+00:00"),
        _evidence("quote-2", "book-2", "2026-07-26T12:00:00+00:00"),
    )
    result = service.reconcile(
        _snapshot(
            complete_fresh_sportsbook_count=2,
            consensus_constituent_quote_ids=("quote-old", "quote-2"),
            source_quote_ids=("quote-old", "quote-new", "quote-2"),
            constituent_sportsbook_ids=("book-1", "book-2"),
            sportsbook_coverage=2,
        ),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert any(
        reason.code == "PRB_CONSENSUS_CONSTITUENT_MISMATCH"
        for reason in result.blocking_violations
    )


def test_scope_mismatched_constituent_blocks():
    service = _service()
    evidence = (
        _evidence(
            "quote-scope", "book-1", "2026-07-26T12:00:00+00:00", period="first_half"
        ),
        _evidence("quote-2", "book-2", "2026-07-26T12:00:00+00:00"),
        _evidence("quote-3", "book-3", "2026-07-26T12:00:00+00:00"),
    )
    result = service.reconcile(
        _snapshot(
            complete_fresh_sportsbook_count=2,
            consensus_constituent_quote_ids=("quote-scope", "quote-2", "quote-3"),
            source_quote_ids=("quote-scope", "quote-2", "quote-3"),
            constituent_sportsbook_ids=("book-1", "book-2", "book-3"),
            sportsbook_coverage=2,
        ),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert any(
        reason.code == "PRB_CONSENSUS_CONSTITUENT_MISMATCH"
        for reason in result.blocking_violations
    )


def test_different_event_evidence_blocks_scope_mismatch():
    service = _service()
    evidence = (
        _evidence("quote-1", "book-1", "2026-07-26T12:00:00+00:00", event_id="event-x"),
        _evidence("quote-2", "book-2", "2026-07-26T12:01:00+00:00", event_id="event-x"),
    )
    result = service.reconcile(
        _snapshot(),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert any(
        reason.code == "PRB_CONSENSUS_SCOPE_MISMATCH"
        for reason in result.blocking_violations
    )


def test_different_market_evidence_blocks_scope_mismatch():
    service = _service()
    evidence = (
        _evidence(
            "quote-1", "book-1", "2026-07-26T12:00:00+00:00", market_id="market-x"
        ),
        _evidence(
            "quote-2", "book-2", "2026-07-26T12:01:00+00:00", market_id="market-x"
        ),
    )
    result = service.reconcile(
        _snapshot(), as_of="2026-07-26T12:05:00+00:00", consensus_evidence=evidence
    )
    assert any(
        reason.code == "PRB_CONSENSUS_SCOPE_MISMATCH"
        for reason in result.blocking_violations
    )


def test_different_selection_evidence_blocks_scope_mismatch():
    service = _service()
    evidence = (
        _evidence(
            "quote-1", "book-1", "2026-07-26T12:00:00+00:00", selection_id="selection-x"
        ),
        _evidence(
            "quote-2", "book-2", "2026-07-26T12:01:00+00:00", selection_id="selection-x"
        ),
    )
    result = service.reconcile(
        _snapshot(), as_of="2026-07-26T12:05:00+00:00", consensus_evidence=evidence
    )
    assert any(
        reason.code == "PRB_CONSENSUS_SCOPE_MISMATCH"
        for reason in result.blocking_violations
    )


def test_opposing_outcome_evidence_blocks_scope_mismatch():
    service = _service()
    evidence = (
        _evidence(
            "quote-1",
            "book-1",
            "2026-07-26T12:00:00+00:00",
            outcome_id="outcome-opponent",
        ),
        _evidence(
            "quote-2",
            "book-2",
            "2026-07-26T12:01:00+00:00",
            outcome_id="outcome-opponent",
        ),
    )
    result = service.reconcile(
        _snapshot(), as_of="2026-07-26T12:05:00+00:00", consensus_evidence=evidence
    )
    assert any(
        reason.code == "PRB_CONSENSUS_SCOPE_MISMATCH"
        for reason in result.blocking_violations
    )


def test_different_outcome_schema_evidence_blocks_scope_mismatch():
    service = _service()
    evidence = (
        _evidence(
            "quote-1",
            "book-1",
            "2026-07-26T12:00:00+00:00",
            outcome_schema="over_under",
        ),
        _evidence(
            "quote-2",
            "book-2",
            "2026-07-26T12:01:00+00:00",
            outcome_schema="over_under",
        ),
    )
    result = service.reconcile(
        _snapshot(), as_of="2026-07-26T12:05:00+00:00", consensus_evidence=evidence
    )
    assert any(
        reason.code == "PRB_CONSENSUS_SCOPE_MISMATCH"
        for reason in result.blocking_violations
    )


def test_cross_outcome_evidence_cannot_contribute_to_consensus():
    service = _service()
    evidence = (
        _evidence(
            "quote-1",
            "book-1",
            "2026-07-26T12:00:00+00:00",
            outcome_id="outcome-opponent",
            no_vig_probability=Decimal("0.990000"),
        ),
        _evidence(
            "quote-2",
            "book-2",
            "2026-07-26T12:01:00+00:00",
            outcome_id="outcome-opponent",
            no_vig_probability=Decimal("0.990000"),
        ),
    )
    result = service.reconcile(
        _snapshot(cross_book_consensus_probability=Decimal("0.520000")),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert any(
        reason.code == "PRB_CONSENSUS_SCOPE_MISMATCH"
        for reason in result.blocking_violations
    )
    assert not any(
        reason.code == "PRB_CONSENSUS_EVIDENCE_MISMATCH"
        for reason in result.blocking_violations
    )


def test_duplicate_quote_id_two_sportsbooks_blocks():
    service = _service()
    evidence = (
        _evidence("quote-dupe", "book-1", "2026-07-26T12:00:00+00:00"),
        _evidence("quote-dupe", "book-2", "2026-07-26T12:01:00+00:00"),
    )
    result = service.reconcile(
        _snapshot(
            complete_fresh_sportsbook_count=0,
            consensus_constituent_quote_ids=(),
            source_quote_ids=(),
        ),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert any(
        reason.code == "PRB_DUPLICATE_QUOTE_ID" for reason in result.blocking_violations
    )


def test_duplicate_quote_id_two_providers_blocks():
    service = _service()
    evidence = (
        _evidence(
            "quote-dupe",
            "book-1",
            "2026-07-26T12:00:00+00:00",
            provider_id="provider-A",
        ),
        _evidence(
            "quote-dupe",
            "book-1",
            "2026-07-26T12:01:00+00:00",
            provider_id="provider-B",
        ),
    )
    result = service.reconcile(
        _snapshot(
            complete_fresh_sportsbook_count=0,
            consensus_constituent_quote_ids=(),
            source_quote_ids=(),
        ),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert any(
        reason.code == "PRB_DUPLICATE_QUOTE_ID" for reason in result.blocking_violations
    )


def test_duplicate_quote_id_different_probability_blocks():
    service = _service()
    evidence = (
        _evidence(
            "quote-dupe",
            "book-1",
            "2026-07-26T12:00:00+00:00",
            no_vig_probability=Decimal("0.500000"),
        ),
        _evidence(
            "quote-dupe",
            "book-1",
            "2026-07-26T12:01:00+00:00",
            no_vig_probability=Decimal("0.700000"),
        ),
    )
    result = service.reconcile(
        _snapshot(
            complete_fresh_sportsbook_count=0,
            consensus_constituent_quote_ids=(),
            source_quote_ids=(),
        ),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert any(
        reason.code == "PRB_DUPLICATE_QUOTE_ID" for reason in result.blocking_violations
    )


def test_duplicate_quote_id_different_scope_blocks():
    service = _service()
    evidence = (
        _evidence(
            "quote-dupe", "book-1", "2026-07-26T12:00:00+00:00", outcome_id="outcome-1"
        ),
        _evidence(
            "quote-dupe", "book-1", "2026-07-26T12:01:00+00:00", outcome_id="outcome-2"
        ),
    )
    result = service.reconcile(
        _snapshot(
            complete_fresh_sportsbook_count=0,
            consensus_constituent_quote_ids=(),
            source_quote_ids=(),
        ),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert any(
        reason.code == "PRB_DUPLICATE_QUOTE_ID" for reason in result.blocking_violations
    )


def test_duplicate_quote_ids_do_not_increase_coverage_and_order_is_enforced():
    service = _service()
    evidence = (
        _evidence("quote-dupe", "book-1", "2026-07-26T12:00:00+00:00"),
        _evidence("quote-dupe", "book-1", "2026-07-26T12:01:00+00:00"),
        _evidence("quote-2", "book-2", "2026-07-26T12:00:00+00:00"),
    )
    result = service.reconcile(
        _snapshot(
            complete_fresh_sportsbook_count=2,
            consensus_constituent_quote_ids=("quote-dupe", "quote-2"),
            source_quote_ids=("quote-dupe", "quote-2"),
        ),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert any(
        reason.code == "PRB_DUPLICATE_QUOTE_ID" for reason in result.blocking_violations
    )
    assert result.complete_fresh_sportsbook_count == 0


def test_canonical_hash_changes_with_evidence_identity_change():
    service = _service()
    snapshot_a = _snapshot()
    evidence_a = (
        _evidence("quote-1", "book-1", "2026-07-26T12:00:00+00:00"),
        _evidence("quote-2", "book-2", "2026-07-26T12:01:00+00:00"),
    )
    result_a = service.reconcile(
        snapshot_a, as_of="2026-07-26T12:05:00+00:00", consensus_evidence=evidence_a
    )

    snapshot_b = _snapshot(selection_id="selection-2", outcome_id="outcome-2")
    evidence_b = (
        _evidence(
            "quote-1",
            "book-1",
            "2026-07-26T12:00:00+00:00",
            selection_id="selection-2",
            outcome_id="outcome-2",
        ),
        _evidence(
            "quote-2",
            "book-2",
            "2026-07-26T12:01:00+00:00",
            selection_id="selection-2",
            outcome_id="outcome-2",
        ),
    )
    result_b = service.reconcile(
        snapshot_b, as_of="2026-07-26T12:05:00+00:00", consensus_evidence=evidence_b
    )

    assert result_a.canonical_input_hash != result_b.canonical_input_hash


def test_declared_ids_validated_against_selected_evidence_not_scoped_only():
    service = _service()
    evidence = (
        _evidence("quote-stale", "book-1", "2026-07-26T11:20:00+00:00"),
        _evidence("quote-fresh", "book-1", "2026-07-26T12:01:00+00:00"),
        _evidence("quote-2", "book-2", "2026-07-26T12:00:00+00:00"),
    )
    result = service.reconcile(
        _snapshot(
            complete_fresh_sportsbook_count=2,
            consensus_constituent_quote_ids=("quote-stale", "quote-2"),
            source_quote_ids=("quote-stale", "quote-fresh", "quote-2"),
            constituent_sportsbook_ids=("book-1", "book-2"),
            sportsbook_coverage=2,
        ),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert any(
        reason.code == "PRB_CONSENSUS_CONSTITUENT_MISMATCH"
        for reason in result.blocking_violations
    )


def test_reordered_evidence_produces_identical_derived_consensus_outcome():
    service = _service()
    evidence = (
        _evidence(
            "quote-1",
            "book-1",
            "2026-07-26T12:00:00+00:00",
            no_vig_probability=Decimal("0.500000"),
        ),
        _evidence(
            "quote-2",
            "book-2",
            "2026-07-26T12:01:00+00:00",
            no_vig_probability=Decimal("0.540000"),
        ),
    )
    snapshot = _snapshot(cross_book_consensus_probability=None)
    result_a = service.reconcile(
        snapshot,
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    result_b = service.reconcile(
        snapshot,
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=tuple(reversed(evidence)),
    )
    assert (
        result_a.reconciled_execution_probability
        == result_b.reconciled_execution_probability
    )


def test_provider_aliases_for_one_sportsbook_do_not_alter_consensus_or_coverage():
    service = _service()
    evidence = (
        _evidence(
            "quote-a",
            "book-1",
            "2026-07-26T12:00:00+00:00",
            provider_id="p1",
            no_vig_probability=Decimal("0.500000"),
        ),
        _evidence(
            "quote-b",
            "book-1",
            "2026-07-26T12:01:00+00:00",
            provider_id="p2",
            no_vig_probability=Decimal("0.600000"),
        ),
        _evidence(
            "quote-c",
            "book-2",
            "2026-07-26T12:00:00+00:00",
            provider_id="p3",
            no_vig_probability=Decimal("0.520000"),
        ),
    )
    result = service.reconcile(
        _snapshot(
            cross_book_consensus_probability=Decimal("0.560000"),
            complete_fresh_sportsbook_count=2,
            consensus_constituent_quote_ids=("quote-b", "quote-c"),
            source_quote_ids=("quote-a", "quote-b", "quote-c"),
            constituent_sportsbook_ids=("book-1", "book-2"),
            sportsbook_coverage=2,
        ),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert result.complete_fresh_sportsbook_count == 2
    assert result.execution_eligible is True


def test_consensus_mismatch_emits_prb_consensus_evidence_mismatch():
    service = _service()
    result = service.reconcile(
        _snapshot(cross_book_consensus_probability=Decimal("0.100000")),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=_default_evidence(),
    )
    assert any(
        reason.code == "PRB_CONSENSUS_EVIDENCE_MISMATCH"
        for reason in result.blocking_violations
    )


def test_constituent_mismatch_emits_prb_consensus_constituent_mismatch():
    service = _service()
    result = service.reconcile(
        _snapshot(consensus_constituent_quote_ids=("quote-missing", "quote-2")),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=_default_evidence(),
    )
    assert any(
        reason.code == "PRB_CONSENSUS_CONSTITUENT_MISMATCH"
        for reason in result.blocking_violations
    )


def test_legacy_reconciler_not_primary_public_authority():
    assert ProbabilityReconciler.__name__ == "ProbabilityReconciliationServiceV1"
    assert LegacyProbabilityReconciler.__name__ == "ProbabilityReconciler"


def test_stability_shrinkage_reason_emitted():
    service = _service()
    result = service.reconcile(
        _snapshot(trust_factors={"model_stability": Decimal("0.700000")}),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=_default_evidence(),
    )
    assert any(
        reason.code == "PRD_MODEL_STABILITY_SHRINKAGE" for reason in result.downgrades
    )


def test_market_disagreement_shrinkage_reason_emitted():
    service = _service()
    result = service.reconcile(
        _snapshot(market_dispersion=Decimal("0.090000")),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=_default_evidence(),
    )
    assert any(
        reason.code == "PRD_MARKET_AGREEMENT_SHRINKAGE" for reason in result.downgrades
    )


def test_sip_weight_ceiling_reason_emitted():
    policy = _policy(
        base_sip_weight=Decimal("0.900000"), sip_weight_ceiling=Decimal("0.750000")
    )
    service = ProbabilityReconciler(policy)
    evidence = (
        _evidence("quote-1", "book-1", "2026-07-26T12:05:00+00:00"),
        _evidence("quote-2", "book-2", "2026-07-26T12:05:00+00:00"),
        _evidence("quote-3", "book-3", "2026-07-26T12:05:00+00:00"),
        _evidence("quote-4", "book-4", "2026-07-26T12:05:00+00:00"),
    )
    result = service.reconcile(
        _snapshot(
            trust_factors={"model_stability": Decimal("1.000000")},
            market_dispersion=Decimal("0.000000"),
            data_quality_score=Decimal("1.000000"),
            execution_quote_timestamp="2026-07-26T12:05:00+00:00",
            complete_fresh_sportsbook_count=4,
            consensus_constituent_quote_ids=(
                "quote-1",
                "quote-2",
                "quote-3",
                "quote-4",
            ),
            source_quote_ids=("quote-1", "quote-2", "quote-3", "quote-4"),
            constituent_sportsbook_ids=("book-1", "book-2", "book-3", "book-4"),
            sportsbook_coverage=4,
        ),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=evidence,
    )
    assert any(reason.code == "PRD_SIP_WEIGHT_CEILING" for reason in result.downgrades)


def test_confidence_interval_widening_reason_emitted():
    service = _service()
    result = service.reconcile(
        _snapshot(market_dispersion=Decimal("0.080000")),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=_default_evidence(),
    )
    assert any(
        reason.code == "PRD_CONFIDENCE_INTERVAL_WIDENED" for reason in result.downgrades
    )


def test_numeric_blocked_fallback_non_execution_eligible():
    service = _service()
    result = service.reconcile(
        _snapshot(complete_fresh_sportsbook_count=1),
        as_of="2026-07-26T12:05:00+00:00",
        consensus_evidence=_default_evidence(),
    )
    assert result.reconciled_execution_probability >= Decimal("0")
    assert result.execution_eligible is False


def test_legacy_sip_adjusted_probability_no_implicit_mapping():
    payload = {
        "snapshot_id": "s",
        "league": "wnba",
        "event_id": "e",
        "market_id": "m",
        "market_type": "moneyline",
        "period": "full_game",
        "outcome_id": "o",
        "selection_id": "sel",
        "outcome_schema": "home_away",
        "event_start_time": "2026-07-26T20:00:00+00:00",
        "as_of": "2026-07-26T12:05:00+00:00",
        "raw_american_odds": -110,
        "raw_decimal_odds": "1.909091",
        "raw_implied_probability": "0.523809",
        "no_vig_probability": "0.515000",
        "cross_book_consensus_probability": "0.520000",
        "raw_sip_probability": "0.610000",
        "calibrated_sip_probability": "0.640000",
        "confidence_lower_bound": "0.510000",
        "confidence_upper_bound": "0.690000",
        "break_even_probability": "0.523809",
        "historical_prior_probability": "0.530000",
        "historical_prior_source": "prior",
        "historical_prior_version": "v1",
        "historical_prior_timestamp": "2026-07-25T00:00:00+00:00",
        "historical_prior_sample_scope": "scope",
        "historical_prior_missing": False,
        "execution_quote_id": "quote-1",
        "execution_sportsbook_id": "book-1",
        "execution_quote_timestamp": "2026-07-26T12:00:00+00:00",
        "consensus_constituent_quote_ids": ["quote-1", "quote-2"],
        "constituent_sportsbook_ids": ["book-1", "book-2"],
        "oldest_constituent_timestamp": "2026-07-26T11:58:00+00:00",
        "consensus_calculated_at": "2026-07-26T12:01:00+00:00",
        "complete_fresh_sportsbook_count": 2,
        "market_dispersion": "0.010000",
        "source_quote_set_id": "set-1",
        "quote_timestamp": "2026-07-26T12:00:00+00:00",
        "forecast_timestamp": "2026-07-26T12:01:00+00:00",
        "model_version": "model-v1",
        "calibration_version": "cal-v1",
        "reconciliation_version": "recon-v1",
        "reconciliation_policy_version": "policy-v1",
        "reconciliation_method_version": "method-v1",
        "confidence_interval_method_version": "interval-v1",
        "data_quality_score": "0.820000",
        "sportsbook_coverage": 4,
        "reconciliation_method": "weighted-shrink",
        "sip_adjusted_probability": "0.800000",
    }

    with pytest.raises(RuntimeError):
        LegacyProbabilityAdapterV1.snapshot_from_legacy_mapping(
            payload,
            compatibility_mode=False,
        )

    with pytest.raises(ValueError, match="sip_adjusted_probability"):
        LegacyProbabilityAdapterV1.snapshot_from_legacy_mapping(
            payload,
            compatibility_mode=True,
        )
