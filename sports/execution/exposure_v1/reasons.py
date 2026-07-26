from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
import math
from types import MappingProxyType
from typing import Any

from sports.execution.exposure_v1.hashing import stable_hash
from sports.execution.exposure_v1.statuses import (
    ExposureReasonCategoryV1,
    ExposureReasonSeverityV1,
)

_METADATA_TYPE_KEY = "__exposure_reason_metadata_type__"


class ExposureReasonCodeV1(StrEnum):
    EXP_DUPLICATE_RECORD_ID = "EXP_DUPLICATE_RECORD_ID"
    EXP_RECORD_HASH_CONFLICT = "EXP_RECORD_HASH_CONFLICT"
    EXP_FORGED_CANONICAL_HASH = "EXP_FORGED_CANONICAL_HASH"
    EXP_DUPLICATE_POSITION_ID = "EXP_DUPLICATE_POSITION_ID"
    EXP_DUPLICATE_RESERVATION_ID = "EXP_DUPLICATE_RESERVATION_ID"
    EXP_DUPLICATE_PARLAY_ID = "EXP_DUPLICATE_PARLAY_ID"
    EXP_DUPLICATE_PARLAY_LEG_ID = "EXP_DUPLICATE_PARLAY_LEG_ID"
    EXP_DUPLICATE_CONTRIBUTION_ID = "EXP_DUPLICATE_CONTRIBUTION_ID"
    EXP_DUPLICATE_TOP_LEVEL_MEASURE = "EXP_DUPLICATE_TOP_LEVEL_MEASURE"
    EXP_EMPTY_CANDIDATE_CONTRIBUTIONS = "EXP_EMPTY_CANDIDATE_CONTRIBUTIONS"
    EXP_INCOMPLETE_CANDIDATE_MEASURE_SET = "EXP_INCOMPLETE_CANDIDATE_MEASURE_SET"
    EXP_MISSING_CANONICAL_IDENTITY = "EXP_MISSING_CANONICAL_IDENTITY"
    EXP_RECONCILIATION_REFERENCE_MISSING = "EXP_RECONCILIATION_REFERENCE_MISSING"
    EXP_CURRENCY_MISMATCH = "EXP_CURRENCY_MISMATCH"
    EXP_NEGATIVE_MONETARY_VALUE = "EXP_NEGATIVE_MONETARY_VALUE"
    EXP_PAYOUT_INCONSISTENT = "EXP_PAYOUT_INCONSISTENT"
    EXP_STATE_CONFLICT = "EXP_STATE_CONFLICT"
    EXP_RECORD_AFTER_AS_OF = "EXP_RECORD_AFTER_AS_OF"
    EXP_FUTURE_EFFECTIVE_AT = "EXP_FUTURE_EFFECTIVE_AT"
    EXP_AS_OF_ELIGIBLE_AFTER_AS_OF = "EXP_AS_OF_ELIGIBLE_AFTER_AS_OF"
    EXP_BANKROLL_REFERENCE_INVALID = "EXP_BANKROLL_REFERENCE_INVALID"
    EXP_CASH_SYNTHETIC_MIX = "EXP_CASH_SYNTHETIC_MIX"
    EXP_PORTFOLIO_KIND_CONFLICT = "EXP_PORTFOLIO_KIND_CONFLICT"
    EXP_PROPOSED_CANDIDATE_RECORD_REJECTED = "EXP_PROPOSED_CANDIDATE_RECORD_REJECTED"
    EXP_RESERVATION_AUTHORITY_MISSING = "EXP_RESERVATION_AUTHORITY_MISSING"
    EXP_OPEN_POSITION_IDENTITY_MISSING = "EXP_OPEN_POSITION_IDENTITY_MISSING"
    EXP_CURRENT_ECONOMICS_FOR_HISTORICAL_STATE = (
        "EXP_CURRENT_ECONOMICS_FOR_HISTORICAL_STATE"
    )
    EXP_CORRECTED_RECORD_NOT_HISTORICAL = "EXP_CORRECTED_RECORD_NOT_HISTORICAL"
    EXP_BANKROLL_DENOMINATOR_ZERO = "EXP_BANKROLL_DENOMINATOR_ZERO"
    EXP_AVAILABLE_BALANCE_ZERO = "EXP_AVAILABLE_BALANCE_ZERO"
    EXP_CORRELATION_GROUP_MISSING = "EXP_CORRELATION_GROUP_MISSING"
    EXP_MANUAL_SOURCE_INCOMPLETE = "EXP_MANUAL_SOURCE_INCOMPLETE"
    EXP_SETTLEMENT_HORIZON_UNKNOWN = "EXP_SETTLEMENT_HORIZON_UNKNOWN"
    EXP_CANDIDATE_COLLISION = "EXP_CANDIDATE_COLLISION"
    EXP_CONTRIBUTION_LINEAGE_FAILURE = "EXP_CONTRIBUTION_LINEAGE_FAILURE"
    EXP_CONTRIBUTION_DUAL_LINEAGE = "EXP_CONTRIBUTION_DUAL_LINEAGE"
    EXP_PROJECTION_ARITHMETIC_MISMATCH = "EXP_PROJECTION_ARITHMETIC_MISMATCH"
    EXP_PROJECTION_SOURCE_MISMATCH = "EXP_PROJECTION_SOURCE_MISMATCH"
    EXP_PROJECTION_PORTFOLIO_MISMATCH = "EXP_PROJECTION_PORTFOLIO_MISMATCH"
    EXP_PROJECTION_VERSION_MISMATCH = "EXP_PROJECTION_VERSION_MISMATCH"
    EXP_CORRECTION_REVERSAL_CONFLICT = "EXP_CORRECTION_REVERSAL_CONFLICT"
    EXP_LINEAGE_TARGET_MISSING = "EXP_LINEAGE_TARGET_MISSING"
    EXP_CROSS_REPOSITORY_DUPLICATE_IMPORT = "EXP_CROSS_REPOSITORY_DUPLICATE_IMPORT"
    EXP_SNAPSHOT_AGGREGATE_SCOPE_MISMATCH = "EXP_SNAPSHOT_AGGREGATE_SCOPE_MISMATCH"
    EXP_INVALID_PARLAY_CARDINALITY = "EXP_INVALID_PARLAY_CARDINALITY"
    EXP_UNSUPPORTED_VERSION = "EXP_UNSUPPORTED_VERSION"
    EXP_UNSUPPORTED_CONTRIBUTION_SCHEMA_VERSION = (
        "EXP_UNSUPPORTED_CONTRIBUTION_SCHEMA_VERSION"
    )
    EXP_INVALID_DIMENSION_KEY = "EXP_INVALID_DIMENSION_KEY"
    EXP_INVALID_CONTRIBUTION_SCOPE = "EXP_INVALID_CONTRIBUTION_SCOPE"
    EXP_INVALID_CONTRIBUTION_STATE = "EXP_INVALID_CONTRIBUTION_STATE"
    EXP_PROJECTION_OBJECT_MISMATCH = "EXP_PROJECTION_OBJECT_MISMATCH"
    EXP_CANDIDATE_ECONOMICS_MISMATCH = "EXP_CANDIDATE_ECONOMICS_MISMATCH"
    EXP_CANDIDATE_PARLAY_EVIDENCE_MISMATCH = (
        "EXP_CANDIDATE_PARLAY_EVIDENCE_MISMATCH"
    )
    EXP_FORGED_REDUCED_RECORD_IDENTITY = "EXP_FORGED_REDUCED_RECORD_IDENTITY"
    EXP_INVALID_CORRECTION_DIRECTION = "EXP_INVALID_CORRECTION_DIRECTION"
    EXP_CORRECTION_CYCLE = "EXP_CORRECTION_CYCLE"
    EXP_MULTIPLE_ACTIVE_LINEAGE_HEADS = "EXP_MULTIPLE_ACTIVE_LINEAGE_HEADS"
    EXP_DUPLICATE_SNAPSHOT_RECORD_REFERENCE = (
        "EXP_DUPLICATE_SNAPSHOT_RECORD_REFERENCE"
    )
    EXP_DUPLICATE_DIMENSION_AGGREGATE = "EXP_DUPLICATE_DIMENSION_AGGREGATE"
    EXP_DUPLICATE_DIMENSION_RATIO = "EXP_DUPLICATE_DIMENSION_RATIO"
    EXP_CONTRIBUTION_SOURCE_HASH_MISMATCH = (
        "EXP_CONTRIBUTION_SOURCE_HASH_MISMATCH"
    )
    EXP_INCOMPLETE_SNAPSHOT_MEASURE_SET = (
        "EXP_INCOMPLETE_SNAPSHOT_MEASURE_SET"
    )
    EXP_SNAPSHOT_TOTALS_MISMATCH = "EXP_SNAPSHOT_TOTALS_MISMATCH"
    EXP_SNAPSHOT_COUNTS_MISMATCH = "EXP_SNAPSHOT_COUNTS_MISMATCH"
    EXP_SNAPSHOT_DIMENSION_MISMATCH = "EXP_SNAPSHOT_DIMENSION_MISMATCH"
    EXP_DUPLICATE_MANUAL_EXECUTION = "EXP_DUPLICATE_MANUAL_EXECUTION"


def _freeze_metadata(value: Any) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError(
                    "reason metadata keys must be non-empty strings"
                )
            normalized_key = key.strip()
            if normalized_key == _METADATA_TYPE_KEY:
                raise ValueError("reason metadata key is reserved")
            if normalized_key in normalized:
                raise ValueError("reason metadata keys must not collide")
            normalized[normalized_key] = _freeze_metadata(item)
        return MappingProxyType(
            dict(sorted(normalized.items(), key=lambda item: item[0]))
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_metadata(item) for item in value)
    if isinstance(value, set | frozenset):
        frozen = [_freeze_metadata(item) for item in value]
        return MappingProxyType(
            {
                _METADATA_TYPE_KEY: "set",
                "items": tuple(sorted(frozen, key=lambda item: repr(item))),
            }
        )
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("reason metadata floats must be finite")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("reason metadata decimals must be finite")
        return MappingProxyType(
            {
                _METADATA_TYPE_KEY: "decimal",
                "value": format(value, "f"),
            }
        )
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise ValueError(
        "reason metadata values must use canonical JSON-like scalar or container types"
    )


@dataclass(frozen=True, slots=True)
class ExposureReasonV1:
    code: ExposureReasonCodeV1
    severity: ExposureReasonSeverityV1
    category: ExposureReasonCategoryV1
    message: str
    metadata: MappingProxyType[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        try:
            code = ExposureReasonCodeV1(self.code)
        except (TypeError, ValueError) as exc:
            raise ValueError("code must be an ExposureReasonCodeV1") from exc
        try:
            severity = ExposureReasonSeverityV1(self.severity)
            category = ExposureReasonCategoryV1(self.category)
        except (TypeError, ValueError) as exc:
            raise ValueError("reason severity and category must use V1 enums") from exc
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("message must be a non-empty string")
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "message", self.message.strip())
        normalized_metadata = _freeze_metadata(self.metadata)
        if not isinstance(normalized_metadata, MappingProxyType):
            raise ValueError("reason metadata must be a mapping")
        object.__setattr__(self, "metadata", normalized_metadata)

    def canonical_sort_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.code.value,
            self.category.value,
            self.severity.value,
            self.message,
            stable_hash(dict(self.metadata)),
        )
