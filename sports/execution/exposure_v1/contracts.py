from __future__ import annotations

from dataclasses import InitVar, dataclass, field, fields
from decimal import Decimal, localcontext
from types import MappingProxyType
from typing import Any, Mapping

from sports.execution.exposure_v1.hashing import (
    _authoritative_hash_matches_seal,
    _seal_authoritative_hash,
    stable_hash,
)
from sports.execution.exposure_v1.reasons import ExposureReasonCodeV1, ExposureReasonV1
from sports.execution.exposure_v1.statuses import (
    CanonicalSportsbookIdV1,
    ExposureAvailabilityStatusV1,
    ExposureContributionScopeV1,
    ExposureDimensionTypeV1,
    ExposureMeasureV1,
    ExposurePortfolioKindV1,
    ExposureProjectionStatusV1,
    ExposureReasonCategoryV1,
    ExposureReasonSeverityV1,
    ExposureRecordKindV1,
    ExposureRecordStateV1,
)
from sports.execution.exposure_v1.validation import (
    ExposureRecordIdentity,
    ExposureValidationError,
    SUPPORTED_CALCULATION_INPUT_VERSIONS,
    SUPPORTED_CALCULATION_VERSIONS,
    SUPPORTED_SCHEMA_VERSIONS,
    assert_same_currency,
    build_reason,
    normalize_currency,
    normalize_monetary_decimal,
    normalize_ratio_decimal,
    normalize_utc_timestamp,
    raise_domain_error,
    require_supported_version,
    require_text,
    validate_candidate_collision,
    validate_record_collection,
)

SCHEMA_VERSION_V1 = "v1"
CALCULATION_INPUT_VERSION_V1 = "exposure_input_v1"
CALCULATION_VERSION_V1 = "exposure_calc_v1"
ZERO = Decimal("0")


def _normalize_canonical_sportsbook_id(
    value: str | CanonicalSportsbookIdV1,
) -> CanonicalSportsbookIdV1:
    try:
        return CanonicalSportsbookIdV1(require_text("canonical_sportsbook_id", value))
    except (TypeError, ValueError) as exc:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_MISSING_CANONICAL_IDENTITY,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.IDENTITY,
                message=(
                    "canonical_sportsbook_id must use the frozen V1 "
                    "sportsbook taxonomy; display labels and aliases reject"
                ),
                metadata={
                    "canonical_sportsbook_id": (
                        value if isinstance(value, str) else type(value).__name__
                    )
                },
            )
        )
        raise AssertionError("unreachable") from exc


@dataclass(frozen=True, slots=True)
class ExposureStateInclusionV1:
    state: ExposureRecordStateV1
    committed: bool
    reserved: bool
    projected: bool
    historical_only: bool


STATE_INCLUSION_MATRIX_V1: tuple[ExposureStateInclusionV1, ...] = (
    ExposureStateInclusionV1(
        ExposureRecordStateV1.PROPOSED_CANDIDATE, False, False, True, False
    ),
    ExposureStateInclusionV1(
        ExposureRecordStateV1.PENDING_CONFIRMATION, False, False, False, False
    ),
    ExposureStateInclusionV1(
        ExposureRecordStateV1.SUBMITTED, False, False, False, False
    ),
    ExposureStateInclusionV1(
        ExposureRecordStateV1.ACCEPTED, False, False, False, False
    ),
    ExposureStateInclusionV1(ExposureRecordStateV1.OPEN, True, False, False, False),
    ExposureStateInclusionV1(
        ExposureRecordStateV1.PARTIALLY_SETTLED, True, False, False, False
    ),
    ExposureStateInclusionV1(ExposureRecordStateV1.SETTLED, False, False, False, True),
    ExposureStateInclusionV1(ExposureRecordStateV1.REJECTED, False, False, False, True),
    ExposureStateInclusionV1(ExposureRecordStateV1.CANCELED, False, False, False, True),
    ExposureStateInclusionV1(ExposureRecordStateV1.VOIDED, False, False, False, True),
    ExposureStateInclusionV1(
        ExposureRecordStateV1.CORRECTED, False, False, False, True
    ),
    ExposureStateInclusionV1(
        ExposureRecordStateV1.SUPERSEDED, False, False, False, True
    ),
)
STATE_RULES_BY_STATE = MappingProxyType(
    {row.state: row for row in STATE_INCLUSION_MATRIX_V1}
)


@dataclass(frozen=True, slots=True)
class ExposureContributionHashRefV1:
    contribution_id: str
    canonical_contribution_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "contribution_id",
            require_text("contribution_id", self.contribution_id),
        )
        object.__setattr__(
            self,
            "canonical_contribution_hash",
            require_text(
                "canonical_contribution_hash", self.canonical_contribution_hash
            ),
        )


@dataclass(frozen=True, slots=True)
class ExposureDimensionAggregateV1:
    dimension_type: ExposureDimensionTypeV1
    dimension_key: str
    measure: ExposureMeasureV1
    currency: str
    portfolio_kind: ExposurePortfolioKindV1
    amount: Decimal

    def __post_init__(self) -> None:
        try:
            object.__setattr__(
                self, "dimension_type", ExposureDimensionTypeV1(self.dimension_type)
            )
            object.__setattr__(self, "measure", ExposureMeasureV1(self.measure))
            object.__setattr__(
                self,
                "portfolio_kind",
                ExposurePortfolioKindV1(self.portfolio_kind),
            )
        except (TypeError, ValueError) as exc:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_INVALID_DIMENSION_KEY,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="dimension aggregate uses an unsupported V1 taxonomy value",
                )
            )
            raise AssertionError("unreachable") from exc
        key = require_text("dimension_key", self.dimension_key)
        object.__setattr__(self, "dimension_key", key)
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        object.__setattr__(
            self,
            "amount",
            normalize_monetary_decimal("dimension amount", self.amount, self.currency),
        )


@dataclass(frozen=True, slots=True)
class ExposureDimensionRatioV1:
    dimension_type: ExposureDimensionTypeV1
    dimension_key: str
    measure: ExposureMeasureV1
    value: Decimal | None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(
                self, "dimension_type", ExposureDimensionTypeV1(self.dimension_type)
            )
            object.__setattr__(self, "measure", ExposureMeasureV1(self.measure))
        except (TypeError, ValueError) as exc:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_INVALID_DIMENSION_KEY,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="dimension ratio uses an unsupported V1 taxonomy value",
                )
            )
            raise AssertionError("unreachable") from exc
        object.__setattr__(
            self, "dimension_key", require_text("dimension_key", self.dimension_key)
        )
        object.__setattr__(self, "value", normalize_ratio_decimal("ratio", self.value))
        if self.value is not None and self.value < ZERO:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_INVALID_DIMENSION_KEY,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="ratio must be nonnegative",
                    metadata={"dimension_key": self.dimension_key},
                )
            )


@dataclass(frozen=True, slots=True)
class ExposureLegV1:
    leg_id: str
    league: str
    event_id: str
    market_id: str
    market_type: str
    period: str
    selection_id: str | None
    outcome_id: str | None
    team_ids: tuple[str, ...] = ()
    player_ids: tuple[str, ...] = ()
    canonical_sportsbook_id: CanonicalSportsbookIdV1 | str = ""
    settlement_horizon: str = ""
    reconciliation_result_id: str = ""
    reconciliation_version: str = ""
    correlation_group_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("leg_id", self.leg_id),
            ("league", self.league),
            ("event_id", self.event_id),
            ("market_id", self.market_id),
            ("market_type", self.market_type),
            ("period", self.period),
            ("settlement_horizon", self.settlement_horizon),
            ("reconciliation_result_id", self.reconciliation_result_id),
            ("reconciliation_version", self.reconciliation_version),
        ):
            object.__setattr__(self, name, require_text(name, value))
        object.__setattr__(
            self,
            "canonical_sportsbook_id",
            _normalize_canonical_sportsbook_id(self.canonical_sportsbook_id),
        )
        if not (self.selection_id or self.outcome_id):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_MISSING_CANONICAL_IDENTITY,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.IDENTITY,
                    message="leg requires selection_id or outcome_id",
                    metadata={"leg_id": self.leg_id},
                )
            )
        object.__setattr__(
            self,
            "selection_id",
            require_text("selection_id", self.selection_id)
            if self.selection_id
            else None,
        )
        object.__setattr__(
            self,
            "outcome_id",
            require_text("outcome_id", self.outcome_id) if self.outcome_id else None,
        )
        object.__setattr__(
            self, "team_ids", _sorted_unique_tuple("team_id", self.team_ids)
        )
        object.__setattr__(
            self, "player_ids", _sorted_unique_tuple("player_id", self.player_ids)
        )
        object.__setattr__(
            self,
            "correlation_group_ids",
            _sorted_unique_tuple("correlation_group_id", self.correlation_group_ids),
        )

    def hash_payload(self) -> dict[str, Any]:
        return {
            "leg_id": self.leg_id,
            "league": self.league,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "market_type": self.market_type,
            "period": self.period,
            "selection_id": self.selection_id,
            "outcome_id": self.outcome_id,
            "team_ids": self.team_ids,
            "player_ids": self.player_ids,
            "canonical_sportsbook_id": self.canonical_sportsbook_id,
            "settlement_horizon": self.settlement_horizon,
            "reconciliation_result_id": self.reconciliation_result_id,
            "reconciliation_version": self.reconciliation_version,
            "correlation_group_ids": self.correlation_group_ids,
        }


@dataclass(frozen=True, slots=True)
class ManualRecordedRealProvenanceV1:
    external_execution_reference: str
    sportsbook_account_reference: str
    evidence_reference: str
    recorded_by_actor_id: str
    recorded_at: str
    immutable_revision_id: str
    schema_version: str = SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        for name, value in (
            ("external_execution_reference", self.external_execution_reference),
            ("sportsbook_account_reference", self.sportsbook_account_reference),
            ("evidence_reference", self.evidence_reference),
            ("recorded_by_actor_id", self.recorded_by_actor_id),
            ("immutable_revision_id", self.immutable_revision_id),
        ):
            object.__setattr__(self, name, require_text(name, value))
        object.__setattr__(
            self,
            "recorded_at",
            normalize_utc_timestamp("recorded_at", self.recorded_at),
        )
        object.__setattr__(
            self,
            "schema_version",
            require_supported_version(
                "schema_version",
                self.schema_version,
                supported=SUPPORTED_SCHEMA_VERSIONS,
            ),
        )

    def hash_payload(self) -> dict[str, Any]:
        return {
            "external_execution_reference": self.external_execution_reference,
            "sportsbook_account_reference": self.sportsbook_account_reference,
            "evidence_reference": self.evidence_reference,
            "recorded_by_actor_id": self.recorded_by_actor_id,
            "recorded_at": self.recorded_at,
            "immutable_revision_id": self.immutable_revision_id,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ExposureRecordV1:
    record_id: str
    record_kind: ExposureRecordKindV1
    source_repository: str
    source_record_id: str
    source_revision: str
    portfolio_id: str
    portfolio_kind: ExposurePortfolioKindV1
    account_id: str
    currency: str
    position_id: str | None
    order_intent_id: str | None
    reservation_id: str | None
    candidate_projection_id: str | None
    reconciliation_result_id: str | None
    reconciliation_version: str
    model_version: str
    calibration_version: str
    strategy_id: str
    strategy_version: str
    state: ExposureRecordStateV1
    effective_at: str
    as_of_eligible_at: str
    stake_committed: Decimal
    stake_reserved: Decimal
    maximum_possible_loss: Decimal
    potential_profit: Decimal
    gross_payout: Decimal
    net_liability: Decimal
    league: str
    event_id: str
    market_id: str
    market_type: str
    period: str
    selection_id: str | None
    outcome_id: str | None
    team_ids: tuple[str, ...]
    player_ids: tuple[str, ...]
    canonical_sportsbook_id: CanonicalSportsbookIdV1
    settlement_horizon: str
    parlay_id: str | None = None
    legs: tuple[ExposureLegV1, ...] = ()
    correlation_group_ids: tuple[str, ...] = ()
    correction_of_record_id: str | None = None
    reversal_of_record_id: str | None = None
    residual_reference_id: str | None = None
    manual_provenance: ManualRecordedRealProvenanceV1 | None = None
    canonical_record_hash: str = ""
    schema_version: str = SCHEMA_VERSION_V1
    calculation_input_version: str = CALCULATION_INPUT_VERSION_V1
    _authoritative_origin_hash: str = field(
        init=False,
        default="",
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        _normalize_record_fields(self)
        _validate_record_semantics(self)
        computed = stable_hash(self.hash_payload())
        if self.canonical_record_hash and self.canonical_record_hash != computed:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                    message="record canonical hash does not match authoritative payload",
                    metadata={"record_id": self.record_id},
                )
            )
        object.__setattr__(self, "canonical_record_hash", computed)
        object.__setattr__(
            self,
            "_authoritative_origin_hash",
            _seal_authoritative_hash(computed),
        )

    def hash_payload(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "record_kind": self.record_kind.value,
            "source_repository": self.source_repository,
            "source_record_id": self.source_record_id,
            "source_revision": self.source_revision,
            "portfolio_id": self.portfolio_id,
            "portfolio_kind": self.portfolio_kind.value,
            "account_id": self.account_id,
            "currency": self.currency,
            "position_id": self.position_id,
            "order_intent_id": self.order_intent_id,
            "reservation_id": self.reservation_id,
            "candidate_projection_id": self.candidate_projection_id,
            "reconciliation_result_id": self.reconciliation_result_id,
            "reconciliation_version": self.reconciliation_version,
            "model_version": self.model_version,
            "calibration_version": self.calibration_version,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "state": self.state.value,
            "effective_at": self.effective_at,
            "as_of_eligible_at": self.as_of_eligible_at,
            "stake_committed": self.stake_committed,
            "stake_reserved": self.stake_reserved,
            "maximum_possible_loss": self.maximum_possible_loss,
            "potential_profit": self.potential_profit,
            "gross_payout": self.gross_payout,
            "net_liability": self.net_liability,
            "league": self.league,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "market_type": self.market_type,
            "period": self.period,
            "selection_id": self.selection_id,
            "outcome_id": self.outcome_id,
            "team_ids": self.team_ids,
            "player_ids": self.player_ids,
            "canonical_sportsbook_id": self.canonical_sportsbook_id,
            "settlement_horizon": self.settlement_horizon,
            "parlay_id": self.parlay_id,
            "legs": [leg.hash_payload() for leg in self.legs],
            "correlation_group_ids": self.correlation_group_ids,
            "correction_of_record_id": self.correction_of_record_id,
            "reversal_of_record_id": self.reversal_of_record_id,
            "residual_reference_id": self.residual_reference_id,
            "manual_provenance": (
                self.manual_provenance.hash_payload()
                if self.manual_provenance is not None
                else None
            ),
            "schema_version": self.schema_version,
            "calculation_input_version": self.calculation_input_version,
        }


@dataclass(frozen=True, slots=True)
class BankrollReferenceV1:
    bankroll_reference_id: str
    portfolio_id: str
    account_id: str
    currency: str
    ledger_version: str
    available_balance: Decimal
    total_bankroll: Decimal
    reserved_balance: Decimal
    as_of: str
    source_reference: str
    canonical_input_hash: str = ""
    schema_version: str = SCHEMA_VERSION_V1
    _authoritative_origin_hash: str = field(
        init=False,
        default="",
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        for name, value in (
            ("bankroll_reference_id", self.bankroll_reference_id),
            ("portfolio_id", self.portfolio_id),
            ("account_id", self.account_id),
            ("ledger_version", self.ledger_version),
            ("source_reference", self.source_reference),
        ):
            object.__setattr__(self, name, require_text(name, value))
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        object.__setattr__(self, "as_of", normalize_utc_timestamp("as_of", self.as_of))
        object.__setattr__(
            self,
            "schema_version",
            require_supported_version(
                "schema_version",
                self.schema_version,
                supported=SUPPORTED_SCHEMA_VERSIONS,
            ),
        )
        object.__setattr__(
            self,
            "available_balance",
            normalize_monetary_decimal(
                "available_balance", self.available_balance, self.currency
            ),
        )
        object.__setattr__(
            self,
            "total_bankroll",
            normalize_monetary_decimal(
                "total_bankroll", self.total_bankroll, self.currency
            ),
        )
        object.__setattr__(
            self,
            "reserved_balance",
            normalize_monetary_decimal(
                "reserved_balance", self.reserved_balance, self.currency
            ),
        )
        if self.available_balance + self.reserved_balance > self.total_bankroll:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_BANKROLL_REFERENCE_INVALID,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.BANKROLL_REFERENCE,
                    message="available plus reserved balance cannot exceed total bankroll",
                    metadata={"bankroll_reference_id": self.bankroll_reference_id},
                )
            )
        computed = stable_hash(self.hash_payload())
        if self.canonical_input_hash and self.canonical_input_hash != computed:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.BANKROLL_REFERENCE,
                    message="bankroll canonical hash does not match authoritative payload",
                    metadata={"bankroll_reference_id": self.bankroll_reference_id},
                )
            )
        object.__setattr__(self, "canonical_input_hash", computed)
        object.__setattr__(
            self,
            "_authoritative_origin_hash",
            _seal_authoritative_hash(computed),
        )

    def hash_payload(self) -> dict[str, Any]:
        return {
            "bankroll_reference_id": self.bankroll_reference_id,
            "portfolio_id": self.portfolio_id,
            "account_id": self.account_id,
            "currency": self.currency,
            "ledger_version": self.ledger_version,
            "available_balance": self.available_balance,
            "total_bankroll": self.total_bankroll,
            "reserved_balance": self.reserved_balance,
            "as_of": self.as_of,
            "source_reference": self.source_reference,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ExposureCandidateV1:
    candidate_id: str
    portfolio_id: str
    portfolio_kind: ExposurePortfolioKindV1
    account_id: str
    currency: str
    reconciliation_result_id: str
    allocation_amount: Decimal
    maximum_possible_loss: Decimal
    potential_profit: Decimal
    gross_payout: Decimal
    net_liability: Decimal
    league: str
    event_id: str
    market_id: str
    market_type: str
    period: str
    selection_id: str | None
    outcome_id: str | None
    team_ids: tuple[str, ...]
    player_ids: tuple[str, ...]
    canonical_sportsbook_id: CanonicalSportsbookIdV1
    strategy_version: str
    strategy_id: str
    model_version: str
    calibration_version: str
    reconciliation_version: str
    settlement_horizon: str
    parlay_id: str | None = None
    legs: tuple[ExposureLegV1, ...] = ()
    correlation_group_ids: tuple[str, ...] = ()
    as_of: str = ""
    canonical_input_hash: str = ""
    schema_version: str = SCHEMA_VERSION_V1
    _authoritative_origin_hash: str = field(
        init=False,
        default="",
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        for name, value in (
            ("candidate_id", self.candidate_id),
            ("portfolio_id", self.portfolio_id),
            ("account_id", self.account_id),
            ("reconciliation_result_id", self.reconciliation_result_id),
            ("league", self.league),
            ("event_id", self.event_id),
            ("market_id", self.market_id),
            ("market_type", self.market_type),
            ("period", self.period),
            ("strategy_version", self.strategy_version),
            ("strategy_id", self.strategy_id),
            ("model_version", self.model_version),
            ("calibration_version", self.calibration_version),
            ("reconciliation_version", self.reconciliation_version),
            ("settlement_horizon", self.settlement_horizon),
        ):
            object.__setattr__(self, name, require_text(name, value))
        object.__setattr__(
            self,
            "canonical_sportsbook_id",
            _normalize_canonical_sportsbook_id(self.canonical_sportsbook_id),
        )
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        object.__setattr__(self, "as_of", normalize_utc_timestamp("as_of", self.as_of))
        object.__setattr__(
            self,
            "schema_version",
            require_supported_version(
                "schema_version",
                self.schema_version,
                supported=SUPPORTED_SCHEMA_VERSIONS,
            ),
        )
        if not (self.selection_id or self.outcome_id):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_MISSING_CANONICAL_IDENTITY,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.IDENTITY,
                    message="candidate requires selection_id or outcome_id",
                    metadata={"candidate_id": self.candidate_id},
                )
            )
        object.__setattr__(
            self,
            "selection_id",
            require_text("selection_id", self.selection_id)
            if self.selection_id
            else None,
        )
        object.__setattr__(
            self,
            "outcome_id",
            require_text("outcome_id", self.outcome_id) if self.outcome_id else None,
        )
        object.__setattr__(
            self, "team_ids", _sorted_unique_tuple("team_id", self.team_ids)
        )
        object.__setattr__(
            self, "player_ids", _sorted_unique_tuple("player_id", self.player_ids)
        )
        object.__setattr__(
            self,
            "correlation_group_ids",
            _sorted_unique_tuple("correlation_group_id", self.correlation_group_ids),
        )
        object.__setattr__(self, "legs", _normalize_legs(self.legs))
        object.__setattr__(
            self,
            "allocation_amount",
            normalize_monetary_decimal(
                "allocation_amount", self.allocation_amount, self.currency
            ),
        )
        object.__setattr__(
            self,
            "maximum_possible_loss",
            normalize_monetary_decimal(
                "maximum_possible_loss", self.maximum_possible_loss, self.currency
            ),
        )
        object.__setattr__(
            self,
            "potential_profit",
            normalize_monetary_decimal(
                "potential_profit", self.potential_profit, self.currency
            ),
        )
        object.__setattr__(
            self,
            "gross_payout",
            normalize_monetary_decimal(
                "gross_payout", self.gross_payout, self.currency
            ),
        )
        object.__setattr__(
            self,
            "net_liability",
            normalize_monetary_decimal(
                "net_liability", self.net_liability, self.currency
            ),
        )
        _validate_candidate_semantics(self)
        computed = stable_hash(self.hash_payload())
        if self.canonical_input_hash and self.canonical_input_hash != computed:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                    message="candidate canonical hash does not match authoritative payload",
                    metadata={"candidate_id": self.candidate_id},
                )
            )
        object.__setattr__(self, "canonical_input_hash", computed)
        object.__setattr__(
            self,
            "_authoritative_origin_hash",
            _seal_authoritative_hash(computed),
        )

    def hash_payload(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "portfolio_id": self.portfolio_id,
            "portfolio_kind": self.portfolio_kind.value,
            "account_id": self.account_id,
            "currency": self.currency,
            "reconciliation_result_id": self.reconciliation_result_id,
            "allocation_amount": self.allocation_amount,
            "maximum_possible_loss": self.maximum_possible_loss,
            "potential_profit": self.potential_profit,
            "gross_payout": self.gross_payout,
            "net_liability": self.net_liability,
            "league": self.league,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "market_type": self.market_type,
            "period": self.period,
            "selection_id": self.selection_id,
            "outcome_id": self.outcome_id,
            "team_ids": self.team_ids,
            "player_ids": self.player_ids,
            "canonical_sportsbook_id": self.canonical_sportsbook_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "model_version": self.model_version,
            "calibration_version": self.calibration_version,
            "reconciliation_version": self.reconciliation_version,
            "settlement_horizon": self.settlement_horizon,
            "parlay_id": self.parlay_id,
            "legs": [leg.hash_payload() for leg in self.legs],
            "correlation_group_ids": self.correlation_group_ids,
            "as_of": self.as_of,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ExposureCalculationRequestV1:
    request_id: str
    portfolio_id: str
    account_id: str
    currency: str
    as_of: str
    calculation_version: str
    record_ids: tuple[str, ...]
    record_hashes: MappingProxyType[str, str]
    bankroll_reference_id: str
    bankroll_reference_hash: str
    candidate_id: str | None
    candidate_input_hash: str | None
    identity_map_versions: MappingProxyType[str, str]
    canonical_input_hash: str = ""
    idempotency_key: str = ""
    schema_version: str = SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        for name, value in (
            ("request_id", self.request_id),
            ("portfolio_id", self.portfolio_id),
            ("account_id", self.account_id),
            ("bankroll_reference_id", self.bankroll_reference_id),
            ("bankroll_reference_hash", self.bankroll_reference_hash),
            ("idempotency_key", self.idempotency_key),
        ):
            object.__setattr__(self, name, require_text(name, value))
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        object.__setattr__(self, "as_of", normalize_utc_timestamp("as_of", self.as_of))
        object.__setattr__(
            self,
            "calculation_version",
            require_supported_version(
                "calculation_version",
                self.calculation_version,
                supported=SUPPORTED_CALCULATION_VERSIONS,
            ),
        )
        object.__setattr__(
            self,
            "schema_version",
            require_supported_version(
                "schema_version",
                self.schema_version,
                supported=SUPPORTED_SCHEMA_VERSIONS,
            ),
        )
        normalized_ids = tuple(
            sorted({require_text("record_id", item) for item in self.record_ids})
        )
        normalized_hashes = {
            require_text("record_hash key", key): require_text("record_hash", value)
            for key, value in dict(self.record_hashes).items()
        }
        if set(normalized_ids) != set(normalized_hashes.keys()):
            raise ValueError("record_ids and record_hashes must align exactly")
        normalized_identity_versions = {
            require_text("identity_map_version key", key): require_text(
                "identity_map_version", value
            )
            for key, value in dict(self.identity_map_versions).items()
        }
        object.__setattr__(self, "record_ids", normalized_ids)
        object.__setattr__(
            self,
            "record_hashes",
            MappingProxyType(
                dict(sorted(normalized_hashes.items(), key=lambda item: item[0]))
            ),
        )
        object.__setattr__(
            self,
            "identity_map_versions",
            MappingProxyType(
                dict(
                    sorted(
                        normalized_identity_versions.items(), key=lambda item: item[0]
                    )
                )
            ),
        )
        if self.candidate_id is not None:
            object.__setattr__(
                self, "candidate_id", require_text("candidate_id", self.candidate_id)
            )
            object.__setattr__(
                self,
                "candidate_input_hash",
                require_text("candidate_input_hash", self.candidate_input_hash or ""),
            )
        elif self.candidate_input_hash is not None:
            raise ValueError("candidate_input_hash requires candidate_id")
        computed = stable_hash(self.hash_payload())
        if self.canonical_input_hash and self.canonical_input_hash != computed:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="request canonical hash does not match authoritative payload",
                    metadata={"request_id": self.request_id},
                )
            )
        object.__setattr__(self, "canonical_input_hash", computed)

    def hash_payload(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "portfolio_id": self.portfolio_id,
            "account_id": self.account_id,
            "currency": self.currency,
            "as_of": self.as_of,
            "calculation_version": self.calculation_version,
            "record_ids": self.record_ids,
            "record_hashes": dict(self.record_hashes),
            "bankroll_reference_id": self.bankroll_reference_id,
            "bankroll_reference_hash": self.bankroll_reference_hash,
            "candidate_id": self.candidate_id,
            "candidate_input_hash": self.candidate_input_hash,
            "identity_map_versions": dict(self.identity_map_versions),
            "idempotency_key": self.idempotency_key,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ExposureContributionV1:
    contribution_id: str
    snapshot_or_projection_id: str
    portfolio_id: str
    account_id: str
    record_id: str | None
    position_id: str | None
    order_intent_id: str | None
    reservation_id: str | None
    candidate_id: str | None
    parlay_id: str | None
    leg_id: str | None
    dimension_type: ExposureDimensionTypeV1
    dimension_key: str
    measure: ExposureMeasureV1
    amount: Decimal
    currency: str
    portfolio_kind: ExposurePortfolioKindV1
    position_state: ExposureRecordStateV1
    source_repository: str
    calculation_version: str
    as_of: str
    source_input_hash: str
    contribution_scope: ExposureContributionScopeV1
    additive_to_portfolio_totals: bool
    canonical_contribution_hash: str = ""
    non_additive_note: str = ""
    schema_version: str = SCHEMA_VERSION_V1
    _authoritative_origin_hash: str = field(
        init=False,
        default="",
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        try:
            object.__setattr__(
                self, "dimension_type", ExposureDimensionTypeV1(self.dimension_type)
            )
            object.__setattr__(self, "measure", ExposureMeasureV1(self.measure))
            object.__setattr__(
                self,
                "portfolio_kind",
                ExposurePortfolioKindV1(self.portfolio_kind),
            )
            object.__setattr__(
                self, "position_state", ExposureRecordStateV1(self.position_state)
            )
            object.__setattr__(
                self,
                "contribution_scope",
                ExposureContributionScopeV1(self.contribution_scope),
            )
        except (TypeError, ValueError) as exc:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_INVALID_CONTRIBUTION_SCOPE,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="contribution uses an unsupported V1 taxonomy value",
                )
            )
            raise AssertionError("unreachable") from exc
        for name, value in (
            ("contribution_id", self.contribution_id),
            ("snapshot_or_projection_id", self.snapshot_or_projection_id),
            ("portfolio_id", self.portfolio_id),
            ("account_id", self.account_id),
            ("dimension_key", self.dimension_key),
            ("source_repository", self.source_repository),
            ("as_of", self.as_of),
            ("source_input_hash", self.source_input_hash),
        ):
            object.__setattr__(self, name, require_text(name, value))
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        object.__setattr__(
            self,
            "record_id",
            require_text("record_id", self.record_id) if self.record_id else None,
        )
        object.__setattr__(
            self,
            "candidate_id",
            require_text("candidate_id", self.candidate_id)
            if self.candidate_id
            else None,
        )
        object.__setattr__(
            self,
            "position_id",
            require_text("position_id", self.position_id) if self.position_id else None,
        )
        object.__setattr__(
            self,
            "order_intent_id",
            require_text("order_intent_id", self.order_intent_id)
            if self.order_intent_id
            else None,
        )
        object.__setattr__(
            self,
            "reservation_id",
            require_text("reservation_id", self.reservation_id)
            if self.reservation_id
            else None,
        )
        object.__setattr__(
            self,
            "parlay_id",
            require_text("parlay_id", self.parlay_id) if self.parlay_id else None,
        )
        object.__setattr__(
            self, "leg_id", require_text("leg_id", self.leg_id) if self.leg_id else None
        )
        object.__setattr__(
            self,
            "calculation_version",
            require_supported_version(
                "calculation_version",
                self.calculation_version,
                supported=SUPPORTED_CALCULATION_VERSIONS,
            ),
        )
        normalized_schema_version = require_text("schema_version", self.schema_version)
        if normalized_schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_UNSUPPORTED_CONTRIBUTION_SCHEMA_VERSION,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="unsupported contribution schema version",
                    metadata={"schema_version": normalized_schema_version},
                )
            )
        object.__setattr__(self, "schema_version", normalized_schema_version)
        object.__setattr__(self, "as_of", normalize_utc_timestamp("as_of", self.as_of))
        object.__setattr__(
            self,
            "amount",
            normalize_monetary_decimal("amount", self.amount, self.currency),
        )
        derived_scope = _derive_contribution_scope(self.dimension_type, self.leg_id)
        if self.contribution_scope != derived_scope:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PROJECTION_SOURCE_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="contribution scope does not match contribution identity",
                    metadata={"contribution_id": self.contribution_id},
                )
            )
        if bool(self.record_id) == bool(self.candidate_id):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_CONTRIBUTION_DUAL_LINEAGE
                    if self.record_id
                    else ExposureReasonCodeV1.EXP_CONTRIBUTION_LINEAGE_FAILURE,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="contribution must reference exactly one authoritative source",
                    metadata={"contribution_id": self.contribution_id},
                )
            )
        if self.contribution_scope == ExposureContributionScopeV1.TOP_LEVEL_ECONOMIC:
            if (
                self.leg_id is not None
                or self.dimension_type != ExposureDimensionTypeV1.PORTFOLIO
            ):
                raise_domain_error(
                    build_reason(
                        code=ExposureReasonCodeV1.EXP_INVALID_CONTRIBUTION_SCOPE,
                        severity=ExposureReasonSeverityV1.ERROR,
                        category=ExposureReasonCategoryV1.CALCULATION,
                        message="top-level economic contribution cannot carry leg or dimensional allocation identity",
                        metadata={"contribution_id": self.contribution_id},
                    )
                )
            if not self.additive_to_portfolio_totals:
                raise_domain_error(
                    build_reason(
                        code=ExposureReasonCodeV1.EXP_INVALID_CONTRIBUTION_SCOPE,
                        severity=ExposureReasonSeverityV1.ERROR,
                        category=ExposureReasonCategoryV1.CALCULATION,
                        message="top-level economic contribution must be additive",
                        metadata={"contribution_id": self.contribution_id},
                    )
                )
        else:
            if self.additive_to_portfolio_totals:
                raise_domain_error(
                    build_reason(
                        code=ExposureReasonCodeV1.EXP_INVALID_CONTRIBUTION_SCOPE,
                        severity=ExposureReasonSeverityV1.ERROR,
                        category=ExposureReasonCategoryV1.CALCULATION,
                        message="dimension contribution cannot be additive",
                        metadata={"contribution_id": self.contribution_id},
                    )
                )
        computed = stable_hash(self.hash_payload())
        if (
            self.canonical_contribution_hash
            and self.canonical_contribution_hash != computed
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="contribution canonical hash does not match authoritative payload",
                    metadata={"contribution_id": self.contribution_id},
                )
            )
        object.__setattr__(self, "canonical_contribution_hash", computed)
        object.__setattr__(
            self,
            "_authoritative_origin_hash",
            _seal_authoritative_hash(computed),
        )

    def hash_payload(self) -> dict[str, Any]:
        return {
            "contribution_id": self.contribution_id,
            "snapshot_or_projection_id": self.snapshot_or_projection_id,
            "portfolio_id": self.portfolio_id,
            "account_id": self.account_id,
            "record_id": self.record_id,
            "position_id": self.position_id,
            "order_intent_id": self.order_intent_id,
            "reservation_id": self.reservation_id,
            "candidate_id": self.candidate_id,
            "parlay_id": self.parlay_id,
            "leg_id": self.leg_id,
            "dimension_type": self.dimension_type.value,
            "dimension_key": self.dimension_key,
            "measure": self.measure.value,
            "amount": self.amount,
            "currency": self.currency,
            "portfolio_kind": self.portfolio_kind.value,
            "position_state": self.position_state.value,
            "source_repository": self.source_repository,
            "calculation_version": self.calculation_version,
            "as_of": self.as_of,
            "source_input_hash": self.source_input_hash,
            "contribution_scope": self.contribution_scope.value,
            "additive_to_portfolio_totals": self.additive_to_portfolio_totals,
            "non_additive_note": self.non_additive_note,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ExposureTotalsV1:
    stake_committed: Decimal
    stake_reserved: Decimal
    maximum_possible_loss: Decimal
    potential_profit: Decimal
    gross_payout: Decimal
    net_liability: Decimal
    currency: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        object.__setattr__(
            self,
            "stake_committed",
            normalize_monetary_decimal(
                "stake_committed", self.stake_committed, self.currency
            ),
        )
        object.__setattr__(
            self,
            "stake_reserved",
            normalize_monetary_decimal(
                "stake_reserved", self.stake_reserved, self.currency
            ),
        )
        object.__setattr__(
            self,
            "maximum_possible_loss",
            normalize_monetary_decimal(
                "maximum_possible_loss", self.maximum_possible_loss, self.currency
            ),
        )
        object.__setattr__(
            self,
            "potential_profit",
            normalize_monetary_decimal(
                "potential_profit", self.potential_profit, self.currency
            ),
        )
        object.__setattr__(
            self,
            "gross_payout",
            normalize_monetary_decimal(
                "gross_payout", self.gross_payout, self.currency
            ),
        )
        object.__setattr__(
            self,
            "net_liability",
            normalize_monetary_decimal(
                "net_liability", self.net_liability, self.currency
            ),
        )
        if self.gross_payout != self.stake_committed + self.potential_profit:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PAYOUT_INCONSISTENT,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.MONETARY,
                    message="gross payout must equal committed stake plus potential profit",
                )
            )
        if self.net_liability != self.maximum_possible_loss:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PAYOUT_INCONSISTENT,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.MONETARY,
                    message="net liability must equal maximum possible loss",
                )
            )

    def measure_amounts(self) -> dict[ExposureMeasureV1, Decimal]:
        return {
            ExposureMeasureV1.STAKE_COMMITTED: self.stake_committed,
            ExposureMeasureV1.STAKE_RESERVED: self.stake_reserved,
            ExposureMeasureV1.MAXIMUM_LOSS: self.maximum_possible_loss,
            ExposureMeasureV1.POTENTIAL_PROFIT: self.potential_profit,
            ExposureMeasureV1.GROSS_PAYOUT: self.gross_payout,
            ExposureMeasureV1.NET_LIABILITY: self.net_liability,
        }

    def as_mapping(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            **{
                measure.value: amount
                for measure, amount in self.measure_amounts().items()
            },
        }


@dataclass(frozen=True, slots=True)
class AuthoritativeExposureSnapshotV1:
    snapshot_id: str
    portfolio_id: str
    account_id: str
    portfolio_kind: ExposurePortfolioKindV1
    currency: str
    as_of: str
    calculation_version: str
    bankroll_reference_id: str
    bankroll_reference_hash: str
    included_record_ids: tuple[str, ...]
    included_record_hashes: MappingProxyType[str, str]
    contribution_refs: tuple[ExposureContributionHashRefV1, ...]
    stake_committed: Decimal
    stake_reserved: Decimal
    maximum_possible_loss: Decimal
    potential_profit: Decimal
    gross_payout: Decimal
    net_liability: Decimal
    exposure_pct_bankroll: Decimal | None
    exposure_pct_available: Decimal | None
    counts_by_state: MappingProxyType[str, int]
    counts_by_record_kind: MappingProxyType[str, int]
    counts_by_portfolio_kind: MappingProxyType[str, int]
    dimension_aggregates: tuple[ExposureDimensionAggregateV1, ...]
    concentration_ratios: tuple[ExposureDimensionRatioV1, ...]
    correlated_maximum_loss_by_group: MappingProxyType[str, Decimal]
    reasons: tuple[ExposureReasonV1, ...] = ()
    status: ExposureAvailabilityStatusV1 = ExposureAvailabilityStatusV1.AVAILABLE
    canonical_input_hash: str = ""
    schema_version: str = SCHEMA_VERSION_V1
    authoritative_record_identities: tuple[ExposureRecordIdentity, ...] = field(
        init=False,
        default=(),
        repr=False,
        compare=False,
    )
    authoritative_lineage_record_hashes: MappingProxyType[str, str] = field(
        init=False,
        default_factory=lambda: MappingProxyType({}),
        repr=False,
        compare=False,
    )
    authoritative_lineage_record_identities: tuple[ExposureRecordIdentity, ...] = field(
        init=False,
        default=(),
        repr=False,
        compare=False,
    )
    _authoritative_records_evidence: tuple[ExposureRecordV1, ...] = field(
        init=False,
        default=(),
        repr=False,
        compare=False,
    )
    _authoritative_contributions_evidence: tuple[ExposureContributionV1, ...] = field(
        init=False,
        default=(),
        repr=False,
        compare=False,
    )
    _authoritative_bankroll_evidence: BankrollReferenceV1 | None = field(
        init=False,
        default=None,
        repr=False,
        compare=False,
    )
    _authoritative_availability_evidence_hash: str = field(
        init=False,
        default="",
        repr=False,
        compare=False,
    )
    _authoritative_lineage_evidence_hash: str = field(
        init=False,
        default="",
        repr=False,
        compare=False,
    )
    _authoritative_bankroll_evidence_hash: str = field(
        init=False,
        default="",
        repr=False,
        compare=False,
    )
    _authoritative_origin_hash: str = field(
        init=False,
        default="",
        repr=False,
        compare=False,
    )
    authoritative_records: InitVar[
        tuple[ExposureRecordV1, ...] | list[ExposureRecordV1] | None
    ] = None
    authoritative_contributions: InitVar[
        tuple[ExposureContributionV1, ...] | list[ExposureContributionV1] | None
    ] = None
    authoritative_bankroll_reference: InitVar[BankrollReferenceV1 | None] = None

    def __post_init__(
        self,
        authoritative_records: tuple[ExposureRecordV1, ...]
        | list[ExposureRecordV1]
        | None,
        authoritative_contributions: tuple[ExposureContributionV1, ...]
        | list[ExposureContributionV1]
        | None,
        authoritative_bankroll_reference: BankrollReferenceV1 | None,
    ) -> None:
        for name, value in (
            ("snapshot_id", self.snapshot_id),
            ("portfolio_id", self.portfolio_id),
            ("account_id", self.account_id),
            ("bankroll_reference_id", self.bankroll_reference_id),
            ("bankroll_reference_hash", self.bankroll_reference_hash),
        ):
            object.__setattr__(self, name, require_text(name, value))
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        object.__setattr__(self, "as_of", normalize_utc_timestamp("as_of", self.as_of))
        object.__setattr__(
            self,
            "calculation_version",
            require_supported_version(
                "calculation_version",
                self.calculation_version,
                supported=SUPPORTED_CALCULATION_VERSIONS,
            ),
        )
        object.__setattr__(
            self,
            "schema_version",
            require_supported_version(
                "schema_version",
                self.schema_version,
                supported=SUPPORTED_SCHEMA_VERSIONS,
            ),
        )
        object.__setattr__(
            self,
            "_authoritative_records_evidence",
            tuple(authoritative_records or ()),
        )
        object.__setattr__(
            self,
            "_authoritative_contributions_evidence",
            tuple(authoritative_contributions or ()),
        )
        object.__setattr__(
            self,
            "_authoritative_bankroll_evidence",
            authoritative_bankroll_reference,
        )
        _normalize_snapshot_fields(self)
        _revalidate_snapshot_authority(self, constructing=True)
        object.__setattr__(
            self,
            "_authoritative_lineage_evidence_hash",
            _snapshot_lineage_evidence_hash(self),
        )
        object.__setattr__(
            self,
            "_authoritative_availability_evidence_hash",
            _snapshot_availability_evidence_hash(self),
        )
        object.__setattr__(
            self,
            "_authoritative_bankroll_evidence_hash",
            _snapshot_bankroll_evidence_hash(self),
        )
        computed = stable_hash(self.hash_payload())
        if self.canonical_input_hash and self.canonical_input_hash != computed:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="snapshot canonical hash does not match authoritative payload",
                    metadata={"snapshot_id": self.snapshot_id},
                )
            )
        object.__setattr__(self, "canonical_input_hash", computed)
        object.__setattr__(
            self,
            "_authoritative_origin_hash",
            _seal_authoritative_hash(computed),
        )

    def hash_payload(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "portfolio_id": self.portfolio_id,
            "account_id": self.account_id,
            "portfolio_kind": self.portfolio_kind.value,
            "currency": self.currency,
            "as_of": self.as_of,
            "calculation_version": self.calculation_version,
            "bankroll_reference_id": self.bankroll_reference_id,
            "bankroll_reference_hash": self.bankroll_reference_hash,
            "included_record_ids": self.included_record_ids,
            "included_record_hashes": dict(self.included_record_hashes),
            "authoritative_identity_refs": [
                {
                    "record_id": identity.record_id,
                    "record_hash": identity.record_hash,
                    "position_id": identity.position_id,
                    "order_intent_id": identity.order_intent_id,
                    "reservation_id": identity.reservation_id,
                    "parlay_id": identity.parlay_id,
                    "parlay_leg_ids": identity.parlay_leg_ids,
                }
                for identity in self.authoritative_record_identities
            ],
            "authoritative_lineage_record_hashes": dict(
                self.authoritative_lineage_record_hashes
            ),
            "contribution_refs": [
                {
                    "contribution_id": ref.contribution_id,
                    "canonical_contribution_hash": ref.canonical_contribution_hash,
                }
                for ref in self.contribution_refs
            ],
            "stake_committed": self.stake_committed,
            "stake_reserved": self.stake_reserved,
            "maximum_possible_loss": self.maximum_possible_loss,
            "potential_profit": self.potential_profit,
            "gross_payout": self.gross_payout,
            "net_liability": self.net_liability,
            "exposure_pct_bankroll": self.exposure_pct_bankroll,
            "exposure_pct_available": self.exposure_pct_available,
            "counts_by_state": dict(self.counts_by_state),
            "counts_by_record_kind": dict(self.counts_by_record_kind),
            "counts_by_portfolio_kind": dict(self.counts_by_portfolio_kind),
            "dimension_aggregates": [
                {
                    "dimension_type": item.dimension_type.value,
                    "dimension_key": item.dimension_key,
                    "measure": item.measure.value,
                    "currency": item.currency,
                    "portfolio_kind": item.portfolio_kind.value,
                    "amount": item.amount,
                }
                for item in self.dimension_aggregates
            ],
            "concentration_ratios": [
                {
                    "dimension_type": item.dimension_type.value,
                    "dimension_key": item.dimension_key,
                    "measure": item.measure.value,
                    "value": item.value,
                }
                for item in self.concentration_ratios
            ],
            "correlated_maximum_loss_by_group": dict(
                self.correlated_maximum_loss_by_group
            ),
            "reasons": self.reasons,
            "status": self.status.value,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ExposureProjectionV1:
    projection_id: str
    portfolio_id: str
    account_id: str
    portfolio_kind: ExposurePortfolioKindV1
    currency: str
    current_snapshot_id: str
    current_snapshot_hash: str
    candidate_id: str
    candidate_input_hash: str
    before_totals: ExposureTotalsV1
    candidate_top_level_contributions: tuple[ExposureContributionV1, ...]
    candidate_dimension_contributions: tuple[ExposureContributionV1, ...]
    projected_totals: ExposureTotalsV1
    affected_dimensions: tuple[str, ...]
    bankroll_reference_id: str
    bankroll_reference_hash: str
    bankroll_available_balance: Decimal
    bankroll_reserved_balance: Decimal
    hypothetical_available_after: Decimal | None
    calculation_version: str
    as_of: str
    reasons: tuple[ExposureReasonV1, ...] = ()
    status: ExposureProjectionStatusV1 = ExposureProjectionStatusV1.AVAILABLE
    canonical_input_hash: str = ""
    schema_version: str = SCHEMA_VERSION_V1
    _authoritative_snapshot_evidence: AuthoritativeExposureSnapshotV1 | None = field(
        init=False,
        default=None,
        repr=False,
        compare=False,
    )
    _authoritative_candidate_evidence: ExposureCandidateV1 | None = field(
        init=False,
        default=None,
        repr=False,
        compare=False,
    )
    _authoritative_bankroll_evidence: BankrollReferenceV1 | None = field(
        init=False,
        default=None,
        repr=False,
        compare=False,
    )
    _authoritative_origin_hash: str = field(
        init=False,
        default="",
        repr=False,
        compare=False,
    )
    current_snapshot_evidence: InitVar[AuthoritativeExposureSnapshotV1 | None] = None
    candidate_evidence: InitVar[ExposureCandidateV1 | None] = None
    bankroll_evidence: InitVar[BankrollReferenceV1 | None] = None

    @classmethod
    def from_authoritative_inputs(
        cls,
        *,
        projection_id: str,
        current_snapshot: AuthoritativeExposureSnapshotV1,
        candidate: ExposureCandidateV1,
        bankroll_reference: BankrollReferenceV1,
        candidate_top_level_contributions: tuple[ExposureContributionV1, ...]
        | list[ExposureContributionV1],
        candidate_dimension_contributions: tuple[ExposureContributionV1, ...]
        | list[ExposureContributionV1],
        affected_dimensions: tuple[str, ...] | list[str] | None = None,
        reasons: tuple[ExposureReasonV1, ...] | list[ExposureReasonV1] = (),
        status: ExposureProjectionStatusV1 = ExposureProjectionStatusV1.AVAILABLE,
    ) -> "ExposureProjectionV1":
        _validate_authoritative_projection_inputs(
            current_snapshot=current_snapshot,
            candidate=candidate,
            bankroll_reference=bankroll_reference,
        )
        top_level = tuple(candidate_top_level_contributions)
        dimensionals = tuple(candidate_dimension_contributions)
        seen_ids: set[str] = set()
        for contribution in top_level + dimensionals:
            if contribution.contribution_id in seen_ids:
                raise_domain_error(
                    build_reason(
                        code=ExposureReasonCodeV1.EXP_DUPLICATE_CONTRIBUTION_ID,
                        severity=ExposureReasonSeverityV1.ERROR,
                        category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                        message="duplicate contribution id",
                        metadata={"contribution_id": contribution.contribution_id},
                    )
                )
            seen_ids.add(contribution.contribution_id)
        derived_dimensions = tuple(
            sorted(
                {
                    f"{contribution.dimension_type.value}:{contribution.dimension_key}"
                    for contribution in dimensionals
                }
            )
        )
        canonical_dimensions = (
            tuple(
                sorted(
                    require_text("affected_dimension", item)
                    for item in affected_dimensions
                )
            )
            if affected_dimensions is not None
            else derived_dimensions
        )
        if (
            affected_dimensions is not None
            and canonical_dimensions != derived_dimensions
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PROJECTION_SOURCE_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="affected dimensions must match canonical derivation from dimensional contributions",
                    metadata={"projection_id": projection_id},
                )
            )
        projected = _build_projected_totals(
            before=current_snapshot,
            candidate=candidate,
            contributions=top_level,
        )
        return cls(
            projection_id=projection_id,
            portfolio_id=current_snapshot.portfolio_id,
            account_id=current_snapshot.account_id,
            portfolio_kind=current_snapshot.portfolio_kind,
            currency=current_snapshot.currency,
            current_snapshot_id=current_snapshot.snapshot_id,
            current_snapshot_hash=current_snapshot.canonical_input_hash,
            candidate_id=candidate.candidate_id,
            candidate_input_hash=candidate.canonical_input_hash,
            before_totals=ExposureTotalsV1(
                stake_committed=current_snapshot.stake_committed,
                stake_reserved=current_snapshot.stake_reserved,
                maximum_possible_loss=current_snapshot.maximum_possible_loss,
                potential_profit=current_snapshot.potential_profit,
                gross_payout=current_snapshot.gross_payout,
                net_liability=current_snapshot.net_liability,
                currency=current_snapshot.currency,
            ),
            candidate_top_level_contributions=top_level,
            candidate_dimension_contributions=dimensionals,
            projected_totals=projected,
            affected_dimensions=canonical_dimensions,
            bankroll_reference_id=bankroll_reference.bankroll_reference_id,
            bankroll_reference_hash=bankroll_reference.canonical_input_hash,
            bankroll_available_balance=bankroll_reference.available_balance,
            bankroll_reserved_balance=bankroll_reference.reserved_balance,
            hypothetical_available_after=normalize_monetary_decimal(
                "hypothetical_available_after",
                bankroll_reference.available_balance - candidate.allocation_amount,
                bankroll_reference.currency,
            ),
            calculation_version=current_snapshot.calculation_version,
            as_of=current_snapshot.as_of,
            reasons=tuple(reasons),
            status=status,
            current_snapshot_evidence=current_snapshot,
            candidate_evidence=candidate,
            bankroll_evidence=bankroll_reference,
        )

    def __post_init__(
        self,
        current_snapshot_evidence: AuthoritativeExposureSnapshotV1 | None,
        candidate_evidence: ExposureCandidateV1 | None,
        bankroll_evidence: BankrollReferenceV1 | None,
    ) -> None:
        object.__setattr__(
            self,
            "_authoritative_snapshot_evidence",
            current_snapshot_evidence,
        )
        object.__setattr__(
            self,
            "_authoritative_candidate_evidence",
            candidate_evidence,
        )
        object.__setattr__(
            self,
            "_authoritative_bankroll_evidence",
            bankroll_evidence,
        )
        for name, value in (
            ("projection_id", self.projection_id),
            ("portfolio_id", self.portfolio_id),
            ("account_id", self.account_id),
            ("current_snapshot_id", self.current_snapshot_id),
            ("current_snapshot_hash", self.current_snapshot_hash),
            ("candidate_id", self.candidate_id),
            ("candidate_input_hash", self.candidate_input_hash),
            ("bankroll_reference_id", self.bankroll_reference_id),
            ("bankroll_reference_hash", self.bankroll_reference_hash),
        ):
            object.__setattr__(self, name, require_text(name, value))
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        object.__setattr__(self, "as_of", normalize_utc_timestamp("as_of", self.as_of))
        object.__setattr__(
            self,
            "calculation_version",
            require_supported_version(
                "calculation_version",
                self.calculation_version,
                supported=SUPPORTED_CALCULATION_VERSIONS,
            ),
        )
        object.__setattr__(
            self,
            "schema_version",
            require_supported_version(
                "schema_version",
                self.schema_version,
                supported=SUPPORTED_SCHEMA_VERSIONS,
            ),
        )
        object.__setattr__(
            self,
            "affected_dimensions",
            tuple(
                sorted(
                    require_text("affected_dimension", item)
                    for item in self.affected_dimensions
                )
            ),
        )
        object.__setattr__(
            self,
            "candidate_top_level_contributions",
            tuple(
                sorted(
                    tuple(self.candidate_top_level_contributions),
                    key=lambda item: (item.measure.value, item.contribution_id),
                )
            ),
        )
        object.__setattr__(
            self,
            "candidate_dimension_contributions",
            tuple(
                sorted(
                    tuple(self.candidate_dimension_contributions),
                    key=lambda item: (
                        item.dimension_type.value,
                        item.dimension_key,
                        item.contribution_id,
                    ),
                )
            ),
        )
        object.__setattr__(
            self,
            "reasons",
            tuple(
                sorted(
                    tuple(self.reasons),
                    key=lambda item: item.canonical_sort_key(),
                )
            ),
        )
        assert_same_currency(
            "projection before totals",
            self.currency,
            self.before_totals.currency,
        )
        assert_same_currency(
            "projection projected totals",
            self.currency,
            self.projected_totals.currency,
        )
        assert_same_currency(
            "projection totals",
            self.before_totals.currency,
            self.projected_totals.currency,
        )
        object.__setattr__(
            self,
            "bankroll_available_balance",
            normalize_monetary_decimal(
                "bankroll_available_balance",
                self.bankroll_available_balance,
                self.before_totals.currency,
            ),
        )
        object.__setattr__(
            self,
            "bankroll_reserved_balance",
            normalize_monetary_decimal(
                "bankroll_reserved_balance",
                self.bankroll_reserved_balance,
                self.before_totals.currency,
            ),
        )
        if self.hypothetical_available_after is not None:
            object.__setattr__(
                self,
                "hypothetical_available_after",
                normalize_monetary_decimal(
                    "hypothetical_available_after",
                    self.hypothetical_available_after,
                    self.before_totals.currency,
                ),
            )
        _validate_projection_authority_evidence(
            self,
            current_snapshot=current_snapshot_evidence,
            candidate=candidate_evidence,
            bankroll_reference=bankroll_evidence,
        )
        assert candidate_evidence is not None
        _validate_projection_consistency(self, candidate=candidate_evidence)
        computed = stable_hash(self.hash_payload())
        if self.canonical_input_hash and self.canonical_input_hash != computed:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="projection canonical hash does not match authoritative payload",
                    metadata={"projection_id": self.projection_id},
                )
            )
        object.__setattr__(self, "canonical_input_hash", computed)
        object.__setattr__(
            self,
            "_authoritative_origin_hash",
            _seal_authoritative_hash(computed),
        )

    def hash_payload(self) -> dict[str, Any]:
        return {
            "projection_id": self.projection_id,
            "portfolio_id": self.portfolio_id,
            "account_id": self.account_id,
            "portfolio_kind": self.portfolio_kind.value,
            "currency": self.currency,
            "current_snapshot_id": self.current_snapshot_id,
            "current_snapshot_hash": self.current_snapshot_hash,
            "candidate_id": self.candidate_id,
            "candidate_input_hash": self.candidate_input_hash,
            "before_totals": self.before_totals.as_mapping(),
            "candidate_top_level_contributions": [
                item.hash_payload() for item in self.candidate_top_level_contributions
            ],
            "candidate_dimension_contributions": [
                item.hash_payload() for item in self.candidate_dimension_contributions
            ],
            "projected_totals": self.projected_totals.as_mapping(),
            "affected_dimensions": self.affected_dimensions,
            "bankroll_reference_id": self.bankroll_reference_id,
            "bankroll_reference_hash": self.bankroll_reference_hash,
            "bankroll_available_balance": self.bankroll_available_balance,
            "bankroll_reserved_balance": self.bankroll_reserved_balance,
            "hypothetical_available_after": self.hypothetical_available_after,
            "calculation_version": self.calculation_version,
            "as_of": self.as_of,
            "reasons": self.reasons,
            "status": self.status.value,
            "schema_version": self.schema_version,
        }


def _init_field_payload(
    instance: object,
    *,
    omitted: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    return {
        field_spec.name: getattr(instance, field_spec.name)
        for field_spec in fields(type(instance))
        if field_spec.init and field_spec.name not in omitted
    }


def _rebuild_leg(leg: ExposureLegV1) -> ExposureLegV1:
    if not isinstance(leg, ExposureLegV1):
        raise ValueError("authoritative legs must be ExposureLegV1 objects")
    if type(leg.canonical_sportsbook_id) is not CanonicalSportsbookIdV1:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                message="leg taxonomy fields differ from constructed authority",
                metadata={"leg_id": leg.leg_id},
            )
        )
    return ExposureLegV1(**_init_field_payload(leg))


def _raise_contract_integrity_error(
    *,
    contract_name: str,
    identity_name: str,
    identity_value: str,
) -> None:
    raise_domain_error(
        build_reason(
            code=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
            severity=ExposureReasonSeverityV1.ERROR,
            category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
            message=(
                f"{contract_name} differs from constructor-normalized "
                "semantic authority"
            ),
            metadata={identity_name: identity_value},
        )
    )


def _validate_record_contract_integrity(record: ExposureRecordV1) -> None:
    if not isinstance(record, ExposureRecordV1):
        raise ValueError("record must be ExposureRecordV1")
    if (
        type(record.record_kind) is not ExposureRecordKindV1
        or type(record.portfolio_kind) is not ExposurePortfolioKindV1
        or type(record.state) is not ExposureRecordStateV1
        or type(record.canonical_sportsbook_id) is not CanonicalSportsbookIdV1
    ):
        _raise_contract_integrity_error(
            contract_name="record",
            identity_name="record_id",
            identity_value=record.record_id,
        )
    payload = _init_field_payload(
        record,
        omitted=frozenset({"canonical_record_hash"}),
    )
    payload["legs"] = tuple(_rebuild_leg(leg) for leg in record.legs)
    if record.manual_provenance is not None:
        payload["manual_provenance"] = ManualRecordedRealProvenanceV1(
            **_init_field_payload(record.manual_provenance)
        )
    rebuilt = ExposureRecordV1(**payload)
    if (
        rebuilt.canonical_record_hash != record.canonical_record_hash
        or stable_hash(record.hash_payload()) != record.canonical_record_hash
        or not _authoritative_hash_matches_seal(
            record.canonical_record_hash,
            record._authoritative_origin_hash,
        )
    ):
        _raise_contract_integrity_error(
            contract_name="record",
            identity_name="record_id",
            identity_value=record.record_id,
        )


def _validate_candidate_contract_integrity(
    candidate: ExposureCandidateV1,
) -> None:
    if not isinstance(candidate, ExposureCandidateV1):
        raise ValueError("candidate must be ExposureCandidateV1")
    if (
        type(candidate.portfolio_kind) is not ExposurePortfolioKindV1
        or type(candidate.canonical_sportsbook_id) is not CanonicalSportsbookIdV1
    ):
        _raise_contract_integrity_error(
            contract_name="candidate",
            identity_name="candidate_id",
            identity_value=candidate.candidate_id,
        )
    payload = _init_field_payload(
        candidate,
        omitted=frozenset({"canonical_input_hash"}),
    )
    payload["legs"] = tuple(_rebuild_leg(leg) for leg in candidate.legs)
    rebuilt = ExposureCandidateV1(**payload)
    if (
        rebuilt.canonical_input_hash != candidate.canonical_input_hash
        or stable_hash(candidate.hash_payload()) != candidate.canonical_input_hash
        or not _authoritative_hash_matches_seal(
            candidate.canonical_input_hash,
            candidate._authoritative_origin_hash,
        )
    ):
        _raise_contract_integrity_error(
            contract_name="candidate",
            identity_name="candidate_id",
            identity_value=candidate.candidate_id,
        )


def _validate_bankroll_contract_integrity(
    bankroll: BankrollReferenceV1,
) -> None:
    if not isinstance(bankroll, BankrollReferenceV1):
        raise ValueError("bankroll must be BankrollReferenceV1")
    rebuilt = BankrollReferenceV1(
        **_init_field_payload(
            bankroll,
            omitted=frozenset({"canonical_input_hash"}),
        )
    )
    if (
        rebuilt.canonical_input_hash != bankroll.canonical_input_hash
        or stable_hash(bankroll.hash_payload()) != bankroll.canonical_input_hash
        or not _authoritative_hash_matches_seal(
            bankroll.canonical_input_hash,
            bankroll._authoritative_origin_hash,
        )
    ):
        _raise_contract_integrity_error(
            contract_name="bankroll reference",
            identity_name="bankroll_reference_id",
            identity_value=bankroll.bankroll_reference_id,
        )


def _validate_contribution_contract_integrity(
    contribution: ExposureContributionV1,
) -> None:
    if not isinstance(contribution, ExposureContributionV1):
        raise ValueError("contribution must be ExposureContributionV1")
    if (
        type(contribution.dimension_type) is not ExposureDimensionTypeV1
        or type(contribution.measure) is not ExposureMeasureV1
        or type(contribution.portfolio_kind) is not ExposurePortfolioKindV1
        or type(contribution.position_state) is not ExposureRecordStateV1
        or type(contribution.contribution_scope) is not ExposureContributionScopeV1
    ):
        _raise_contract_integrity_error(
            contract_name="contribution",
            identity_name="contribution_id",
            identity_value=contribution.contribution_id,
        )
    rebuilt = ExposureContributionV1(
        **_init_field_payload(
            contribution,
            omitted=frozenset({"canonical_contribution_hash"}),
        )
    )
    if (
        rebuilt.canonical_contribution_hash != contribution.canonical_contribution_hash
        or stable_hash(contribution.hash_payload())
        != contribution.canonical_contribution_hash
        or not _authoritative_hash_matches_seal(
            contribution.canonical_contribution_hash,
            contribution._authoritative_origin_hash,
        )
    ):
        _raise_contract_integrity_error(
            contract_name="contribution",
            identity_name="contribution_id",
            identity_value=contribution.contribution_id,
        )


def validate_authoritative_projection_integrity(
    projection: ExposureProjectionV1,
) -> ExposureProjectionV1:
    if not isinstance(projection, ExposureProjectionV1):
        raise ValueError("projection must be ExposureProjectionV1")
    computed = stable_hash(projection.hash_payload())
    if (
        computed != projection.canonical_input_hash
        or not _authoritative_hash_matches_seal(
            projection.canonical_input_hash,
            projection._authoritative_origin_hash,
        )
    ):
        _raise_contract_integrity_error(
            contract_name="projection",
            identity_name="projection_id",
            identity_value=projection.projection_id,
        )
    current_snapshot = projection._authoritative_snapshot_evidence
    candidate = projection._authoritative_candidate_evidence
    bankroll = projection._authoritative_bankroll_evidence
    _validate_projection_authority_evidence(
        projection,
        current_snapshot=current_snapshot,
        candidate=candidate,
        bankroll_reference=bankroll,
    )
    assert candidate is not None
    _validate_projection_consistency(projection, candidate=candidate)
    return projection


def compute_portfolio_total_from_contributions(
    contributions: tuple[ExposureContributionV1, ...] | list[ExposureContributionV1],
    *,
    measure: ExposureMeasureV1,
    currency: str,
    snapshot_or_projection_id: str,
    portfolio_id: str,
    account_id: str,
    portfolio_kind: ExposurePortfolioKindV1,
    as_of: str,
    calculation_version: str,
    authoritative_records: tuple[ExposureRecordV1, ...] | list[ExposureRecordV1],
    authoritative_candidates: tuple[ExposureCandidateV1, ...]
    | list[ExposureCandidateV1],
) -> Decimal:
    try:
        normalized_measure = ExposureMeasureV1(measure)
    except (TypeError, ValueError) as exc:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_INVALID_DIMENSION_KEY,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message="portfolio summation requires a frozen exposure measure",
                metadata={
                    "measure": (
                        measure if isinstance(measure, str) else type(measure).__name__
                    )
                },
            )
        )
        raise AssertionError("unreachable") from exc
    normalized_currency = normalize_currency(currency)
    normalized_scope_id = require_text(
        "snapshot_or_projection_id", snapshot_or_projection_id
    )
    normalized_portfolio_id = require_text("portfolio_id", portfolio_id)
    normalized_account_id = require_text("account_id", account_id)
    normalized_portfolio_kind = ExposurePortfolioKindV1(portfolio_kind)
    normalized_as_of = normalize_utc_timestamp("as_of", as_of)
    normalized_calculation_version = require_supported_version(
        "calculation_version",
        calculation_version,
        supported=SUPPORTED_CALCULATION_VERSIONS,
    )
    record_objects = tuple(authoritative_records)
    record_identities = validate_record_collection(
        record_objects,
        as_of=normalized_as_of,
    )
    all_record_identities = _derive_all_record_identities(record_objects)
    all_records_by_id = {
        record.record_id: record
        for record in record_objects
        if isinstance(record, ExposureRecordV1)
    }
    head_ids = {identity.record_id for identity in record_identities}
    records_by_id = {
        record_id: record
        for record_id, record in all_records_by_id.items()
        if record_id in head_ids
    }
    for record in all_records_by_id.values():
        if (
            record.portfolio_id != normalized_portfolio_id
            or record.account_id != normalized_account_id
            or record.portfolio_kind != normalized_portfolio_kind
            or record.currency != normalized_currency
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PROJECTION_SOURCE_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                    message="authoritative record object does not match summation scope",
                    metadata={"record_id": record.record_id},
                )
            )
    candidates_by_id: dict[str, ExposureCandidateV1] = {}
    for candidate in authoritative_candidates:
        if not isinstance(candidate, ExposureCandidateV1):
            raise ValueError(
                "authoritative_candidates must contain ExposureCandidateV1 objects"
            )
        _validate_candidate_contract_integrity(candidate)
        if stable_hash(candidate.hash_payload()) != candidate.canonical_input_hash:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                    message="candidate hash does not match its actual object",
                    metadata={"candidate_id": candidate.candidate_id},
                )
            )
        if not _authoritative_hash_matches_seal(
            candidate.canonical_input_hash,
            candidate._authoritative_origin_hash,
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                    message=(
                        "candidate hash differs from its process-local "
                        "construction seal"
                    ),
                    metadata={"candidate_id": candidate.candidate_id},
                )
            )
        if candidate.candidate_id in candidates_by_id:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_CANDIDATE_COLLISION,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                    message="authoritative candidate ids must be unique",
                    metadata={"candidate_id": candidate.candidate_id},
                )
            )
        if (
            candidate.portfolio_id != normalized_portfolio_id
            or candidate.account_id != normalized_account_id
            or candidate.portfolio_kind != normalized_portfolio_kind
            or candidate.currency != normalized_currency
            or candidate.as_of != normalized_as_of
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PROJECTION_SOURCE_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                    message="authoritative candidate object does not match summation scope",
                    metadata={"candidate_id": candidate.candidate_id},
                )
            )
        validate_candidate_collision(
            candidate_id=candidate.candidate_id,
            candidate_hash=candidate.canonical_input_hash,
            record_identities=all_record_identities,
        )
        candidates_by_id[candidate.candidate_id] = candidate
    contribution_objects = tuple(contributions)
    for contribution in contribution_objects:
        if not isinstance(contribution, ExposureContributionV1):
            raise ValueError(
                "contributions must contain only ExposureContributionV1 objects"
            )
        _validate_contribution_contract_integrity(contribution)
    assembly_schema_versions = (
        {record.schema_version for record in record_objects}
        | {candidate.schema_version for candidate in candidates_by_id.values()}
        | {contribution.schema_version for contribution in contribution_objects}
    )
    if len(assembly_schema_versions) > 1:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_PROJECTION_VERSION_MISMATCH,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message=(
                    "generic contribution assembly requires one schema "
                    "version across all bound objects"
                ),
                metadata={"schema_versions": tuple(sorted(assembly_schema_versions))},
            )
        )
    seen_contribution_ids: set[str] = set()
    seen_top_level_keys: set[tuple[str, ExposureMeasureV1]] = set()
    selected_amounts: list[Decimal] = []
    for contribution in contribution_objects:
        if contribution.contribution_id in seen_contribution_ids:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_DUPLICATE_CONTRIBUTION_ID,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                    message="contribution ids must be globally unique before summation",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        seen_contribution_ids.add(contribution.contribution_id)
        if (
            stable_hash(contribution.hash_payload())
            != contribution.canonical_contribution_hash
            or not _authoritative_hash_matches_seal(
                contribution.canonical_contribution_hash,
                contribution._authoritative_origin_hash,
            )
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                    message="contribution hash does not match its actual payload",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        assert_same_currency("contribution", normalized_currency, contribution.currency)
        if (
            contribution.snapshot_or_projection_id != normalized_scope_id
            or contribution.portfolio_id != normalized_portfolio_id
            or contribution.account_id != normalized_account_id
            or contribution.portfolio_kind != normalized_portfolio_kind
            or contribution.as_of != normalized_as_of
            or contribution.calculation_version != normalized_calculation_version
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PROJECTION_SOURCE_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="contribution scope does not match validated summation scope",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        if contribution.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_UNSUPPORTED_CONTRIBUTION_SCHEMA_VERSION,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="unsupported contribution schema version",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        if contribution.record_id:
            record = records_by_id.get(contribution.record_id)
            if record is None:
                raise_domain_error(
                    build_reason(
                        code=ExposureReasonCodeV1.EXP_CONTRIBUTION_SOURCE_HASH_MISMATCH,
                        severity=ExposureReasonSeverityV1.ERROR,
                        category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                        message="record contribution has no bound authoritative record object",
                        metadata={"contribution_id": contribution.contribution_id},
                    )
                )
            if (
                contribution.source_input_hash != record.canonical_record_hash
                or contribution.position_id != record.position_id
                or contribution.order_intent_id != record.order_intent_id
                or contribution.reservation_id != record.reservation_id
                or contribution.parlay_id != record.parlay_id
                or contribution.source_repository != record.source_repository
            ):
                raise_domain_error(
                    build_reason(
                        code=ExposureReasonCodeV1.EXP_CONTRIBUTION_SOURCE_HASH_MISMATCH,
                        severity=ExposureReasonSeverityV1.ERROR,
                        category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                        message="record contribution does not match its actual bound record",
                        metadata={"contribution_id": contribution.contribution_id},
                    )
                )
            if contribution.position_state != record.state:
                raise_domain_error(
                    build_reason(
                        code=ExposureReasonCodeV1.EXP_INVALID_CONTRIBUTION_STATE,
                        severity=ExposureReasonSeverityV1.ERROR,
                        category=ExposureReasonCategoryV1.STATE,
                        message="record contribution state must match its bound record object",
                        metadata={"contribution_id": contribution.contribution_id},
                    )
                )
            if contribution.schema_version != record.schema_version:
                raise_domain_error(
                    build_reason(
                        code=ExposureReasonCodeV1.EXP_PROJECTION_VERSION_MISMATCH,
                        severity=ExposureReasonSeverityV1.ERROR,
                        category=ExposureReasonCategoryV1.CALCULATION,
                        message="record and contribution schema versions must align",
                        metadata={"contribution_id": contribution.contribution_id},
                    )
                )
            expected_amount = _record_measure_amount(
                record,
                contribution.measure,
            )
            expected_dimension_specs = _required_record_dimension_specs(record)
            economics_error_code = ExposureReasonCodeV1.EXP_SNAPSHOT_TOTALS_MISMATCH
        else:
            candidate = candidates_by_id.get(contribution.candidate_id or "")
            if candidate is None:
                raise_domain_error(
                    build_reason(
                        code=ExposureReasonCodeV1.EXP_CONTRIBUTION_SOURCE_HASH_MISMATCH,
                        severity=ExposureReasonSeverityV1.ERROR,
                        category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                        message="candidate contribution has no bound authoritative candidate object",
                        metadata={"contribution_id": contribution.contribution_id},
                    )
                )
            if contribution.position_state != ExposureRecordStateV1.PROPOSED_CANDIDATE:
                raise_domain_error(
                    build_reason(
                        code=ExposureReasonCodeV1.EXP_INVALID_CONTRIBUTION_STATE,
                        severity=ExposureReasonSeverityV1.ERROR,
                        category=ExposureReasonCategoryV1.STATE,
                        message="candidate contribution must use proposed_candidate state",
                        metadata={"contribution_id": contribution.contribution_id},
                    )
                )
            if any(
                identity is not None
                for identity in (
                    contribution.position_id,
                    contribution.order_intent_id,
                    contribution.reservation_id,
                )
            ):
                raise_domain_error(
                    build_reason(
                        code=ExposureReasonCodeV1.EXP_CONTRIBUTION_LINEAGE_FAILURE,
                        severity=ExposureReasonSeverityV1.ERROR,
                        category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                        message="candidate contribution cannot claim record lifecycle identity",
                        metadata={"contribution_id": contribution.contribution_id},
                    )
                )
            if (
                contribution.source_input_hash != candidate.canonical_input_hash
                or contribution.parlay_id != candidate.parlay_id
                or contribution.source_repository != "sports.execution.exposure_v1"
            ):
                raise_domain_error(
                    build_reason(
                        code=ExposureReasonCodeV1.EXP_CONTRIBUTION_SOURCE_HASH_MISMATCH,
                        severity=ExposureReasonSeverityV1.ERROR,
                        category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                        message="candidate contribution does not match its actual bound candidate",
                        metadata={"contribution_id": contribution.contribution_id},
                    )
                )
            if contribution.schema_version != candidate.schema_version:
                raise_domain_error(
                    build_reason(
                        code=ExposureReasonCodeV1.EXP_PROJECTION_VERSION_MISMATCH,
                        severity=ExposureReasonSeverityV1.ERROR,
                        category=ExposureReasonCategoryV1.CALCULATION,
                        message="candidate and contribution schema versions must align",
                        metadata={"contribution_id": contribution.contribution_id},
                    )
                )
            expected_amount = _candidate_measure_amount(
                candidate,
                contribution.measure,
            )
            expected_dimension_specs = _required_candidate_dimension_specs(candidate)
            economics_error_code = ExposureReasonCodeV1.EXP_CANDIDATE_ECONOMICS_MISMATCH
        if (
            contribution.contribution_scope
            != ExposureContributionScopeV1.TOP_LEVEL_ECONOMIC
        ):
            dimension_spec = (
                contribution.leg_id,
                contribution.dimension_type,
                contribution.dimension_key,
            )
            if (
                dimension_spec not in expected_dimension_specs
                or contribution.measure != ExposureMeasureV1.MAXIMUM_LOSS
                or contribution.amount != expected_amount
            ):
                raise_domain_error(
                    build_reason(
                        code=ExposureReasonCodeV1.EXP_INVALID_DIMENSION_KEY,
                        severity=ExposureReasonSeverityV1.ERROR,
                        category=ExposureReasonCategoryV1.CALCULATION,
                        message="dimension contribution does not derive from its bound source object",
                        metadata={"contribution_id": contribution.contribution_id},
                    )
                )
            continue
        if contribution.dimension_key != normalized_portfolio_id:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_INVALID_CONTRIBUTION_SCOPE,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="top-level contribution must use the authoritative portfolio key",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        if contribution.amount != expected_amount:
            raise_domain_error(
                build_reason(
                    code=economics_error_code,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="top-level contribution amount does not match its bound source object",
                    metadata={
                        "contribution_id": contribution.contribution_id,
                        "measure": contribution.measure.value,
                    },
                )
            )
        source_id = contribution.record_id or contribution.candidate_id or ""
        key = (source_id, contribution.measure)
        if key in seen_top_level_keys:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_DUPLICATE_TOP_LEVEL_MEASURE,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="duplicate top-level economic contribution for same source and measure",
                    metadata={
                        "source_id": source_id,
                        "measure": contribution.measure.value,
                    },
                )
            )
        seen_top_level_keys.add(key)
        if contribution.measure == normalized_measure:
            selected_amounts.append(contribution.amount)
    with localcontext() as ctx:
        ctx.prec = 28
        total = sum(selected_amounts, ZERO)
    return normalize_monetary_decimal("portfolio_total", total, normalized_currency)


def _sorted_unique_tuple(
    name: str, values: tuple[str, ...] | list[str]
) -> tuple[str, ...]:
    normalized = [require_text(name, item) for item in values]
    if len(set(normalized)) != len(normalized):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_DUPLICATE_PARLAY_LEG_ID,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                message=f"duplicate {name} values are not allowed",
            )
        )
    return tuple(sorted(normalized))


def _derive_all_record_identities(
    records: tuple[ExposureRecordV1, ...] | list[ExposureRecordV1],
) -> tuple[ExposureRecordIdentity, ...]:
    unique_records = {
        record.record_id: record
        for record in records
        if isinstance(record, ExposureRecordV1)
    }
    return tuple(
        ExposureRecordIdentity.from_record(record)
        for record in sorted(
            unique_records.values(),
            key=lambda item: item.record_id,
        )
    )


def _normalize_legs(
    legs: tuple[ExposureLegV1, ...] | list[ExposureLegV1],
) -> tuple[ExposureLegV1, ...]:
    normalized = tuple(sorted(tuple(legs), key=lambda item: item.leg_id))
    leg_ids = [leg.leg_id for leg in normalized]
    if len(set(leg_ids)) != len(leg_ids):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_DUPLICATE_PARLAY_LEG_ID,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.PARLAY,
                message="duplicate candidate or record leg ids are not allowed",
            )
        )
    return normalized


def _normalize_record_fields(record: ExposureRecordV1) -> None:
    for name, value in (
        ("record_id", record.record_id),
        ("source_repository", record.source_repository),
        ("source_record_id", record.source_record_id),
        ("source_revision", record.source_revision),
        ("portfolio_id", record.portfolio_id),
        ("account_id", record.account_id),
        ("reconciliation_version", record.reconciliation_version),
        ("model_version", record.model_version),
        ("calibration_version", record.calibration_version),
        ("strategy_id", record.strategy_id),
        ("strategy_version", record.strategy_version),
        ("league", record.league),
        ("event_id", record.event_id),
        ("market_id", record.market_id),
        ("market_type", record.market_type),
        ("period", record.period),
        ("settlement_horizon", record.settlement_horizon),
    ):
        object.__setattr__(record, name, require_text(name, value))
    object.__setattr__(
        record,
        "canonical_sportsbook_id",
        _normalize_canonical_sportsbook_id(record.canonical_sportsbook_id),
    )
    object.__setattr__(record, "currency", normalize_currency(record.currency))
    object.__setattr__(
        record,
        "effective_at",
        normalize_utc_timestamp("effective_at", record.effective_at),
    )
    object.__setattr__(
        record,
        "as_of_eligible_at",
        normalize_utc_timestamp("as_of_eligible_at", record.as_of_eligible_at),
    )
    object.__setattr__(
        record,
        "selection_id",
        require_text("selection_id", record.selection_id)
        if record.selection_id
        else None,
    )
    object.__setattr__(
        record,
        "outcome_id",
        require_text("outcome_id", record.outcome_id) if record.outcome_id else None,
    )
    object.__setattr__(
        record,
        "position_id",
        require_text("position_id", record.position_id) if record.position_id else None,
    )
    object.__setattr__(
        record,
        "order_intent_id",
        require_text("order_intent_id", record.order_intent_id)
        if record.order_intent_id
        else None,
    )
    object.__setattr__(
        record,
        "reservation_id",
        require_text("reservation_id", record.reservation_id)
        if record.reservation_id
        else None,
    )
    object.__setattr__(
        record,
        "candidate_projection_id",
        require_text("candidate_projection_id", record.candidate_projection_id)
        if record.candidate_projection_id
        else None,
    )
    object.__setattr__(
        record,
        "reconciliation_result_id",
        require_text("reconciliation_result_id", record.reconciliation_result_id)
        if record.reconciliation_result_id
        else None,
    )
    object.__setattr__(
        record,
        "parlay_id",
        require_text("parlay_id", record.parlay_id) if record.parlay_id else None,
    )
    object.__setattr__(
        record,
        "correction_of_record_id",
        require_text("correction_of_record_id", record.correction_of_record_id)
        if record.correction_of_record_id
        else None,
    )
    object.__setattr__(
        record,
        "reversal_of_record_id",
        require_text("reversal_of_record_id", record.reversal_of_record_id)
        if record.reversal_of_record_id
        else None,
    )
    object.__setattr__(
        record,
        "residual_reference_id",
        require_text("residual_reference_id", record.residual_reference_id)
        if record.residual_reference_id
        else None,
    )
    if record.manual_provenance is not None and not isinstance(
        record.manual_provenance, ManualRecordedRealProvenanceV1
    ):
        raise ValueError(
            "manual_provenance must be ManualRecordedRealProvenanceV1 when supplied"
        )
    object.__setattr__(
        record, "team_ids", _sorted_unique_tuple("team_id", record.team_ids)
    )
    object.__setattr__(
        record, "player_ids", _sorted_unique_tuple("player_id", record.player_ids)
    )
    object.__setattr__(
        record,
        "correlation_group_ids",
        _sorted_unique_tuple("correlation_group_id", record.correlation_group_ids),
    )
    object.__setattr__(record, "legs", _normalize_legs(record.legs))
    object.__setattr__(
        record,
        "schema_version",
        require_supported_version(
            "schema_version", record.schema_version, supported=SUPPORTED_SCHEMA_VERSIONS
        ),
    )
    object.__setattr__(
        record,
        "calculation_input_version",
        require_supported_version(
            "calculation_input_version",
            record.calculation_input_version,
            supported=SUPPORTED_CALCULATION_INPUT_VERSIONS,
        ),
    )
    for monetary_field in (
        "stake_committed",
        "stake_reserved",
        "maximum_possible_loss",
        "potential_profit",
        "gross_payout",
        "net_liability",
    ):
        object.__setattr__(
            record,
            monetary_field,
            normalize_monetary_decimal(
                monetary_field, getattr(record, monetary_field), record.currency
            ),
        )


def _validate_record_semantics(record: ExposureRecordV1) -> None:
    rules = STATE_RULES_BY_STATE[record.state]
    if record.state == ExposureRecordStateV1.PROPOSED_CANDIDATE:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_PROPOSED_CANDIDATE_RECORD_REJECTED,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.STATE,
                message="ExposureRecordV1 cannot represent proposed candidates",
                metadata={"record_id": record.record_id},
            )
        )
    if not (record.selection_id or record.outcome_id):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_MISSING_CANONICAL_IDENTITY,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.IDENTITY,
                message="authoritative records require selection_id or outcome_id",
                metadata={"record_id": record.record_id},
            )
        )
    if record.correction_of_record_id and record.reversal_of_record_id:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_CORRECTION_REVERSAL_CONFLICT,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.STATE,
                message="record cannot be both correction and reversal",
                metadata={"record_id": record.record_id},
            )
        )
    if (
        record.correction_of_record_id == record.record_id
        or record.reversal_of_record_id == record.record_id
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_CORRECTION_REVERSAL_CONFLICT,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.STATE,
                message="record cannot self-reference correction or reversal lineage",
                metadata={"record_id": record.record_id},
            )
        )
    if record.portfolio_kind == ExposurePortfolioKindV1.CASH and record.record_kind in {
        ExposureRecordKindV1.PRACTICE_POSITION,
        ExposureRecordKindV1.SYNTHETIC_POSITION,
    }:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_PORTFOLIO_KIND_CONFLICT,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.STATE,
                message="cash portfolios cannot contain practice or synthetic record kinds",
                metadata={"record_id": record.record_id},
            )
        )
    if (
        record.record_kind == ExposureRecordKindV1.POSITION
        and record.portfolio_kind
        not in {ExposurePortfolioKindV1.CASH, ExposurePortfolioKindV1.RECORDED_REAL}
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_PORTFOLIO_KIND_CONFLICT,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.STATE,
                message="position records must use cash or recorded_real portfolio kinds",
                metadata={"record_id": record.record_id},
            )
        )
    if (
        record.record_kind == ExposureRecordKindV1.SYNTHETIC_POSITION
        and record.portfolio_kind != ExposurePortfolioKindV1.SYNTHETIC
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_PORTFOLIO_KIND_CONFLICT,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.STATE,
                message="synthetic position must use synthetic portfolio kind",
                metadata={"record_id": record.record_id},
            )
        )
    if (
        record.record_kind == ExposureRecordKindV1.PRACTICE_POSITION
        and record.portfolio_kind == ExposurePortfolioKindV1.CASH
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_PORTFOLIO_KIND_CONFLICT,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.STATE,
                message="practice positions cannot participate in cash totals",
                metadata={"record_id": record.record_id},
            )
        )
    if (
        record.record_kind == ExposureRecordKindV1.PRACTICE_POSITION
        and record.portfolio_kind != ExposurePortfolioKindV1.PRACTICE
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_PORTFOLIO_KIND_CONFLICT,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.STATE,
                message="practice positions must use practice portfolio kind",
                metadata={"record_id": record.record_id},
            )
        )
    if (
        record.record_kind == ExposureRecordKindV1.MANUAL_REAL_POSITION
        and record.portfolio_kind != ExposurePortfolioKindV1.RECORDED_REAL
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_PORTFOLIO_KIND_CONFLICT,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.STATE,
                message="manual real positions must use recorded_real portfolio kind",
                metadata={"record_id": record.record_id},
            )
        )
    if (
        record.record_kind == ExposureRecordKindV1.MANUAL_REAL_POSITION
        and record.manual_provenance is None
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_MANUAL_SOURCE_INCOMPLETE,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.SOURCE,
                message="manual recorded-real positions require immutable typed provenance",
                metadata={"record_id": record.record_id},
            )
        )
    if (
        record.record_kind != ExposureRecordKindV1.MANUAL_REAL_POSITION
        and record.manual_provenance is not None
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_MANUAL_SOURCE_INCOMPLETE,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.SOURCE,
                message="manual provenance may only appear on manual recorded-real positions",
                metadata={"record_id": record.record_id},
            )
        )
    if record.record_kind in {
        ExposureRecordKindV1.POSITION,
        ExposureRecordKindV1.MANUAL_REAL_POSITION,
    }:
        if not record.position_id:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_OPEN_POSITION_IDENTITY_MISSING,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.STATE,
                    message="position-like records require position identity",
                    metadata={"record_id": record.record_id},
                )
            )
        if (
            record.portfolio_kind
            in {ExposurePortfolioKindV1.CASH, ExposurePortfolioKindV1.RECORDED_REAL}
            and not record.reconciliation_result_id
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_RECONCILIATION_REFERENCE_MISSING,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.RECONCILIATION_REFERENCE,
                    message="cash-authoritative position records require reconciliation_result_id",
                    metadata={"record_id": record.record_id},
                )
            )
    if record.record_kind == ExposureRecordKindV1.RESERVATION:
        if record.portfolio_kind not in {
            ExposurePortfolioKindV1.CASH,
            ExposurePortfolioKindV1.RECORDED_REAL,
        }:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PORTFOLIO_KIND_CONFLICT,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.STATE,
                    message="reservations must use cash or recorded_real portfolio kinds",
                    metadata={"record_id": record.record_id},
                )
            )
        if not record.reservation_id or record.stake_committed != ZERO:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_RESERVATION_AUTHORITY_MISSING,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.RESERVATION,
                    message="reservation requires reservation_id and zero committed stake",
                    metadata={"record_id": record.record_id},
                )
            )

        active_reservation_states = {
            ExposureRecordStateV1.PENDING_CONFIRMATION,
            ExposureRecordStateV1.SUBMITTED,
            ExposureRecordStateV1.ACCEPTED,
        }
        if record.state in active_reservation_states and record.stake_reserved <= ZERO:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_RESERVATION_AUTHORITY_MISSING,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.RESERVATION,
                    message="active reservation states require positive reserved stake",
                    metadata={"record_id": record.record_id},
                )
            )
        if rules.historical_only and record.stake_reserved != ZERO:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_CURRENT_ECONOMICS_FOR_HISTORICAL_STATE,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.RESERVATION,
                    message="terminal reservations must have zero current reserved exposure",
                    metadata={"record_id": record.record_id},
                )
            )
        if (
            record.potential_profit != ZERO
            or record.gross_payout != ZERO
            or record.maximum_possible_loss != record.stake_reserved
            or record.net_liability != record.stake_reserved
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PAYOUT_INCONSISTENT,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.MONETARY,
                    message="reservation economics must match reserved-liability semantics",
                    metadata={"record_id": record.record_id},
                )
            )
        if record.state not in active_reservation_states and not rules.historical_only:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_RESERVATION_AUTHORITY_MISSING,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.RESERVATION,
                    message="reservation state must be active or terminal historical",
                    metadata={"record_id": record.record_id},
                )
            )
    else:
        if record.stake_reserved != ZERO:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_RESERVATION_AUTHORITY_MISSING,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.RESERVATION,
                    message="only reservation records may carry reserved stake",
                    metadata={"record_id": record.record_id},
                )
            )
        if record.state in {
            ExposureRecordStateV1.PENDING_CONFIRMATION,
            ExposureRecordStateV1.SUBMITTED,
            ExposureRecordStateV1.ACCEPTED,
        }:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_RESERVATION_AUTHORITY_MISSING,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.RESERVATION,
                    message="state alone cannot fabricate reservation exposure without a reservation record",
                    metadata={
                        "record_id": record.record_id,
                        "state": record.state.value,
                    },
                )
            )
    if record.parlay_id and len(record.legs) < 2:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_INVALID_PARLAY_CARDINALITY,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.PARLAY,
                message="parlay_id requires at least two full legs",
                metadata={"record_id": record.record_id},
            )
        )
    if record.legs and not record.parlay_id:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_INVALID_PARLAY_CARDINALITY,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.PARLAY,
                message="legs require a parent parlay_id",
                metadata={"record_id": record.record_id},
            )
        )
    if rules.historical_only:
        if any(
            amount != ZERO
            for amount in (
                record.stake_committed,
                record.stake_reserved,
                record.maximum_possible_loss,
                record.potential_profit,
                record.gross_payout,
                record.net_liability,
            )
        ):
            code = (
                ExposureReasonCodeV1.EXP_CORRECTED_RECORD_NOT_HISTORICAL
                if record.state == ExposureRecordStateV1.CORRECTED
                else ExposureReasonCodeV1.EXP_CURRENT_ECONOMICS_FOR_HISTORICAL_STATE
            )
            raise_domain_error(
                build_reason(
                    code=code,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.STATE,
                    message="historical states cannot retain current exposure economics",
                    metadata={
                        "record_id": record.record_id,
                        "state": record.state.value,
                    },
                )
            )
        return
    if record.record_kind != ExposureRecordKindV1.RESERVATION:
        if record.gross_payout != record.stake_committed + record.potential_profit:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PAYOUT_INCONSISTENT,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.MONETARY,
                    message="gross payout must equal committed stake plus potential profit",
                    metadata={"record_id": record.record_id},
                )
            )
        if all(
            amount == ZERO
            for amount in (
                record.stake_committed,
                record.maximum_possible_loss,
                record.gross_payout,
                record.net_liability,
            )
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_STATE_CONFLICT,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.MONETARY,
                    message="current non-historical records must carry meaningful exposure economics",
                    metadata={"record_id": record.record_id},
                )
            )
        if record.net_liability != record.maximum_possible_loss:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PAYOUT_INCONSISTENT,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.MONETARY,
                    message="net liability must equal maximum possible loss",
                    metadata={"record_id": record.record_id},
                )
            )
    if record.state == ExposureRecordStateV1.OPEN:
        if record.stake_committed <= ZERO or record.position_id is None:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_OPEN_POSITION_IDENTITY_MISSING,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.STATE,
                    message="open positions require identity and positive committed economics",
                    metadata={"record_id": record.record_id},
                )
            )
        if record.stake_reserved != ZERO:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_STATE_CONFLICT,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.STATE,
                    message="open position cannot retain reserved stake in the same record",
                    metadata={"record_id": record.record_id},
                )
            )
    if (
        record.state == ExposureRecordStateV1.PARTIALLY_SETTLED
        and record.stake_committed <= ZERO
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_STATE_CONFLICT,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.STATE,
                message="partially settled records must represent unresolved residual economics",
                metadata={"record_id": record.record_id},
            )
        )
    if record.state == ExposureRecordStateV1.PARTIALLY_SETTLED:
        if not record.position_id or not record.residual_reference_id:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_STATE_CONFLICT,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.STATE,
                    message="partially settled records require position identity and residual reference",
                    metadata={"record_id": record.record_id},
                )
            )


def _validate_candidate_semantics(candidate: ExposureCandidateV1) -> None:
    if candidate.portfolio_kind == ExposurePortfolioKindV1.SYNTHETIC:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_PORTFOLIO_KIND_CONFLICT,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.STATE,
                message="candidate projections cannot target synthetic cash exposure authority",
                metadata={"candidate_id": candidate.candidate_id},
            )
        )
    if candidate.allocation_amount <= ZERO:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_NEGATIVE_MONETARY_VALUE,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.MONETARY,
                message="candidate allocation must be positive",
                metadata={"candidate_id": candidate.candidate_id},
            )
        )
    if (
        candidate.gross_payout
        != candidate.allocation_amount + candidate.potential_profit
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_PAYOUT_INCONSISTENT,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.MONETARY,
                message="candidate gross payout must equal allocation plus potential profit",
                metadata={"candidate_id": candidate.candidate_id},
            )
        )
    if candidate.net_liability != candidate.maximum_possible_loss:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_PAYOUT_INCONSISTENT,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.MONETARY,
                message="candidate net liability must equal maximum possible loss",
                metadata={"candidate_id": candidate.candidate_id},
            )
        )
    if candidate.parlay_id and len(candidate.legs) < 2:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_INVALID_PARLAY_CARDINALITY,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.PARLAY,
                message="candidate parlay_id requires at least two full legs",
                metadata={"candidate_id": candidate.candidate_id},
            )
        )
    if candidate.legs and not candidate.parlay_id:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_INVALID_PARLAY_CARDINALITY,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.PARLAY,
                message="candidate legs require a parent parlay_id",
                metadata={"candidate_id": candidate.candidate_id},
            )
        )


def _derive_contribution_scope(
    dimension_type: ExposureDimensionTypeV1,
    leg_id: str | None,
) -> ExposureContributionScopeV1:
    if leg_id is not None or dimension_type != ExposureDimensionTypeV1.PORTFOLIO:
        return ExposureContributionScopeV1.DIMENSION_ALLOCATION
    return ExposureContributionScopeV1.TOP_LEVEL_ECONOMIC


def _normalize_snapshot_fields(snapshot: AuthoritativeExposureSnapshotV1) -> None:
    try:
        object.__setattr__(
            snapshot,
            "status",
            ExposureAvailabilityStatusV1(snapshot.status),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("status must be an ExposureAvailabilityStatusV1") from exc
    normalized_record_ids = [
        require_text("record_id", item) for item in snapshot.included_record_ids
    ]
    if len(set(normalized_record_ids)) != len(normalized_record_ids):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_DUPLICATE_SNAPSHOT_RECORD_REFERENCE,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                message="snapshot included record ids must be unique",
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )
    object.__setattr__(
        snapshot,
        "included_record_ids",
        tuple(sorted(normalized_record_ids)),
    )
    normalized_record_hashes = {
        require_text("record_hash key", key): require_text("record_hash", value)
        for key, value in dict(snapshot.included_record_hashes).items()
    }
    if set(snapshot.included_record_ids) != set(normalized_record_hashes.keys()):
        raise ValueError(
            "included_record_ids and included_record_hashes must align exactly"
        )
    if len(set(normalized_record_hashes.values())) != len(normalized_record_hashes):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_DUPLICATE_SNAPSHOT_RECORD_REFERENCE,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                message="snapshot record hashes must identify unique canonical records",
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )
    object.__setattr__(
        snapshot,
        "included_record_hashes",
        MappingProxyType(
            dict(sorted(normalized_record_hashes.items(), key=lambda item: item[0]))
        ),
    )
    object.__setattr__(
        snapshot,
        "contribution_refs",
        tuple(
            sorted(
                tuple(snapshot.contribution_refs), key=lambda item: item.contribution_id
            )
        ),
    )
    if len({item.contribution_id for item in snapshot.contribution_refs}) != len(
        snapshot.contribution_refs
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_DUPLICATE_CONTRIBUTION_ID,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                message="snapshot contribution references must be unique",
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )
    if len(
        {item.canonical_contribution_hash for item in snapshot.contribution_refs}
    ) != len(snapshot.contribution_refs):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_DUPLICATE_CONTRIBUTION_ID,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                message="snapshot contribution hashes must be unique",
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )
    object.__setattr__(
        snapshot,
        "stake_committed",
        normalize_monetary_decimal(
            "stake_committed", snapshot.stake_committed, snapshot.currency
        ),
    )
    object.__setattr__(
        snapshot,
        "stake_reserved",
        normalize_monetary_decimal(
            "stake_reserved", snapshot.stake_reserved, snapshot.currency
        ),
    )
    object.__setattr__(
        snapshot,
        "maximum_possible_loss",
        normalize_monetary_decimal(
            "maximum_possible_loss", snapshot.maximum_possible_loss, snapshot.currency
        ),
    )
    object.__setattr__(
        snapshot,
        "potential_profit",
        normalize_monetary_decimal(
            "potential_profit", snapshot.potential_profit, snapshot.currency
        ),
    )
    object.__setattr__(
        snapshot,
        "gross_payout",
        normalize_monetary_decimal(
            "gross_payout", snapshot.gross_payout, snapshot.currency
        ),
    )
    object.__setattr__(
        snapshot,
        "net_liability",
        normalize_monetary_decimal(
            "net_liability", snapshot.net_liability, snapshot.currency
        ),
    )
    object.__setattr__(
        snapshot,
        "exposure_pct_bankroll",
        normalize_ratio_decimal(
            "exposure_pct_bankroll", snapshot.exposure_pct_bankroll
        ),
    )
    object.__setattr__(
        snapshot,
        "exposure_pct_available",
        normalize_ratio_decimal(
            "exposure_pct_available", snapshot.exposure_pct_available
        ),
    )
    object.__setattr__(
        snapshot,
        "counts_by_state",
        _normalize_count_mapping(snapshot.counts_by_state, name="counts_by_state"),
    )
    object.__setattr__(
        snapshot,
        "counts_by_record_kind",
        _normalize_count_mapping(
            snapshot.counts_by_record_kind, name="counts_by_record_kind"
        ),
    )
    object.__setattr__(
        snapshot,
        "counts_by_portfolio_kind",
        _normalize_count_mapping(
            snapshot.counts_by_portfolio_kind, name="counts_by_portfolio_kind"
        ),
    )
    object.__setattr__(
        snapshot,
        "dimension_aggregates",
        tuple(
            sorted(
                tuple(snapshot.dimension_aggregates),
                key=lambda item: (
                    item.dimension_type.value,
                    item.dimension_key,
                    item.measure.value,
                ),
            )
        ),
    )
    aggregate_keys = [
        (item.dimension_type, item.dimension_key, item.measure)
        for item in snapshot.dimension_aggregates
    ]
    if len(set(aggregate_keys)) != len(aggregate_keys):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_DUPLICATE_DIMENSION_AGGREGATE,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                message="snapshot dimension aggregate keys must be unique",
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )
    object.__setattr__(
        snapshot,
        "concentration_ratios",
        tuple(
            sorted(
                tuple(snapshot.concentration_ratios),
                key=lambda item: (
                    item.dimension_type.value,
                    item.dimension_key,
                    item.measure.value,
                ),
            )
        ),
    )
    ratio_keys = [
        (item.dimension_type, item.dimension_key, item.measure)
        for item in snapshot.concentration_ratios
    ]
    if len(set(ratio_keys)) != len(ratio_keys):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_DUPLICATE_DIMENSION_RATIO,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                message="snapshot concentration ratio keys must be unique",
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )
    object.__setattr__(
        snapshot,
        "correlated_maximum_loss_by_group",
        _normalize_decimal_mapping(
            snapshot.correlated_maximum_loss_by_group,
            currency=snapshot.currency,
            name="correlated_maximum_loss_by_group",
        ),
    )
    object.__setattr__(
        snapshot,
        "reasons",
        tuple(
            sorted(
                tuple(snapshot.reasons),
                key=lambda item: item.canonical_sort_key(),
            )
        ),
    )


def _validate_snapshot_record_evidence(
    snapshot: AuthoritativeExposureSnapshotV1,
    *,
    authoritative_records: tuple[ExposureRecordV1, ...] | list[ExposureRecordV1] | None,
    bind_lineage_evidence: bool = False,
) -> Mapping[str, ExposureRecordV1]:
    records = tuple(authoritative_records or ())
    identities = validate_record_collection(records, as_of=snapshot.as_of)
    lineage_record_identities = _derive_all_record_identities(records)
    lineage_records_by_id = {
        record.record_id: record
        for record in records
        if isinstance(record, ExposureRecordV1)
    }
    lineage_record_hashes = {
        record_id: record.canonical_record_hash
        for record_id, record in lineage_records_by_id.items()
    }
    if bind_lineage_evidence:
        object.__setattr__(
            snapshot,
            "authoritative_lineage_record_hashes",
            MappingProxyType(
                dict(
                    sorted(
                        lineage_record_hashes.items(),
                        key=lambda item: item[0],
                    )
                )
            ),
        )
    elif dict(snapshot.authoritative_lineage_record_hashes) != lineage_record_hashes:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                message=(
                    "snapshot full-lineage record references no longer match "
                    "the retained authoritative record evidence"
                ),
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )
    for record_id, record in lineage_records_by_id.items():
        if (
            record.portfolio_id != snapshot.portfolio_id
            or record.account_id != snapshot.account_id
            or record.portfolio_kind != snapshot.portfolio_kind
            or record.currency != snapshot.currency
            or record.schema_version != snapshot.schema_version
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PROJECTION_SOURCE_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                    message=(
                        "snapshot full-lineage record does not match snapshot "
                        "scope or schema"
                    ),
                    metadata={
                        "snapshot_id": snapshot.snapshot_id,
                        "record_id": record_id,
                    },
                )
            )
    actual_hashes = {
        identity.record_id: identity.record_hash for identity in identities
    }
    if tuple(
        sorted(actual_hashes)
    ) != snapshot.included_record_ids or actual_hashes != dict(
        snapshot.included_record_hashes
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                message="snapshot record references must derive from actual validated record heads",
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )
    head_ids = set(actual_hashes)
    records_by_id = {
        record_id: record
        for record_id, record in lineage_records_by_id.items()
        if record_id in head_ids
    }
    for record_id, record in records_by_id.items():
        if (
            record.portfolio_id != snapshot.portfolio_id
            or record.account_id != snapshot.account_id
            or record.portfolio_kind != snapshot.portfolio_kind
            or record.currency != snapshot.currency
            or record.schema_version != snapshot.schema_version
            or STATE_RULES_BY_STATE[record.state].historical_only
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PROJECTION_SOURCE_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                    message="snapshot record head does not match current snapshot scope",
                    metadata={
                        "snapshot_id": snapshot.snapshot_id,
                        "record_id": record_id,
                    },
                )
            )
    object.__setattr__(
        snapshot,
        "authoritative_record_identities",
        tuple(sorted(identities, key=lambda identity: identity.record_id)),
    )
    object.__setattr__(
        snapshot,
        "authoritative_lineage_record_identities",
        lineage_record_identities,
    )
    return MappingProxyType(records_by_id)


def _validate_snapshot_bankroll_evidence(
    snapshot: AuthoritativeExposureSnapshotV1,
    *,
    authoritative_bankroll_reference: BankrollReferenceV1 | None,
    error_code: ExposureReasonCodeV1 = (
        ExposureReasonCodeV1.EXP_BANKROLL_REFERENCE_INVALID
    ),
) -> BankrollReferenceV1:
    bankroll = authoritative_bankroll_reference
    if not isinstance(bankroll, BankrollReferenceV1):
        raise_domain_error(
            build_reason(
                code=error_code,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.BANKROLL_REFERENCE,
                message="authoritative snapshot requires an actual bankroll reference object",
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )
    try:
        _validate_bankroll_contract_integrity(bankroll)
    except (ExposureValidationError, AttributeError, KeyError, TypeError, ValueError):
        raise_domain_error(
            build_reason(
                code=error_code,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.BANKROLL_REFERENCE,
                message=(
                    "snapshot bankroll reference fails constructor semantic "
                    "revalidation"
                ),
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )
    if (
        stable_hash(bankroll.hash_payload()) != bankroll.canonical_input_hash
        or not _authoritative_hash_matches_seal(
            bankroll.canonical_input_hash,
            bankroll._authoritative_origin_hash,
        )
        or bankroll.bankroll_reference_id != snapshot.bankroll_reference_id
        or bankroll.canonical_input_hash != snapshot.bankroll_reference_hash
        or bankroll.portfolio_id != snapshot.portfolio_id
        or bankroll.account_id != snapshot.account_id
        or bankroll.currency != snapshot.currency
        or bankroll.as_of != snapshot.as_of
        or bankroll.schema_version != snapshot.schema_version
        or bankroll.available_balance + bankroll.reserved_balance
        > bankroll.total_bankroll
    ):
        raise_domain_error(
            build_reason(
                code=error_code,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.BANKROLL_REFERENCE,
                message="snapshot bankroll reference must derive from the bound bankroll object",
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )
    return bankroll


def _apply_snapshot_denominator_availability(
    snapshot: AuthoritativeExposureSnapshotV1,
    *,
    bankroll_reference: BankrollReferenceV1,
) -> None:
    denominator_codes = frozenset(
        {
            ExposureReasonCodeV1.EXP_BANKROLL_DENOMINATOR_ZERO,
            ExposureReasonCodeV1.EXP_AVAILABLE_BALANCE_ZERO,
        }
    )
    reasons = [
        reason for reason in snapshot.reasons if reason.code not in denominator_codes
    ]
    if snapshot.maximum_possible_loss > ZERO:
        if bankroll_reference.total_bankroll == ZERO:
            reasons.append(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_BANKROLL_DENOMINATOR_ZERO,
                    severity=ExposureReasonSeverityV1.WARNING,
                    category=ExposureReasonCategoryV1.BANKROLL_REFERENCE,
                    message=(
                        "total bankroll denominator is zero while current "
                        "maximum loss is positive"
                    ),
                    metadata={
                        "snapshot_id": snapshot.snapshot_id,
                        "bankroll_reference_id": (
                            bankroll_reference.bankroll_reference_id
                        ),
                    },
                )
            )
        if bankroll_reference.available_balance == ZERO:
            reasons.append(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_AVAILABLE_BALANCE_ZERO,
                    severity=ExposureReasonSeverityV1.WARNING,
                    category=ExposureReasonCategoryV1.BANKROLL_REFERENCE,
                    message=(
                        "available-balance denominator is zero while current "
                        "maximum loss is positive"
                    ),
                    metadata={
                        "snapshot_id": snapshot.snapshot_id,
                        "bankroll_reference_id": (
                            bankroll_reference.bankroll_reference_id
                        ),
                    },
                )
            )
    if any(reason.code in denominator_codes for reason in reasons):
        object.__setattr__(
            snapshot,
            "status",
            (
                ExposureAvailabilityStatusV1.ERROR
                if snapshot.status == ExposureAvailabilityStatusV1.ERROR
                else ExposureAvailabilityStatusV1.UNAVAILABLE
            ),
        )
    object.__setattr__(
        snapshot,
        "reasons",
        tuple(sorted(reasons, key=lambda reason: reason.canonical_sort_key())),
    )


def _revalidate_snapshot_authority(
    snapshot: AuthoritativeExposureSnapshotV1,
    *,
    projection_context: bool = False,
    constructing: bool = False,
) -> None:
    records_by_id = _validate_snapshot_record_evidence(
        snapshot,
        authoritative_records=snapshot._authoritative_records_evidence,
        bind_lineage_evidence=constructing,
    )
    if (
        not constructing
        and _snapshot_lineage_evidence_hash(snapshot)
        != snapshot._authoritative_lineage_evidence_hash
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                message=(
                    "snapshot full-lineage evidence differs from its "
                    "construction-time seal"
                ),
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )
    bankroll_reference = _validate_snapshot_bankroll_evidence(
        snapshot,
        authoritative_bankroll_reference=snapshot._authoritative_bankroll_evidence,
        error_code=(
            ExposureReasonCodeV1.EXP_PROJECTION_OBJECT_MISMATCH
            if projection_context
            else ExposureReasonCodeV1.EXP_BANKROLL_REFERENCE_INVALID
        ),
    )
    _apply_snapshot_denominator_availability(
        snapshot,
        bankroll_reference=bankroll_reference,
    )
    if (
        not constructing
        and _snapshot_availability_evidence_hash(snapshot)
        != snapshot._authoritative_availability_evidence_hash
    ):
        if projection_context:
            _raise_projection_object_mismatch(
                "snapshot availability status or reasons differ from construction evidence"
            )
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                message=(
                    "snapshot availability status or reasons differ from "
                    "construction evidence"
                ),
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )
    if (
        not constructing
        and _snapshot_bankroll_evidence_hash(snapshot)
        != snapshot._authoritative_bankroll_evidence_hash
    ):
        if projection_context:
            _raise_projection_object_mismatch(
                "snapshot bankroll evidence differs from construction evidence"
            )
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                message=(
                    "snapshot bankroll evidence differs from construction evidence"
                ),
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )
    _validate_snapshot_counts(snapshot)
    _validate_snapshot_consistency(snapshot)
    _validate_snapshot_contribution_evidence(
        snapshot,
        authoritative_contributions=(snapshot._authoritative_contributions_evidence),
        records_by_id=records_by_id,
        bankroll_reference=bankroll_reference,
    )


def _snapshot_availability_evidence_hash(
    snapshot: AuthoritativeExposureSnapshotV1,
) -> str:
    return stable_hash(
        {
            "status": snapshot.status.value,
            "reasons": snapshot.reasons,
        }
    )


def _snapshot_lineage_evidence_hash(
    snapshot: AuthoritativeExposureSnapshotV1,
) -> str:
    return stable_hash(dict(snapshot.authoritative_lineage_record_hashes))


def _snapshot_bankroll_evidence_hash(
    snapshot: AuthoritativeExposureSnapshotV1,
) -> str:
    bankroll = snapshot._authoritative_bankroll_evidence
    if not isinstance(bankroll, BankrollReferenceV1):
        return ""
    return stable_hash(
        {
            "bankroll_reference_id": bankroll.bankroll_reference_id,
            "canonical_input_hash": bankroll.canonical_input_hash,
            "origin_hash": bankroll._authoritative_origin_hash,
        }
    )


def _validate_snapshot_counts(
    snapshot: AuthoritativeExposureSnapshotV1,
) -> None:
    def count_values(values: tuple[str, ...]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for value in values:
            counts[value] = counts.get(value, 0) + 1
        return counts

    identities = snapshot.authoritative_record_identities
    expected_state = count_values(tuple(item.state for item in identities))
    expected_record_kind = count_values(tuple(item.record_kind for item in identities))
    expected_portfolio_kind = count_values(
        tuple(item.portfolio_kind.value for item in identities)
    )
    if (
        dict(snapshot.counts_by_state) != expected_state
        or dict(snapshot.counts_by_record_kind) != expected_record_kind
        or dict(snapshot.counts_by_portfolio_kind) != expected_portfolio_kind
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_SNAPSHOT_COUNTS_MISMATCH,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message="snapshot counts must derive exactly from validated record heads",
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )


def _validate_snapshot_contribution_evidence(
    snapshot: AuthoritativeExposureSnapshotV1,
    *,
    authoritative_contributions: tuple[ExposureContributionV1, ...]
    | list[ExposureContributionV1]
    | None,
    records_by_id: Mapping[str, ExposureRecordV1],
    bankroll_reference: BankrollReferenceV1,
) -> None:
    contributions = tuple(authoritative_contributions or ())
    refs_by_id = {
        ref.contribution_id: ref.canonical_contribution_hash
        for ref in snapshot.contribution_refs
    }
    evidence_by_id: dict[str, ExposureContributionV1] = {}
    for contribution in contributions:
        if not isinstance(contribution, ExposureContributionV1):
            raise ValueError(
                "authoritative_contributions must contain ExposureContributionV1"
            )
        _validate_contribution_contract_integrity(contribution)
        if contribution.contribution_id in evidence_by_id:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_DUPLICATE_CONTRIBUTION_ID,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                    message="snapshot contribution evidence ids must be unique",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        evidence_by_id[contribution.contribution_id] = contribution
    evidence_hashes = {
        contribution_id: contribution.canonical_contribution_hash
        for contribution_id, contribution in evidence_by_id.items()
    }
    if refs_by_id != evidence_hashes:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_CONTRIBUTION_SOURCE_HASH_MISMATCH,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                message="snapshot contribution references must derive from actual contribution objects",
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )
    record_measure_amounts = {
        record_id: {
            ExposureMeasureV1.STAKE_COMMITTED: record.stake_committed,
            ExposureMeasureV1.STAKE_RESERVED: record.stake_reserved,
            ExposureMeasureV1.MAXIMUM_LOSS: record.maximum_possible_loss,
            ExposureMeasureV1.POTENTIAL_PROFIT: record.potential_profit,
            ExposureMeasureV1.GROSS_PAYOUT: record.gross_payout,
            ExposureMeasureV1.NET_LIABILITY: record.net_liability,
        }
        for record_id, record in records_by_id.items()
    }
    required_measures = frozenset(
        {
            ExposureMeasureV1.STAKE_COMMITTED,
            ExposureMeasureV1.STAKE_RESERVED,
            ExposureMeasureV1.MAXIMUM_LOSS,
            ExposureMeasureV1.POTENTIAL_PROFIT,
            ExposureMeasureV1.GROSS_PAYOUT,
            ExposureMeasureV1.NET_LIABILITY,
        }
    )
    top_level_keys: set[tuple[str, ExposureMeasureV1]] = set()
    dimension_keys: set[
        tuple[
            str,
            str | None,
            ExposureDimensionTypeV1,
            str,
        ]
    ] = set()
    dimension_contributions: list[ExposureContributionV1] = []
    derived_totals = {measure: ZERO for measure in required_measures}
    for contribution in contributions:
        record = records_by_id.get(contribution.record_id or "")
        if (
            stable_hash(contribution.hash_payload())
            != contribution.canonical_contribution_hash
            or not _authoritative_hash_matches_seal(
                contribution.canonical_contribution_hash,
                contribution._authoritative_origin_hash,
            )
            or contribution.snapshot_or_projection_id != snapshot.snapshot_id
            or contribution.portfolio_id != snapshot.portfolio_id
            or contribution.account_id != snapshot.account_id
            or contribution.portfolio_kind != snapshot.portfolio_kind
            or contribution.currency != snapshot.currency
            or contribution.as_of != snapshot.as_of
            or contribution.calculation_version != snapshot.calculation_version
            or contribution.schema_version != snapshot.schema_version
            or contribution.record_id is None
            or contribution.candidate_id is not None
            or record is None
            or contribution.source_input_hash
            != snapshot.included_record_hashes.get(contribution.record_id)
            or contribution.position_id != record.position_id
            or contribution.order_intent_id != record.order_intent_id
            or contribution.reservation_id != record.reservation_id
            or contribution.parlay_id != record.parlay_id
            or contribution.source_repository != record.source_repository
            or (
                contribution.leg_id is not None
                and contribution.leg_id not in {leg.leg_id for leg in record.legs}
            )
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_CONTRIBUTION_SOURCE_HASH_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                    message="snapshot contribution evidence does not match frozen record and scope references",
                    metadata={
                        "snapshot_id": snapshot.snapshot_id,
                        "contribution_id": contribution.contribution_id,
                    },
                )
            )
        if contribution.position_state != record.state:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_INVALID_CONTRIBUTION_STATE,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.STATE,
                    message="snapshot record contribution state must match its bound record",
                    metadata={
                        "snapshot_id": snapshot.snapshot_id,
                        "contribution_id": contribution.contribution_id,
                    },
                )
            )
        if (
            contribution.contribution_scope
            != ExposureContributionScopeV1.TOP_LEVEL_ECONOMIC
        ):
            dimension_spec = (
                contribution.leg_id,
                contribution.dimension_type,
                contribution.dimension_key,
            )
            expected_dimension_specs = _required_record_dimension_specs(record)
            dimension_key = (record.record_id, *dimension_spec)
            if (
                dimension_spec not in expected_dimension_specs
                or contribution.measure != ExposureMeasureV1.MAXIMUM_LOSS
                or contribution.amount != record.maximum_possible_loss
                or dimension_key in dimension_keys
            ):
                raise_domain_error(
                    build_reason(
                        code=ExposureReasonCodeV1.EXP_SNAPSHOT_DIMENSION_MISMATCH,
                        severity=ExposureReasonSeverityV1.ERROR,
                        category=ExposureReasonCategoryV1.CALCULATION,
                        message="snapshot dimension contribution must derive exactly from its bound record",
                        metadata={
                            "record_id": record.record_id,
                            "contribution_id": contribution.contribution_id,
                        },
                    )
                )
            dimension_keys.add(dimension_key)
            dimension_contributions.append(contribution)
            continue
        if contribution.dimension_key != snapshot.portfolio_id:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_INVALID_CONTRIBUTION_SCOPE,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="snapshot top-level contribution must use the portfolio dimension key",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        top_level_key = (record.record_id, contribution.measure)
        if top_level_key in top_level_keys:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_DUPLICATE_TOP_LEVEL_MEASURE,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="snapshot has duplicate source and top-level measure",
                    metadata={
                        "record_id": record.record_id,
                        "measure": contribution.measure.value,
                    },
                )
            )
        top_level_keys.add(top_level_key)
        expected_amount = record_measure_amounts[record.record_id].get(
            contribution.measure
        )
        if expected_amount is None or contribution.amount != expected_amount:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_SNAPSHOT_TOTALS_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="snapshot contribution amount must derive from its bound record",
                    metadata={
                        "record_id": record.record_id,
                        "measure": contribution.measure.value,
                    },
                )
            )
        with localcontext() as ctx:
            ctx.prec = 28
            derived_totals[contribution.measure] += contribution.amount

    expected_top_level_keys = {
        (record_id, measure)
        for record_id in records_by_id
        for measure in required_measures
    }
    if top_level_keys != expected_top_level_keys:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_INCOMPLETE_SNAPSHOT_MEASURE_SET,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message="snapshot requires exactly six top-level measures for every included record",
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )
    expected_dimension_keys = {
        (record_id, *dimension_spec)
        for record_id, record in records_by_id.items()
        for dimension_spec in _required_record_dimension_specs(record)
    }
    if dimension_keys != expected_dimension_keys:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_SNAPSHOT_DIMENSION_MISMATCH,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message="snapshot requires exact complete record-derived dimension coverage",
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )
    snapshot_totals = {
        ExposureMeasureV1.STAKE_COMMITTED: snapshot.stake_committed,
        ExposureMeasureV1.STAKE_RESERVED: snapshot.stake_reserved,
        ExposureMeasureV1.MAXIMUM_LOSS: snapshot.maximum_possible_loss,
        ExposureMeasureV1.POTENTIAL_PROFIT: snapshot.potential_profit,
        ExposureMeasureV1.GROSS_PAYOUT: snapshot.gross_payout,
        ExposureMeasureV1.NET_LIABILITY: snapshot.net_liability,
    }
    if derived_totals != snapshot_totals:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_SNAPSHOT_TOTALS_MISMATCH,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message="snapshot totals must equal the bound top-level contribution ledger",
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )
    _validate_snapshot_derived_dimensions(
        snapshot,
        dimension_contributions=tuple(dimension_contributions),
        bankroll_reference=bankroll_reference,
    )


def _validate_snapshot_derived_dimensions(
    snapshot: AuthoritativeExposureSnapshotV1,
    *,
    dimension_contributions: tuple[ExposureContributionV1, ...],
    bankroll_reference: BankrollReferenceV1,
) -> None:
    amounts_by_source_dimension: dict[
        tuple[
            str,
            ExposureDimensionTypeV1,
            str,
            ExposureMeasureV1,
        ],
        Decimal,
    ] = {}
    for contribution in dimension_contributions:
        source_dimension = (
            contribution.record_id or "",
            contribution.dimension_type,
            contribution.dimension_key,
            contribution.measure,
        )
        amounts_by_source_dimension[source_dimension] = contribution.amount

    grouped_amounts: dict[
        tuple[ExposureDimensionTypeV1, str, ExposureMeasureV1],
        list[Decimal],
    ] = {}
    for (
        _record_id,
        dimension_type,
        dimension_key,
        measure,
    ), amount in amounts_by_source_dimension.items():
        grouped_amounts.setdefault(
            (dimension_type, dimension_key, measure),
            [],
        ).append(amount)

    expected_aggregates: list[ExposureDimensionAggregateV1] = []
    for (
        dimension_type,
        dimension_key,
        measure,
    ), amounts in grouped_amounts.items():
        with localcontext() as ctx:
            ctx.prec = 28
            amount = sum(amounts, ZERO)
        expected_aggregates.append(
            ExposureDimensionAggregateV1(
                dimension_type=dimension_type,
                dimension_key=dimension_key,
                measure=measure,
                currency=snapshot.currency,
                portfolio_kind=snapshot.portfolio_kind,
                amount=amount,
            )
        )
    expected_aggregate_tuple = tuple(
        sorted(
            expected_aggregates,
            key=lambda item: (
                item.dimension_type.value,
                item.dimension_key,
                item.measure.value,
            ),
        )
    )

    expected_ratios = tuple(
        ExposureDimensionRatioV1(
            dimension_type=aggregate.dimension_type,
            dimension_key=aggregate.dimension_key,
            measure=aggregate.measure,
            value=_derive_ratio(
                aggregate.amount,
                snapshot.maximum_possible_loss,
            ),
        )
        for aggregate in expected_aggregate_tuple
    )
    expected_correlated = {
        aggregate.dimension_key: aggregate.amount
        for aggregate in expected_aggregate_tuple
        if aggregate.dimension_type == ExposureDimensionTypeV1.CORRELATION_GROUP
        and aggregate.measure == ExposureMeasureV1.MAXIMUM_LOSS
    }
    expected_exposure_pct_bankroll = _derive_ratio(
        snapshot.maximum_possible_loss,
        bankroll_reference.total_bankroll,
    )
    expected_exposure_pct_available = _derive_ratio(
        snapshot.maximum_possible_loss,
        bankroll_reference.available_balance,
    )

    mismatched_fields = []
    if snapshot.dimension_aggregates != expected_aggregate_tuple:
        mismatched_fields.append("dimension_aggregates")
    if snapshot.concentration_ratios != expected_ratios:
        mismatched_fields.append("concentration_ratios")
    if dict(snapshot.correlated_maximum_loss_by_group) != expected_correlated:
        mismatched_fields.append("correlated_maximum_loss_by_group")
    if snapshot.exposure_pct_bankroll != expected_exposure_pct_bankroll:
        mismatched_fields.append("exposure_pct_bankroll")
    if snapshot.exposure_pct_available != expected_exposure_pct_available:
        mismatched_fields.append("exposure_pct_available")
    if mismatched_fields:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_SNAPSHOT_DIMENSION_MISMATCH,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message="snapshot dimensional and denominator outputs must derive from bound evidence",
                metadata={"fields": tuple(sorted(mismatched_fields))},
            )
        )


def _derive_ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator == ZERO:
        return None
    with localcontext() as ctx:
        ctx.prec = 28
        value = numerator / denominator
    return normalize_ratio_decimal("derived_ratio", value)


def _validate_snapshot_consistency(snapshot: AuthoritativeExposureSnapshotV1) -> None:
    if snapshot.gross_payout != snapshot.stake_committed + snapshot.potential_profit:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_PAYOUT_INCONSISTENT,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.MONETARY,
                message="snapshot gross payout must equal stake committed plus potential profit",
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )
    if snapshot.net_liability != snapshot.maximum_possible_loss:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_PAYOUT_INCONSISTENT,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.MONETARY,
                message="snapshot net liability must equal maximum possible loss",
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )
    if snapshot.status == ExposureAvailabilityStatusV1.AVAILABLE and any(
        reason.severity == ExposureReasonSeverityV1.ERROR for reason in snapshot.reasons
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_STATE_CONFLICT,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message="available snapshot cannot contain error-severity reasons",
                metadata={"snapshot_id": snapshot.snapshot_id},
            )
        )
    for aggregate in snapshot.dimension_aggregates:
        if (
            aggregate.currency != snapshot.currency
            or aggregate.portfolio_kind != snapshot.portfolio_kind
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_SNAPSHOT_AGGREGATE_SCOPE_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="dimension aggregate scope must match snapshot currency and portfolio kind",
                    metadata={
                        "snapshot_id": snapshot.snapshot_id,
                        "dimension_key": aggregate.dimension_key,
                    },
                )
            )
    for ratio in snapshot.concentration_ratios:
        if ratio.value is not None and (
            ratio.value < ZERO or ratio.value > Decimal("1.000000")
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_SNAPSHOT_AGGREGATE_SCOPE_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="concentration ratio must remain inside [0,1] when available",
                    metadata={
                        "snapshot_id": snapshot.snapshot_id,
                        "dimension_key": ratio.dimension_key,
                    },
                )
            )


def _normalize_count_mapping(
    mapping: MappingProxyType[str, int], *, name: str
) -> MappingProxyType[str, int]:
    normalized: dict[str, int] = {}
    for key, value in dict(mapping).items():
        if not isinstance(key, str):
            raise ValueError(f"{name} keys must be strings")
        text_key = require_text(name, key)
        if text_key in normalized:
            raise ValueError(f"{name} keys must not collide after normalization")
        if type(value) is not int or value < 0:
            raise ValueError(f"{name}[{text_key}] must be a nonnegative int")
        normalized[text_key] = value
    return MappingProxyType(dict(sorted(normalized.items(), key=lambda item: item[0])))


def _normalize_decimal_mapping(
    mapping: MappingProxyType[str, Decimal],
    *,
    currency: str,
    name: str,
) -> MappingProxyType[str, Decimal]:
    normalized: dict[str, Decimal] = {}
    for key, value in dict(mapping).items():
        if not isinstance(key, str):
            raise ValueError(f"{name} keys must be strings")
        text_key = require_text(name, key)
        if text_key in normalized:
            raise ValueError(f"{name} keys must not collide after normalization")
        normalized[text_key] = normalize_monetary_decimal(name, value, currency)
    return MappingProxyType(dict(sorted(normalized.items(), key=lambda item: item[0])))


def _validate_authoritative_projection_inputs(
    *,
    current_snapshot: AuthoritativeExposureSnapshotV1,
    candidate: ExposureCandidateV1,
    bankroll_reference: BankrollReferenceV1,
) -> None:
    if not isinstance(current_snapshot, AuthoritativeExposureSnapshotV1):
        _raise_projection_object_mismatch(
            "current snapshot evidence must be AuthoritativeExposureSnapshotV1"
        )
    if not isinstance(candidate, ExposureCandidateV1):
        _raise_projection_object_mismatch(
            "candidate evidence must be ExposureCandidateV1"
        )
    if not isinstance(bankroll_reference, BankrollReferenceV1):
        _raise_projection_object_mismatch(
            "bankroll evidence must be BankrollReferenceV1"
        )
    try:
        _validate_candidate_contract_integrity(candidate)
        _validate_bankroll_contract_integrity(bankroll_reference)
    except (ExposureValidationError, AttributeError, KeyError, TypeError, ValueError):
        _raise_projection_object_mismatch(
            "candidate or bankroll evidence fails constructor semantic revalidation"
        )
    if not _authoritative_hash_matches_seal(
        candidate.canonical_input_hash,
        candidate._authoritative_origin_hash,
    ):
        _raise_projection_object_mismatch(
            "candidate hash differs from its process-local construction seal"
        )
    if not _authoritative_hash_matches_seal(
        bankroll_reference.canonical_input_hash,
        bankroll_reference._authoritative_origin_hash,
    ):
        _raise_projection_object_mismatch(
            "bankroll hash differs from its process-local construction seal"
        )

    _revalidate_snapshot_authority(
        current_snapshot,
        projection_context=True,
    )
    if current_snapshot.status != ExposureAvailabilityStatusV1.AVAILABLE:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_STATE_CONFLICT,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message=(
                    "authoritative projection requires an available current snapshot"
                ),
                metadata={"snapshot_id": current_snapshot.snapshot_id},
            )
        )
    validate_candidate_collision(
        candidate_id=candidate.candidate_id,
        candidate_hash=candidate.canonical_input_hash,
        record_identities=(current_snapshot.authoritative_lineage_record_identities),
    )
    if not _authoritative_hash_matches_seal(
        current_snapshot.canonical_input_hash,
        current_snapshot._authoritative_origin_hash,
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                message=(
                    "snapshot canonical hash differs from its final "
                    "construction-time authority seal"
                ),
                metadata={"snapshot_id": current_snapshot.snapshot_id},
            )
        )

    for evidence_name, expected_hash, payload in (
        (
            "current snapshot",
            current_snapshot.canonical_input_hash,
            current_snapshot.hash_payload(),
        ),
        ("candidate", candidate.canonical_input_hash, candidate.hash_payload()),
        (
            "bankroll reference",
            bankroll_reference.canonical_input_hash,
            bankroll_reference.hash_payload(),
        ),
    ):
        if stable_hash(payload) != expected_hash:
            _raise_projection_object_mismatch(
                f"{evidence_name} canonical hash does not match its bound object"
            )

    if (
        candidate.portfolio_id != current_snapshot.portfolio_id
        or candidate.account_id != current_snapshot.account_id
        or candidate.portfolio_kind != current_snapshot.portfolio_kind
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_PROJECTION_OBJECT_MISMATCH,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message="candidate scope does not match current snapshot",
                metadata={"candidate_id": candidate.candidate_id},
            )
        )
    if (
        bankroll_reference.portfolio_id != current_snapshot.portfolio_id
        or bankroll_reference.account_id != current_snapshot.account_id
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_PROJECTION_OBJECT_MISMATCH,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.BANKROLL_REFERENCE,
                message="bankroll scope does not match current snapshot",
                metadata={
                    "bankroll_reference_id": bankroll_reference.bankroll_reference_id
                },
            )
        )
    assert_same_currency(
        "candidate and snapshot",
        current_snapshot.currency,
        candidate.currency,
    )
    assert_same_currency(
        "bankroll and snapshot",
        current_snapshot.currency,
        bankroll_reference.currency,
    )
    if (
        candidate.as_of != current_snapshot.as_of
        or bankroll_reference.as_of != current_snapshot.as_of
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_PROJECTION_OBJECT_MISMATCH,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.TEMPORAL,
                message="snapshot, candidate, and bankroll must share the exact V1 as_of",
                metadata={"snapshot_id": current_snapshot.snapshot_id},
            )
        )
    if (
        current_snapshot.bankroll_reference_id
        != bankroll_reference.bankroll_reference_id
        or current_snapshot.bankroll_reference_hash
        != bankroll_reference.canonical_input_hash
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_BANKROLL_REFERENCE_INVALID,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.BANKROLL_REFERENCE,
                message="supplied bankroll is not the reference frozen into the snapshot",
                metadata={"snapshot_id": current_snapshot.snapshot_id},
            )
        )
    if (
        len(
            {
                current_snapshot.schema_version,
                candidate.schema_version,
                bankroll_reference.schema_version,
            }
        )
        != 1
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_PROJECTION_VERSION_MISMATCH,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message="snapshot, candidate, and bankroll schema versions must align",
                metadata={"snapshot_id": current_snapshot.snapshot_id},
            )
        )


def _raise_projection_object_mismatch(message: str) -> None:
    raise_domain_error(
        build_reason(
            code=ExposureReasonCodeV1.EXP_PROJECTION_OBJECT_MISMATCH,
            severity=ExposureReasonSeverityV1.ERROR,
            category=ExposureReasonCategoryV1.CALCULATION,
            message=message,
        )
    )


def _validate_projection_authority_evidence(
    projection: ExposureProjectionV1,
    *,
    current_snapshot: AuthoritativeExposureSnapshotV1 | None,
    candidate: ExposureCandidateV1 | None,
    bankroll_reference: BankrollReferenceV1 | None,
) -> None:
    if current_snapshot is None or candidate is None or bankroll_reference is None:
        _raise_projection_object_mismatch(
            "authoritative projection construction requires bound snapshot, candidate, and bankroll objects"
        )
    _validate_authoritative_projection_inputs(
        current_snapshot=current_snapshot,
        candidate=candidate,
        bankroll_reference=bankroll_reference,
    )
    expected_before = ExposureTotalsV1(
        stake_committed=current_snapshot.stake_committed,
        stake_reserved=current_snapshot.stake_reserved,
        maximum_possible_loss=current_snapshot.maximum_possible_loss,
        potential_profit=current_snapshot.potential_profit,
        gross_payout=current_snapshot.gross_payout,
        net_liability=current_snapshot.net_liability,
        currency=current_snapshot.currency,
    )
    expected_projected = _build_projected_totals(
        before=current_snapshot,
        candidate=candidate,
        contributions=projection.candidate_top_level_contributions,
    )
    expected_available = normalize_monetary_decimal(
        "hypothetical_available_after",
        bankroll_reference.available_balance - candidate.allocation_amount,
        bankroll_reference.currency,
    )
    mismatched_fields = []
    for name, actual, expected in (
        ("portfolio_id", projection.portfolio_id, current_snapshot.portfolio_id),
        ("account_id", projection.account_id, current_snapshot.account_id),
        ("portfolio_kind", projection.portfolio_kind, current_snapshot.portfolio_kind),
        ("currency", projection.currency, current_snapshot.currency),
        (
            "current_snapshot_id",
            projection.current_snapshot_id,
            current_snapshot.snapshot_id,
        ),
        (
            "current_snapshot_hash",
            projection.current_snapshot_hash,
            current_snapshot.canonical_input_hash,
        ),
        ("candidate_id", projection.candidate_id, candidate.candidate_id),
        (
            "candidate_input_hash",
            projection.candidate_input_hash,
            candidate.canonical_input_hash,
        ),
        (
            "bankroll_reference_id",
            projection.bankroll_reference_id,
            bankroll_reference.bankroll_reference_id,
        ),
        (
            "bankroll_reference_hash",
            projection.bankroll_reference_hash,
            bankroll_reference.canonical_input_hash,
        ),
        (
            "bankroll_available_balance",
            projection.bankroll_available_balance,
            bankroll_reference.available_balance,
        ),
        (
            "bankroll_reserved_balance",
            projection.bankroll_reserved_balance,
            bankroll_reference.reserved_balance,
        ),
        (
            "hypothetical_available_after",
            projection.hypothetical_available_after,
            expected_available,
        ),
        (
            "calculation_version",
            projection.calculation_version,
            current_snapshot.calculation_version,
        ),
        ("as_of", projection.as_of, current_snapshot.as_of),
        ("schema_version", projection.schema_version, current_snapshot.schema_version),
    ):
        if actual != expected:
            mismatched_fields.append(name)
    if projection.before_totals != expected_before:
        mismatched_fields.append("before_totals")
    if projection.projected_totals != expected_projected:
        mismatched_fields.append("projected_totals")
    if mismatched_fields:
        raise_domain_error(
            build_reason(
                code=(
                    ExposureReasonCodeV1.EXP_CANDIDATE_ECONOMICS_MISMATCH
                    if "projected_totals" in mismatched_fields
                    else ExposureReasonCodeV1.EXP_PROJECTION_OBJECT_MISMATCH
                ),
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message="projection fields do not match bound authoritative evidence",
                metadata={"fields": tuple(sorted(mismatched_fields))},
            )
        )


def _required_candidate_dimension_specs(
    candidate: ExposureCandidateV1 | ExposureRecordV1,
) -> frozenset[tuple[str | None, ExposureDimensionTypeV1, str]]:
    specs: set[tuple[str | None, ExposureDimensionTypeV1, str]] = {
        (None, ExposureDimensionTypeV1.ACCOUNT, candidate.account_id),
        (
            None,
            ExposureDimensionTypeV1.PORTFOLIO_KIND,
            candidate.portfolio_kind.value,
        ),
        (None, ExposureDimensionTypeV1.CURRENCY, candidate.currency),
        (None, ExposureDimensionTypeV1.LEAGUE, candidate.league),
        (None, ExposureDimensionTypeV1.EVENT, candidate.event_id),
        (None, ExposureDimensionTypeV1.MARKET, candidate.market_id),
        (None, ExposureDimensionTypeV1.MARKET_TYPE, candidate.market_type),
        (None, ExposureDimensionTypeV1.PERIOD, candidate.period),
        (
            None,
            ExposureDimensionTypeV1.SPORTSBOOK,
            candidate.canonical_sportsbook_id,
        ),
        (None, ExposureDimensionTypeV1.STRATEGY, candidate.strategy_id),
        (
            None,
            ExposureDimensionTypeV1.STRATEGY_VERSION,
            candidate.strategy_version,
        ),
        (None, ExposureDimensionTypeV1.MODEL_VERSION, candidate.model_version),
        (
            None,
            ExposureDimensionTypeV1.CALIBRATION_VERSION,
            candidate.calibration_version,
        ),
        (
            None,
            ExposureDimensionTypeV1.RECONCILIATION_VERSION,
            candidate.reconciliation_version,
        ),
        (
            None,
            ExposureDimensionTypeV1.SETTLEMENT_HORIZON,
            candidate.settlement_horizon,
        ),
    }
    if candidate.selection_id:
        specs.add((None, ExposureDimensionTypeV1.SELECTION, candidate.selection_id))
    if candidate.outcome_id:
        specs.add((None, ExposureDimensionTypeV1.OUTCOME, candidate.outcome_id))
    specs.update(
        (None, ExposureDimensionTypeV1.TEAM, team_id) for team_id in candidate.team_ids
    )
    specs.update(
        (None, ExposureDimensionTypeV1.PLAYER, player_id)
        for player_id in candidate.player_ids
    )
    specs.update(
        (None, ExposureDimensionTypeV1.CORRELATION_GROUP, group_id)
        for group_id in candidate.correlation_group_ids
    )
    if candidate.parlay_id:
        specs.add((None, ExposureDimensionTypeV1.PARLAY, candidate.parlay_id))
    for leg in candidate.legs:
        specs.update(
            {
                (leg.leg_id, ExposureDimensionTypeV1.PARLAY_LEG, leg.leg_id),
                (leg.leg_id, ExposureDimensionTypeV1.LEAGUE, leg.league),
                (leg.leg_id, ExposureDimensionTypeV1.EVENT, leg.event_id),
                (leg.leg_id, ExposureDimensionTypeV1.MARKET, leg.market_id),
                (
                    leg.leg_id,
                    ExposureDimensionTypeV1.MARKET_TYPE,
                    leg.market_type,
                ),
                (leg.leg_id, ExposureDimensionTypeV1.PERIOD, leg.period),
                (
                    leg.leg_id,
                    ExposureDimensionTypeV1.SPORTSBOOK,
                    leg.canonical_sportsbook_id,
                ),
                (
                    leg.leg_id,
                    ExposureDimensionTypeV1.SETTLEMENT_HORIZON,
                    leg.settlement_horizon,
                ),
                (
                    leg.leg_id,
                    ExposureDimensionTypeV1.RECONCILIATION_VERSION,
                    leg.reconciliation_version,
                ),
            }
        )
        if leg.selection_id:
            specs.add((leg.leg_id, ExposureDimensionTypeV1.SELECTION, leg.selection_id))
        if leg.outcome_id:
            specs.add((leg.leg_id, ExposureDimensionTypeV1.OUTCOME, leg.outcome_id))
        specs.update(
            (leg.leg_id, ExposureDimensionTypeV1.TEAM, team_id)
            for team_id in leg.team_ids
        )
        specs.update(
            (leg.leg_id, ExposureDimensionTypeV1.PLAYER, player_id)
            for player_id in leg.player_ids
        )
        specs.update(
            (
                leg.leg_id,
                ExposureDimensionTypeV1.CORRELATION_GROUP,
                group_id,
            )
            for group_id in leg.correlation_group_ids
        )
    return frozenset(specs)


def _required_record_dimension_specs(
    record: ExposureRecordV1,
) -> frozenset[tuple[str | None, ExposureDimensionTypeV1, str]]:
    return _required_candidate_dimension_specs(record)


def _validate_candidate_dimension_evidence(
    *,
    candidate: ExposureCandidateV1,
    contributions: tuple[ExposureContributionV1, ...],
) -> None:
    expected = _required_candidate_dimension_specs(candidate)
    actual: list[tuple[str | None, ExposureDimensionTypeV1, str]] = []
    for contribution in contributions:
        actual.append(
            (
                contribution.leg_id,
                contribution.dimension_type,
                contribution.dimension_key,
            )
        )
        if (
            contribution.parlay_id != candidate.parlay_id
            or contribution.measure != ExposureMeasureV1.MAXIMUM_LOSS
            or contribution.amount != candidate.maximum_possible_loss
            or contribution.position_state != ExposureRecordStateV1.PROPOSED_CANDIDATE
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_CANDIDATE_PARLAY_EVIDENCE_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.PARLAY,
                    message="candidate dimension evidence does not match frozen economics and state",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
    if len(set(actual)) != len(actual) or set(actual) != set(expected):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_CANDIDATE_PARLAY_EVIDENCE_MISMATCH,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.PARLAY,
                message="candidate dimensions must exactly cover the authoritative candidate and full leg identities",
                metadata={
                    "candidate_id": candidate.candidate_id,
                    "expected_count": len(expected),
                    "actual_count": len(actual),
                },
            )
        )


def _validate_projection_consistency(
    projection: ExposureProjectionV1,
    *,
    candidate: ExposureCandidateV1,
) -> None:
    top_level = tuple(projection.candidate_top_level_contributions)
    dimensionals = tuple(projection.candidate_dimension_contributions)
    seen_ids: set[str] = set()
    seen_measure_keys: set[tuple[str, ExposureMeasureV1]] = set()
    expected_candidate_measures = _candidate_measure_amounts(candidate)

    if not top_level:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_EMPTY_CANDIDATE_CONTRIBUTIONS,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message="candidate projections require a complete top-level economic contribution set",
                metadata={"projection_id": projection.projection_id},
            )
        )

    for contribution in top_level + dimensionals:
        _validate_contribution_contract_integrity(contribution)
        if contribution.contribution_id in seen_ids:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_DUPLICATE_CONTRIBUTION_ID,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                    message="duplicate contribution id",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        seen_ids.add(contribution.contribution_id)

        if (
            stable_hash(contribution.hash_payload())
            != contribution.canonical_contribution_hash
            or not _authoritative_hash_matches_seal(
                contribution.canonical_contribution_hash,
                contribution._authoritative_origin_hash,
            )
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                    message=(
                        "projection contribution differs from its "
                        "construction-time authority seal"
                    ),
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )

        if contribution.snapshot_or_projection_id != projection.projection_id:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_CONTRIBUTION_LINEAGE_FAILURE,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="contribution projection lineage does not match projection id",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        if contribution.source_input_hash != projection.candidate_input_hash:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_CONTRIBUTION_LINEAGE_FAILURE,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="contribution source hash does not match authoritative candidate hash",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        if contribution.candidate_id != projection.candidate_id:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_CANDIDATE_COLLISION,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.INPUT_INTEGRITY,
                    message="projection contribution candidate identity mismatch",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        if (
            contribution.portfolio_id != projection.portfolio_id
            or contribution.account_id != projection.account_id
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PROJECTION_PORTFOLIO_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="projection contribution scope does not match portfolio or account",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        if contribution.portfolio_kind != projection.portfolio_kind:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PROJECTION_PORTFOLIO_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="projection contribution portfolio kind mismatch",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        if contribution.currency != projection.currency:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_CURRENCY_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CURRENCY,
                    message="projection contribution currency mismatch",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        if contribution.as_of != projection.as_of:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PROJECTION_SOURCE_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="projection contribution as_of mismatch",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        if contribution.calculation_version != projection.calculation_version:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PROJECTION_VERSION_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="projection contribution calculation version mismatch",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        if contribution.schema_version != projection.schema_version:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PROJECTION_VERSION_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="projection contribution schema version mismatch",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        if contribution.position_state != ExposureRecordStateV1.PROPOSED_CANDIDATE:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_INVALID_CONTRIBUTION_STATE,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.STATE,
                    message="candidate projection contributions must use proposed_candidate state",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        if any(
            value is not None
            for value in (
                contribution.record_id,
                contribution.position_id,
                contribution.order_intent_id,
                contribution.reservation_id,
            )
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_CONTRIBUTION_LINEAGE_FAILURE,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.IDENTITY,
                    message="candidate contributions cannot claim record, position, order, or reservation identity",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        if contribution.source_repository != "sports.execution.exposure_v1":
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PROJECTION_SOURCE_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.SOURCE,
                    message="candidate contribution source repository is not authoritative",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        if contribution.parlay_id != candidate.parlay_id:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_CANDIDATE_PARLAY_EVIDENCE_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.PARLAY,
                    message="candidate contribution parent parlay identity mismatch",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )

    for contribution in top_level:
        if (
            contribution.contribution_scope
            != ExposureContributionScopeV1.TOP_LEVEL_ECONOMIC
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_DUPLICATE_TOP_LEVEL_MEASURE,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="candidate top-level contributions must be economic top-level entries",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        if (
            contribution.dimension_type != ExposureDimensionTypeV1.PORTFOLIO
            or contribution.dimension_key != projection.portfolio_id
            or contribution.leg_id is not None
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_INVALID_CONTRIBUTION_SCOPE,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="candidate top-level contribution must use the authoritative portfolio dimension",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
        measure_key = (projection.candidate_id, contribution.measure)
        if measure_key in seen_measure_keys:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_DUPLICATE_TOP_LEVEL_MEASURE,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="candidate contributes more than once to the same economic measure",
                    metadata={"measure": contribution.measure.value},
                )
            )
        seen_measure_keys.add(measure_key)
        expected_amount = expected_candidate_measures[contribution.measure]
        if contribution.amount != expected_amount:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PROJECTION_ARITHMETIC_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="candidate contribution amount must match frozen candidate economics",
                    metadata={
                        "contribution_id": contribution.contribution_id,
                        "measure": contribution.measure.value,
                    },
                )
            )

    if set(expected_candidate_measures.keys()) != {item.measure for item in top_level}:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_INCOMPLETE_CANDIDATE_MEASURE_SET,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message="candidate top-level contributions must contain the exact complete measure set",
                metadata={"projection_id": projection.projection_id},
            )
        )

    for contribution in dimensionals:
        if (
            contribution.contribution_scope
            != ExposureContributionScopeV1.DIMENSION_ALLOCATION
            or contribution.additive_to_portfolio_totals
        ):
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_CONTRIBUTION_LINEAGE_FAILURE,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="dimension contributions must remain non-additive dimensional allocations",
                    metadata={"contribution_id": contribution.contribution_id},
                )
            )
    _validate_candidate_dimension_evidence(
        candidate=candidate,
        contributions=dimensionals,
    )

    candidate_totals = _sum_candidate_measure_contributions(
        top_level, currency=projection.before_totals.currency
    )
    expected_after = {
        measure: projection.before_totals.measure_amounts()[measure]
        + candidate_totals.get(measure, ZERO)
        for measure in projection.before_totals.measure_amounts()
    }
    actual_after = projection.projected_totals.measure_amounts()
    for measure, expected_value in expected_after.items():
        if actual_after[measure] != expected_value:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PROJECTION_ARITHMETIC_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="projected totals must equal before totals plus candidate economic contributions",
                    metadata={
                        "projection_id": projection.projection_id,
                        "measure": measure.value,
                    },
                )
            )

    if projection.hypothetical_available_after is not None:
        expected_available = (
            projection.bankroll_available_balance
            - expected_candidate_measures[ExposureMeasureV1.STAKE_COMMITTED]
        )
        if projection.hypothetical_available_after != expected_available:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_PROJECTION_ARITHMETIC_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.CALCULATION,
                    message="hypothetical_available_after must equal descriptive arithmetic on current available balance",
                    metadata={"projection_id": projection.projection_id},
                )
            )

    derived_dimensions = tuple(
        sorted(
            {
                f"{contribution.dimension_type.value}:{contribution.dimension_key}"
                for contribution in dimensionals
            }
        )
    )
    if projection.affected_dimensions != derived_dimensions:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_PROJECTION_SOURCE_MISMATCH,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message="affected dimensions must equal the derived dimensional contribution set",
                metadata={"projection_id": projection.projection_id},
            )
        )

    if projection.status == ExposureProjectionStatusV1.AVAILABLE and any(
        reason.severity == ExposureReasonSeverityV1.ERROR
        for reason in projection.reasons
    ):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_STATE_CONFLICT,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message="available projection cannot contain error-severity reasons",
                metadata={"projection_id": projection.projection_id},
            )
        )


def _sum_candidate_measure_contributions(
    contributions: tuple[ExposureContributionV1, ...],
    *,
    currency: str,
) -> dict[ExposureMeasureV1, Decimal]:
    totals: dict[ExposureMeasureV1, Decimal] = {}
    for contribution in contributions:
        totals[contribution.measure] = normalize_monetary_decimal(
            contribution.measure.value,
            totals.get(contribution.measure, ZERO) + contribution.amount,
            currency,
        )
    return totals


def _candidate_measure_amounts(
    candidate: ExposureCandidateV1,
) -> dict[ExposureMeasureV1, Decimal]:
    return {
        ExposureMeasureV1.STAKE_COMMITTED: candidate.allocation_amount,
        ExposureMeasureV1.STAKE_RESERVED: ZERO,
        ExposureMeasureV1.MAXIMUM_LOSS: candidate.maximum_possible_loss,
        ExposureMeasureV1.POTENTIAL_PROFIT: candidate.potential_profit,
        ExposureMeasureV1.GROSS_PAYOUT: candidate.gross_payout,
        ExposureMeasureV1.NET_LIABILITY: candidate.net_liability,
    }


def _record_measure_amount(
    record: ExposureRecordV1,
    measure: ExposureMeasureV1,
) -> Decimal:
    amounts = {
        ExposureMeasureV1.STAKE_COMMITTED: record.stake_committed,
        ExposureMeasureV1.STAKE_RESERVED: record.stake_reserved,
        ExposureMeasureV1.MAXIMUM_LOSS: record.maximum_possible_loss,
        ExposureMeasureV1.POTENTIAL_PROFIT: record.potential_profit,
        ExposureMeasureV1.GROSS_PAYOUT: record.gross_payout,
        ExposureMeasureV1.NET_LIABILITY: record.net_liability,
    }
    amount = amounts.get(measure)
    if amount is None:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_INVALID_DIMENSION_KEY,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message="record does not define the requested top-level measure",
                metadata={"measure": measure.value},
            )
        )
    return amount


def _candidate_measure_amount(
    candidate: ExposureCandidateV1,
    measure: ExposureMeasureV1,
) -> Decimal:
    amount = _candidate_measure_amounts(candidate).get(measure)
    if amount is None:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_INVALID_DIMENSION_KEY,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message="candidate does not define the requested top-level measure",
                metadata={"measure": measure.value},
            )
        )
    return amount


def _build_projected_totals(
    *,
    before: AuthoritativeExposureSnapshotV1,
    candidate: ExposureCandidateV1,
    contributions: tuple[ExposureContributionV1, ...],
) -> ExposureTotalsV1:
    expected = _candidate_measure_amounts(candidate)
    if not contributions:
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_EMPTY_CANDIDATE_CONTRIBUTIONS,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message="candidate top-level contributions cannot be empty",
                metadata={"candidate_id": candidate.candidate_id},
            )
        )
    measures = [item.measure for item in contributions]
    if len(measures) != len(set(measures)):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_DUPLICATE_TOP_LEVEL_MEASURE,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message="candidate top-level contributions contain a duplicate measure",
                metadata={"candidate_id": candidate.candidate_id},
            )
        )
    if set(measures) != set(expected.keys()):
        raise_domain_error(
            build_reason(
                code=ExposureReasonCodeV1.EXP_INCOMPLETE_CANDIDATE_MEASURE_SET,
                severity=ExposureReasonSeverityV1.ERROR,
                category=ExposureReasonCategoryV1.CALCULATION,
                message="candidate top-level contributions must include the complete measure set",
                metadata={"candidate_id": candidate.candidate_id},
            )
        )
    for contribution in contributions:
        if contribution.amount != expected[contribution.measure]:
            raise_domain_error(
                build_reason(
                    code=ExposureReasonCodeV1.EXP_CANDIDATE_ECONOMICS_MISMATCH,
                    severity=ExposureReasonSeverityV1.ERROR,
                    category=ExposureReasonCategoryV1.MONETARY,
                    message="candidate contribution amount does not match the authoritative candidate",
                    metadata={
                        "candidate_id": candidate.candidate_id,
                        "contribution_id": contribution.contribution_id,
                        "measure": contribution.measure.value,
                    },
                )
            )
    return ExposureTotalsV1(
        stake_committed=before.stake_committed
        + expected[ExposureMeasureV1.STAKE_COMMITTED],
        stake_reserved=before.stake_reserved
        + expected[ExposureMeasureV1.STAKE_RESERVED],
        maximum_possible_loss=before.maximum_possible_loss
        + expected[ExposureMeasureV1.MAXIMUM_LOSS],
        potential_profit=before.potential_profit
        + expected[ExposureMeasureV1.POTENTIAL_PROFIT],
        gross_payout=before.gross_payout + expected[ExposureMeasureV1.GROSS_PAYOUT],
        net_liability=before.net_liability + expected[ExposureMeasureV1.NET_LIABILITY],
        currency=before.currency,
    )
