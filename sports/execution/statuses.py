from __future__ import annotations

from enum import StrEnum


class ProbabilitySnapshotStatusV1(StrEnum):
    AVAILABLE = "available"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class ProbabilityReconciliationStatusV1(StrEnum):
    RECONCILED = "reconciled"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    REJECTED = "rejected"
    ERROR = "error"


class ModelStatusV1(StrEnum):
    APPROVED = "approved"
    EXPERIMENTAL = "experimental"
    SHADOW = "shadow"
    UNTRAINED = "untrained"
    UNSTABLE = "unstable"


class CalibrationStatusV1(StrEnum):
    APPROVED = "approved"
    CALIBRATION_MISSING = "calibration_missing"
    CALIBRATION_FAILED = "calibration_failed"
    CALIBRATION_STALE = "calibration_stale"


class ModelApprovalStatusV1(StrEnum):
    APPROVED = "approved"
    VERSION_UNAPPROVED = "version_unapproved"


class CalibrationApprovalStatusV1(StrEnum):
    APPROVED = "approved"
    VERSION_UNAPPROVED = "version_unapproved"


class VersionApprovalStatusV1(StrEnum):
    APPROVED = "approved"
    VERSION_UNAPPROVED = "version_unapproved"


class DecisionReasonSeverityV1(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DecisionReasonCategoryV1(StrEnum):
    DATA_QUALITY = "data_quality"
    COVERAGE = "coverage"
    FRESHNESS = "freshness"
    MODEL = "model"
    CALIBRATION = "calibration"
    POLICY = "policy"
    RECONCILIATION = "reconciliation"
    VALIDATION = "validation"


class StrategyDecisionStatusV1(StrEnum):
    ELIGIBLE = "eligible"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class ExposureSnapshotStatusV1(StrEnum):
    AVAILABLE = "available"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class RiskDecisionStatusV1(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    OVERRIDDEN = "overridden"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class SizingDecisionStatusV1(StrEnum):
    APPROVED = "approved"
    NO_POSITION = "no_position"
    CAPPED = "capped"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class OrderIntentStateV1(StrEnum):
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


ORDER_INTENT_TRANSITIONS_V1: dict[
    OrderIntentStateV1, tuple[OrderIntentStateV1, ...]
] = {
    OrderIntentStateV1.THESIS_DRAFTED: (OrderIntentStateV1.FORECAST_CREATED,),
    OrderIntentStateV1.FORECAST_CREATED: (OrderIntentStateV1.PROBABILITIES_RECONCILED,),
    OrderIntentStateV1.PROBABILITIES_RECONCILED: (OrderIntentStateV1.STRATEGY_MATCHED,),
    OrderIntentStateV1.STRATEGY_MATCHED: (OrderIntentStateV1.RISK_EVALUATED,),
    OrderIntentStateV1.RISK_EVALUATED: (OrderIntentStateV1.RISK_APPROVED,),
    OrderIntentStateV1.RISK_APPROVED: (OrderIntentStateV1.ORDER_INTENT_CREATED,),
    OrderIntentStateV1.ORDER_INTENT_CREATED: (OrderIntentStateV1.USER_CONFIRMED,),
    OrderIntentStateV1.USER_CONFIRMED: (OrderIntentStateV1.EXECUTION_SUBMITTED,),
    OrderIntentStateV1.EXECUTION_SUBMITTED: (
        OrderIntentStateV1.ACCEPTED,
        OrderIntentStateV1.REJECTED,
    ),
    OrderIntentStateV1.ACCEPTED: (OrderIntentStateV1.POSITION_OPEN,),
    OrderIntentStateV1.REJECTED: (),
    OrderIntentStateV1.POSITION_OPEN: (OrderIntentStateV1.SETTLEMENT_PENDING,),
    OrderIntentStateV1.SETTLEMENT_PENDING: (OrderIntentStateV1.SETTLED,),
    OrderIntentStateV1.SETTLED: (OrderIntentStateV1.REVIEWED,),
    OrderIntentStateV1.REVIEWED: (),
}


def order_intent_transition_allowed(
    from_state: OrderIntentStateV1,
    to_state: OrderIntentStateV1,
) -> bool:
    return to_state in ORDER_INTENT_TRANSITIONS_V1.get(from_state, ())


class ExecutionReceiptStatusV1(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ERROR = "error"


class PositionStatusV1(StrEnum):
    OPEN = "open"
    MONITORING = "monitoring"
    SETTLEMENT_PENDING = "settlement_pending"
    SETTLED = "settled"
    REVIEWED = "reviewed"
    VOIDED = "voided"
    ERROR = "error"


class SettlementResultStatusV1(StrEnum):
    PENDING = "pending"
    SETTLED = "settled"
    VOIDED = "voided"
    ERROR = "error"


class DomainEventStatusV1(StrEnum):
    RECORDED = "recorded"
    REJECTED = "rejected"
    ERROR = "error"
