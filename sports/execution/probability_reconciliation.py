from __future__ import annotations

"""Authoritative Probability Reconciliation V1.

Confidence interval behavior in this Phase 1 implementation is a deterministic,
conservative heuristic (not a statistical recalibration model). It preserves the
source interval width and may widen it for freshness degradation, thin coverage,
market dispersion, and lower data quality.
"""

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sports.execution.contracts import (
    PROBABILITY_PRECISION,
    ConsensusQuoteEvidenceV1,
    DecisionReasonV1,
    ProbabilityReconciliationResultV1,
    ProbabilitySnapshotV1,
)
from sports.execution.probability_policy import ProbabilityReconciliationPolicyV1
from sports.execution.probability_validation import (
    clamp_probability,
    ensure_decimal_probability,
    parse_utc_timestamp,
    validate_snapshot_for_reconciliation,
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


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(PROBABILITY_PRECISION, rounding=ROUND_HALF_UP)


def _clamp(value: Decimal) -> Decimal:
    return _quantize(clamp_probability(value))


def _reason(
    code: str,
    severity: DecisionReasonSeverityV1,
    category: DecisionReasonCategoryV1,
    message: str,
    *,
    observed_value: str = "",
    threshold: str = "",
    source_reference: str = "",
) -> DecisionReasonV1:
    return DecisionReasonV1(
        code=code,
        severity=severity,
        category=category,
        message=message,
        observed_value=observed_value,
        threshold=threshold,
        source_reference=source_reference,
    )


def _stable_hash(payload: dict[str, object]) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _coverage_factor(complete_fresh_books: int) -> Decimal:
    if complete_fresh_books >= 4:
        return Decimal("1.000000")
    if complete_fresh_books == 3:
        return Decimal("0.650000")
    if complete_fresh_books == 2:
        return Decimal("0.350000")
    return Decimal("0.000000")


def _market_agreement_factor(
    snapshot: ProbabilitySnapshotV1, policy: ProbabilityReconciliationPolicyV1
) -> Decimal:
    if policy.dispersion_scale <= Decimal("0"):
        return Decimal("0.000000")
    return _clamp(Decimal("1") - (snapshot.market_dispersion / policy.dispersion_scale))


def _normalized_anchor_weights(
    snapshot: ProbabilitySnapshotV1,
) -> tuple[Decimal, Decimal]:
    if (
        snapshot.historical_prior_missing
        or snapshot.historical_prior_probability is None
    ):
        return Decimal("1.000000"), Decimal("0.000000")

    total = snapshot.reconciliation_weights.get(
        "consensus", Decimal("0")
    ) + snapshot.reconciliation_weights.get("prior", Decimal("0"))
    if total <= Decimal("0"):
        return Decimal("0.700000"), Decimal("0.300000")

    consensus_weight = _quantize(
        snapshot.reconciliation_weights.get("consensus", Decimal("0")) / total
    )
    prior_weight = _quantize(Decimal("1.000000") - consensus_weight)
    return consensus_weight, prior_weight


def _exact_weight_triplet(
    sip_weight: Decimal, anchor_consensus_weight: Decimal
) -> tuple[Decimal, Decimal, Decimal]:
    sip = _quantize(sip_weight)
    consensus = _quantize((Decimal("1.000000") - sip) * anchor_consensus_weight)
    prior = _quantize(Decimal("1.000000") - sip - consensus)
    return sip, consensus, prior


def _evidence_key(item: ConsensusQuoteEvidenceV1) -> tuple[str, str]:
    return (item.canonical_sportsbook_id, item.quote_id)


def _scope_mismatch_fields(
    record: ConsensusQuoteEvidenceV1,
    *,
    league: str,
    event_id: str,
    market_id: str,
    market_type: str,
    period: str,
    selection_id: str,
    outcome_id: str,
    outcome_schema: str,
) -> list[str]:
    mismatches: list[str] = []
    if record.league != league:
        mismatches.append("league")
    if record.event_id != event_id:
        mismatches.append("event_id")
    if record.market_id != market_id:
        mismatches.append("market_id")
    if record.market_type != market_type:
        mismatches.append("market_type")
    if record.period != period:
        mismatches.append("period")
    if record.selection_id != selection_id:
        mismatches.append("selection_id")
    if record.outcome_id != outcome_id:
        mismatches.append("outcome_id")
    if record.outcome_schema != outcome_schema:
        mismatches.append("outcome_schema")
    return mismatches


def _duplicate_quote_id_groups(
    evidence: tuple[ConsensusQuoteEvidenceV1, ...],
) -> dict[str, tuple[ConsensusQuoteEvidenceV1, ...]]:
    grouped: dict[str, list[ConsensusQuoteEvidenceV1]] = {}
    for record in evidence:
        grouped.setdefault(record.quote_id, []).append(record)
    duplicates: dict[str, tuple[ConsensusQuoteEvidenceV1, ...]] = {}
    for quote_id, items in grouped.items():
        if len(items) > 1:
            duplicates[quote_id] = tuple(items)
    return duplicates


def _derive_coverage(
    *,
    evidence: tuple[ConsensusQuoteEvidenceV1, ...],
    league: str,
    event_id: str,
    market_id: str,
    market_type: str,
    period: str,
    selection_id: str,
    outcome_id: str,
    outcome_schema: str,
    as_of_dt: datetime,
    max_quote_age_seconds: int,
) -> tuple[
    int,
    tuple[ConsensusQuoteEvidenceV1, ...],
    tuple[ConsensusQuoteEvidenceV1, ...],
    dict[str, str],
]:
    scoped: list[ConsensusQuoteEvidenceV1] = []
    candidates: list[ConsensusQuoteEvidenceV1] = []
    rejected_reasons: dict[str, str] = {}

    for record in evidence:
        if _scope_mismatch_fields(
            record,
            league=league,
            event_id=event_id,
            market_id=market_id,
            market_type=market_type,
            period=period,
            selection_id=selection_id,
            outcome_id=outcome_id,
            outcome_schema=outcome_schema,
        ):
            rejected_reasons.setdefault(record.quote_id, "scope_mismatch")
            continue
        scoped.append(record)

        if not record.included_in_consensus:
            rejected_reasons.setdefault(record.quote_id, "excluded_from_consensus")
            continue

        observed = parse_utc_timestamp("observed_at", record.observed_at)
        if observed > as_of_dt:
            rejected_reasons.setdefault(record.quote_id, "future_observation")
            continue
        age_seconds = int((as_of_dt - observed).total_seconds())
        if age_seconds > max_quote_age_seconds:
            rejected_reasons.setdefault(record.quote_id, "stale")
            continue
        if not record.active:
            rejected_reasons.setdefault(record.quote_id, "inactive_or_suspended")
            continue
        if not record.complete_market:
            rejected_reasons.setdefault(record.quote_id, "incomplete_market")
            continue
        candidates.append(record)

    latest_by_book: dict[str, tuple[datetime, str, ConsensusQuoteEvidenceV1]] = {}
    for record in candidates:
        observed = parse_utc_timestamp("observed_at", record.observed_at)
        candidate_key = (observed, record.quote_id)
        current = latest_by_book.get(record.canonical_sportsbook_id)
        if current is None or candidate_key > (current[0], current[1]):
            if current is not None:
                rejected_reasons[current[2].quote_id] = (
                    "superseded_by_newer_quote_same_sportsbook"
                )
            latest_by_book[record.canonical_sportsbook_id] = (
                observed,
                record.quote_id,
                record,
            )
        else:
            rejected_reasons.setdefault(
                record.quote_id,
                "superseded_by_newer_quote_same_sportsbook",
            )

    selected = tuple(
        sorted((item[2] for item in latest_by_book.values()), key=_evidence_key)
    )
    scoped_sorted = tuple(sorted(scoped, key=_evidence_key))
    return len(selected), selected, scoped_sorted, rejected_reasons


def _derive_consensus_probability(
    evidence: tuple[ConsensusQuoteEvidenceV1, ...],
) -> Decimal | None:
    if not evidence:
        return None
    total = Decimal("0")
    for row in evidence:
        total += row.no_vig_probability
    return _quantize(total / Decimal(len(evidence)))


def _authoritative_input_hash(
    snapshot: ProbabilitySnapshotV1,
    *,
    as_of: str,
    evidence: tuple[ConsensusQuoteEvidenceV1, ...],
    active_policy: ProbabilityReconciliationPolicyV1,
) -> str:
    payload = {
        "scope": {
            "league": snapshot.league,
            "event_id": snapshot.event_id,
            "market_id": snapshot.market_id,
            "market_type": snapshot.market_type,
            "period": snapshot.period,
            "outcome_id": snapshot.outcome_id,
            "selection_id": snapshot.selection_id,
            "outcome_schema": snapshot.outcome_schema,
            "event_start_time": snapshot.event_start_time,
            "as_of": as_of,
        },
        "execution_quote": {
            "execution_quote_id": snapshot.execution_quote_id,
            "execution_sportsbook_id": snapshot.execution_sportsbook_id,
            "execution_quote_timestamp": snapshot.execution_quote_timestamp,
            "source_quote_set_id": snapshot.source_quote_set_id,
        },
        "probabilities": {
            "raw_implied_probability": format(snapshot.raw_implied_probability, "f"),
            "no_vig_probability": format(snapshot.no_vig_probability, "f"),
            "cross_book_consensus_probability": (
                format(snapshot.cross_book_consensus_probability, "f")
                if snapshot.cross_book_consensus_probability is not None
                else None
            ),
            "raw_sip_probability": format(snapshot.raw_sip_probability, "f"),
            "calibrated_sip_probability": format(
                snapshot.calibrated_sip_probability, "f"
            ),
            "break_even_probability": format(snapshot.break_even_probability, "f"),
            "confidence_lower_bound": format(snapshot.confidence_lower_bound, "f"),
            "confidence_upper_bound": format(snapshot.confidence_upper_bound, "f"),
            "historical_prior_probability": (
                format(snapshot.historical_prior_probability, "f")
                if snapshot.historical_prior_probability is not None
                else None
            ),
        },
        "historical_prior_metadata": {
            "historical_prior_source": snapshot.historical_prior_source,
            "historical_prior_version": snapshot.historical_prior_version,
            "historical_prior_timestamp": snapshot.historical_prior_timestamp,
            "historical_prior_sample_scope": snapshot.historical_prior_sample_scope,
            "historical_prior_missing": snapshot.historical_prior_missing,
        },
        "quality": {
            "data_quality_score": format(snapshot.data_quality_score, "f"),
            "market_dispersion": format(snapshot.market_dispersion, "f"),
        },
        "weights": {
            key: format(value, "f")
            for key, value in sorted(snapshot.reconciliation_weights.items())
        },
        "trust_inputs": {
            key: format(value, "f")
            for key, value in sorted(snapshot.trust_factors.items())
        },
        "versions": {
            "model_version": snapshot.model_version,
            "calibration_version": snapshot.calibration_version,
            "reconciliation_policy_version": snapshot.reconciliation_policy_version,
            "reconciliation_version": snapshot.reconciliation_version,
            "reconciliation_method_version": snapshot.reconciliation_method_version,
            "confidence_interval_method_version": snapshot.confidence_interval_method_version,
            "active_policy_version": active_policy.policy_version,
            "active_reconciliation_version": active_policy.reconciliation_version,
            "active_reconciliation_method_version": active_policy.reconciliation_method_version,
            "active_confidence_interval_method_version": active_policy.confidence_interval_method_version,
        },
        "statuses": {
            "model_status": snapshot.model_status,
            "calibration_status": snapshot.calibration_status,
            "model_approval_status": snapshot.model_approval_status,
            "calibration_approval_status": snapshot.calibration_approval_status,
            "policy_approval_status": snapshot.policy_approval_status,
            "reconciliation_approval_status": snapshot.reconciliation_approval_status,
        },
        "consensus_evidence": [
            {
                "quote_id": row.quote_id,
                "canonical_sportsbook_id": row.canonical_sportsbook_id,
                "provider_id": row.provider_id,
                "league": row.league,
                "event_id": row.event_id,
                "observed_at": row.observed_at,
                "active": row.active,
                "complete_market": row.complete_market,
                "included_in_consensus": row.included_in_consensus,
                "market_id": row.market_id,
                "market_type": row.market_type,
                "selection_id": row.selection_id,
                "outcome_id": row.outcome_id,
                "period": row.period,
                "outcome_schema": row.outcome_schema,
                "no_vig_probability": format(row.no_vig_probability, "f"),
                "schema_version": row.schema_version,
            }
            for row in sorted(evidence, key=_evidence_key)
        ],
    }
    return _stable_hash(payload)


def _calculate_confidence_width(
    snapshot: ProbabilitySnapshotV1,
    *,
    data_quality_factor: Decimal,
    freshness_factor: Decimal,
    coverage_factor: Decimal,
) -> tuple[Decimal, Decimal, bool]:
    source_width = _quantize(
        snapshot.confidence_upper_bound - snapshot.confidence_lower_bound
    )
    freshness_extra = _quantize((Decimal("1") - freshness_factor) * Decimal("0.050000"))
    coverage_extra = _quantize((Decimal("1") - coverage_factor) * Decimal("0.060000"))
    dispersion_extra = _quantize(snapshot.market_dispersion * Decimal("0.500000"))
    quality_extra = _quantize(
        (Decimal("1") - data_quality_factor) * Decimal("0.050000")
    )
    widened = _quantize(
        source_width
        + freshness_extra
        + coverage_extra
        + dispersion_extra
        + quality_extra
    )
    if widened < source_width:
        widened = source_width
    return source_width, widened, widened > source_width


class ProbabilityReconciliationServiceV1:
    def __init__(self, policy: ProbabilityReconciliationPolicyV1) -> None:
        self.policy = policy

    def reconcile(
        self,
        snapshot: ProbabilitySnapshotV1,
        *,
        as_of: str,
        consensus_evidence: tuple[ConsensusQuoteEvidenceV1, ...] = (),
    ) -> ProbabilityReconciliationResultV1:
        as_of_dt = parse_utc_timestamp("as_of", as_of)
        validate_snapshot_for_reconciliation(snapshot)

        execution_quote_dt = parse_utc_timestamp(
            "execution_quote_timestamp", snapshot.execution_quote_timestamp
        )
        event_start_dt = parse_utc_timestamp(
            "event_start_time", snapshot.event_start_time
        )

        blocking: list[DecisionReasonV1] = []
        downgrades: list[DecisionReasonV1] = []
        warnings: list[DecisionReasonV1] = []
        info: list[DecisionReasonV1] = []

        if snapshot.league not in self.policy.supported_leagues:
            blocking.append(
                _reason(
                    "PRB_UNSUPPORTED_LEAGUE",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.VALIDATION,
                    "League is not supported by reconciliation policy",
                    observed_value=snapshot.league,
                )
            )
        if snapshot.market_type not in self.policy.supported_market_types:
            blocking.append(
                _reason(
                    "PRB_UNSUPPORTED_MARKET_TYPE",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.VALIDATION,
                    "Market type is not supported by reconciliation policy",
                    observed_value=snapshot.market_type,
                )
            )
        if snapshot.period not in self.policy.supported_periods:
            blocking.append(
                _reason(
                    "PRB_UNSUPPORTED_PERIOD",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.VALIDATION,
                    "Period is not supported by reconciliation policy",
                    observed_value=snapshot.period,
                )
            )
        if snapshot.outcome_schema not in self.policy.supported_outcome_schemas:
            blocking.append(
                _reason(
                    "PRB_UNSUPPORTED_OUTCOME_SCHEMA",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.VALIDATION,
                    "Outcome schema is not supported by reconciliation policy",
                    observed_value=snapshot.outcome_schema,
                )
            )

        if not consensus_evidence:
            blocking.append(
                _reason(
                    "PRB_MISSING_COVERAGE_EVIDENCE",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.COVERAGE,
                    "Consensus quote evidence is required for authoritative coverage",
                )
            )

        if snapshot.calibration_status != CalibrationStatusV1.APPROVED:
            blocking.append(
                _reason(
                    "PRB_CALIBRATION_STATUS_BLOCKED",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.CALIBRATION,
                    "Calibration status is not approved",
                    observed_value=snapshot.calibration_status,
                )
            )
        if snapshot.calibration_approval_status != CalibrationApprovalStatusV1.APPROVED:
            blocking.append(
                _reason(
                    "PRB_CALIBRATION_UNAPPROVED",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.CALIBRATION,
                    "Calibration version approval is required",
                    observed_value=snapshot.calibration_approval_status,
                )
            )
        if snapshot.model_status != ModelStatusV1.APPROVED:
            blocking.append(
                _reason(
                    "PRB_MODEL_STATUS_BLOCKED",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.MODEL,
                    "Model status is not approved for execution",
                    observed_value=snapshot.model_status,
                )
            )
        if snapshot.model_approval_status != ModelApprovalStatusV1.APPROVED:
            blocking.append(
                _reason(
                    "PRB_MODEL_UNAPPROVED",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.MODEL,
                    "Model version approval is required",
                    observed_value=snapshot.model_approval_status,
                )
            )

        if snapshot.policy_approval_status != VersionApprovalStatusV1.APPROVED:
            blocking.append(
                _reason(
                    "PRB_POLICY_UNAPPROVED",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.POLICY,
                    "Policy version approval is required",
                    observed_value=snapshot.policy_approval_status,
                )
            )
        if snapshot.reconciliation_approval_status != VersionApprovalStatusV1.APPROVED:
            blocking.append(
                _reason(
                    "PRB_RECONCILIATION_UNAPPROVED",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.RECONCILIATION,
                    "Reconciliation version approval is required",
                    observed_value=snapshot.reconciliation_approval_status,
                )
            )

        if snapshot.reconciliation_policy_version != self.policy.policy_version:
            blocking.append(
                _reason(
                    "PRB_POLICY_VERSION_MISMATCH",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.POLICY,
                    "Snapshot policy version does not match active policy",
                    observed_value=snapshot.reconciliation_policy_version,
                    threshold=self.policy.policy_version,
                )
            )
        if snapshot.reconciliation_version != self.policy.reconciliation_version:
            blocking.append(
                _reason(
                    "PRB_RECONCILIATION_VERSION_MISMATCH",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.RECONCILIATION,
                    "Snapshot reconciliation version does not match active version",
                    observed_value=snapshot.reconciliation_version,
                    threshold=self.policy.reconciliation_version,
                )
            )
        if (
            snapshot.reconciliation_method_version
            != self.policy.reconciliation_method_version
        ):
            blocking.append(
                _reason(
                    "PRB_RECONCILIATION_METHOD_VERSION_MISMATCH",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.RECONCILIATION,
                    "Snapshot reconciliation method version does not match active method",
                    observed_value=snapshot.reconciliation_method_version,
                    threshold=self.policy.reconciliation_method_version,
                )
            )
        if (
            snapshot.confidence_interval_method_version
            != self.policy.confidence_interval_method_version
        ):
            blocking.append(
                _reason(
                    "PRB_INTERVAL_METHOD_VERSION_MISMATCH",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.RECONCILIATION,
                    "Snapshot interval method version does not match active method",
                    observed_value=snapshot.confidence_interval_method_version,
                    threshold=self.policy.confidence_interval_method_version,
                )
            )

        if snapshot.model_version not in self.policy.active_model_versions:
            blocking.append(
                _reason(
                    "PRB_MODEL_VERSION_UNAPPROVED",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.MODEL,
                    "Model version is not in active approved set",
                    observed_value=snapshot.model_version,
                )
            )

        if snapshot.calibration_version not in self.policy.active_calibration_versions:
            blocking.append(
                _reason(
                    "PRB_CALIBRATION_VERSION_UNAPPROVED",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.CALIBRATION,
                    "Calibration version is not in active approved set",
                    observed_value=snapshot.calibration_version,
                )
            )

        if execution_quote_dt > as_of_dt:
            blocking.append(
                _reason(
                    "PRB_QUOTE_IN_FUTURE",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.FRESHNESS,
                    "Execution quote timestamp is in the future relative to as_of",
                    observed_value=snapshot.execution_quote_timestamp,
                    threshold=as_of,
                )
            )

        if event_start_dt <= as_of_dt:
            blocking.append(
                _reason(
                    "PRB_EVENT_ALREADY_STARTED",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.FRESHNESS,
                    "Reconciliation is blocked because event already started",
                    observed_value=snapshot.event_start_time,
                    threshold=as_of,
                )
            )

        quote_age_seconds = int(max(0, (as_of_dt - execution_quote_dt).total_seconds()))
        if quote_age_seconds > self.policy.maximum_quote_age_seconds:
            blocking.append(
                _reason(
                    "PRB_STALE_EXECUTION_QUOTE",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.FRESHNESS,
                    "Execution quote is stale beyond policy limit",
                    observed_value=str(quote_age_seconds),
                    threshold=str(self.policy.maximum_quote_age_seconds),
                )
            )

        if snapshot.data_quality_score < self.policy.hard_minimum_data_quality:
            blocking.append(
                _reason(
                    "PRB_DATA_QUALITY_BELOW_MINIMUM",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.DATA_QUALITY,
                    "Data quality is below the hard minimum",
                    observed_value=format(snapshot.data_quality_score, "f"),
                    threshold=format(self.policy.hard_minimum_data_quality, "f"),
                )
            )

        duplicate_quote_groups = _duplicate_quote_id_groups(consensus_evidence)
        if duplicate_quote_groups:
            duplicate_details = {
                quote_id: [
                    {
                        "canonical_sportsbook_id": row.canonical_sportsbook_id,
                        "provider_id": row.provider_id,
                        "league": row.league,
                        "event_id": row.event_id,
                        "market_id": row.market_id,
                        "market_type": row.market_type,
                        "selection_id": row.selection_id,
                        "outcome_id": row.outcome_id,
                        "period": row.period,
                        "outcome_schema": row.outcome_schema,
                        "observed_at": row.observed_at,
                        "active": row.active,
                        "complete_market": row.complete_market,
                        "included_in_consensus": row.included_in_consensus,
                        "no_vig_probability": format(row.no_vig_probability, "f"),
                    }
                    for row in items
                ]
                for quote_id, items in duplicate_quote_groups.items()
            }
            blocking.append(
                _reason(
                    "PRB_DUPLICATE_QUOTE_ID",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.COVERAGE,
                    "Duplicate quote_id records are not permitted in consensus evidence",
                    observed_value=str(sorted(duplicate_quote_groups.keys())),
                    threshold=json.dumps(duplicate_details, sort_keys=True),
                )
            )

        scope_mismatch_map: dict[str, list[str]] = {}
        for row in consensus_evidence:
            mismatches = _scope_mismatch_fields(
                row,
                league=snapshot.league,
                event_id=snapshot.event_id,
                market_id=snapshot.market_id,
                market_type=snapshot.market_type,
                period=snapshot.period,
                selection_id=snapshot.selection_id,
                outcome_id=snapshot.outcome_id,
                outcome_schema=snapshot.outcome_schema,
            )
            if mismatches:
                scope_mismatch_map[row.quote_id] = mismatches

        if scope_mismatch_map:
            blocking.append(
                _reason(
                    "PRB_CONSENSUS_SCOPE_MISMATCH",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.COVERAGE,
                    "Consensus evidence contains out-of-scope quote records",
                    observed_value=str(sorted(scope_mismatch_map.keys())),
                    threshold=json.dumps(scope_mismatch_map, sort_keys=True),
                )
            )

        if duplicate_quote_groups or scope_mismatch_map:
            derived_coverage_count = 0
            selected_evidence = ()
            scoped_evidence = ()
            rejected_reasons: dict[str, str] = {}
        else:
            (
                derived_coverage_count,
                selected_evidence,
                scoped_evidence,
                rejected_reasons,
            ) = _derive_coverage(
                evidence=consensus_evidence,
                league=snapshot.league,
                event_id=snapshot.event_id,
                market_id=snapshot.market_id,
                market_type=snapshot.market_type,
                period=snapshot.period,
                selection_id=snapshot.selection_id,
                outcome_id=snapshot.outcome_id,
                outcome_schema=snapshot.outcome_schema,
                as_of_dt=as_of_dt,
                max_quote_age_seconds=self.policy.maximum_quote_age_seconds,
            )

        admitted_quote_ids = {row.quote_id for row in selected_evidence}
        declared_quote_ids = set(snapshot.consensus_constituent_quote_ids)
        rejected_declared = sorted(declared_quote_ids - admitted_quote_ids)
        if declared_quote_ids != admitted_quote_ids:
            rejected_detail = {
                quote_id: rejected_reasons.get(quote_id, "not_admitted")
                for quote_id in rejected_declared
            }
            blocking.append(
                _reason(
                    "PRB_CONSENSUS_CONSTITUENT_MISMATCH",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.COVERAGE,
                    "Declared constituent quote IDs must match the admitted consensus quote set",
                    observed_value=(
                        f"declared={sorted(declared_quote_ids)} admitted={sorted(admitted_quote_ids)}"
                    ),
                    threshold=(
                        f"rejected={rejected_declared} reasons={json.dumps(rejected_detail, sort_keys=True)}"
                    ),
                )
            )

        derived_consensus_probability = _derive_consensus_probability(selected_evidence)
        submitted_consensus_probability = snapshot.cross_book_consensus_probability
        if submitted_consensus_probability is None:
            if derived_consensus_probability is None:
                blocking.append(
                    _reason(
                        "PRB_MISSING_CONSENSUS",
                        DecisionReasonSeverityV1.ERROR,
                        DecisionReasonCategoryV1.COVERAGE,
                        "Cross-book consensus probability is missing and cannot be derived from admitted evidence",
                    )
                )
        elif derived_consensus_probability is not None and (
            _quantize(submitted_consensus_probability)
            != _quantize(derived_consensus_probability)
        ):
            blocking.append(
                _reason(
                    "PRB_CONSENSUS_EVIDENCE_MISMATCH",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.COVERAGE,
                    "Submitted consensus probability does not match consensus derived from admitted quote evidence",
                    observed_value=format(submitted_consensus_probability, "f"),
                    threshold=format(derived_consensus_probability, "f"),
                )
            )

        if snapshot.complete_fresh_sportsbook_count != derived_coverage_count:
            blocking.append(
                _reason(
                    "PRB_COVERAGE_EVIDENCE_MISMATCH",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.COVERAGE,
                    "Claimed complete fresh sportsbook coverage differs from derived evidence",
                    observed_value=str(snapshot.complete_fresh_sportsbook_count),
                    threshold=str(derived_coverage_count),
                )
            )

        if derived_coverage_count < 2:
            blocking.append(
                _reason(
                    "PRB_INSUFFICIENT_COVERAGE",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.COVERAGE,
                    "At least two complete fresh distinct sportsbooks are required",
                    observed_value=str(derived_coverage_count),
                    threshold="2",
                )
            )

        if (
            snapshot.historical_prior_missing
            or snapshot.historical_prior_probability is None
        ):
            warnings.append(
                _reason(
                    "PRW_MISSING_HISTORICAL_PRIOR",
                    DecisionReasonSeverityV1.WARNING,
                    DecisionReasonCategoryV1.RECONCILIATION,
                    "Historical prior is missing; anchor falls back to market consensus",
                )
            )

        freshness_factor = _clamp(
            Decimal("1")
            - (
                Decimal(quote_age_seconds)
                / Decimal(max(1, self.policy.maximum_quote_age_seconds))
            )
        )
        data_quality_factor = _clamp(
            (snapshot.data_quality_score - self.policy.hard_minimum_data_quality)
            / (Decimal("1") - self.policy.hard_minimum_data_quality)
        )
        coverage_factor = _coverage_factor(derived_coverage_count)
        calibration_factor = Decimal("1.000000")
        stability_factor = _clamp(
            snapshot.trust_factors.get("model_stability", Decimal("1"))
        )
        market_agreement_factor = _market_agreement_factor(snapshot, self.policy)
        model_status_factor = (
            Decimal("1.000000")
            if snapshot.model_status == ModelStatusV1.APPROVED
            else Decimal("0.000000")
        )

        trust = _clamp(
            data_quality_factor
            * freshness_factor
            * coverage_factor
            * calibration_factor
            * stability_factor
            * market_agreement_factor
            * model_status_factor
        )

        if stability_factor < Decimal("1"):
            downgrades.append(
                _reason(
                    "PRD_MODEL_STABILITY_SHRINKAGE",
                    DecisionReasonSeverityV1.INFO,
                    DecisionReasonCategoryV1.MODEL,
                    "Model stability reduced trust and increased shrinkage",
                    observed_value=format(stability_factor, "f"),
                    threshold="1.000000",
                )
            )
        if market_agreement_factor < Decimal("1"):
            downgrades.append(
                _reason(
                    "PRD_MARKET_AGREEMENT_SHRINKAGE",
                    DecisionReasonSeverityV1.INFO,
                    DecisionReasonCategoryV1.RECONCILIATION,
                    "Market disagreement reduced trust and increased shrinkage",
                    observed_value=format(market_agreement_factor, "f"),
                    threshold="1.000000",
                )
            )

        base_sip = ensure_decimal_probability(
            "base_sip_weight", self.policy.base_sip_weight
        )
        effective_sip_weight = _clamp(base_sip * trust)
        if effective_sip_weight < self.policy.sip_weight_floor:
            effective_sip_weight = self.policy.quantize_probability(
                self.policy.sip_weight_floor
            )
            downgrades.append(
                _reason(
                    "PRD_SIP_WEIGHT_FLOORED",
                    DecisionReasonSeverityV1.INFO,
                    DecisionReasonCategoryV1.RECONCILIATION,
                    "SIP weight reached floor due low trust",
                )
            )
        if effective_sip_weight > self.policy.sip_weight_ceiling:
            effective_sip_weight = self.policy.quantize_probability(
                self.policy.sip_weight_ceiling
            )
            downgrades.append(
                _reason(
                    "PRD_SIP_WEIGHT_CEILING",
                    DecisionReasonSeverityV1.INFO,
                    DecisionReasonCategoryV1.RECONCILIATION,
                    "SIP weight reached policy ceiling",
                )
            )

        anchor_consensus_weight, anchor_prior_weight = _normalized_anchor_weights(
            snapshot
        )
        historical_prior = (
            snapshot.historical_prior_probability
            if snapshot.historical_prior_probability is not None
            else snapshot.no_vig_probability
        )

        authoritative_consensus_probability = (
            derived_consensus_probability
            if derived_consensus_probability is not None
            else submitted_consensus_probability
        )
        consensus_for_anchor = (
            authoritative_consensus_probability
            if authoritative_consensus_probability is not None
            else historical_prior
        )

        anchor = _clamp(
            anchor_consensus_weight * consensus_for_anchor
            + anchor_prior_weight * historical_prior
        )

        if derived_coverage_count == 2:
            downgrades.append(
                _reason(
                    "PRD_COVERAGE_STRONG_SHRINKAGE",
                    DecisionReasonSeverityV1.WARNING,
                    DecisionReasonCategoryV1.COVERAGE,
                    "Coverage at two books forces strong shrinkage toward anchor",
                )
            )
        elif derived_coverage_count == 3:
            downgrades.append(
                _reason(
                    "PRD_COVERAGE_MODERATE_SHRINKAGE",
                    DecisionReasonSeverityV1.INFO,
                    DecisionReasonCategoryV1.COVERAGE,
                    "Coverage at three books applies moderate shrinkage",
                )
            )

        if data_quality_factor < Decimal("1"):
            downgrades.append(
                _reason(
                    "PRD_DATA_QUALITY_SHRINKAGE",
                    DecisionReasonSeverityV1.INFO,
                    DecisionReasonCategoryV1.DATA_QUALITY,
                    "Lower data quality increased shrinkage toward anchor",
                    observed_value=format(data_quality_factor, "f"),
                    threshold="1.000000",
                )
            )

        if freshness_factor < Decimal("1"):
            downgrades.append(
                _reason(
                    "PRD_FRESHNESS_SHRINKAGE",
                    DecisionReasonSeverityV1.INFO,
                    DecisionReasonCategoryV1.FRESHNESS,
                    "Quote age increased shrinkage toward anchor",
                    observed_value=format(freshness_factor, "f"),
                    threshold="1.000000",
                )
            )

        reconciled_probability = _clamp(
            anchor
            + effective_sip_weight * (snapshot.calibrated_sip_probability - anchor)
        )

        source_width, output_width, widened = _calculate_confidence_width(
            snapshot,
            data_quality_factor=data_quality_factor,
            freshness_factor=freshness_factor,
            coverage_factor=coverage_factor,
        )
        if widened:
            downgrades.append(
                _reason(
                    "PRD_CONFIDENCE_INTERVAL_WIDENED",
                    DecisionReasonSeverityV1.INFO,
                    DecisionReasonCategoryV1.RECONCILIATION,
                    "Confidence interval widened conservatively",
                    observed_value=format(source_width, "f"),
                    threshold=format(output_width, "f"),
                    source_reference=self.policy.confidence_interval_method_version,
                )
            )

        lower_bound = _clamp(reconciled_probability - (output_width / Decimal("2")))
        upper_bound = _clamp(reconciled_probability + (output_width / Decimal("2")))
        lower_bound = min(lower_bound, reconciled_probability)
        upper_bound = max(upper_bound, reconciled_probability)
        lower_bound = _clamp(lower_bound)
        upper_bound = _clamp(upper_bound)

        sip_weight, consensus_weight, prior_weight = _exact_weight_triplet(
            effective_sip_weight,
            anchor_consensus_weight,
        )

        component_probabilities = {
            "calibrated_sip_probability": snapshot.calibrated_sip_probability,
            "historical_prior_probability": historical_prior,
        }
        if snapshot.cross_book_consensus_probability is not None:
            component_probabilities["cross_book_consensus_probability"] = (
                snapshot.cross_book_consensus_probability
            )

        raw_component_probabilities = {
            "raw_implied_probability": snapshot.raw_implied_probability,
            "raw_sip_probability": snapshot.raw_sip_probability,
            "calibrated_sip_probability": snapshot.calibrated_sip_probability,
            "historical_prior_probability": historical_prior,
        }
        if snapshot.cross_book_consensus_probability is not None:
            raw_component_probabilities["cross_book_consensus_probability"] = (
                snapshot.cross_book_consensus_probability
            )

        component_weights = {
            "calibrated_sip_probability": sip_weight,
            "cross_book_consensus_probability": consensus_weight,
            "historical_prior_probability": prior_weight,
        }

        trust_factors = {
            "data_quality_factor": data_quality_factor,
            "freshness_factor": freshness_factor,
            "coverage_factor": coverage_factor,
            "calibration_factor": calibration_factor,
            "stability_factor": stability_factor,
            "market_agreement_factor": market_agreement_factor,
            "model_status_factor": model_status_factor,
            "trust": trust,
        }

        computed_snapshot_hash = snapshot.compute_canonical_input_hash()
        if snapshot.canonical_input_hash != computed_snapshot_hash:
            blocking.append(
                _reason(
                    "PRB_CANONICAL_HASH_MISMATCH",
                    DecisionReasonSeverityV1.ERROR,
                    DecisionReasonCategoryV1.VALIDATION,
                    "Provided canonical input hash does not match recomputed snapshot hash",
                    observed_value=snapshot.canonical_input_hash,
                    threshold=computed_snapshot_hash,
                )
            )

        authoritative_hash = _authoritative_input_hash(
            snapshot,
            as_of=as_of,
            evidence=selected_evidence,
            active_policy=self.policy,
        )

        blocked = len(blocking) > 0
        status = (
            ProbabilityReconciliationStatusV1.STALE
            if any(item.code == "PRB_STALE_EXECUTION_QUOTE" for item in blocking)
            else ProbabilityReconciliationStatusV1.RECONCILED
        )
        if blocked and status == ProbabilityReconciliationStatusV1.RECONCILED:
            status = ProbabilityReconciliationStatusV1.REJECTED

        if blocked:
            fallback_probability = historical_prior
            if snapshot.cross_book_consensus_probability is not None:
                fallback_probability = snapshot.cross_book_consensus_probability
            if any(item.code == "PRB_MISSING_CONSENSUS" for item in blocking):
                fallback_probability = historical_prior
            reconciled_probability = _clamp(fallback_probability)
            lower_bound = _clamp(
                min(reconciled_probability, snapshot.confidence_lower_bound)
            )
            upper_bound = _clamp(
                max(reconciled_probability, snapshot.confidence_upper_bound)
            )
            component_weights = {
                "calibrated_sip_probability": Decimal("0.000000"),
                "cross_book_consensus_probability": Decimal("1.000000"),
                "historical_prior_probability": Decimal("0.000000"),
            }

        result_id_seed = (
            f"{authoritative_hash}|{self.policy.policy_version}|{self.policy.reconciliation_version}|"
            f"{self.policy.reconciliation_method_version}|{self.policy.confidence_interval_method_version}"
        )
        result_id = hashlib.sha256(result_id_seed.encode("utf-8")).hexdigest()[:24]

        return ProbabilityReconciliationResultV1(
            reconciliation_result_id=f"recon-{result_id}",
            snapshot_id=snapshot.snapshot_id,
            market_id=snapshot.market_id,
            outcome_id=snapshot.outcome_id,
            reconciled_execution_probability=reconciled_probability,
            lower_bound=lower_bound,
            upper_bound=upper_bound,
            break_even_probability=snapshot.break_even_probability,
            component_probabilities=component_probabilities,
            component_weights=component_weights,
            effective_component_weights=component_weights,
            trust_factors=trust_factors,
            blocking_violations=tuple(blocking),
            downgrades=tuple(downgrades),
            warning_reasons=tuple(warnings),
            informational_adjustments=tuple(info),
            adjustment_reasons=tuple(item.message for item in downgrades),
            warnings=tuple(item.message for item in warnings),
            raw_component_probabilities=raw_component_probabilities,
            explanation=(
                "authoritative reconciliation with conservative interval widening"
                if not blocked
                else "reconciliation blocked with numeric explanatory fallback"
            ),
            model_version=snapshot.model_version,
            calibration_version=snapshot.calibration_version,
            strategy_version=snapshot.strategy_version,
            reconciliation_version=snapshot.reconciliation_version,
            reconciliation_policy_version=snapshot.reconciliation_policy_version,
            reconciliation_method_version=snapshot.reconciliation_method_version,
            confidence_interval_method_version=snapshot.confidence_interval_method_version,
            policy_version=snapshot.policy_version,
            model_status=snapshot.model_status,
            calibration_status=snapshot.calibration_status,
            model_approval_status=snapshot.model_approval_status,
            calibration_approval_status=snapshot.calibration_approval_status,
            policy_approval_status=snapshot.policy_approval_status,
            reconciliation_approval_status=snapshot.reconciliation_approval_status,
            required_input_evidence_present=(
                authoritative_consensus_probability is not None
                and bool(selected_evidence)
            ),
            is_stale=status == ProbabilityReconciliationStatusV1.STALE,
            data_quality_score=snapshot.data_quality_score,
            quote_age_seconds=quote_age_seconds,
            sportsbook_coverage=derived_coverage_count,
            complete_fresh_sportsbook_count=derived_coverage_count,
            quote_freshness_as_of=as_of,
            as_of=as_of,
            canonical_input_hash=authoritative_hash,
            source_quote_set_id=snapshot.source_quote_set_id,
            status=status,
            created_at=as_of,
            updated_at=as_of,
        )
