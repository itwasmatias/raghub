from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from types import MappingProxyType
from typing import Any

from sports.execution.statuses import (
    CalibrationApprovalStatusV1,
    CalibrationStatusV1,
    DecisionReasonCategoryV1,
    DecisionReasonSeverityV1,
    DomainEventStatusV1,
    ExecutionReceiptStatusV1,
    ExposureSnapshotStatusV1,
    ModelApprovalStatusV1,
    ModelStatusV1,
    OrderIntentStateV1,
    PositionStatusV1,
    ProbabilityReconciliationStatusV1,
    ProbabilitySnapshotStatusV1,
    RiskDecisionStatusV1,
    SettlementResultStatusV1,
    SizingDecisionStatusV1,
    StrategyDecisionStatusV1,
    VersionApprovalStatusV1,
)


MONEY_PRECISION = Decimal("0.01")
PROBABILITY_PRECISION = Decimal("0.000001")
UTC_TIMESTAMP_EXAMPLE = "2026-07-25T00:00:00+00:00"
CONTRACT_VERSION_V1 = "v1"
SCHEMA_VERSION_V1 = "v1"


def _require_nonempty_text(name: str, value: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _parse_utc_timestamp(name: str, value: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if timestamp.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware UTC")
    if timestamp.utcoffset() != timezone.utc.utcoffset(timestamp):
        raise ValueError(f"{name} must be UTC (+00:00)")
    return timestamp.astimezone(timezone.utc)


def _normalize_probability(name: str, value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    quantized = value.quantize(PROBABILITY_PRECISION, rounding=ROUND_HALF_UP)
    if quantized < Decimal("0") or quantized > Decimal("1"):
        raise ValueError(f"{name} must be within [0, 1]")
    return quantized


def _normalize_decimal_map(
    values: Mapping[str, Decimal],
    *,
    map_name: str,
    must_sum_to_one: bool,
) -> MappingProxyType[str, Decimal]:
    normalized: dict[str, Decimal] = {}
    total = Decimal("0")
    for raw_key, raw_value in values.items():
        key = str(raw_key)
        _require_nonempty_text(f"{map_name} key", key)
        if not raw_value.is_finite():
            raise ValueError(f"{map_name}[{key}] must be finite")
        quantized = raw_value.quantize(PROBABILITY_PRECISION, rounding=ROUND_HALF_UP)
        if quantized < Decimal("0"):
            raise ValueError(f"{map_name}[{key}] must be nonnegative")
        normalized[key] = quantized
        total += quantized
    if must_sum_to_one and normalized:
        if total.quantize(PROBABILITY_PRECISION, rounding=ROUND_HALF_UP) != Decimal(
            "1.000000"
        ):
            raise ValueError(f"{map_name} must sum to one at contract precision")
    return MappingProxyType(normalized)


def _normalize_probability_map(
    values: Mapping[str, Decimal],
    *,
    map_name: str,
) -> MappingProxyType[str, Decimal]:
    normalized: dict[str, Decimal] = {}
    for raw_key, raw_value in values.items():
        key = str(raw_key)
        _require_nonempty_text(f"{map_name} key", key)
        probability = _normalize_probability(f"{map_name}[{key}]", raw_value)
        if probability is None:
            raise ValueError(f"{map_name}[{key}] must be present")
        normalized[key] = probability
    return MappingProxyType(normalized)


def _odds_to_decimal(american_odds: int) -> Decimal:
    if american_odds == 0:
        raise ValueError("american odds cannot be zero")
    if american_odds > 0:
        return (Decimal("1") + Decimal(american_odds) / Decimal("100")).quantize(
            PROBABILITY_PRECISION,
            rounding=ROUND_HALF_UP,
        )
    return (Decimal("1") + Decimal("100") / abs(Decimal(american_odds))).quantize(
        PROBABILITY_PRECISION,
        rounding=ROUND_HALF_UP,
    )


def _stable_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _serialize_value(value: Any) -> Any:
    if is_dataclass(value):
        return {
            field_spec.name: _serialize_value(getattr(value, field_spec.name))
            for field_spec in fields(value)
        }
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
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
class DecisionReasonV1:
    code: str
    severity: DecisionReasonSeverityV1
    category: DecisionReasonCategoryV1
    message: str
    observed_value: str = ""
    threshold: str = ""
    source_reference: str = ""

    def __post_init__(self) -> None:
        _require_nonempty_text("code", self.code)
        _require_nonempty_text("message", self.message)


@dataclass(frozen=True, slots=True)
class ConsensusQuoteEvidenceV1:
    quote_id: str
    canonical_sportsbook_id: str
    provider_id: str
    league: str
    event_id: str
    market_id: str
    market_type: str
    selection_id: str
    outcome_id: str
    period: str
    outcome_schema: str
    observed_at: str
    active: bool
    complete_market: bool
    included_in_consensus: bool
    no_vig_probability: Decimal
    schema_version: str = SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _require_nonempty_text("quote_id", self.quote_id)
        _require_nonempty_text("canonical_sportsbook_id", self.canonical_sportsbook_id)
        _require_nonempty_text("provider_id", self.provider_id)
        _require_nonempty_text("league", self.league)
        _require_nonempty_text("event_id", self.event_id)
        _require_nonempty_text("market_id", self.market_id)
        _require_nonempty_text("market_type", self.market_type)
        _require_nonempty_text("selection_id", self.selection_id)
        _require_nonempty_text("outcome_id", self.outcome_id)
        _require_nonempty_text("period", self.period)
        _require_nonempty_text("outcome_schema", self.outcome_schema)
        _parse_utc_timestamp("observed_at", self.observed_at)
        normalized_probability = _normalize_probability(
            "no_vig_probability",
            self.no_vig_probability,
        )
        if normalized_probability is None:
            raise ValueError("no_vig_probability must be present")
        object.__setattr__(self, "no_vig_probability", normalized_probability)


@dataclass(frozen=True, slots=True)
class ProbabilitySnapshotV1:
    snapshot_id: str
    league: str
    event_id: str
    market_id: str
    market_type: str
    period: str
    outcome_id: str
    selection_id: str
    outcome_schema: str
    event_start_time: str
    as_of: str
    raw_american_odds: int
    raw_decimal_odds: Decimal
    raw_implied_probability: Decimal
    no_vig_probability: Decimal
    cross_book_consensus_probability: Decimal | None
    raw_sip_probability: Decimal
    calibrated_sip_probability: Decimal
    reconciled_execution_probability: Decimal | None
    confidence_lower_bound: Decimal
    confidence_upper_bound: Decimal
    break_even_probability: Decimal
    historical_prior_probability: Decimal | None
    historical_prior_source: str
    historical_prior_version: str
    historical_prior_timestamp: str
    historical_prior_sample_scope: str
    historical_prior_missing: bool
    execution_quote_id: str
    execution_sportsbook_id: str
    execution_quote_timestamp: str
    consensus_constituent_quote_ids: tuple[str, ...]
    constituent_sportsbook_ids: tuple[str, ...]
    oldest_constituent_timestamp: str
    consensus_calculated_at: str
    complete_fresh_sportsbook_count: int
    market_dispersion: Decimal
    canonical_input_hash: str
    source_quote_set_id: str
    quote_timestamp: str
    forecast_timestamp: str
    model_version: str
    calibration_version: str
    reconciliation_version: str
    reconciliation_policy_version: str
    reconciliation_method_version: str
    confidence_interval_method_version: str
    model_status: ModelStatusV1
    calibration_status: CalibrationStatusV1
    model_approval_status: ModelApprovalStatusV1
    calibration_approval_status: CalibrationApprovalStatusV1
    policy_approval_status: VersionApprovalStatusV1
    reconciliation_approval_status: VersionApprovalStatusV1
    data_quality_score: Decimal
    sportsbook_coverage: int
    reconciliation_method: str
    strategy_version: str = ""
    policy_version: str = ""
    reconciliation_weights: dict[str, Decimal] = field(default_factory=dict)
    trust_factors: dict[str, Decimal] = field(default_factory=dict)
    source_quote_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    status: ProbabilitySnapshotStatusV1 = ProbabilitySnapshotStatusV1.AVAILABLE
    contract_version: str = CONTRACT_VERSION_V1
    schema_version: str = SCHEMA_VERSION_V1
    created_at: str = UTC_TIMESTAMP_EXAMPLE
    updated_at: str = UTC_TIMESTAMP_EXAMPLE
    idempotency_key: str = ""
    audit_id: str = ""

    def __post_init__(self) -> None:
        for name, value in (
            ("snapshot_id", self.snapshot_id),
            ("league", self.league),
            ("event_id", self.event_id),
            ("market_id", self.market_id),
            ("market_type", self.market_type),
            ("period", self.period),
            ("outcome_id", self.outcome_id),
            ("selection_id", self.selection_id),
            ("outcome_schema", self.outcome_schema),
            ("execution_quote_id", self.execution_quote_id),
            ("execution_sportsbook_id", self.execution_sportsbook_id),
            ("model_version", self.model_version),
            ("calibration_version", self.calibration_version),
            ("reconciliation_version", self.reconciliation_version),
            ("reconciliation_policy_version", self.reconciliation_policy_version),
            ("reconciliation_method_version", self.reconciliation_method_version),
            (
                "confidence_interval_method_version",
                self.confidence_interval_method_version,
            ),
            ("reconciliation_method", self.reconciliation_method),
            ("source_quote_set_id", self.source_quote_set_id),
        ):
            _require_nonempty_text(name, value)

        as_of_dt = _parse_utc_timestamp("as_of", self.as_of)
        _parse_utc_timestamp("event_start_time", self.event_start_time)
        quote_dt = _parse_utc_timestamp("quote_timestamp", self.quote_timestamp)
        execution_quote_dt = _parse_utc_timestamp(
            "execution_quote_timestamp",
            self.execution_quote_timestamp,
        )
        forecast_dt = _parse_utc_timestamp(
            "forecast_timestamp", self.forecast_timestamp
        )
        oldest_constituent_dt = _parse_utc_timestamp(
            "oldest_constituent_timestamp",
            self.oldest_constituent_timestamp,
        )
        consensus_calculated_dt = _parse_utc_timestamp(
            "consensus_calculated_at",
            self.consensus_calculated_at,
        )
        _parse_utc_timestamp("created_at", self.created_at)
        _parse_utc_timestamp("updated_at", self.updated_at)

        if quote_dt > as_of_dt:
            raise ValueError("quote_timestamp cannot be after as_of")
        if execution_quote_dt > as_of_dt:
            raise ValueError("execution_quote_timestamp cannot be after as_of")
        if oldest_constituent_dt > as_of_dt:
            raise ValueError("oldest_constituent_timestamp cannot be after as_of")
        if consensus_calculated_dt > as_of_dt:
            raise ValueError("consensus_calculated_at cannot be after as_of")
        if forecast_dt > as_of_dt:
            raise ValueError("forecast_timestamp cannot be after as_of")

        raw_decimal_odds = self.raw_decimal_odds.quantize(
            PROBABILITY_PRECISION,
            rounding=ROUND_HALF_UP,
        )
        if raw_decimal_odds <= Decimal("1"):
            raise ValueError("raw_decimal_odds must be greater than one")
        object.__setattr__(self, "raw_decimal_odds", raw_decimal_odds)

        for field_name in (
            "raw_implied_probability",
            "no_vig_probability",
            "cross_book_consensus_probability",
            "raw_sip_probability",
            "calibrated_sip_probability",
            "confidence_lower_bound",
            "confidence_upper_bound",
            "break_even_probability",
            "historical_prior_probability",
            "reconciled_execution_probability",
            "data_quality_score",
            "market_dispersion",
        ):
            normalized = _normalize_probability(
                field_name,
                getattr(self, field_name),
            )
            object.__setattr__(self, field_name, normalized)

        if self.confidence_lower_bound > self.confidence_upper_bound:
            raise ValueError("confidence bounds must satisfy lower <= upper")
        if self.reconciled_execution_probability is not None and not (
            self.confidence_lower_bound
            <= self.reconciled_execution_probability
            <= self.confidence_upper_bound
        ):
            raise ValueError("reconciled probability must be inside confidence bounds")

        if (
            self.cross_book_consensus_probability is None
            and not self.historical_prior_missing
        ):
            # Missing consensus is allowed at contract level for pre-reconciliation evidence,
            # but prior evidence must be explicit when consensus is unavailable.
            if self.historical_prior_probability is None:
                raise ValueError(
                    "historical_prior_probability is required when consensus is missing"
                )

        implied_from_decimal = (Decimal("1") / self.raw_decimal_odds).quantize(
            PROBABILITY_PRECISION, rounding=ROUND_HALF_UP
        )
        implied_from_american = (
            Decimal("1") / _odds_to_decimal(self.raw_american_odds)
        ).quantize(PROBABILITY_PRECISION, rounding=ROUND_HALF_UP)
        tolerance = PROBABILITY_PRECISION
        if abs(implied_from_decimal - self.raw_implied_probability) > tolerance:
            raise ValueError(
                "raw_implied_probability is inconsistent with decimal odds"
            )
        if abs(implied_from_american - self.raw_implied_probability) > tolerance:
            raise ValueError(
                "raw_implied_probability is inconsistent with american odds"
            )
        if abs(implied_from_decimal - self.break_even_probability) > tolerance:
            raise ValueError("break_even_probability is inconsistent with odds")

        if self.historical_prior_missing:
            if self.historical_prior_probability is not None:
                raise ValueError(
                    "historical_prior_probability must be absent when historical_prior_missing"
                )
        else:
            if self.historical_prior_probability is None:
                raise ValueError(
                    "historical_prior_probability is required when prior is not missing"
                )
            _require_nonempty_text(
                "historical_prior_source", self.historical_prior_source
            )
            _require_nonempty_text(
                "historical_prior_version",
                self.historical_prior_version,
            )
            _parse_utc_timestamp(
                "historical_prior_timestamp", self.historical_prior_timestamp
            )
            _require_nonempty_text(
                "historical_prior_sample_scope",
                self.historical_prior_sample_scope,
            )

        if self.sportsbook_coverage < 0:
            raise ValueError("sportsbook_coverage must be nonnegative")
        if self.complete_fresh_sportsbook_count < 0:
            raise ValueError("complete_fresh_sportsbook_count must be nonnegative")

        object.__setattr__(
            self,
            "reconciliation_weights",
            _normalize_decimal_map(
                self.reconciliation_weights,
                map_name="reconciliation_weights",
                must_sum_to_one=True,
            ),
        )
        object.__setattr__(
            self,
            "trust_factors",
            _normalize_probability_map(
                self.trust_factors,
                map_name="trust_factors",
            ),
        )

        if not self.canonical_input_hash:
            object.__setattr__(
                self, "canonical_input_hash", self.compute_canonical_input_hash()
            )

    def compute_canonical_input_hash(self) -> str:
        payload = {
            "snapshot_id": self.snapshot_id,
            "league": self.league,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "market_type": self.market_type,
            "period": self.period,
            "outcome_id": self.outcome_id,
            "selection_id": self.selection_id,
            "outcome_schema": self.outcome_schema,
            "event_start_time": self.event_start_time,
            "as_of": self.as_of,
            "execution_quote_id": self.execution_quote_id,
            "execution_sportsbook_id": self.execution_sportsbook_id,
            "execution_quote_timestamp": self.execution_quote_timestamp,
            "consensus_constituent_quote_ids": sorted(
                set(self.consensus_constituent_quote_ids)
            ),
            "constituent_sportsbook_ids": sorted(set(self.constituent_sportsbook_ids)),
            "oldest_constituent_timestamp": self.oldest_constituent_timestamp,
            "consensus_calculated_at": self.consensus_calculated_at,
            "complete_fresh_sportsbook_count": self.complete_fresh_sportsbook_count,
            "source_quote_set_id": self.source_quote_set_id,
            "raw_american_odds": self.raw_american_odds,
            "raw_decimal_odds": format(self.raw_decimal_odds, "f"),
            "raw_implied_probability": format(self.raw_implied_probability, "f"),
            "no_vig_probability": format(self.no_vig_probability, "f"),
            "cross_book_consensus_probability": (
                format(self.cross_book_consensus_probability, "f")
                if self.cross_book_consensus_probability is not None
                else None
            ),
            "raw_sip_probability": format(self.raw_sip_probability, "f"),
            "calibrated_sip_probability": format(self.calibrated_sip_probability, "f"),
            "historical_prior_probability": (
                format(self.historical_prior_probability, "f")
                if self.historical_prior_probability is not None
                else None
            ),
            "historical_prior_source": self.historical_prior_source,
            "historical_prior_version": self.historical_prior_version,
            "historical_prior_timestamp": self.historical_prior_timestamp,
            "historical_prior_sample_scope": self.historical_prior_sample_scope,
            "historical_prior_missing": self.historical_prior_missing,
            "model_version": self.model_version,
            "calibration_version": self.calibration_version,
            "reconciliation_version": self.reconciliation_version,
            "reconciliation_policy_version": self.reconciliation_policy_version,
            "reconciliation_method_version": self.reconciliation_method_version,
            "confidence_interval_method_version": self.confidence_interval_method_version,
        }
        return _stable_hash(payload)


@dataclass(frozen=True, slots=True)
class ProbabilityReconciliationResultV1:
    reconciliation_result_id: str
    snapshot_id: str
    market_id: str
    outcome_id: str
    reconciled_execution_probability: Decimal
    lower_bound: Decimal
    upper_bound: Decimal
    break_even_probability: Decimal = Decimal("0")
    reconciled_edge: Decimal = Decimal("0")
    probability: Decimal | None = None
    component_probabilities: dict[str, Decimal] = field(default_factory=dict)
    component_weights: dict[str, Decimal] = field(default_factory=dict)
    effective_component_weights: dict[str, Decimal] = field(default_factory=dict)
    trust_factors: dict[str, Decimal] = field(default_factory=dict)
    adjustment_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    blocking_violations: tuple[DecisionReasonV1, ...] = ()
    downgrades: tuple[DecisionReasonV1, ...] = ()
    warning_reasons: tuple[DecisionReasonV1, ...] = ()
    informational_adjustments: tuple[DecisionReasonV1, ...] = ()
    raw_component_probabilities: dict[str, Decimal] = field(default_factory=dict)
    explanation: str = ""
    model_version: str = ""
    calibration_version: str = ""
    strategy_version: str = ""
    reconciliation_version: str = ""
    reconciliation_policy_version: str = ""
    reconciliation_method_version: str = ""
    confidence_interval_method_version: str = ""
    policy_version: str = ""
    model_status: ModelStatusV1 = ModelStatusV1.APPROVED
    calibration_status: CalibrationStatusV1 = CalibrationStatusV1.APPROVED
    model_approval_status: ModelApprovalStatusV1 = ModelApprovalStatusV1.APPROVED
    calibration_approval_status: CalibrationApprovalStatusV1 = (
        CalibrationApprovalStatusV1.APPROVED
    )
    policy_approval_status: VersionApprovalStatusV1 = VersionApprovalStatusV1.APPROVED
    reconciliation_approval_status: VersionApprovalStatusV1 = (
        VersionApprovalStatusV1.APPROVED
    )
    required_input_evidence_present: bool = True
    is_stale: bool = False
    data_quality_score: Decimal = Decimal("0")
    quote_age_seconds: int = 0
    sportsbook_coverage: int = 0
    complete_fresh_sportsbook_count: int = 0
    quote_freshness_as_of: str = UTC_TIMESTAMP_EXAMPLE
    as_of: str = UTC_TIMESTAMP_EXAMPLE
    canonical_input_hash: str = ""
    source_quote_set_id: str = ""
    status: ProbabilityReconciliationStatusV1 = (
        ProbabilityReconciliationStatusV1.RECONCILED
    )
    contract_version: str = CONTRACT_VERSION_V1
    schema_version: str = SCHEMA_VERSION_V1
    created_at: str = UTC_TIMESTAMP_EXAMPLE
    updated_at: str = UTC_TIMESTAMP_EXAMPLE
    idempotency_key: str = ""
    audit_id: str = ""

    def __post_init__(self) -> None:
        for name, value in (
            ("reconciliation_result_id", self.reconciliation_result_id),
            ("snapshot_id", self.snapshot_id),
            ("market_id", self.market_id),
            ("outcome_id", self.outcome_id),
        ):
            _require_nonempty_text(name, value)

        _parse_utc_timestamp("as_of", self.as_of)
        _parse_utc_timestamp("quote_freshness_as_of", self.quote_freshness_as_of)
        _parse_utc_timestamp("created_at", self.created_at)
        _parse_utc_timestamp("updated_at", self.updated_at)

        for field_name in (
            "reconciled_execution_probability",
            "lower_bound",
            "upper_bound",
            "break_even_probability",
            "data_quality_score",
        ):
            normalized = _normalize_probability(field_name, getattr(self, field_name))
            if normalized is None:
                raise ValueError(f"{field_name} must be present")
            object.__setattr__(self, field_name, normalized)

        if self.probability is None:
            object.__setattr__(
                self, "probability", self.reconciled_execution_probability
            )
        else:
            normalized_probability = _normalize_probability(
                "probability", self.probability
            )
            if normalized_probability is None:
                raise ValueError("probability must be present")
            object.__setattr__(self, "probability", normalized_probability)
            if self.probability != self.reconciled_execution_probability:
                raise ValueError(
                    "probability is a compatibility alias and must match "
                    "reconciled_execution_probability"
                )

        if self.lower_bound > self.upper_bound:
            raise ValueError("lower_bound must be <= upper_bound")
        if not (
            self.lower_bound
            <= self.reconciled_execution_probability
            <= self.upper_bound
        ):
            raise ValueError("reconciled_execution_probability must be inside bounds")

        expected_edge = (
            self.reconciled_execution_probability - self.break_even_probability
        ).quantize(PROBABILITY_PRECISION, rounding=ROUND_HALF_UP)
        object.__setattr__(self, "reconciled_edge", expected_edge)

        object.__setattr__(
            self,
            "component_probabilities",
            _normalize_probability_map(
                self.component_probabilities,
                map_name="component_probabilities",
            ),
        )
        object.__setattr__(
            self,
            "raw_component_probabilities",
            _normalize_probability_map(
                self.raw_component_probabilities,
                map_name="raw_component_probabilities",
            ),
        )

        normalized_weights = _normalize_decimal_map(
            self.component_weights,
            map_name="component_weights",
            must_sum_to_one=self.status == ProbabilityReconciliationStatusV1.RECONCILED,
        )
        object.__setattr__(self, "component_weights", normalized_weights)

        if self.effective_component_weights:
            object.__setattr__(
                self,
                "effective_component_weights",
                _normalize_decimal_map(
                    self.effective_component_weights,
                    map_name="effective_component_weights",
                    must_sum_to_one=True,
                ),
            )
        else:
            object.__setattr__(self, "effective_component_weights", normalized_weights)

        object.__setattr__(
            self,
            "trust_factors",
            _normalize_probability_map(
                self.trust_factors,
                map_name="trust_factors",
            ),
        )

        if self.quote_age_seconds < 0:
            raise ValueError("quote_age_seconds must be nonnegative")
        if self.sportsbook_coverage < 0:
            raise ValueError("sportsbook_coverage must be nonnegative")
        if self.complete_fresh_sportsbook_count < 0:
            raise ValueError("complete_fresh_sportsbook_count must be nonnegative")

        if self.status == ProbabilityReconciliationStatusV1.RECONCILED:
            for name, value in (
                ("model_version", self.model_version),
                ("calibration_version", self.calibration_version),
                ("reconciliation_version", self.reconciliation_version),
                ("reconciliation_policy_version", self.reconciliation_policy_version),
                ("reconciliation_method_version", self.reconciliation_method_version),
                (
                    "confidence_interval_method_version",
                    self.confidence_interval_method_version,
                ),
            ):
                _require_nonempty_text(name, value)

    @property
    def execution_eligible(self) -> bool:
        return (
            self.status == ProbabilityReconciliationStatusV1.RECONCILED
            and not self.blocking_violations
            and self.policy_approval_status == VersionApprovalStatusV1.APPROVED
            and self.reconciliation_approval_status == VersionApprovalStatusV1.APPROVED
            and self.model_approval_status == ModelApprovalStatusV1.APPROVED
            and self.calibration_approval_status == CalibrationApprovalStatusV1.APPROVED
            and self.model_status == ModelStatusV1.APPROVED
            and self.calibration_status == CalibrationStatusV1.APPROVED
            and self.required_input_evidence_present
            and not self.is_stale
        )


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
