from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4


MONEY_QUANTUM = Decimal("0.01")
PROBABILITY_QUANTUM = Decimal("0.0001")


class LedgerEntryType(StrEnum):
    CREDIT = "credit"
    DEBIT = "debit"
    HOLD = "hold"
    RELEASE = "release"
    SETTLEMENT = "settlement"
    ADJUSTMENT = "adjustment"


class WagerKind(StrEnum):
    SINGLE = "single"
    PARLAY = "parlay"


class WagerStatus(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    ACCEPTED = "accepted"
    SETTLED = "settled"
    VOIDED = "voided"
    REJECTED = "rejected"


class SettlementOutcome(StrEnum):
    WIN = "win"
    LOSS = "loss"
    PUSH = "push"
    VOID = "void"


class ValidationCode(StrEnum):
    STAKE_NON_POSITIVE = "stake_non_positive"
    ODDS_ZERO = "odds_zero"
    ODDS_NOT_INTEGER = "odds_not_integer"
    PROBABILITY_OUT_OF_RANGE = "probability_out_of_range"
    PARLAY_NEEDS_TWO_LEGS = "parlay_needs_two_legs"
    LEG_EVENT_MISSING = "leg_event_missing"
    IDEMPOTENCY_KEY_MISSING = "idempotency_key_missing"


@dataclass(frozen=True, slots=True)
class BankrollAccount:
    account_id: str
    currency: str
    balance_usd: Decimal
    available_usd: Decimal
    held_usd: Decimal
    status: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    entry_id: str
    account_id: str
    entry_type: LedgerEntryType
    amount_usd: Decimal
    balance_after_usd: Decimal
    available_after_usd: Decimal
    held_after_usd: Decimal
    reference_kind: str
    reference_id: str
    idempotency_key: str
    created_at: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class WagerLeg:
    leg_id: str
    canonical_event_id: str
    market: str
    period: str
    selection: str
    sportsbook: str
    american_odds: int
    model_probability: Decimal | None = None
    market_probability: Decimal | None = None
    correlation_group: str | None = None


@dataclass(frozen=True, slots=True)
class WagerTicket:
    wager_id: str
    account_id: str
    kind: WagerKind
    status: WagerStatus
    stake_usd: Decimal
    potential_profit_usd: Decimal
    potential_payout_usd: Decimal
    execution_mode: str
    idempotency_key: str
    placed_at: str
    updated_at: str
    legs: tuple[WagerLeg, ...]
    validation_notes: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SettlementRecord:
    settlement_id: str
    wager_id: str
    outcome: SettlementOutcome
    payout_usd: Decimal
    profit_usd: Decimal
    settled_at: str
    source: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class WagerValidationRules:
    minimum_stake_usd: Decimal = Decimal("1.00")
    maximum_stake_usd: Decimal = Decimal("10000.00")
    require_idempotency: bool = True


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: ValidationCode
    detail: str


class WagerValidationError(ValueError):
    def __init__(self, issues: list[ValidationIssue]) -> None:
        self.issues = issues
        super().__init__("; ".join(item.detail for item in issues))


class SportsbookExecutionGateway(Protocol):
    def place_wager(self, ticket: WagerTicket) -> dict[str, Any]: ...


class DisabledExecutionGateway:
    """Safety default: execution is unavailable until controls are proven."""

    def place_wager(self, ticket: WagerTicket) -> dict[str, Any]:
        raise RuntimeError(
            "Real-money sportsbook execution is disabled until ledger, validation, "
            "idempotency, settlement, and audit controls are proven."
        )


class MarketStatus(StrEnum):
    UPCOMING = "upcoming"
    LIVE = "live"
    SUSPENDED = "suspended"
    OPEN = "open"
    CLOSED = "closed"
    SETTLED = "settled"
    VOID = "void"


class OutcomeStatus(StrEnum):
    UPCOMING = "upcoming"
    LIVE = "live"
    SUSPENDED = "suspended"
    OPEN = "open"
    CLOSED = "closed"
    SETTLED = "settled"


class OutcomeResult(StrEnum):
    PENDING = "pending"
    WIN = "win"
    LOSS = "loss"
    PUSH = "push"
    VOID = "void"


class PositionMode(StrEnum):
    PRACTICE = "practice"
    RECORDED_REAL = "recorded_real"


class PositionStatus(StrEnum):
    OPEN = "open"
    SETTLED = "settled"
    VOID = "void"


class DataQualityStatus(StrEnum):
    VERIFIED = "verified"
    DEGRADED = "degraded"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class AggregateType(StrEnum):
    EVENT = "event"
    MARKET = "market"
    QUOTE = "quote"
    FORECAST = "forecast"
    BET = "bet"
    PARLAY = "parlay"
    POSITION = "position"
    LEDGER = "ledger"
    SETTLEMENT = "settlement"
    EXPOSURE = "exposure"


class ActorType(StrEnum):
    SYSTEM = "system"
    USER = "user"
    MODEL = "model"
    OPERATOR = "operator"


class SourceType(StrEnum):
    SPORTSBOOK = "sportsbook"
    MODEL_PIPELINE = "model_pipeline"
    MANUAL_ENTRY = "manual_entry"
    SETTLEMENT_FEED = "settlement_feed"
    INTERNAL = "internal"


class MarketTargetType(StrEnum):
    EVENT = "event"
    PLAYER_PROP = "player_prop"
    TEAM_PROP = "team_prop"


class LedgerTransactionType(StrEnum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    POSITION_CREATE = "position_create"
    STAKE_RESERVATION = "stake_reservation"
    SETTLEMENT = "settlement"
    PROFIT_CREDIT = "profit_credit"
    LOSS = "loss"
    PUSH = "push"
    VOID = "void"
    REVERSAL = "reversal"
    ADJUSTMENT = "adjustment"


class BetStatus(StrEnum):
    RECORDED = "recorded"
    SETTLED = "settled"
    VOID = "void"


class ParlayStatus(StrEnum):
    OPEN = "open"
    SETTLED = "settled"
    VOID = "void"


class OrderValidationCode(StrEnum):
    OUTCOME_REQUIRED = "outcome_required"
    MODE_UNSUPPORTED = "mode_unsupported"
    STAKE_INVALID = "stake_invalid"
    REQUESTED_ODDS_INVALID = "requested_odds_invalid"
    ACCEPTED_ODDS_INVALID = "accepted_odds_invalid"
    BANKROLL_INSUFFICIENT = "bankroll_insufficient"
    MARKET_NOT_OPEN = "market_not_open"
    PARLAY_NEEDS_MULTIPLE_MARKETS = "parlay_needs_multiple_markets"


@dataclass(frozen=True, slots=True)
class Market:
    id: str
    canonical_event_id: str
    league: str
    market_type: str
    period: str
    title: str
    description: str
    status: MarketStatus
    opens_at: str
    closes_at: str
    settlement_rule: str
    resolution_source: str
    created_at: str
    updated_at: str
    sport: str = "basketball"
    target_type: MarketTargetType = MarketTargetType.EVENT
    provider: str = "sip-normalized"
    home_team: str | None = None
    away_team: str | None = None
    player_id: str | None = None
    player_name: str | None = None
    player_team_id: str | None = None
    prop_statistic: str | None = None
    prop_threshold: Decimal | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Event:
    id: str
    sport: str
    league: str
    starts_at: str
    home_team: str
    away_team: str
    status: MarketStatus
    provider: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Outcome:
    id: str
    market_id: str
    name: str
    selection_key: str
    team_id: str | None
    status: OutcomeStatus
    result: OutcomeResult
    player_id: str | None = None
    player_name: str | None = None
    statistic: str | None = None
    threshold: Decimal | None = None
    direction: str | None = None


@dataclass(frozen=True, slots=True)
class Quote:
    id: str
    market_id: str
    outcome_id: str
    provider: str
    sportsbook: str
    american_odds: int
    decimal_odds: Decimal
    implied_probability: Decimal
    observed_at: str
    is_best_price: bool
    is_stale: bool
    quote_status: DataQualityStatus = DataQualityStatus.VERIFIED


@dataclass(frozen=True, slots=True)
class ProbabilitySnapshot:
    id: str
    market_id: str
    outcome_id: str
    provider: str
    sportsbook: str
    american_odds: int
    decimal_odds: Decimal
    raw_implied_probability: Decimal
    no_vig_probability: Decimal
    sportsbook_consensus_probability: Decimal
    sip_adjusted_probability: Decimal
    edge: Decimal
    expected_value: Decimal
    quote_timestamp: str
    forecast_timestamp: str
    model_version: str
    data_quality_status: DataQualityStatus


@dataclass(frozen=True, slots=True)
class ModelForecast:
    id: str
    market_id: str
    outcome_id: str
    probability: Decimal
    generated_at: str
    model_version: str
    feature_version: str
    data_quality_status: DataQualityStatus
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OrderValidationResult:
    is_valid: bool
    issues: tuple[ValidationIssue, ...]


@dataclass(frozen=True, slots=True)
class OrderIntent:
    id: str
    account_id: str
    market_id: str
    outcome_id: str
    stake: Decimal
    requested_odds: int
    accepted_odds: int
    mode: PositionMode
    estimated_payout: Decimal
    exposure_impact: dict[str, Decimal]
    validation_result: OrderValidationResult
    created_at: str


@dataclass(frozen=True, slots=True)
class BetLeg:
    id: str
    bet_id: str
    market_id: str
    outcome_id: str
    accepted_odds: int
    sequence: int


@dataclass(frozen=True, slots=True)
class Bet:
    id: str
    account_id: str
    market_id: str
    outcome_id: str
    mode: PositionMode
    stake: Decimal
    accepted_odds: int
    status: BetStatus
    created_at: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class Parlay:
    id: str
    account_id: str
    mode: PositionMode
    stake: Decimal
    combined_decimal_odds: Decimal
    naive_implied_probability: Decimal
    estimated_sip_probability: Decimal | None
    potential_return: Decimal
    expected_value: Decimal | None
    correlation_warning: str | None
    status: ParlayStatus
    created_at: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class Position:
    id: str
    account_id: str
    bet_id: str
    market_id: str
    outcome_id: str
    mode: PositionMode
    stake: Decimal
    average_accepted_odds: int
    entry_model_probability: Decimal | None
    current_model_probability: Decimal | None
    entry_market_probability: Decimal | None
    current_market_probability: Decimal | None
    potential_profit: Decimal
    potential_return: Decimal
    estimated_current_value: Decimal | None
    realized_profit_loss: Decimal
    status: PositionStatus
    opened_at: str
    settled_at: str | None
    edge_at_entry: Decimal | None = None
    current_edge: Decimal | None = None
    entry_odds: int | None = None
    current_odds: int | None = None
    market_type: str | None = None
    league: str | None = None
    event_date: str | None = None
    settlement_horizon: str | None = None


@dataclass(frozen=True, slots=True)
class SyntheticPositionValuation:
    id: str
    position_id: str
    entry_probability: Decimal
    synthetic_shares: Decimal
    current_market_probability: Decimal
    current_sip_probability: Decimal
    estimated_market_value: Decimal
    estimated_sip_value: Decimal
    estimated_change_since_entry_market: Decimal
    estimated_change_since_entry_sip: Decimal
    valuation_timestamp: str


@dataclass(frozen=True, slots=True)
class LedgerTransaction:
    id: str
    account_id: str
    position_id: str | None
    transaction_type: LedgerTransactionType
    amount: Decimal
    balance_after: Decimal
    reserved_after: Decimal
    available_after: Decimal
    note: str
    created_at: str
    reference_id: str | None = None


@dataclass(frozen=True, slots=True)
class Settlement:
    id: str
    position_id: str
    market_id: str
    outcome_id: str
    result: OutcomeResult
    payout: Decimal
    realized_profit_loss: Decimal
    settled_at: str
    resolution_source: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class ExposureSnapshot:
    id: str
    account_id: str
    as_of: str
    exposure_by_league: dict[str, Decimal]
    exposure_by_team: dict[str, Decimal]
    exposure_by_player: dict[str, Decimal]
    exposure_by_event: dict[str, Decimal]
    exposure_by_market: dict[str, Decimal]
    exposure_by_market_type: dict[str, Decimal]
    exposure_by_sportsbook: dict[str, Decimal]
    exposure_by_outcome: dict[str, Decimal]
    exposure_by_date: dict[str, Decimal]
    exposure_by_settlement_horizon: dict[str, Decimal]


@dataclass(frozen=True, slots=True)
class DomainEvent:
    id: str
    aggregate_type: AggregateType
    aggregate_id: str
    event_type: str
    event_version: int
    occurred_at: str
    recorded_at: str
    actor_type: ActorType
    actor_id: str
    correlation_id: str
    causation_id: str | None
    payload: dict[str, Any]
    payload_hash: str
    previous_event_hash: str | None
    source: SourceType
    model_version: str | None
    schema_version: str


class OrderIntentValidationEngine:
    def __init__(
        self,
        *,
        minimum_stake: Decimal = Decimal("1.00"),
        maximum_stake: Decimal = Decimal("10000.00"),
    ) -> None:
        self.minimum_stake = minimum_stake
        self.maximum_stake = maximum_stake

    def validate(
        self,
        *,
        outcome_id: str,
        stake: Decimal,
        requested_odds: int,
        accepted_odds: int,
        mode: str,
        market_status: str,
        available_bankroll: Decimal,
    ) -> OrderValidationResult:
        issues: list[ValidationIssue] = []

        if not str(outcome_id).strip():
            issues.append(
                ValidationIssue(
                    ValidationCode.LEG_EVENT_MISSING,
                    OrderValidationCode.OUTCOME_REQUIRED.value,
                )
            )

        if mode not in {PositionMode.PRACTICE.value, PositionMode.RECORDED_REAL.value}:
            issues.append(
                ValidationIssue(
                    ValidationCode.IDEMPOTENCY_KEY_MISSING,
                    OrderValidationCode.MODE_UNSUPPORTED.value,
                )
            )

        if stake <= 0 or stake < self.minimum_stake or stake > self.maximum_stake:
            issues.append(
                ValidationIssue(
                    ValidationCode.STAKE_NON_POSITIVE,
                    OrderValidationCode.STAKE_INVALID.value,
                )
            )

        if requested_odds == 0:
            issues.append(
                ValidationIssue(
                    ValidationCode.ODDS_ZERO,
                    OrderValidationCode.REQUESTED_ODDS_INVALID.value,
                )
            )
        if accepted_odds == 0:
            issues.append(
                ValidationIssue(
                    ValidationCode.ODDS_ZERO,
                    OrderValidationCode.ACCEPTED_ODDS_INVALID.value,
                )
            )

        allowed_market_statuses = {
            MarketStatus.UPCOMING.value,
            MarketStatus.OPEN.value,
            MarketStatus.LIVE.value,
        }
        if market_status not in allowed_market_statuses:
            issues.append(
                ValidationIssue(
                    ValidationCode.LEG_EVENT_MISSING,
                    OrderValidationCode.MARKET_NOT_OPEN.value,
                )
            )

        if available_bankroll < stake:
            issues.append(
                ValidationIssue(
                    ValidationCode.STAKE_NON_POSITIVE,
                    OrderValidationCode.BANKROLL_INSUFFICIENT.value,
                )
            )

        return OrderValidationResult(is_valid=not issues, issues=tuple(issues))


def no_vig_probabilities_from_raw(values: list[Decimal]) -> list[Decimal]:
    if not values:
        return []
    total = sum(values, Decimal("0"))
    if total <= 0:
        return [Decimal("0") for _ in values]
    return [
        (value / total).quantize(PROBABILITY_QUANTUM, rounding=ROUND_HALF_UP)
        for value in values
    ]


def expected_value_per_dollar(
    probability_value: Decimal, decimal_odds: Decimal
) -> Decimal:
    payout = probability_value * decimal_odds
    return (payout - Decimal("1")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def compute_synthetic_shares(stake: Decimal, entry_probability: Decimal) -> Decimal:
    if entry_probability <= 0:
        raise ValueError("entry_probability must be greater than zero")
    return (stake / entry_probability).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )


def estimate_synthetic_value(shares: Decimal, current_probability: Decimal) -> Decimal:
    return (shares * current_probability).quantize(
        MONEY_QUANTUM, rounding=ROUND_HALF_UP
    )


def american_to_decimal(american_odds: int) -> Decimal:
    if american_odds == 0:
        raise ValueError("american odds cannot be zero")
    if american_odds > 0:
        value = Decimal("1") + Decimal(american_odds) / Decimal("100")
    else:
        value = Decimal("1") + Decimal("100") / Decimal(abs(american_odds))
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def implied_probability_from_american(american_odds: int) -> Decimal:
    if american_odds == 0:
        raise ValueError("american odds cannot be zero")
    if american_odds > 0:
        value = Decimal("100") / (Decimal("100") + Decimal(american_odds))
    else:
        value = Decimal(abs(american_odds)) / (
            Decimal(abs(american_odds)) + Decimal("100")
        )
    return value.quantize(PROBABILITY_QUANTUM, rounding=ROUND_HALF_UP)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def money(value: Decimal | str | int | float) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("money amount must be numeric") from error
    return parsed.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def probability(value: Decimal | str | int | float | None) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("probability must be numeric") from error
    if parsed < 0 or parsed > 1:
        raise ValueError("probability must be between 0 and 1")
    return parsed.quantize(PROBABILITY_QUANTUM, rounding=ROUND_HALF_UP)


def potential_profit_for_american_odds(
    stake_usd: Decimal, american_odds: int
) -> Decimal:
    if american_odds == 0:
        raise ValueError("american odds cannot be zero")
    if american_odds > 0:
        result = stake_usd * Decimal(american_odds) / Decimal("100")
    else:
        result = stake_usd * Decimal("100") / Decimal(abs(american_odds))
    return result.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def validate_wager_ticket(
    ticket: WagerTicket,
    *,
    rules: WagerValidationRules | None = None,
) -> None:
    active = rules or WagerValidationRules()
    issues: list[ValidationIssue] = []

    if active.require_idempotency and not ticket.idempotency_key.strip():
        issues.append(
            ValidationIssue(
                ValidationCode.IDEMPOTENCY_KEY_MISSING,
                "idempotency_key is required for safe retry behavior",
            )
        )

    if ticket.stake_usd <= 0:
        issues.append(
            ValidationIssue(
                ValidationCode.STAKE_NON_POSITIVE,
                "stake_usd must be greater than zero",
            )
        )

    if (
        ticket.stake_usd < active.minimum_stake_usd
        or ticket.stake_usd > active.maximum_stake_usd
    ):
        issues.append(
            ValidationIssue(
                ValidationCode.STAKE_NON_POSITIVE,
                (
                    f"stake_usd must be between {active.minimum_stake_usd} and "
                    f"{active.maximum_stake_usd}"
                ),
            )
        )

    if ticket.kind == WagerKind.PARLAY and len(ticket.legs) < 2:
        issues.append(
            ValidationIssue(
                ValidationCode.PARLAY_NEEDS_TWO_LEGS,
                "parlay wagers require at least two legs",
            )
        )

    for leg in ticket.legs:
        if not leg.canonical_event_id.strip():
            issues.append(
                ValidationIssue(
                    ValidationCode.LEG_EVENT_MISSING,
                    "each leg must reference a canonical_event_id",
                )
            )
        if leg.american_odds == 0:
            issues.append(
                ValidationIssue(
                    ValidationCode.ODDS_ZERO,
                    "american odds cannot be zero",
                )
            )
        if int(leg.american_odds) != leg.american_odds:
            issues.append(
                ValidationIssue(
                    ValidationCode.ODDS_NOT_INTEGER,
                    "american odds must be an integer",
                )
            )
        if leg.model_probability is not None and not (
            Decimal("0") <= leg.model_probability <= Decimal("1")
        ):
            issues.append(
                ValidationIssue(
                    ValidationCode.PROBABILITY_OUT_OF_RANGE,
                    "model_probability must be between 0 and 1",
                )
            )
        if leg.market_probability is not None and not (
            Decimal("0") <= leg.market_probability <= Decimal("1")
        ):
            issues.append(
                ValidationIssue(
                    ValidationCode.PROBABILITY_OUT_OF_RANGE,
                    "market_probability must be between 0 and 1",
                )
            )

    if issues:
        raise WagerValidationError(issues)


def build_single_wager_ticket(
    *,
    account_id: str,
    canonical_event_id: str,
    sportsbook: str,
    selection: str,
    american_odds: int,
    stake_usd: Decimal | str | float,
    market: str = "moneyline",
    period: str = "full_game",
    model_probability: Decimal | str | float | None = None,
    market_probability: Decimal | str | float | None = None,
    idempotency_key: str | None = None,
    execution_mode: str = "paper_only",
) -> WagerTicket:
    stake = money(stake_usd)
    leg = WagerLeg(
        leg_id=f"leg-{uuid4().hex[:12]}",
        canonical_event_id=canonical_event_id,
        market=market,
        period=period,
        selection=selection,
        sportsbook=sportsbook,
        american_odds=int(american_odds),
        model_probability=probability(model_probability),
        market_probability=probability(market_probability),
    )
    profit = potential_profit_for_american_odds(stake, int(american_odds))
    now = now_iso()
    ticket = WagerTicket(
        wager_id=f"wager-{uuid4().hex[:12]}",
        account_id=account_id,
        kind=WagerKind.SINGLE,
        status=WagerStatus.VALIDATED,
        stake_usd=stake,
        potential_profit_usd=profit,
        potential_payout_usd=(stake + profit).quantize(MONEY_QUANTUM),
        execution_mode=execution_mode,
        idempotency_key=(idempotency_key or f"single:{uuid4().hex}"),
        placed_at=now,
        updated_at=now,
        legs=(leg,),
    )
    validate_wager_ticket(ticket)
    return ticket


def build_parlay_wager_ticket(
    *,
    account_id: str,
    stake_usd: Decimal | str | float,
    legs: list[WagerLeg] | tuple[WagerLeg, ...],
    idempotency_key: str | None = None,
    execution_mode: str = "paper_only",
) -> WagerTicket:
    stake = money(stake_usd)
    normalized_legs = tuple(legs)
    if not normalized_legs:
        raise ValueError("parlay requires at least one leg")

    combined_decimal = Decimal("1")
    for leg in normalized_legs:
        if leg.american_odds == 0:
            raise ValueError("american odds cannot be zero")
        if leg.american_odds > 0:
            leg_decimal = Decimal("1") + Decimal(leg.american_odds) / Decimal("100")
        else:
            leg_decimal = Decimal("1") + Decimal("100") / Decimal(
                abs(leg.american_odds)
            )
        combined_decimal *= leg_decimal

    payout = (stake * combined_decimal).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    profit = (payout - stake).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    now = now_iso()
    ticket = WagerTicket(
        wager_id=f"wager-{uuid4().hex[:12]}",
        account_id=account_id,
        kind=WagerKind.PARLAY,
        status=WagerStatus.VALIDATED,
        stake_usd=stake,
        potential_profit_usd=profit,
        potential_payout_usd=payout,
        execution_mode=execution_mode,
        idempotency_key=(idempotency_key or f"parlay:{uuid4().hex}"),
        placed_at=now,
        updated_at=now,
        legs=normalized_legs,
    )
    validate_wager_ticket(ticket)
    return ticket
