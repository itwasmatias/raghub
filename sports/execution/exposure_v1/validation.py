from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from types import MappingProxyType
from typing import Any, Sequence

from sports.execution.exposure_v1.hashing import stable_hash
from sports.execution.exposure_v1.reasons import ExposureReasonCodeV1, ExposureReasonV1
from sports.execution.exposure_v1.statuses import (
    ExposurePortfolioKindV1,
    ExposureReasonCategoryV1,
    ExposureReasonSeverityV1,
    ExposureRecordKindV1,
    ExposureRecordStateV1,
)


USD_MINOR_UNIT = Decimal("0.01")
RATIO_PRECISION = Decimal("0.000001")
SUPPORTED_SCHEMA_VERSIONS = frozenset({"v1"})
SUPPORTED_CALCULATION_INPUT_VERSIONS = frozenset({"exposure_input_v1"})
SUPPORTED_CALCULATION_VERSIONS = frozenset({"exposure_calc_v1"})
HISTORICAL_RECORD_STATES_V1 = frozenset(
    {
        ExposureRecordStateV1.SETTLED,
        ExposureRecordStateV1.REJECTED,
        ExposureRecordStateV1.CANCELED,
        ExposureRecordStateV1.VOIDED,
        ExposureRecordStateV1.CORRECTED,
        ExposureRecordStateV1.SUPERSEDED,
    }
)


@dataclass(slots=True)
class ExposureValidationError(Exception):
    reasons: tuple[ExposureReasonV1, ...]

    def __str__(self) -> str:
        return "; ".join(f"{reason.code}:{reason.message}" for reason in self.reasons)


@dataclass(frozen=True, slots=True)
class ExposureRecordIdentity:
    record_id: str
    record_hash: str
    record_kind: str
    state: str
    position_id: str | None
    order_intent_id: str | None
    reservation_id: str | None
    parlay_id: str | None
    parlay_leg_ids: tuple[str, ...]
    currency: str
    portfolio_kind: ExposurePortfolioKindV1
    effective_at: str
    as_of_eligible_at: str
    source_repository: str
    source_record_id: str
    source_revision: str
    canonical_import_identity: str
    correction_of_record_id: str | None = None
    reversal_of_record_id: str | None = None

    @classmethod
    def from_record(cls, record: Any) -> "ExposureRecordIdentity":
        return cls(
            record_id=record.record_id,
            record_hash=record.canonical_record_hash,
            record_kind=record.record_kind.value,
            state=record.state.value,
            position_id=record.position_id,
            order_intent_id=record.order_intent_id,
            reservation_id=record.reservation_id,
            parlay_id=record.parlay_id,
            parlay_leg_ids=tuple(leg.leg_id for leg in record.legs),
            currency=record.currency,
            portfolio_kind=record.portfolio_kind,
            effective_at=record.effective_at,
            as_of_eligible_at=record.as_of_eligible_at,
            source_repository=record.source_repository,
            source_record_id=record.source_record_id,
            source_revision=record.source_revision,
            canonical_import_identity=stable_hash(
                {
                    "external_source": record.canonical_sportsbook_id,
                    "account_id": record.account_id,
                    "external_record_id": record.source_record_id,
                    "external_revision": record.source_revision,
                    "canonical_event_id": record.event_id,
                    "canonical_market_id": record.market_id,
                    "canonical_selection_id": record.selection_id,
                    "canonical_outcome_id": record.outcome_id,
                }
            ),
            correction_of_record_id=record.correction_of_record_id,
            reversal_of_record_id=record.reversal_of_record_id,
        )


@dataclass(frozen=True, slots=True)
class ExposureCandidateIdentity:
    candidate_id: str
    candidate_hash: str
    position_id: str | None
    reservation_id: str | None
    record_id: str | None


@dataclass(frozen=True, slots=True)
class ExposureDomainContext:
    current_time: str
    calculation_as_of: str


def build_reason(
    *,
    code: ExposureReasonCodeV1,
    severity: ExposureReasonSeverityV1,
    category: ExposureReasonCategoryV1,
    message: str,
    metadata: dict[str, Any] | None = None,
) -> ExposureReasonV1:
    return ExposureReasonV1(
        code=code,
        severity=severity,
        category=category,
        message=message,
        metadata=metadata or {},
    )


def raise_domain_error(*reasons: ExposureReasonV1) -> None:
    ordered = tuple(sorted(reasons, key=lambda reason: reason.canonical_sort_key()))
    raise ExposureValidationError(reasons=ordered)


def require_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def normalize_currency(value: str) -> str:
    return require_text("currency", value).upper()


def require_supported_version(
    name: str,
    value: str,
    *,
    supported: frozenset[str],
) -> str:
    normalized = require_text(name, value)
    if normalized not in supported:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_UNSUPPORTED_VERSION,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message=f"unsupported {name}",
                metadata={"name": name, "value": normalized},
            )
        )
    return normalized


def require_nonnegative_decimal(name: str, value: Decimal) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    if value < Decimal("0"):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_NEGATIVE_MONETARY_VALUE,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.MONETARY,
                message=f"{name} must be nonnegative",
                metadata={"field": name, "value": format(value, "f")},
            )
        )
    return value


def normalize_monetary_decimal(name: str, value: Decimal, currency: str) -> Decimal:
    amount = require_nonnegative_decimal(name, value)
    currency_code = normalize_currency(currency)
    with localcontext() as ctx:
        ctx.prec = 28
        if currency_code == "USD":
            return amount.quantize(USD_MINOR_UNIT, rounding=ROUND_HALF_EVEN)
        return +amount


def normalize_ratio_decimal(name: str, value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal):
        raise ValueError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    with localcontext() as ctx:
        ctx.prec = 28
        return value.quantize(RATIO_PRECISION, rounding=ROUND_HALF_EVEN)


def parse_utc_timestamp(name: str, value: str) -> datetime:
    normalized_text = require_text(name, value)
    try:
        parsed = datetime.fromisoformat(normalized_text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


def normalize_utc_timestamp(name: str, value: str) -> str:
    parsed = parse_utc_timestamp(name, value)
    return parsed.isoformat()


def assert_same_currency(name: str, expected: str, actual: str) -> None:
    normalized_expected = normalize_currency(expected)
    normalized_actual = normalize_currency(actual)
    if normalized_expected != normalized_actual:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_CURRENCY_MISMATCH,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CURRENCY,
                message=f"{name} currency mismatch",
                metadata={
                    "expected": normalized_expected,
                    "actual": normalized_actual,
                },
            )
        )


def require_nonempty_tuple(
    name: str, values: tuple[str, ...] | list[str]
) -> tuple[str, ...]:
    normalized = tuple(sorted(require_text(name, item) for item in values))
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def validate_record_collection(
    records: Sequence[Any],
    *,
    as_of: str,
    current_time: str | None = None,
) -> tuple[ExposureRecordIdentity, ...]:
    from sports.execution.exposure_v1.contracts import (
        ExposureRecordV1,
        _validate_record_contract_integrity,
    )

    cutoff = parse_utc_timestamp("as_of", as_of)
    now = parse_utc_timestamp("current_time", current_time or as_of)
    reasons: list[ExposureReasonV1] = []
    unique: dict[str, ExposureRecordV1] = {}
    hashes_by_id: dict[str, str] = {}

    for raw_record in records:
        if not isinstance(raw_record, ExposureRecordV1):
            reasons.append(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_FORGED_REDUCED_RECORD_IDENTITY,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                    message="authoritative collection validation requires actual ExposureRecordV1 objects",
                    metadata={
                        "input_type": type(raw_record).__name__,
                    },
                )
            )
            continue
        try:
            _validate_record_contract_integrity(raw_record)
        except ExposureValidationError as exc:
            reasons.extend(exc.reasons)
            continue
        except (AttributeError, KeyError, TypeError, ValueError):
            reasons.append(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                    message="record hash does not match the actual canonical record payload",
                    metadata={"record_id": raw_record.record_id},
                )
            )
            continue
        previous_hash = hashes_by_id.get(raw_record.record_id)
        if previous_hash is not None:
            if previous_hash != raw_record.canonical_record_hash:
                reasons.append(
                    build_reason(
                        code=ExposureReasonCodeV1.EXP_RECORD_HASH_CONFLICT,
                        severity=ExposureReasonSeverityV1.ERROR,
                        category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                        message="duplicate record id has a different actual canonical hash",
                        metadata={"record_id": raw_record.record_id},
                    )
                )
            continue
        hashes_by_id[raw_record.record_id] = raw_record.canonical_record_hash
        unique[raw_record.record_id] = raw_record

        effective_at = parse_utc_timestamp(
            f"effective_at[{raw_record.record_id}]", raw_record.effective_at
        )
        eligible_at = parse_utc_timestamp(
            f"as_of_eligible_at[{raw_record.record_id}]",
            raw_record.as_of_eligible_at,
        )
        if effective_at > now:
            reasons.append(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_FUTURE_EFFECTIVE_AT,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.TEMPORAL,
                    message="record effective timestamp is in the future",
                    metadata={"record_id": raw_record.record_id},
                )
            )
        if effective_at > cutoff:
            reasons.append(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_RECORD_AFTER_AS_OF,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.TEMPORAL,
                    message="record effective timestamp is after as_of",
                    metadata={"record_id": raw_record.record_id},
                )
            )
        if eligible_at > cutoff:
            reasons.append(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_AS_OF_ELIGIBLE_AFTER_AS_OF,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.TEMPORAL,
                    message="record eligibility timestamp is after as_of",
                    metadata={"record_id": raw_record.record_id},
                )
            )
        if raw_record.manual_provenance is not None:
            recorded_at = parse_utc_timestamp(
                f"manual_recorded_at[{raw_record.record_id}]",
                raw_record.manual_provenance.recorded_at,
            )
            if (
                recorded_at > eligible_at
                or recorded_at > cutoff
                or recorded_at > now
            ):
                reasons.append(
                    build_reason(
                        code=ExposureReasonCodeV1.EXP_MANUAL_SOURCE_INCOMPLETE,
                        severity=ExposureReasonSeverityV1.ERROR,
                        category=ExposureReasonCategoryV1.TEMPORAL,
                        message="manual provenance must exist by record eligibility and calculation cutoff",
                        metadata={"record_id": raw_record.record_id},
                    )
                )

    seen_currencies = {record.currency for record in unique.values()}
    seen_kinds = {record.portfolio_kind for record in unique.values()}
    if len(seen_currencies) > 1:
        reasons.append(
            build_reason(
                code=ExposureReasonCodeV1.EXP_CURRENCY_MISMATCH,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CURRENCY,
                message="mixed-currency record collections are unsupported",
            )
        )
    if {
        ExposurePortfolioKindV1.CASH,
        ExposurePortfolioKindV1.SYNTHETIC,
    }.issubset(seen_kinds):
        reasons.append(
            build_reason(
                code=ExposureReasonCodeV1.EXP_CASH_SYNTHETIC_MIX,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.STATE,
                message="cash and synthetic records cannot share authoritative totals",
            )
        )
    if len(seen_kinds) > 1:
        reasons.append(
            build_reason(
                code=ExposureReasonCodeV1.EXP_PORTFOLIO_KIND_CONFLICT,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.STATE,
                message="mixed portfolio kinds cannot share one authoritative collection",
            )
        )

    parent_by_child: dict[str, str] = {}
    action_by_child: dict[str, str] = {}
    children_by_parent: dict[str, list[str]] = {}
    for record in unique.values():
        target = record.correction_of_record_id or record.reversal_of_record_id
        if not target:
            continue
        if target == record.record_id:
            reasons.append(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_CORRECTION_REVERSAL_CONFLICT,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.STATE,
                    message="record cannot self-reference lineage",
                    metadata={"record_id": record.record_id},
                )
            )
            continue
        if target not in unique:
            reasons.append(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_LINEAGE_TARGET_MISSING,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.STATE,
                    message="correction or reversal target is missing",
                    metadata={
                        "record_id": record.record_id,
                        "target_record_id": target,
                    },
                )
            )
            continue
        parent_by_child[record.record_id] = target
        action_by_child[record.record_id] = (
            "correction" if record.correction_of_record_id else "reversal"
        )
        children_by_parent.setdefault(target, []).append(record.record_id)

    cycle_found = False
    visit_state: dict[str, int] = {}

    def visit(record_id: str) -> None:
        nonlocal cycle_found
        state = visit_state.get(record_id, 0)
        if state == 1:
            cycle_found = True
            return
        if state == 2:
            return
        visit_state[record_id] = 1
        target_id = parent_by_child.get(record_id)
        if target_id is not None:
            visit(target_id)
        visit_state[record_id] = 2

    for record_id in unique:
        visit(record_id)
    if cycle_found:
        reasons.append(
            build_reason(
                code=ExposureReasonCodeV1.EXP_CORRECTION_CYCLE,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.STATE,
                message="correction and reversal lineage must be acyclic",
            )
        )

    for target_id, child_ids in children_by_parent.items():
        if len(child_ids) > 1:
            reasons.append(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_MULTIPLE_ACTIVE_LINEAGE_HEADS,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.STATE,
                    message="one lineage target cannot have competing replacements",
                    metadata={
                        "target_record_id": target_id,
                        "replacement_record_ids": tuple(sorted(child_ids)),
                    },
                )
            )

    for record_id, record in unique.items():
        if (
            record.state
            in {
                ExposureRecordStateV1.CORRECTED,
                ExposureRecordStateV1.SUPERSEDED,
            }
            and not children_by_parent.get(record_id)
        ):
            reasons.append(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_INVALID_CORRECTION_DIRECTION,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.STATE,
                    message=(
                        "corrected or superseded historical state requires "
                        "an append-only replacement child"
                    ),
                    metadata={"record_id": record_id},
                )
            )

    for child_id, target_id in parent_by_child.items():
        child = unique[child_id]
        target = unique[target_id]
        child_time = parse_utc_timestamp(
            f"effective_at[{child.record_id}]", child.effective_at
        )
        target_time = parse_utc_timestamp(
            f"effective_at[{target.record_id}]", target.effective_at
        )
        action = action_by_child[child_id]
        invalid_direction = child_time <= target_time
        if action == "reversal":
            invalid_direction = (
                invalid_direction
                or target.state != ExposureRecordStateV1.SETTLED
                or child.state != ExposureRecordStateV1.VOIDED
            )
        if invalid_direction:
            reasons.append(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_INVALID_CORRECTION_DIRECTION,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.STATE,
                    message="replacement target and child must satisfy append-only temporal and action semantics",
                    metadata={
                        "record_id": child.record_id,
                        "target_record_id": target.record_id,
                        "action": action,
                    },
                )
            )
        if not _records_preserve_lineage_identity(child, target):
            reasons.append(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_INVALID_CORRECTION_DIRECTION,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.IDENTITY,
                    message="replacement does not preserve authoritative lineage identity",
                    metadata={
                        "record_id": child.record_id,
                        "target_record_id": target.record_id,
                    },
                )
            )

    component_by_id = _lineage_components(
        tuple(unique.keys()),
        parent_by_child=parent_by_child,
    )
    _validate_reused_record_identity(
        records=unique,
        component_by_id=component_by_id,
        reasons=reasons,
    )

    blocking = tuple(
        reason
        for reason in reasons
        if reason.severity == ExposureReasonSeverityV1.ERROR
    )
    if blocking:
        raise_domain_error(*blocking)

    head_ids = tuple(
        sorted(
            record_id
            for record_id in unique
            if not children_by_parent.get(record_id)
            and unique[record_id].state not in HISTORICAL_RECORD_STATES_V1
        )
    )
    return tuple(
        ExposureRecordIdentity.from_record(unique[record_id])
        for record_id in head_ids
    )


def _records_preserve_lineage_identity(child: Any, target: Any) -> bool:
    child_manual_identity = (
        (
            child.manual_provenance.external_execution_reference,
            child.manual_provenance.sportsbook_account_reference,
        )
        if child.manual_provenance is not None
        else None
    )
    target_manual_identity = (
        (
            target.manual_provenance.external_execution_reference,
            target.manual_provenance.sportsbook_account_reference,
        )
        if target.manual_provenance is not None
        else None
    )
    return (
        child.record_kind,
        child.source_record_id,
        child.portfolio_id,
        child.account_id,
        child.portfolio_kind,
        child.currency,
        child.position_id,
        child.order_intent_id,
        child.reservation_id,
        child.candidate_projection_id,
        child.reconciliation_result_id,
        child.reconciliation_version,
        child.model_version,
        child.calibration_version,
        child.strategy_id,
        child.strategy_version,
        child.league,
        child.event_id,
        child.market_id,
        child.market_type,
        child.period,
        child.selection_id,
        child.outcome_id,
        child.team_ids,
        child.player_ids,
        child.canonical_sportsbook_id,
        child.settlement_horizon,
        child.parlay_id,
        tuple(leg.hash_payload() for leg in child.legs),
        child.correlation_group_ids,
        child_manual_identity,
    ) == (
        target.record_kind,
        target.source_record_id,
        target.portfolio_id,
        target.account_id,
        target.portfolio_kind,
        target.currency,
        target.position_id,
        target.order_intent_id,
        target.reservation_id,
        target.candidate_projection_id,
        target.reconciliation_result_id,
        target.reconciliation_version,
        target.model_version,
        target.calibration_version,
        target.strategy_id,
        target.strategy_version,
        target.league,
        target.event_id,
        target.market_id,
        target.market_type,
        target.period,
        target.selection_id,
        target.outcome_id,
        target.team_ids,
        target.player_ids,
        target.canonical_sportsbook_id,
        target.settlement_horizon,
        target.parlay_id,
        tuple(leg.hash_payload() for leg in target.legs),
        target.correlation_group_ids,
        target_manual_identity,
    )


def _lineage_components(
    record_ids: tuple[str, ...],
    *,
    parent_by_child: dict[str, str],
) -> dict[str, str]:
    adjacency: dict[str, set[str]] = {record_id: set() for record_id in record_ids}
    for child_id, parent_id in parent_by_child.items():
        adjacency[child_id].add(parent_id)
        adjacency[parent_id].add(child_id)
    component_by_id: dict[str, str] = {}
    for record_id in sorted(record_ids):
        if record_id in component_by_id:
            continue
        component_id = record_id
        pending = [record_id]
        while pending:
            current = pending.pop()
            if current in component_by_id:
                continue
            component_by_id[current] = component_id
            pending.extend(sorted(adjacency[current], reverse=True))
    return component_by_id


def _validate_reused_record_identity(
    *,
    records: dict[str, Any],
    component_by_id: dict[str, str],
    reasons: list[ExposureReasonV1],
) -> None:
    strict_groups = (
        (
            "external_revision_identity",
            ExposureReasonCodeV1.EXP_CROSS_REPOSITORY_DUPLICATE_IMPORT,
            ExposureReasonCategoryV1.SOURCE,
            _group_record_ids(records, _external_revision_identity),
        ),
        (
            "canonical_import_identity",
            ExposureReasonCodeV1.EXP_CROSS_REPOSITORY_DUPLICATE_IMPORT,
            ExposureReasonCategoryV1.SOURCE,
            _group_record_ids(
                records,
                lambda record: ExposureRecordIdentity.from_record(
                    record
                ).canonical_import_identity,
            ),
        ),
        (
            "manual_execution_revision_identity",
            ExposureReasonCodeV1.EXP_DUPLICATE_MANUAL_EXECUTION,
            ExposureReasonCategoryV1.SOURCE,
            _group_record_ids(
                records,
                _manual_execution_revision_identity,
            ),
        ),
    )
    for label, code, category, identity_groups in strict_groups:
        for identity, record_ids in identity_groups.items():
            if len(record_ids) < 2:
                continue
            reasons.append(
                build_reason(
                    code=code,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=category,
                    message=f"{label} must identify one immutable authoritative revision",
                    metadata={
                        label: identity,
                        "record_ids": tuple(sorted(record_ids)),
                    },
                )
            )

    groups: tuple[
        tuple[
            str,
            ExposureReasonCodeV1,
            ExposureReasonCategoryV1,
            dict[str, list[str]],
        ],
        ...,
    ] = (
        (
            "position_id",
            ExposureReasonCodeV1.EXP_DUPLICATE_POSITION_ID,
            ExposureReasonCategoryV1.INPUT_INTEGRITY,
            _group_record_ids(records, lambda record: record.position_id),
        ),
        (
            "reservation_id",
            ExposureReasonCodeV1.EXP_DUPLICATE_RESERVATION_ID,
            ExposureReasonCategoryV1.INPUT_INTEGRITY,
            _group_record_ids(records, lambda record: record.reservation_id),
        ),
        (
            "parlay_id",
            ExposureReasonCodeV1.EXP_DUPLICATE_PARLAY_ID,
            ExposureReasonCategoryV1.PARLAY,
            _group_record_ids(records, lambda record: record.parlay_id),
        ),
        (
            "leg_id",
            ExposureReasonCodeV1.EXP_DUPLICATE_PARLAY_LEG_ID,
            ExposureReasonCategoryV1.PARLAY,
            _group_leg_record_ids(records),
        ),
        (
            "manual_execution_identity",
            ExposureReasonCodeV1.EXP_DUPLICATE_MANUAL_EXECUTION,
            ExposureReasonCategoryV1.SOURCE,
            _group_record_ids(records, _manual_execution_identity),
        ),
    )
    for label, code, category, identity_groups in groups:
        for identity, record_ids in identity_groups.items():
            if len(record_ids) < 2:
                continue
            components = {component_by_id[record_id] for record_id in record_ids}
            if len(components) == 1:
                continue
            reasons.append(
                build_reason(
                    code=code,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=category,
                    message=f"{label} appears in unrelated authoritative record lineages",
                    metadata={
                        label: identity,
                        "record_ids": tuple(sorted(record_ids)),
                    },
                )
            )


def _group_record_ids(
    records: dict[str, Any],
    identity_getter: Any,
) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for record_id, record in records.items():
        identity = identity_getter(record)
        if identity:
            groups.setdefault(identity, []).append(record_id)
    return groups


def _manual_execution_identity(record: Any) -> str | None:
    provenance = record.manual_provenance
    if provenance is None:
        return None
    return stable_hash(
        {
            "canonical_sportsbook_id": record.canonical_sportsbook_id,
            "sportsbook_account_reference": (
                provenance.sportsbook_account_reference
            ),
            "external_execution_reference": (
                provenance.external_execution_reference
            ),
        }
    )


def _external_revision_identity(record: Any) -> str:
    return stable_hash(
        {
            "canonical_sportsbook_id": record.canonical_sportsbook_id,
            "account_id": record.account_id,
            "external_record_id": record.source_record_id,
            "external_revision": record.source_revision,
        }
    )


def _manual_execution_revision_identity(record: Any) -> str | None:
    provenance = record.manual_provenance
    if provenance is None:
        return None
    return stable_hash(
        {
            "manual_execution_identity": _manual_execution_identity(record),
            "immutable_revision_id": provenance.immutable_revision_id,
        }
    )


def _group_leg_record_ids(records: dict[str, Any]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for record_id, record in records.items():
        for leg in record.legs:
            groups.setdefault(leg.leg_id, []).append(record_id)
    return groups


def validate_candidate_collision(
    *,
    candidate_id: str,
    candidate_hash: str,
    record_identities: tuple[ExposureRecordIdentity, ...],
) -> None:
    normalized_candidate_id = require_text("candidate_id", candidate_id)
    require_text("candidate_hash", candidate_hash)
    collisions: list[ExposureReasonV1] = []
    for record in record_identities:
        if normalized_candidate_id in {
            record.record_id,
            record.position_id,
            record.order_intent_id,
            record.reservation_id,
            record.parlay_id,
            *record.parlay_leg_ids,
        }:
            collisions.append(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_CANDIDATE_COLLISION,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                    message="candidate id collides with authoritative current identity",
                    metadata={"candidate_id": normalized_candidate_id},
                )
            )
            break
    if collisions:
        raise_domain_error(*collisions)
