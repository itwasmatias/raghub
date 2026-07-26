from __future__ import annotations

from enum import StrEnum


class ExposureRecordKindV1(StrEnum):
    POSITION = "position"
    RESERVATION = "reservation"
    MANUAL_REAL_POSITION = "manual_real_position"
    PRACTICE_POSITION = "practice_position"
    SYNTHETIC_POSITION = "synthetic_position"


class ExposurePortfolioKindV1(StrEnum):
    CASH = "cash"
    RECORDED_REAL = "recorded_real"
    PRACTICE = "practice"
    SYNTHETIC = "synthetic"


class CanonicalSportsbookIdV1(StrEnum):
    DRAFTKINGS = "draftkings"
    FANDUEL = "fanduel"
    BETMGM = "betmgm"


class ExposureRecordStateV1(StrEnum):
    PROPOSED_CANDIDATE = "proposed_candidate"
    PENDING_CONFIRMATION = "pending_confirmation"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    OPEN = "open"
    PARTIALLY_SETTLED = "partially_settled"
    SETTLED = "settled"
    REJECTED = "rejected"
    CANCELED = "canceled"
    VOIDED = "voided"
    CORRECTED = "corrected"
    SUPERSEDED = "superseded"


class ExposureReasonSeverityV1(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ExposureReasonCategoryV1(StrEnum):
    INPUT_INTEGRITY = "input_integrity"
    IDENTITY = "identity"
    STATE = "state"
    CURRENCY = "currency"
    MONETARY = "monetary"
    RECONCILIATION_REFERENCE = "reconciliation_reference"
    RESERVATION = "reservation"
    PARLAY = "parlay"
    CORRELATION = "correlation"
    TEMPORAL = "temporal"
    SOURCE = "source"
    CALCULATION = "calculation"
    BANKROLL_REFERENCE = "bankroll_reference"


class ExposureAvailabilityStatusV1(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class ExposureMeasureV1(StrEnum):
    STAKE_COMMITTED = "stake_committed"
    STAKE_RESERVED = "stake_reserved"
    MAXIMUM_LOSS = "maximum_loss"
    POTENTIAL_PROFIT = "potential_profit"
    GROSS_PAYOUT = "gross_payout"
    NET_LIABILITY = "net_liability"
    CORRELATED_MAXIMUM_LOSS = "correlated_maximum_loss"


class ExposureDimensionTypeV1(StrEnum):
    PORTFOLIO = "portfolio"
    ACCOUNT = "account"
    PORTFOLIO_KIND = "portfolio_kind"
    CURRENCY = "currency"
    LEAGUE = "league"
    EVENT = "event"
    MARKET = "market"
    MARKET_TYPE = "market_type"
    PERIOD = "period"
    SELECTION = "selection"
    OUTCOME = "outcome"
    TEAM = "team"
    PLAYER = "player"
    SPORTSBOOK = "sportsbook"
    STRATEGY = "strategy"
    STRATEGY_VERSION = "strategy_version"
    MODEL_VERSION = "model_version"
    CALIBRATION_VERSION = "calibration_version"
    RECONCILIATION_VERSION = "reconciliation_version"
    SETTLEMENT_HORIZON = "settlement_horizon"
    CORRELATION_GROUP = "correlation_group"
    PARLAY = "parlay"
    PARLAY_LEG = "parlay_leg"
    POSITION_STATE = "position_state"
    SOURCE_REPOSITORY = "source_repository"


class ExposureContributionScopeV1(StrEnum):
    TOP_LEVEL_ECONOMIC = "top_level_economic"
    DIMENSION_ALLOCATION = "dimension_allocation"


class ExposureProjectionStatusV1(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    ERROR = "error"
