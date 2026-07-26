from dataclasses import fields
from decimal import Decimal, getcontext
from types import MappingProxyType

import pytest

from sports.execution.exposure_v1.contracts import (
    AuthoritativeExposureSnapshotV1,
    BankrollReferenceV1,
    ExposureCalculationRequestV1,
    ExposureCandidateV1,
    ExposureContributionHashRefV1,
    ExposureContributionV1,
    ExposureDimensionAggregateV1,
    ExposureDimensionRatioV1,
    ExposureLegV1,
    ManualRecordedRealProvenanceV1,
    ExposureProjectionV1,
    ExposureRecordV1,
    ExposureTotalsV1,
    STATE_INCLUSION_MATRIX_V1,
    STATE_RULES_BY_STATE,
    compute_portfolio_total_from_contributions,
    validate_authoritative_projection_integrity,
)
from sports.execution.exposure_v1.hashing import (
    _seal_authoritative_hash,
    canonical_json,
    stable_hash,
)
from sports.execution.exposure_v1.reasons import ExposureReasonCodeV1, ExposureReasonV1
from sports.execution.exposure_v1.statuses import (
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
    normalize_monetary_decimal,
    normalize_ratio_decimal,
    parse_utc_timestamp,
    validate_candidate_collision,
    validate_record_collection,
)


def _leg(**overrides: object) -> ExposureLegV1:
    payload = {
        "leg_id": "leg-1",
        "league": "wnba",
        "event_id": "event-1",
        "market_id": "market-1",
        "market_type": "moneyline",
        "period": "full_game",
        "selection_id": "selection-1",
        "outcome_id": "outcome-1",
        "team_ids": ("team-1",),
        "player_ids": (),
        "canonical_sportsbook_id": "draftkings",
        "settlement_horizon": "same_day",
        "reconciliation_result_id": "recon-1",
        "reconciliation_version": "recon-v1",
        "correlation_group_ids": ("group-1",),
    }
    payload.update(overrides)
    return ExposureLegV1(**payload)


def _record(**overrides: object) -> ExposureRecordV1:
    payload = {
        "record_id": "record-1",
        "record_kind": ExposureRecordKindV1.POSITION,
        "source_repository": "sports.personal",
        "source_record_id": "src-1",
        "source_revision": "rev-1",
        "portfolio_id": "portfolio-1",
        "portfolio_kind": ExposurePortfolioKindV1.CASH,
        "account_id": "account-1",
        "currency": "usd",
        "position_id": "position-1",
        "order_intent_id": "intent-1",
        "reservation_id": None,
        "candidate_projection_id": None,
        "reconciliation_result_id": "recon-1",
        "reconciliation_version": "recon-v1",
        "model_version": "model-v1",
        "calibration_version": "cal-v1",
        "strategy_id": "strategy-1",
        "strategy_version": "strategy-v1",
        "state": ExposureRecordStateV1.OPEN,
        "effective_at": "2026-07-26T00:00:00Z",
        "as_of_eligible_at": "2026-07-26T00:00:00+00:00",
        "stake_committed": Decimal("10.00"),
        "stake_reserved": Decimal("0.00"),
        "maximum_possible_loss": Decimal("10.00"),
        "potential_profit": Decimal("9.00"),
        "gross_payout": Decimal("19.00"),
        "net_liability": Decimal("10.00"),
        "league": "wnba",
        "event_id": "event-1",
        "market_id": "market-1",
        "market_type": "moneyline",
        "period": "full_game",
        "selection_id": "selection-1",
        "outcome_id": "outcome-1",
        "team_ids": ("team-1",),
        "player_ids": (),
        "canonical_sportsbook_id": "draftkings",
        "settlement_horizon": "same_day",
        "parlay_id": None,
        "legs": (),
        "correlation_group_ids": ("group-1",),
        "correction_of_record_id": None,
        "reversal_of_record_id": None,
        "residual_reference_id": None,
    }
    payload.update(overrides)
    return ExposureRecordV1(**payload)


def _reservation_record(**overrides: object) -> ExposureRecordV1:
    payload = {
        "record_id": "reservation-record-1",
        "record_kind": ExposureRecordKindV1.RESERVATION,
        "source_repository": "sports.personal",
        "source_record_id": "reservation-src-1",
        "source_revision": "rev-1",
        "portfolio_id": "portfolio-1",
        "portfolio_kind": ExposurePortfolioKindV1.CASH,
        "account_id": "account-1",
        "currency": "USD",
        "position_id": None,
        "order_intent_id": "intent-1",
        "reservation_id": "reservation-1",
        "candidate_projection_id": None,
        "reconciliation_result_id": "recon-1",
        "reconciliation_version": "recon-v1",
        "model_version": "model-v1",
        "calibration_version": "cal-v1",
        "strategy_id": "strategy-1",
        "strategy_version": "strategy-v1",
        "state": ExposureRecordStateV1.PENDING_CONFIRMATION,
        "effective_at": "2026-07-26T00:00:00+00:00",
        "as_of_eligible_at": "2026-07-26T00:00:00+00:00",
        "stake_committed": Decimal("0.00"),
        "stake_reserved": Decimal("10.00"),
        "maximum_possible_loss": Decimal("10.00"),
        "potential_profit": Decimal("0.00"),
        "gross_payout": Decimal("0.00"),
        "net_liability": Decimal("10.00"),
        "league": "wnba",
        "event_id": "event-1",
        "market_id": "market-1",
        "market_type": "moneyline",
        "period": "full_game",
        "selection_id": "selection-1",
        "outcome_id": "outcome-1",
        "team_ids": ("team-1",),
        "player_ids": (),
        "canonical_sportsbook_id": "draftkings",
        "settlement_horizon": "same_day",
        "parlay_id": None,
        "legs": (),
        "correlation_group_ids": (),
        "correction_of_record_id": None,
        "reversal_of_record_id": None,
        "residual_reference_id": None,
    }
    payload.update(overrides)
    return ExposureRecordV1(**payload)


def _candidate(**overrides: object) -> ExposureCandidateV1:
    payload = {
        "candidate_id": "candidate-1",
        "portfolio_id": "portfolio-1",
        "portfolio_kind": ExposurePortfolioKindV1.CASH,
        "account_id": "account-1",
        "currency": "usd",
        "reconciliation_result_id": "recon-1",
        "allocation_amount": Decimal("10.00"),
        "maximum_possible_loss": Decimal("10.00"),
        "potential_profit": Decimal("9.00"),
        "gross_payout": Decimal("19.00"),
        "net_liability": Decimal("10.00"),
        "league": "wnba",
        "event_id": "event-1",
        "market_id": "market-1",
        "market_type": "moneyline",
        "period": "full_game",
        "selection_id": "selection-1",
        "outcome_id": "outcome-1",
        "team_ids": ("team-1",),
        "player_ids": (),
        "canonical_sportsbook_id": "draftkings",
        "strategy_version": "strategy-v1",
        "strategy_id": "strategy-1",
        "model_version": "model-v1",
        "calibration_version": "cal-v1",
        "reconciliation_version": "recon-v1",
        "settlement_horizon": "same_day",
        "parlay_id": None,
        "legs": (),
        "correlation_group_ids": ("group-1",),
        "as_of": "2026-07-26T00:00:00Z",
    }
    payload.update(overrides)
    return ExposureCandidateV1(**payload)


def _identity(
    record_id: str, record_hash: str, **overrides: object
) -> ExposureRecordIdentity:
    payload = {
        "record_id": record_id,
        "record_hash": record_hash,
        "record_kind": "position",
        "state": "open",
        "position_id": None,
        "order_intent_id": None,
        "reservation_id": None,
        "parlay_id": None,
        "parlay_leg_ids": (),
        "currency": "USD",
        "portfolio_kind": ExposurePortfolioKindV1.CASH,
        "effective_at": "2026-07-26T00:00:00+00:00",
        "as_of_eligible_at": "2026-07-26T00:00:00+00:00",
        "source_repository": "sports.personal",
        "source_record_id": f"src-{record_id}",
        "source_revision": "rev-1",
        "canonical_import_identity": f"src-{record_id}:rev-1",
        "correction_of_record_id": None,
        "reversal_of_record_id": None,
    }
    payload.update(overrides)
    return ExposureRecordIdentity(**payload)


def _snapshot_contribution(
    *,
    snapshot_id: str,
    portfolio_id: str,
    account_id: str,
    portfolio_kind: ExposurePortfolioKindV1,
    currency: str,
    as_of: str,
    calculation_version: str,
    record_id: str,
    record_hash: str,
    contribution_id: str = "contrib-1",
    position_id: str | None = "position-1",
    order_intent_id: str | None = "intent-1",
    reservation_id: str | None = None,
    parlay_id: str | None = None,
    position_state: ExposureRecordStateV1 = ExposureRecordStateV1.OPEN,
    source_repository: str = "sports.personal",
    measure: ExposureMeasureV1 = ExposureMeasureV1.STAKE_COMMITTED,
    amount: Decimal = Decimal("10.00"),
) -> ExposureContributionV1:
    return ExposureContributionV1(
        contribution_id=contribution_id,
        snapshot_or_projection_id=snapshot_id,
        portfolio_id=portfolio_id,
        account_id=account_id,
        record_id=record_id,
        position_id=position_id,
        order_intent_id=order_intent_id,
        reservation_id=reservation_id,
        candidate_id=None,
        parlay_id=parlay_id,
        leg_id=None,
        dimension_type=ExposureDimensionTypeV1.PORTFOLIO,
        dimension_key=portfolio_id,
        measure=measure,
        amount=amount,
        currency=currency,
        portfolio_kind=portfolio_kind,
        position_state=position_state,
        source_repository=source_repository,
        calculation_version=calculation_version,
        as_of=as_of,
        source_input_hash=record_hash,
        contribution_scope=ExposureContributionScopeV1.TOP_LEVEL_ECONOMIC,
        additive_to_portfolio_totals=True,
    )


def _snapshot_contributions(
    *,
    record: ExposureRecordV1,
    snapshot_id: str,
    portfolio_id: str,
    account_id: str,
    portfolio_kind: ExposurePortfolioKindV1,
    currency: str,
    as_of: str,
    calculation_version: str,
) -> tuple[ExposureContributionV1, ...]:
    amounts = {
        ExposureMeasureV1.STAKE_COMMITTED: record.stake_committed,
        ExposureMeasureV1.STAKE_RESERVED: record.stake_reserved,
        ExposureMeasureV1.MAXIMUM_LOSS: record.maximum_possible_loss,
        ExposureMeasureV1.POTENTIAL_PROFIT: record.potential_profit,
        ExposureMeasureV1.GROSS_PAYOUT: record.gross_payout,
        ExposureMeasureV1.NET_LIABILITY: record.net_liability,
    }
    top_level = tuple(
        _snapshot_contribution(
            snapshot_id=snapshot_id,
            portfolio_id=portfolio_id,
            account_id=account_id,
            portfolio_kind=portfolio_kind,
            currency=currency,
            as_of=as_of,
            calculation_version=calculation_version,
            record_id=record.record_id,
            record_hash=record.canonical_record_hash,
            contribution_id=f"snapshot-{record.record_id}-{index}",
            position_id=record.position_id,
            order_intent_id=record.order_intent_id,
            reservation_id=record.reservation_id,
            parlay_id=record.parlay_id,
            position_state=record.state,
            source_repository=record.source_repository,
            measure=measure,
            amount=amount,
        )
        for index, (measure, amount) in enumerate(amounts.items(), start=1)
    )
    dimensional = tuple(
        ExposureContributionV1(
            contribution_id=f"snapshot-{record.record_id}-dimension-{index}",
            snapshot_or_projection_id=snapshot_id,
            portfolio_id=portfolio_id,
            account_id=account_id,
            record_id=record.record_id,
            position_id=record.position_id,
            order_intent_id=record.order_intent_id,
            reservation_id=record.reservation_id,
            candidate_id=None,
            parlay_id=record.parlay_id,
            leg_id=leg_id,
            dimension_type=dimension_type,
            dimension_key=dimension_key,
            measure=ExposureMeasureV1.MAXIMUM_LOSS,
            amount=record.maximum_possible_loss,
            currency=currency,
            portfolio_kind=portfolio_kind,
            position_state=record.state,
            source_repository=record.source_repository,
            calculation_version=calculation_version,
            as_of=as_of,
            source_input_hash=record.canonical_record_hash,
            contribution_scope=ExposureContributionScopeV1.DIMENSION_ALLOCATION,
            additive_to_portfolio_totals=False,
            non_additive_note=("full record maximum loss concentration allocation"),
        )
        for index, (leg_id, dimension_type, dimension_key) in enumerate(
            _record_dimension_specs(record),
            start=1,
        )
    )
    return top_level + dimensional


def _record_dimension_specs(
    record: ExposureRecordV1,
) -> tuple[tuple[str | None, ExposureDimensionTypeV1, str], ...]:
    specs: set[tuple[str | None, ExposureDimensionTypeV1, str]] = {
        (None, ExposureDimensionTypeV1.ACCOUNT, record.account_id),
        (
            None,
            ExposureDimensionTypeV1.PORTFOLIO_KIND,
            record.portfolio_kind.value,
        ),
        (None, ExposureDimensionTypeV1.CURRENCY, record.currency),
        (None, ExposureDimensionTypeV1.LEAGUE, record.league),
        (None, ExposureDimensionTypeV1.EVENT, record.event_id),
        (None, ExposureDimensionTypeV1.MARKET, record.market_id),
        (None, ExposureDimensionTypeV1.MARKET_TYPE, record.market_type),
        (None, ExposureDimensionTypeV1.PERIOD, record.period),
        (
            None,
            ExposureDimensionTypeV1.SPORTSBOOK,
            record.canonical_sportsbook_id,
        ),
        (None, ExposureDimensionTypeV1.STRATEGY, record.strategy_id),
        (
            None,
            ExposureDimensionTypeV1.STRATEGY_VERSION,
            record.strategy_version,
        ),
        (None, ExposureDimensionTypeV1.MODEL_VERSION, record.model_version),
        (
            None,
            ExposureDimensionTypeV1.CALIBRATION_VERSION,
            record.calibration_version,
        ),
        (
            None,
            ExposureDimensionTypeV1.RECONCILIATION_VERSION,
            record.reconciliation_version,
        ),
        (
            None,
            ExposureDimensionTypeV1.SETTLEMENT_HORIZON,
            record.settlement_horizon,
        ),
    }
    if record.selection_id:
        specs.add((None, ExposureDimensionTypeV1.SELECTION, record.selection_id))
    if record.outcome_id:
        specs.add((None, ExposureDimensionTypeV1.OUTCOME, record.outcome_id))
    specs.update(
        (None, ExposureDimensionTypeV1.TEAM, team_id) for team_id in record.team_ids
    )
    specs.update(
        (None, ExposureDimensionTypeV1.PLAYER, player_id)
        for player_id in record.player_ids
    )
    specs.update(
        (None, ExposureDimensionTypeV1.CORRELATION_GROUP, group_id)
        for group_id in record.correlation_group_ids
    )
    if record.parlay_id:
        specs.add((None, ExposureDimensionTypeV1.PARLAY, record.parlay_id))
    for leg in record.legs:
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
    return tuple(
        sorted(
            specs,
            key=lambda item: (
                item[0] or "",
                item[1].value,
                item[2],
            ),
        )
    )


def _snapshot(**overrides: object) -> AuthoritativeExposureSnapshotV1:
    payload = {
        "snapshot_id": "snapshot-1",
        "portfolio_id": "portfolio-1",
        "account_id": "account-1",
        "portfolio_kind": ExposurePortfolioKindV1.CASH,
        "currency": "usd",
        "as_of": "2026-07-26T00:00:00Z",
        "calculation_version": "exposure_calc_v1",
        "bankroll_reference_id": "bankroll-1",
        "bankroll_reference_hash": "",
        "included_record_ids": ("record-1",),
        "included_record_hashes": MappingProxyType({"record-1": "record-hash-1"}),
        "contribution_refs": (),
        "stake_committed": Decimal("10.00"),
        "stake_reserved": Decimal("0.00"),
        "maximum_possible_loss": Decimal("10.00"),
        "potential_profit": Decimal("9.00"),
        "gross_payout": Decimal("19.00"),
        "net_liability": Decimal("10.00"),
        "exposure_pct_bankroll": Decimal("0.100000"),
        "exposure_pct_available": Decimal("0.200000"),
        "counts_by_state": MappingProxyType({"open": 1}),
        "counts_by_record_kind": MappingProxyType({"position": 1}),
        "counts_by_portfolio_kind": MappingProxyType({"cash": 1}),
        "dimension_aggregates": (
            ExposureDimensionAggregateV1(
                dimension_type=ExposureDimensionTypeV1.EVENT,
                dimension_key="event-1",
                measure=ExposureMeasureV1.MAXIMUM_LOSS,
                currency="USD",
                portfolio_kind=ExposurePortfolioKindV1.CASH,
                amount=Decimal("10.00"),
            ),
        ),
        "concentration_ratios": (
            ExposureDimensionRatioV1(
                dimension_type=ExposureDimensionTypeV1.EVENT,
                dimension_key="event-1",
                measure="maximum_loss",
                value=Decimal("1.000000"),
            ),
        ),
        "correlated_maximum_loss_by_group": MappingProxyType(
            {"group-1": Decimal("10.00")}
        ),
        "reasons": (),
        "status": ExposureAvailabilityStatusV1.AVAILABLE,
    }
    payload.update(overrides)
    if "authoritative_bankroll_reference" not in overrides:
        bankroll = _bankroll(
            bankroll_reference_id=str(payload["bankroll_reference_id"]),
            portfolio_id=str(payload["portfolio_id"]),
            account_id=str(payload["account_id"]),
            currency=str(payload["currency"]),
            as_of=str(payload["as_of"]),
        )
        payload["authoritative_bankroll_reference"] = bankroll
        if "bankroll_reference_hash" not in overrides:
            payload["bankroll_reference_hash"] = bankroll.canonical_input_hash
    if "authoritative_records" not in overrides:
        record_ids = tuple(payload["included_record_ids"])
        records = tuple(
            _record(
                record_id=record_id,
                source_record_id=f"src-{record_id}",
                position_id=("position-1" if index == 1 else f"position-{index}"),
                portfolio_id=str(payload["portfolio_id"]),
                account_id=str(payload["account_id"]),
                portfolio_kind=payload["portfolio_kind"],
                currency=str(payload["currency"]),
                effective_at=str(payload["as_of"]),
                as_of_eligible_at=str(payload["as_of"]),
            )
            for index, record_id in enumerate(record_ids, start=1)
        )
        payload["authoritative_records"] = records
        if "included_record_hashes" not in overrides:
            payload["included_record_hashes"] = MappingProxyType(
                {record.record_id: record.canonical_record_hash for record in records}
            )
    records = tuple(payload.get("authoritative_records") or ())
    if "authoritative_contributions" not in overrides:
        contributions = tuple(
            contribution
            for record in records
            for contribution in _snapshot_contributions(
                record=record,
                snapshot_id=str(payload["snapshot_id"]),
                portfolio_id=str(payload["portfolio_id"]),
                account_id=str(payload["account_id"]),
                portfolio_kind=payload["portfolio_kind"],
                currency=str(payload["currency"]),
                as_of=str(payload["as_of"]),
                calculation_version=str(payload["calculation_version"]),
            )
        )
        payload["authoritative_contributions"] = contributions
        if "contribution_refs" not in overrides:
            payload["contribution_refs"] = tuple(
                ExposureContributionHashRefV1(
                    contribution_id=contribution.contribution_id,
                    canonical_contribution_hash=(
                        contribution.canonical_contribution_hash
                    ),
                )
                for contribution in contributions
            )
    contributions = tuple(payload.get("authoritative_contributions") or ())
    source_dimension_amounts = {
        (
            contribution.record_id,
            contribution.dimension_type,
            contribution.dimension_key,
            contribution.measure,
        ): contribution.amount
        for contribution in contributions
        if contribution.contribution_scope
        == ExposureContributionScopeV1.DIMENSION_ALLOCATION
    }
    grouped_dimension_amounts: dict[
        tuple[ExposureDimensionTypeV1, str, ExposureMeasureV1],
        Decimal,
    ] = {}
    for (
        _record_id,
        dimension_type,
        dimension_key,
        measure,
    ), amount in source_dimension_amounts.items():
        key = (dimension_type, dimension_key, measure)
        grouped_dimension_amounts[key] = (
            grouped_dimension_amounts.get(key, Decimal("0.00")) + amount
        )
    derived_aggregates = tuple(
        sorted(
            (
                ExposureDimensionAggregateV1(
                    dimension_type=dimension_type,
                    dimension_key=dimension_key,
                    measure=measure,
                    currency=str(payload["currency"]),
                    portfolio_kind=payload["portfolio_kind"],
                    amount=amount,
                )
                for (
                    dimension_type,
                    dimension_key,
                    measure,
                ), amount in grouped_dimension_amounts.items()
            ),
            key=lambda item: (
                item.dimension_type.value,
                item.dimension_key,
                item.measure.value,
            ),
        )
    )
    maximum_loss = payload["maximum_possible_loss"]

    def ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
        if denominator == Decimal("0"):
            return None
        return normalize_ratio_decimal("test_ratio", numerator / denominator)

    if "dimension_aggregates" not in overrides:
        payload["dimension_aggregates"] = derived_aggregates
    if "concentration_ratios" not in overrides:
        payload["concentration_ratios"] = tuple(
            ExposureDimensionRatioV1(
                dimension_type=aggregate.dimension_type,
                dimension_key=aggregate.dimension_key,
                measure=aggregate.measure,
                value=ratio(aggregate.amount, maximum_loss),
            )
            for aggregate in derived_aggregates
        )
    if "correlated_maximum_loss_by_group" not in overrides:
        payload["correlated_maximum_loss_by_group"] = MappingProxyType(
            {
                aggregate.dimension_key: aggregate.amount
                for aggregate in derived_aggregates
                if aggregate.dimension_type == ExposureDimensionTypeV1.CORRELATION_GROUP
            }
        )
    bankroll = payload["authoritative_bankroll_reference"]
    if "exposure_pct_bankroll" not in overrides:
        payload["exposure_pct_bankroll"] = ratio(
            maximum_loss,
            bankroll.total_bankroll,
        )
    if "exposure_pct_available" not in overrides:
        payload["exposure_pct_available"] = ratio(
            maximum_loss,
            bankroll.available_balance,
        )
    return AuthoritativeExposureSnapshotV1(**payload)


def _bankroll(**overrides: object) -> BankrollReferenceV1:
    payload = {
        "bankroll_reference_id": "bankroll-1",
        "portfolio_id": "portfolio-1",
        "account_id": "account-1",
        "currency": "USD",
        "ledger_version": "ledger-v1",
        "available_balance": Decimal("100.00"),
        "total_bankroll": Decimal("120.00"),
        "reserved_balance": Decimal("0.00"),
        "as_of": "2026-07-26T00:00:00+00:00",
        "source_reference": "ledger-entry",
    }
    payload.update(overrides)
    return BankrollReferenceV1(**payload)


def _candidate_contributions(
    *,
    candidate_id: str = "candidate-1",
    candidate_hash: str | None = None,
    projection_id: str = "projection-1",
    **overrides: object,
) -> tuple[ExposureContributionV1, ...]:
    resolved_candidate_hash = (
        candidate_hash or _candidate(candidate_id=candidate_id).canonical_input_hash
    )
    payloads = {
        ExposureMeasureV1.STAKE_COMMITTED: Decimal("10.00"),
        ExposureMeasureV1.STAKE_RESERVED: Decimal("0.00"),
        ExposureMeasureV1.MAXIMUM_LOSS: Decimal("10.00"),
        ExposureMeasureV1.POTENTIAL_PROFIT: Decimal("9.00"),
        ExposureMeasureV1.GROSS_PAYOUT: Decimal("19.00"),
        ExposureMeasureV1.NET_LIABILITY: Decimal("10.00"),
    }
    result = []
    for index, (measure, amount) in enumerate(payloads.items(), start=1):
        payload = {
            "contribution_id": f"top-{index}",
            "snapshot_or_projection_id": projection_id,
            "portfolio_id": "portfolio-1",
            "account_id": "account-1",
            "record_id": None,
            "position_id": None,
            "order_intent_id": None,
            "reservation_id": None,
            "candidate_id": candidate_id,
            "parlay_id": None,
            "leg_id": None,
            "dimension_type": ExposureDimensionTypeV1.PORTFOLIO,
            "dimension_key": "portfolio-1",
            "measure": measure,
            "amount": amount,
            "currency": "USD",
            "portfolio_kind": ExposurePortfolioKindV1.CASH,
            "position_state": ExposureRecordStateV1.PROPOSED_CANDIDATE,
            "source_repository": "sports.execution.exposure_v1",
            "calculation_version": "exposure_calc_v1",
            "as_of": "2026-07-26T00:00:00+00:00",
            "source_input_hash": resolved_candidate_hash,
            "contribution_scope": ExposureContributionScopeV1.TOP_LEVEL_ECONOMIC,
            "additive_to_portfolio_totals": True,
            "non_additive_note": "",
        }
        payload.update(overrides)
        result.append(ExposureContributionV1(**payload))
    return tuple(result)


def _full_dimension_contributions(
    *,
    candidate: ExposureCandidateV1,
    projection_id: str = "projection-1",
    calculation_version: str = "exposure_calc_v1",
) -> tuple[ExposureContributionV1, ...]:
    specs: list[tuple[str | None, ExposureDimensionTypeV1, str]] = [
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
    ]
    if candidate.selection_id:
        specs.append((None, ExposureDimensionTypeV1.SELECTION, candidate.selection_id))
    if candidate.outcome_id:
        specs.append((None, ExposureDimensionTypeV1.OUTCOME, candidate.outcome_id))
    specs.extend(
        (None, ExposureDimensionTypeV1.TEAM, team_id) for team_id in candidate.team_ids
    )
    specs.extend(
        (None, ExposureDimensionTypeV1.PLAYER, player_id)
        for player_id in candidate.player_ids
    )
    specs.extend(
        (None, ExposureDimensionTypeV1.CORRELATION_GROUP, group_id)
        for group_id in candidate.correlation_group_ids
    )
    if candidate.parlay_id:
        specs.append((None, ExposureDimensionTypeV1.PARLAY, candidate.parlay_id))
        for leg in candidate.legs:
            specs.extend(
                (
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
                )
            )
            if leg.selection_id:
                specs.append(
                    (leg.leg_id, ExposureDimensionTypeV1.SELECTION, leg.selection_id)
                )
            if leg.outcome_id:
                specs.append(
                    (leg.leg_id, ExposureDimensionTypeV1.OUTCOME, leg.outcome_id)
                )
            specs.extend(
                (leg.leg_id, ExposureDimensionTypeV1.TEAM, team_id)
                for team_id in leg.team_ids
            )
            specs.extend(
                (leg.leg_id, ExposureDimensionTypeV1.PLAYER, player_id)
                for player_id in leg.player_ids
            )
            specs.extend(
                (
                    leg.leg_id,
                    ExposureDimensionTypeV1.CORRELATION_GROUP,
                    group_id,
                )
                for group_id in leg.correlation_group_ids
            )
    result = []
    for index, (leg_id, dimension_type, dimension_key) in enumerate(specs, start=1):
        result.append(
            ExposureContributionV1(
                contribution_id=f"dimension-{index}",
                snapshot_or_projection_id=projection_id,
                portfolio_id=candidate.portfolio_id,
                account_id=candidate.account_id,
                record_id=None,
                position_id=None,
                order_intent_id=None,
                reservation_id=None,
                candidate_id=candidate.candidate_id,
                parlay_id=candidate.parlay_id,
                leg_id=leg_id,
                dimension_type=dimension_type,
                dimension_key=dimension_key,
                measure=ExposureMeasureV1.MAXIMUM_LOSS,
                amount=candidate.maximum_possible_loss,
                currency=candidate.currency,
                portfolio_kind=candidate.portfolio_kind,
                position_state=ExposureRecordStateV1.PROPOSED_CANDIDATE,
                source_repository="sports.execution.exposure_v1",
                calculation_version=calculation_version,
                as_of=candidate.as_of,
                source_input_hash=candidate.canonical_input_hash,
                contribution_scope=ExposureContributionScopeV1.DIMENSION_ALLOCATION,
                additive_to_portfolio_totals=False,
                non_additive_note="full parent maximum loss concentration allocation",
            )
        )
    return tuple(result)


def _dimension_contributions(
    *,
    candidate_id: str = "candidate-1",
    candidate_hash: str = "candidate-hash-1",
    projection_id: str = "projection-1",
    **overrides: object,
) -> tuple[ExposureContributionV1, ...]:
    payload = {
        "contribution_id": "leg-1",
        "snapshot_or_projection_id": projection_id,
        "portfolio_id": "portfolio-1",
        "account_id": "account-1",
        "record_id": None,
        "position_id": None,
        "order_intent_id": None,
        "reservation_id": None,
        "candidate_id": candidate_id,
        "parlay_id": "parlay-1",
        "leg_id": "leg-1",
        "dimension_type": ExposureDimensionTypeV1.PARLAY_LEG,
        "dimension_key": "leg-1",
        "measure": ExposureMeasureV1.MAXIMUM_LOSS,
        "amount": Decimal("10.00"),
        "currency": "USD",
        "portfolio_kind": ExposurePortfolioKindV1.CASH,
        "position_state": ExposureRecordStateV1.PROPOSED_CANDIDATE,
        "source_repository": "sports.execution.exposure_v1",
        "calculation_version": "exposure_calc_v1",
        "as_of": "2026-07-26T00:00:00+00:00",
        "source_input_hash": candidate_hash,
        "contribution_scope": ExposureContributionScopeV1.DIMENSION_ALLOCATION,
        "additive_to_portfolio_totals": False,
        "non_additive_note": "leg allocation",
    }
    payload.update(overrides)
    return (ExposureContributionV1(**payload),)


def _projection_evidence_objects() -> tuple[
    AuthoritativeExposureSnapshotV1,
    ExposureCandidateV1,
    BankrollReferenceV1,
]:
    candidate = _candidate(
        parlay_id="parlay-1",
        legs=(
            _leg(leg_id="leg-1"),
            _leg(
                leg_id="leg-2",
                event_id="event-2",
                market_id="market-2",
                selection_id="selection-2",
                outcome_id="outcome-2",
                team_ids=("team-2",),
            ),
        ),
    )
    bankroll = _bankroll()
    snapshot = _snapshot(
        bankroll_reference_id=bankroll.bankroll_reference_id,
        bankroll_reference_hash=bankroll.canonical_input_hash,
    )
    return snapshot, candidate, bankroll


def _projection(**overrides: object) -> ExposureProjectionV1:
    snapshot, candidate, bankroll = _projection_evidence_objects()
    dimensionals = _full_dimension_contributions(candidate=candidate)
    payload = {
        "projection_id": "projection-1",
        "current_snapshot": snapshot,
        "candidate": candidate,
        "bankroll_reference": bankroll,
        "candidate_top_level_contributions": _candidate_contributions(
            candidate_id=candidate.candidate_id,
            candidate_hash=candidate.canonical_input_hash,
            parlay_id=candidate.parlay_id,
        ),
        "candidate_dimension_contributions": dimensionals,
        "affected_dimensions": tuple(
            sorted(
                {
                    f"{item.dimension_type.value}:{item.dimension_key}"
                    for item in dimensionals
                }
            )
        ),
        "reasons": (),
        "status": ExposureProjectionStatusV1.AVAILABLE,
    }
    payload.update(overrides)
    return ExposureProjectionV1.from_authoritative_inputs(**payload)


def _clone(instance: object, **overrides: object) -> dict[str, object]:
    payload = {
        item.name: getattr(instance, item.name)
        for item in fields(type(instance))
        if item.init
    }
    payload.update(overrides)
    if overrides:
        for canonical_field in (
            "canonical_contribution_hash",
            "canonical_input_hash",
            "canonical_record_hash",
        ):
            if canonical_field in payload and canonical_field not in overrides:
                payload[canonical_field] = ""
    return payload


@pytest.mark.parametrize(
    ("state", "committed", "reserved", "projected", "historical"),
    [
        (row.state, row.committed, row.reserved, row.projected, row.historical_only)
        for row in STATE_INCLUSION_MATRIX_V1
    ],
)
def test_every_state_inclusion_matrix_row(
    state, committed, reserved, projected, historical
):
    row = next(item for item in STATE_INCLUSION_MATRIX_V1 if item.state == state)
    assert (row.committed, row.reserved, row.projected, row.historical_only) == (
        committed,
        reserved,
        projected,
        historical,
    )


def test_state_alone_cannot_fabricate_reservation():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_RESERVATION_AUTHORITY_MISSING,
    ):
        _record(
            state=ExposureRecordStateV1.PENDING_CONFIRMATION,
            stake_committed=Decimal("0.00"),
            maximum_possible_loss=Decimal("0.00"),
            gross_payout=Decimal("0.00"),
            net_liability=Decimal("0.00"),
            potential_profit=Decimal("0.00"),
        )


def test_reservation_requires_reservation_id_and_positive_reserved_stake():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_RESERVATION_AUTHORITY_MISSING,
    ):
        _reservation_record(reservation_id=None)


def test_open_position_requires_position_identity_and_committed_economics():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_OPEN_POSITION_IDENTITY_MISSING,
    ):
        _record(position_id=None)


def test_settled_canceled_voided_records_cannot_retain_current_economics():
    for state in (
        ExposureRecordStateV1.SETTLED,
        ExposureRecordStateV1.CANCELED,
        ExposureRecordStateV1.VOIDED,
    ):
        with pytest.raises(
            ExposureValidationError,
            match=ExposureReasonCodeV1.EXP_CURRENT_ECONOMICS_FOR_HISTORICAL_STATE,
        ):
            _record(state=state)


def test_corrected_record_is_historical_only():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CORRECTED_RECORD_NOT_HISTORICAL,
    ):
        _record(state=ExposureRecordStateV1.CORRECTED)


def test_partially_settled_stores_unresolved_residual_only():
    with pytest.raises(
        ExposureValidationError, match=ExposureReasonCodeV1.EXP_STATE_CONFLICT
    ):
        _record(
            state=ExposureRecordStateV1.PARTIALLY_SETTLED, residual_reference_id=None
        )


def test_synthetic_or_practice_cannot_enter_cash_portfolio():
    with pytest.raises(
        ExposureValidationError, match=ExposureReasonCodeV1.EXP_PORTFOLIO_KIND_CONFLICT
    ):
        _record(record_kind=ExposureRecordKindV1.SYNTHETIC_POSITION)
    with pytest.raises(
        ExposureValidationError, match=ExposureReasonCodeV1.EXP_PORTFOLIO_KIND_CONFLICT
    ):
        _record(record_kind=ExposureRecordKindV1.PRACTICE_POSITION)


def test_exact_payout_equation():
    with pytest.raises(
        ExposureValidationError, match=ExposureReasonCodeV1.EXP_PAYOUT_INCONSISTENT
    ):
        _record(gross_payout=Decimal("19.01"))


def test_reservation_specific_economics():
    reservation = _reservation_record()
    assert reservation.maximum_possible_loss == reservation.stake_reserved
    assert reservation.net_liability == reservation.stake_reserved
    assert reservation.gross_payout == Decimal("0.00")


def test_zero_behavior_by_record_kind():
    with pytest.raises(
        ExposureValidationError, match=ExposureReasonCodeV1.EXP_STATE_CONFLICT
    ):
        _record(
            stake_committed=Decimal("0.00"),
            maximum_possible_loss=Decimal("0.00"),
            gross_payout=Decimal("0.00"),
            net_liability=Decimal("0.00"),
            potential_profit=Decimal("0.00"),
        )


def test_bankroll_reference_consistency_and_hashing():
    bankroll = _bankroll()
    assert bankroll.currency == "USD"
    assert bankroll.canonical_input_hash


def test_top_level_contribution_cannot_have_leg_identity():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_PROJECTION_SOURCE_MISMATCH,
    ):
        ExposureContributionV1(
            contribution_id="bad-top",
            snapshot_or_projection_id="projection-1",
            portfolio_id="portfolio-1",
            account_id="account-1",
            record_id=None,
            position_id=None,
            order_intent_id=None,
            reservation_id=None,
            candidate_id="candidate-1",
            parlay_id=None,
            leg_id="leg-1",
            dimension_type=ExposureDimensionTypeV1.PORTFOLIO,
            dimension_key="portfolio-1",
            measure=ExposureMeasureV1.MAXIMUM_LOSS,
            amount=Decimal("10.00"),
            currency="USD",
            portfolio_kind=ExposurePortfolioKindV1.CASH,
            position_state=ExposureRecordStateV1.PROPOSED_CANDIDATE,
            source_repository="sports.execution.exposure_v1",
            calculation_version="exposure_calc_v1",
            as_of="2026-07-26T00:00:00+00:00",
            source_input_hash="candidate-hash-1",
            contribution_scope=ExposureContributionScopeV1.TOP_LEVEL_ECONOMIC,
            additive_to_portfolio_totals=True,
        )


def test_dimension_contribution_cannot_be_additive():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_CONTRIBUTION_SCOPE,
    ):
        ExposureContributionV1(
            contribution_id="bad-dim",
            snapshot_or_projection_id="projection-1",
            portfolio_id="portfolio-1",
            account_id="account-1",
            record_id=None,
            position_id=None,
            order_intent_id=None,
            reservation_id=None,
            candidate_id="candidate-1",
            parlay_id=None,
            leg_id="leg-1",
            dimension_type=ExposureDimensionTypeV1.PARLAY_LEG,
            dimension_key="leg-1",
            measure=ExposureMeasureV1.MAXIMUM_LOSS,
            amount=Decimal("10.00"),
            currency="USD",
            portfolio_kind=ExposurePortfolioKindV1.CASH,
            position_state=ExposureRecordStateV1.PROPOSED_CANDIDATE,
            source_repository="sports.execution.exposure_v1",
            calculation_version="exposure_calc_v1",
            as_of="2026-07-26T00:00:00+00:00",
            source_input_hash="candidate-hash-1",
            contribution_scope=ExposureContributionScopeV1.DIMENSION_ALLOCATION,
            additive_to_portfolio_totals=True,
            non_additive_note="bad",
        )


def test_duplicate_contribution_ids_block():
    dup = _projection().candidate_top_level_contributions[0]
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_DUPLICATE_CONTRIBUTION_ID,
    ):
        _projection(candidate_dimension_contributions=(dup,))


def test_duplicate_top_level_measure_contributions_block():
    base = _projection()
    dup = ExposureContributionV1(
        **_clone(
            base.candidate_top_level_contributions[0], contribution_id="dup-committed"
        )
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_DUPLICATE_TOP_LEVEL_MEASURE,
    ):
        _projection(
            candidate_top_level_contributions=base.candidate_top_level_contributions
            + (dup,)
        )


def test_forged_contribution_source_hash_blocks():
    base = _projection()
    bad = ExposureContributionV1(
        **_clone(
            base.candidate_top_level_contributions[0],
            contribution_id="bad-hash",
            source_input_hash="wrong",
        )
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CONTRIBUTION_LINEAGE_FAILURE,
    ):
        _projection(
            candidate_top_level_contributions=(bad,)
            + base.candidate_top_level_contributions[1:]
        )


def test_parlay_parent_counts_once_economically():
    total = compute_portfolio_total_from_contributions(
        _candidate_contributions(),
        **{
            **_generic_total_kwargs(),
            "measure": ExposureMeasureV1.MAXIMUM_LOSS,
        },
    )
    assert total == Decimal("10.00")


def test_projection_arithmetic_must_match_exactly():
    projection = _projection()
    snapshot, candidate, bankroll = _projection_evidence_objects()
    wrong = ExposureTotalsV1(
        stake_committed=Decimal("21.00"),
        stake_reserved=Decimal("0.00"),
        maximum_possible_loss=Decimal("20.00"),
        potential_profit=Decimal("18.00"),
        gross_payout=Decimal("39.00"),
        net_liability=Decimal("20.00"),
        currency="USD",
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CANDIDATE_ECONOMICS_MISMATCH,
    ):
        ExposureProjectionV1(
            **_clone(projection, projected_totals=wrong),
            current_snapshot_evidence=snapshot,
            candidate_evidence=candidate,
            bankroll_evidence=bankroll,
        )


def test_candidate_counted_exactly_once():
    assert _projection().projected_totals.stake_committed == Decimal("20.00")


def test_wrong_candidate_contribution_blocks():
    base = _projection()
    bad = ExposureContributionV1(
        **_clone(base.candidate_top_level_contributions[0], candidate_id="other")
    )
    with pytest.raises(
        ExposureValidationError, match=ExposureReasonCodeV1.EXP_CANDIDATE_COLLISION
    ):
        _projection(
            candidate_top_level_contributions=(bad,)
            + base.candidate_top_level_contributions[1:]
        )


def test_candidate_id_collision_blocks():
    with pytest.raises(
        ExposureValidationError, match=ExposureReasonCodeV1.EXP_CANDIDATE_COLLISION
    ):
        validate_candidate_collision(
            candidate_id="record-1",
            candidate_hash="candidate-hash-1",
            record_identities=(ExposureRecordIdentity.from_record(_record()),),
        )


def test_forged_snapshot_hash_blocks():
    with pytest.raises(
        ExposureValidationError, match=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH
    ):
        _snapshot(canonical_input_hash="forged")


def test_forged_projection_hash_blocks():
    with pytest.raises(
        ExposureValidationError, match=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH
    ):
        projection = _projection()
        snapshot, candidate, bankroll = _projection_evidence_objects()
        ExposureProjectionV1(
            **_clone(projection, canonical_input_hash="forged"),
            current_snapshot_evidence=snapshot,
            candidate_evidence=candidate,
            bankroll_evidence=bankroll,
        )


def test_request_hash_changes_when_candidate_hash_changes():
    first = ExposureCalculationRequestV1(
        request_id="req-1",
        portfolio_id="portfolio-1",
        account_id="account-1",
        currency="USD",
        as_of="2026-07-26T00:00:00+00:00",
        calculation_version="exposure_calc_v1",
        record_ids=("record-1",),
        record_hashes=MappingProxyType({"record-1": "record-hash-1"}),
        bankroll_reference_id="bankroll-1",
        bankroll_reference_hash="bankroll-hash-1",
        candidate_id="candidate-1",
        candidate_input_hash="candidate-hash-1",
        identity_map_versions=MappingProxyType({"sportsbook": "v1"}),
        idempotency_key="idem-1",
    )
    second = ExposureCalculationRequestV1(
        request_id="req-1",
        portfolio_id="portfolio-1",
        account_id="account-1",
        currency="USD",
        as_of="2026-07-26T00:00:00+00:00",
        calculation_version="exposure_calc_v1",
        record_ids=("record-1",),
        record_hashes=MappingProxyType({"record-1": "record-hash-1"}),
        bankroll_reference_id="bankroll-1",
        bankroll_reference_hash="bankroll-hash-1",
        candidate_id="candidate-1",
        candidate_input_hash="candidate-hash-2",
        identity_map_versions=MappingProxyType({"sportsbook": "v1"}),
        idempotency_key="idem-1",
    )
    assert first.canonical_input_hash != second.canonical_input_hash


def test_request_hash_changes_when_bankroll_hash_changes():
    first = ExposureCalculationRequestV1(
        request_id="req-1",
        portfolio_id="portfolio-1",
        account_id="account-1",
        currency="USD",
        as_of="2026-07-26T00:00:00+00:00",
        calculation_version="exposure_calc_v1",
        record_ids=("record-1",),
        record_hashes=MappingProxyType({"record-1": "record-hash-1"}),
        bankroll_reference_id="bankroll-1",
        bankroll_reference_hash="bankroll-hash-1",
        candidate_id=None,
        candidate_input_hash=None,
        identity_map_versions=MappingProxyType({"sportsbook": "v1"}),
        idempotency_key="idem-1",
    )
    second = ExposureCalculationRequestV1(
        request_id="req-1",
        portfolio_id="portfolio-1",
        account_id="account-1",
        currency="USD",
        as_of="2026-07-26T00:00:00+00:00",
        calculation_version="exposure_calc_v1",
        record_ids=("record-1",),
        record_hashes=MappingProxyType({"record-1": "record-hash-1"}),
        bankroll_reference_id="bankroll-1",
        bankroll_reference_hash="bankroll-hash-2",
        candidate_id=None,
        candidate_input_hash=None,
        identity_map_versions=MappingProxyType({"sportsbook": "v1"}),
        idempotency_key="idem-1",
    )
    assert first.canonical_input_hash != second.canonical_input_hash


def test_nested_mappings_and_lists_cannot_mutate_frozen_contracts():
    team_ids = ["team-1"]
    player_ids = []
    leg = _leg(team_ids=team_ids, player_ids=player_ids)
    team_ids.append("team-2")
    player_ids.append("player-1")
    assert leg.team_ids == ("team-1",)
    assert leg.player_ids == ()


def test_equivalent_timestamp_forms_hash_identically():
    first = _record(record_id="record-ts", effective_at="2026-07-26T00:00:00Z")
    second = _record(record_id="record-ts", effective_at="2026-07-26T00:00:00+00:00")
    assert first.canonical_record_hash == second.canonical_record_hash


def test_currency_stored_uppercase():
    assert _record().currency == "USD"


def test_whitespace_text_canonicalized():
    record = _record(record_id="  record-1  ", source_repository=" sports.personal ")
    assert record.record_id == "record-1"
    assert record.source_repository == "sports.personal"


def test_ratios_retain_six_decimal_precision():
    assert normalize_ratio_decimal("ratio", Decimal("0.1234564")) == Decimal("0.123456")


def test_global_decimal_context_changes_do_not_affect_results():
    original = getcontext().prec
    try:
        getcontext().prec = 4
        assert normalize_monetary_decimal("value", Decimal("10.015"), "USD") == Decimal(
            "10.02"
        )
        assert normalize_ratio_decimal("ratio", Decimal("0.1234567")) == Decimal(
            "0.123457"
        )
    finally:
        getcontext().prec = original


def test_full_candidate_parlay_leg_identity_affects_hash():
    first = _candidate(
        parlay_id="parlay-1",
        legs=(
            _leg(leg_id="leg-1"),
            _leg(
                leg_id="leg-2",
                event_id="event-2",
                market_id="market-2",
                selection_id="selection-2",
                outcome_id="outcome-2",
                team_ids=("team-2",),
            ),
        ),
    )
    second = _candidate(
        parlay_id="parlay-1",
        legs=(
            _leg(leg_id="leg-1", event_id="event-x"),
            _leg(
                leg_id="leg-2",
                event_id="event-2",
                market_id="market-2",
                selection_id="selection-2",
                outcome_id="outcome-2",
                team_ids=("team-2",),
            ),
        ),
    )
    assert first.canonical_input_hash != second.canonical_input_hash


def test_duplicate_candidate_leg_ids_block():
    with pytest.raises(
        ExposureValidationError, match=ExposureReasonCodeV1.EXP_DUPLICATE_PARLAY_LEG_ID
    ):
        _candidate(
            parlay_id="parlay-1", legs=(_leg(leg_id="leg-1"), _leg(leg_id="leg-1"))
        )


def test_exact_duplicate_records_remain_idempotent():
    validated = validate_record_collection(
        (_record(), _record()), as_of="2026-07-26T00:00:00+00:00"
    )
    assert len(validated) == 1


def test_duplicate_parent_parlay_ids_block():
    first = _record(
        record_id="r1",
        position_id="p1",
        source_record_id="src-r1",
        parlay_id="parlay-1",
        legs=(
            _leg(leg_id="r1-leg-1"),
            _leg(leg_id="r1-leg-2", event_id="event-2", market_id="market-2"),
        ),
    )
    second = _record(
        record_id="r2",
        position_id="p2",
        source_record_id="src-r2",
        parlay_id="parlay-1",
        legs=(
            _leg(leg_id="r2-leg-1"),
            _leg(leg_id="r2-leg-2", event_id="event-2", market_id="market-2"),
        ),
    )
    with pytest.raises(
        ExposureValidationError, match=ExposureReasonCodeV1.EXP_DUPLICATE_PARLAY_ID
    ):
        validate_record_collection(
            (first, second),
            as_of="2026-07-26T00:00:00+00:00",
        )


def test_cross_repository_conflicting_imports_block():
    one = _record(
        record_id="r1",
        source_repository="repo-a",
        source_record_id="shared",
        source_revision="rev-1",
    )
    two = _record(
        record_id="r2",
        source_repository="repo-b",
        source_record_id="shared",
        source_revision="rev-1",
        position_id="position-2",
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CROSS_REPOSITORY_DUPLICATE_IMPORT,
    ):
        validate_record_collection((one, two), as_of="2026-07-26T00:00:00+00:00")


def test_correction_reversal_self_reference_blocks():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CORRECTION_REVERSAL_CONFLICT,
    ):
        _record(correction_of_record_id="record-1")


def test_structured_domain_reasons_emitted_by_production_validation():
    with pytest.raises(ExposureValidationError) as exc:
        _record(stake_reserved=Decimal("1.00"))
    assert (
        exc.value.reasons[0].code
        == ExposureReasonCodeV1.EXP_RESERVATION_AUTHORITY_MISSING
    )


def test_no_exposure_input_contract_accepts_probability_fields():
    with pytest.raises(TypeError):
        _record(raw_sip_probability=Decimal("0.50"))
    with pytest.raises(TypeError):
        _candidate(reconciled_execution_probability=Decimal("0.50"))


def test_snapshot_contains_no_approval_or_sizing_fields():
    snapshot_fields = {item.name for item in fields(AuthoritativeExposureSnapshotV1)}
    assert "approved" not in snapshot_fields
    assert "approved_stake" not in snapshot_fields
    assert "kelly_fraction" not in snapshot_fields
    assert "risk_decision_id" not in snapshot_fields


def test_projection_cannot_mutate_bankroll_reference_fields():
    projection = _projection()
    assert projection.bankroll_available_balance == Decimal("100.00")
    assert projection.bankroll_reserved_balance == Decimal("0.00")
    assert projection.hypothetical_available_after == Decimal("90.00")


def test_structured_reasons_serialize_deterministically():
    reason = ExposureReasonV1(
        code=ExposureReasonCodeV1.EXP_BANKROLL_DENOMINATOR_ZERO,
        severity=ExposureReasonSeverityV1.WARNING,
        category=ExposureReasonCategoryV1.BANKROLL_REFERENCE,
        message="bankroll denominator is zero",
        metadata=MappingProxyType({"b": [2, 1], "a": {"z": 2, "x": 1}}),
    )
    assert canonical_json({"reasons": (reason,)}) == canonical_json(
        {"reasons": (reason,)}
    )


def test_parse_utc_timestamp_rejects_naive_values():
    with pytest.raises(ValueError, match="must include timezone"):
        parse_utc_timestamp("effective_at", "2026-07-26T00:00:00")


# Final adversarial probes


def test_empty_candidate_contributions_reject():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_EMPTY_CANDIDATE_CONTRIBUTIONS,
    ):
        _projection(candidate_top_level_contributions=())


def test_partial_candidate_measure_set_rejects():
    base = _projection()
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INCOMPLETE_CANDIDATE_MEASURE_SET,
    ):
        _projection(
            candidate_top_level_contributions=base.candidate_top_level_contributions[
                :-1
            ]
        )


def test_wrong_candidate_contribution_amount_rejects():
    base = _projection()
    bad = ExposureContributionV1(
        **_clone(base.candidate_top_level_contributions[0], amount=Decimal("11.00"))
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CANDIDATE_ECONOMICS_MISMATCH,
    ):
        _projection(
            candidate_top_level_contributions=(bad,)
            + base.candidate_top_level_contributions[1:]
        )


def test_eur_contribution_in_usd_projection_rejects():
    base = _projection()
    bad = ExposureContributionV1(
        **_clone(base.candidate_top_level_contributions[0], currency="EUR")
    )
    with pytest.raises(
        ExposureValidationError, match=ExposureReasonCodeV1.EXP_CURRENCY_MISMATCH
    ):
        _projection(
            candidate_top_level_contributions=(bad,)
            + base.candidate_top_level_contributions[1:]
        )


def test_synthetic_contribution_in_cash_projection_rejects():
    base = _projection()
    bad = ExposureContributionV1(
        **_clone(
            base.candidate_top_level_contributions[0],
            portfolio_kind=ExposurePortfolioKindV1.SYNTHETIC,
        )
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_PROJECTION_PORTFOLIO_MISMATCH,
    ):
        _projection(
            candidate_top_level_contributions=(bad,)
            + base.candidate_top_level_contributions[1:]
        )


def test_wrong_contribution_as_of_rejects():
    base = _projection()
    bad = ExposureContributionV1(
        **_clone(
            base.candidate_top_level_contributions[0], as_of="2026-07-26T00:01:00+00:00"
        )
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_PROJECTION_SOURCE_MISMATCH,
    ):
        _projection(
            candidate_top_level_contributions=(bad,)
            + base.candidate_top_level_contributions[1:]
        )


def test_wrong_calculation_version_rejects():
    with pytest.raises(
        ExposureValidationError, match=ExposureReasonCodeV1.EXP_UNSUPPORTED_VERSION
    ):
        base = _projection()
        ExposureContributionV1(
            **_clone(
                base.candidate_top_level_contributions[0],
                calculation_version="exposure_calc_v9",
            )
        )


def test_proposed_candidate_record_rejects():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_PROPOSED_CANDIDATE_RECORD_REJECTED,
    ):
        _record(state=ExposureRecordStateV1.PROPOSED_CANDIDATE)


def test_missing_both_selection_and_outcome_identity_rejects():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_MISSING_CANONICAL_IDENTITY,
    ):
        _record(selection_id=None, outcome_id=None)


def test_practice_position_in_recorded_real_portfolio_rejects():
    with pytest.raises(
        ExposureValidationError, match=ExposureReasonCodeV1.EXP_PORTFOLIO_KIND_CONFLICT
    ):
        _record(
            record_kind=ExposureRecordKindV1.PRACTICE_POSITION,
            portfolio_kind=ExposurePortfolioKindV1.RECORDED_REAL,
        )


def test_practice_position_in_synthetic_portfolio_rejects():
    with pytest.raises(
        ExposureValidationError, match=ExposureReasonCodeV1.EXP_PORTFOLIO_KIND_CONFLICT
    ):
        _record(
            record_kind=ExposureRecordKindV1.PRACTICE_POSITION,
            portfolio_kind=ExposurePortfolioKindV1.SYNTHETIC,
        )


def test_generic_position_in_incompatible_portfolio_kind_rejects():
    with pytest.raises(
        ExposureValidationError, match=ExposureReasonCodeV1.EXP_PORTFOLIO_KIND_CONFLICT
    ):
        _record(portfolio_kind=ExposurePortfolioKindV1.PRACTICE)


def test_forged_reduced_dedup_identity_rejects():
    record = _record(record_id="r-1")
    forged = ExposureRecordIdentity.from_record(record)
    forged = ExposureRecordIdentity(**_clone(forged, record_hash="forged"))
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_FORGED_REDUCED_RECORD_IDENTITY,
    ):
        validate_record_collection((record, forged), as_of="2026-07-26T00:00:00+00:00")


def test_same_import_through_two_repositories_rejects():
    one = _record(
        record_id="r-1",
        source_repository="repo-a",
        source_record_id="shared",
        source_revision="rev-1",
    )
    two = _record(
        record_id="r-2",
        source_repository="repo-b",
        source_record_id="shared",
        source_revision="rev-1",
        position_id="p-2",
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CROSS_REPOSITORY_DUPLICATE_IMPORT,
    ):
        validate_record_collection((one, two), as_of="2026-07-26T00:00:00+00:00")


def test_reversed_projection_contributions_preserve_hash():
    first = _projection()
    _, candidate, _ = _projection_evidence_objects()
    second = _projection(
        candidate_top_level_contributions=tuple(
            reversed(
                _candidate_contributions(
                    candidate_hash=first.candidate_input_hash,
                    parlay_id=candidate.parlay_id,
                )
            )
        ),
        candidate_dimension_contributions=tuple(
            reversed(_full_dimension_contributions(candidate=candidate))
        ),
    )
    assert first.canonical_input_hash == second.canonical_input_hash


def test_caller_owned_reasons_list_cannot_mutate_projection():
    reasons = [
        ExposureReasonV1(
            code=ExposureReasonCodeV1.EXP_BANKROLL_DENOMINATOR_ZERO,
            severity=ExposureReasonSeverityV1.WARNING,
            category=ExposureReasonCategoryV1.CALCULATION,
            message="warn",
        )
    ]
    projection = _projection(reasons=reasons)
    reasons.append(
        ExposureReasonV1(
            code=ExposureReasonCodeV1.EXP_AVAILABLE_BALANCE_ZERO,
            severity=ExposureReasonSeverityV1.WARNING,
            category=ExposureReasonCategoryV1.CALCULATION,
            message="warn2",
        )
    )
    assert len(projection.reasons) == 1


def test_totals_currency_affects_hash():
    usd = _snapshot(currency="USD")
    eur = _snapshot(currency="EUR")
    assert usd.canonical_input_hash != eur.canonical_input_hash


def test_duplicate_contribution_ids_in_generic_totals_reject():
    contribution = _candidate_contributions()[0]
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_DUPLICATE_CONTRIBUTION_ID,
    ):
        compute_portfolio_total_from_contributions(
            (contribution, contribution),
            **{
                **_generic_total_kwargs(),
                "measure": contribution.measure,
            },
        )


def test_contribution_with_both_record_and_candidate_lineage_rejects():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CONTRIBUTION_DUAL_LINEAGE,
    ):
        ExposureContributionV1(
            **_clone(_candidate_contributions()[0], record_id="record-1")
        )


def test_synthetic_or_eur_snapshot_aggregate_in_cash_usd_snapshot_rejects():
    bad_aggregate = ExposureDimensionAggregateV1(
        dimension_type=ExposureDimensionTypeV1.EVENT,
        dimension_key="event-1",
        measure=ExposureMeasureV1.MAXIMUM_LOSS,
        currency="EUR",
        portfolio_kind=ExposurePortfolioKindV1.SYNTHETIC,
        amount=Decimal("10.00"),
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_SNAPSHOT_AGGREGATE_SCOPE_MISMATCH,
    ):
        _snapshot(dimension_aggregates=(bad_aggregate,))


def test_duplicate_snapshot_contribution_references_reject():
    ref = ExposureContributionHashRefV1("dup", "hash-1")
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_DUPLICATE_CONTRIBUTION_ID,
    ):
        _snapshot(contribution_refs=(ref, ref))


def test_candidate_legs_without_parlay_id_reject():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_PARLAY_CARDINALITY,
    ):
        _candidate(
            legs=(
                _leg(leg_id="leg-1"),
                _leg(
                    leg_id="leg-2",
                    event_id="event-2",
                    market_id="market-2",
                    selection_id="selection-2",
                    outcome_id="outcome-2",
                    team_ids=("team-2",),
                ),
            )
        )


def test_parlay_id_without_legs_rejects():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_PARLAY_CARDINALITY,
    ):
        _candidate(parlay_id="parlay-1")


def test_one_leg_parlay_rejects():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_PARLAY_CARDINALITY,
    ):
        _candidate(parlay_id="parlay-1", legs=(_leg(leg_id="leg-1"),))


def test_missing_leg_in_affected_dimensions_rejects():
    _, candidate, _ = _projection_evidence_objects()
    partial = tuple(
        contribution
        for contribution in _full_dimension_contributions(candidate=candidate)
        if contribution.leg_id != "leg-2"
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CANDIDATE_PARLAY_EVIDENCE_MISMATCH,
    ):
        _projection(
            candidate_dimension_contributions=partial,
            affected_dimensions=tuple(
                sorted(
                    {
                        f"{item.dimension_type.value}:{item.dimension_key}"
                        for item in partial
                    }
                )
            ),
        )


def test_immutable_lifecycle_state_rules():
    with pytest.raises(TypeError):
        STATE_RULES_BY_STATE[ExposureRecordStateV1.OPEN] = STATE_INCLUSION_MATRIX_V1[0]  # type: ignore[index]


def test_historical_reservation_with_nonzero_reserve_rejects():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CURRENT_ECONOMICS_FOR_HISTORICAL_STATE,
    ):
        _reservation_record(state=ExposureRecordStateV1.CANCELED)


def test_valid_historical_reservation_with_zero_current_exposure_succeeds():
    record = _reservation_record(
        state=ExposureRecordStateV1.CANCELED,
        stake_reserved=Decimal("0.00"),
        maximum_possible_loss=Decimal("0.00"),
        net_liability=Decimal("0.00"),
    )
    assert record.state == ExposureRecordStateV1.CANCELED


def test_partial_settlement_without_residual_lineage_rejects():
    with pytest.raises(
        ExposureValidationError, match=ExposureReasonCodeV1.EXP_STATE_CONFLICT
    ):
        _record(
            state=ExposureRecordStateV1.PARTIALLY_SETTLED, residual_reference_id=None
        )


def test_correction_reversal_missing_target_rejects():
    record = _record(
        record_id="replacement",
        correction_of_record_id="missing-target",
        position_id="position-1",
    )
    with pytest.raises(
        ExposureValidationError, match=ExposureReasonCodeV1.EXP_LINEAGE_TARGET_MISSING
    ):
        validate_record_collection((record,), as_of="2026-07-26T00:00:00+00:00")


def test_valid_replacement_lineage_does_not_double_count():
    historical = _record(
        record_id="old",
        state=ExposureRecordStateV1.SUPERSEDED,
        position_id="position-1",
        stake_committed=Decimal("0.00"),
        maximum_possible_loss=Decimal("0.00"),
        gross_payout=Decimal("0.00"),
        net_liability=Decimal("0.00"),
        potential_profit=Decimal("0.00"),
    )
    replacement = _record(
        record_id="new",
        position_id="position-1",
        source_revision="rev-2",
        correction_of_record_id="old",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    validated = validate_record_collection(
        (historical, replacement), as_of="2026-07-26T00:02:00+00:00"
    )
    assert tuple(item.record_id for item in validated) == ("new",)


def test_every_public_canonical_hash_rejects_forged_values():
    with pytest.raises(ExposureValidationError):
        _record(canonical_record_hash="forged")
    with pytest.raises(ExposureValidationError):
        _candidate(canonical_input_hash="forged")
    with pytest.raises(ExposureValidationError):
        _bankroll(canonical_input_hash="forged")
    with pytest.raises(ExposureValidationError):
        ExposureContributionV1(
            **_clone(
                _candidate_contributions()[0], canonical_contribution_hash="forged"
            )
        )


def test_all_exposure_input_contracts_reject_probability_fields():
    with pytest.raises(TypeError):
        _record(raw_sip_probability=Decimal("0.5"))
    with pytest.raises(TypeError):
        _candidate(reconciled_execution_probability=Decimal("0.5"))


# Phase 2A authority-sealing adversarial probes


def _factory_projection_for(
    *,
    candidate: ExposureCandidateV1 | None = None,
    current_snapshot: AuthoritativeExposureSnapshotV1 | None = None,
    bankroll_reference: BankrollReferenceV1 | None = None,
) -> ExposureProjectionV1:
    authoritative_candidate = candidate or _candidate()
    authoritative_bankroll = bankroll_reference or _bankroll()
    authoritative_snapshot = current_snapshot or _snapshot(
        bankroll_reference_id=authoritative_bankroll.bankroll_reference_id,
        bankroll_reference_hash=authoritative_bankroll.canonical_input_hash,
        authoritative_bankroll_reference=authoritative_bankroll,
    )
    dimensionals = _full_dimension_contributions(candidate=authoritative_candidate)
    return ExposureProjectionV1.from_authoritative_inputs(
        projection_id="projection-1",
        current_snapshot=authoritative_snapshot,
        candidate=authoritative_candidate,
        bankroll_reference=authoritative_bankroll,
        candidate_top_level_contributions=_candidate_contributions(
            candidate_id=authoritative_candidate.candidate_id,
            candidate_hash=authoritative_candidate.canonical_input_hash,
            parlay_id=authoritative_candidate.parlay_id,
        ),
        candidate_dimension_contributions=dimensionals,
        affected_dimensions=tuple(
            sorted(
                {
                    f"{item.dimension_type.value}:{item.dimension_key}"
                    for item in dimensionals
                }
            )
        ),
    )


def test_direct_projection_with_arbitrary_snapshot_and_bankroll_hashes_rejects():
    projection = _projection()
    snapshot, candidate, bankroll = _projection_evidence_objects()
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_PROJECTION_OBJECT_MISMATCH,
    ):
        ExposureProjectionV1(
            **_clone(
                projection,
                current_snapshot_hash="fabricated-snapshot-hash",
                bankroll_reference_hash="fabricated-bankroll-hash",
            ),
            current_snapshot_evidence=snapshot,
            candidate_evidence=candidate,
            bankroll_evidence=bankroll,
        )


def test_direct_projection_with_synchronized_candidate_hash_rejects():
    projection = _projection()
    snapshot, candidate, bankroll = _projection_evidence_objects()
    fabricated_hash = "fabricated-candidate-hash"
    top_level = tuple(
        ExposureContributionV1(
            **_clone(contribution, source_input_hash=fabricated_hash)
        )
        for contribution in projection.candidate_top_level_contributions
    )
    dimensionals = tuple(
        ExposureContributionV1(
            **_clone(contribution, source_input_hash=fabricated_hash)
        )
        for contribution in projection.candidate_dimension_contributions
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_PROJECTION_OBJECT_MISMATCH,
    ):
        ExposureProjectionV1(
            **_clone(
                projection,
                candidate_input_hash=fabricated_hash,
                candidate_top_level_contributions=top_level,
                candidate_dimension_contributions=dimensionals,
            ),
            current_snapshot_evidence=snapshot,
            candidate_evidence=candidate,
            bankroll_evidence=bankroll,
        )


@pytest.mark.parametrize(
    "candidate_override",
    (
        {"portfolio_id": "other-portfolio"},
        {"account_id": "other-account"},
        {"currency": "EUR"},
        {"as_of": "2026-07-26T00:01:00+00:00"},
    ),
)
def test_factory_rejects_candidate_scope_mismatch(candidate_override):
    with pytest.raises(ExposureValidationError):
        _factory_projection_for(candidate=_candidate(**candidate_override))


def test_factory_rejects_bankroll_not_frozen_into_snapshot():
    frozen_bankroll = _bankroll(bankroll_reference_id="frozen-bankroll")
    snapshot = _snapshot(
        bankroll_reference_id=frozen_bankroll.bankroll_reference_id,
        bankroll_reference_hash=frozen_bankroll.canonical_input_hash,
        authoritative_bankroll_reference=frozen_bankroll,
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_BANKROLL_REFERENCE_INVALID,
    ):
        _factory_projection_for(
            current_snapshot=snapshot,
            bankroll_reference=_bankroll(bankroll_reference_id="other-bankroll"),
        )


def test_coordinated_false_candidate_economics_reject():
    projection = _projection()
    false_amounts = {
        ExposureMeasureV1.STAKE_COMMITTED: Decimal("10.00"),
        ExposureMeasureV1.STAKE_RESERVED: Decimal("0.00"),
        ExposureMeasureV1.MAXIMUM_LOSS: Decimal("11.00"),
        ExposureMeasureV1.POTENTIAL_PROFIT: Decimal("10.00"),
        ExposureMeasureV1.GROSS_PAYOUT: Decimal("20.00"),
        ExposureMeasureV1.NET_LIABILITY: Decimal("11.00"),
    }
    false_contributions = tuple(
        ExposureContributionV1(
            **_clone(contribution, amount=false_amounts[contribution.measure])
        )
        for contribution in projection.candidate_top_level_contributions
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CANDIDATE_ECONOMICS_MISMATCH,
    ):
        _projection(candidate_top_level_contributions=false_contributions)


def test_authoritative_factory_does_not_accept_projected_totals_override():
    with pytest.raises(TypeError):
        _projection(projected_totals=_projection().projected_totals)


def test_settled_candidate_contribution_rejects():
    projection = _projection()
    settled = tuple(
        ExposureContributionV1(
            **_clone(
                contribution,
                position_state=ExposureRecordStateV1.SETTLED,
            )
        )
        for contribution in projection.candidate_top_level_contributions
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_CONTRIBUTION_STATE,
    ):
        _projection(candidate_top_level_contributions=settled)


def test_ghost_parlay_parent_and_leg_reject():
    projection = _projection()
    ghost = _dimension_contributions(
        candidate_hash=projection.candidate_input_hash,
        parlay_id="ghost-parlay",
        leg_id="ghost-leg",
        dimension_key="ghost-leg",
        contribution_id="ghost-leg",
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CANDIDATE_PARLAY_EVIDENCE_MISMATCH,
    ):
        _projection(
            candidate_dimension_contributions=ghost,
            affected_dimensions=("parlay_leg:ghost-leg",),
        )


def test_partial_candidate_leg_coverage_rejects():
    _, candidate, _ = _projection_evidence_objects()
    partial = tuple(
        contribution
        for contribution in _full_dimension_contributions(candidate=candidate)
        if contribution.leg_id != "leg-2"
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CANDIDATE_PARLAY_EVIDENCE_MISMATCH,
    ):
        _projection(
            candidate_dimension_contributions=partial,
            affected_dimensions=tuple(
                sorted(
                    {
                        f"{item.dimension_type.value}:{item.dimension_key}"
                        for item in partial
                    }
                )
            ),
        )


def test_oversized_candidate_dimension_amount_rejects():
    projection = _projection()
    oversized = _dimension_contributions(
        candidate_hash=projection.candidate_input_hash,
        amount=Decimal("999.00"),
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CANDIDATE_PARLAY_EVIDENCE_MISMATCH,
    ):
        _projection(
            candidate_dimension_contributions=oversized,
            affected_dimensions=("parlay_leg:leg-1",),
        )


def test_totals_currency_is_part_of_canonical_payload():
    usd = ExposureTotalsV1(
        stake_committed=Decimal("0"),
        stake_reserved=Decimal("0"),
        maximum_possible_loss=Decimal("0"),
        potential_profit=Decimal("0"),
        gross_payout=Decimal("0"),
        net_liability=Decimal("0"),
        currency="USD",
    )
    eur = ExposureTotalsV1(
        stake_committed=Decimal("0"),
        stake_reserved=Decimal("0"),
        maximum_possible_loss=Decimal("0"),
        potential_profit=Decimal("0"),
        gross_payout=Decimal("0"),
        net_liability=Decimal("0"),
        currency="EUR",
    )
    assert stable_hash(usd.as_mapping()) != stable_hash(eur.as_mapping())


def test_mixed_projection_totals_currency_rejects():
    projection = _projection()
    snapshot, candidate, bankroll = _projection_evidence_objects()
    before_eur = ExposureTotalsV1(**_clone(projection.before_totals, currency="EUR"))
    after_eur = ExposureTotalsV1(**_clone(projection.projected_totals, currency="EUR"))
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CURRENCY_MISMATCH,
    ):
        ExposureProjectionV1(
            **_clone(
                projection,
                before_totals=before_eur,
                projected_totals=after_eur,
            ),
            current_snapshot_evidence=snapshot,
            candidate_evidence=candidate,
            bankroll_evidence=bankroll,
        )


def test_unsupported_contribution_schema_version_rejects():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_UNSUPPORTED_CONTRIBUTION_SCHEMA_VERSION,
    ):
        ExposureContributionV1(
            **_clone(_candidate_contributions()[0], schema_version="v9")
        )


# Phase 2A identity, snapshot, and reason authority probes


def _zero_historical_record(**overrides: object) -> ExposureRecordV1:
    payload = {
        "state": ExposureRecordStateV1.SUPERSEDED,
        "stake_committed": Decimal("0.00"),
        "stake_reserved": Decimal("0.00"),
        "maximum_possible_loss": Decimal("0.00"),
        "potential_profit": Decimal("0.00"),
        "gross_payout": Decimal("0.00"),
        "net_liability": Decimal("0.00"),
    }
    payload.update(overrides)
    return _record(**payload)


def test_standalone_forged_reduced_record_identity_rejects():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_FORGED_REDUCED_RECORD_IDENTITY,
    ):
        validate_record_collection(
            (_identity("forged", "caller-chosen-hash"),),
            as_of="2026-07-26T00:00:00+00:00",
        )


def test_three_revision_correction_chain_returns_only_final_head():
    first = _zero_historical_record(
        record_id="revision-1",
        source_revision="rev-1",
        effective_at="2026-07-26T00:00:00+00:00",
    )
    second = _zero_historical_record(
        record_id="revision-2",
        source_revision="rev-2",
        correction_of_record_id="revision-1",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    third = _record(
        record_id="revision-3",
        source_revision="rev-3",
        correction_of_record_id="revision-2",
        effective_at="2026-07-26T00:02:00+00:00",
        as_of_eligible_at="2026-07-26T00:02:00+00:00",
    )
    validated = validate_record_collection(
        (third, first, second),
        as_of="2026-07-26T00:03:00+00:00",
    )
    assert tuple(item.record_id for item in validated) == ("revision-3",)


def test_reservation_correction_chain_returns_only_final_head():
    historical = _reservation_record(
        record_id="reservation-rev-1",
        source_revision="rev-1",
        state=ExposureRecordStateV1.SUPERSEDED,
        stake_reserved=Decimal("0.00"),
        maximum_possible_loss=Decimal("0.00"),
        net_liability=Decimal("0.00"),
    )
    replacement = _reservation_record(
        record_id="reservation-rev-2",
        source_revision="rev-2",
        correction_of_record_id="reservation-rev-1",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    validated = validate_record_collection(
        (replacement, historical),
        as_of="2026-07-26T00:02:00+00:00",
    )
    assert tuple(item.record_id for item in validated) == ("reservation-rev-2",)


def test_parlay_correction_chain_returns_only_final_head():
    legs = (
        _leg(leg_id="leg-1"),
        _leg(
            leg_id="leg-2",
            event_id="event-2",
            market_id="market-2",
            selection_id="selection-2",
            outcome_id="outcome-2",
            team_ids=("team-2",),
        ),
    )
    historical = _zero_historical_record(
        record_id="parlay-rev-1",
        source_revision="rev-1",
        parlay_id="parlay-1",
        legs=legs,
    )
    replacement = _record(
        record_id="parlay-rev-2",
        source_revision="rev-2",
        correction_of_record_id="parlay-rev-1",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
        parlay_id="parlay-1",
        legs=legs,
    )
    validated = validate_record_collection(
        (replacement, historical),
        as_of="2026-07-26T00:02:00+00:00",
    )
    assert tuple(item.record_id for item in validated) == ("parlay-rev-2",)


def test_reversed_correction_temporal_direction_rejects():
    historical = _zero_historical_record(
        record_id="old",
        effective_at="2026-07-26T00:02:00+00:00",
        as_of_eligible_at="2026-07-26T00:00:00+00:00",
    )
    replacement = _record(
        record_id="new",
        source_revision="rev-2",
        correction_of_record_id="old",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_CORRECTION_DIRECTION,
    ):
        validate_record_collection(
            (historical, replacement),
            as_of="2026-07-26T00:03:00+00:00",
        )


def test_correction_cycle_rejects():
    first = _zero_historical_record(
        record_id="cycle-1",
        source_revision="rev-1",
        correction_of_record_id="cycle-2",
    )
    second = _zero_historical_record(
        record_id="cycle-2",
        source_revision="rev-2",
        correction_of_record_id="cycle-1",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CORRECTION_CYCLE,
    ):
        validate_record_collection(
            (first, second),
            as_of="2026-07-26T00:02:00+00:00",
        )


def test_competing_lineage_heads_reject():
    historical = _zero_historical_record(record_id="old")
    first = _record(
        record_id="head-1",
        source_revision="rev-2",
        correction_of_record_id="old",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    second = _record(
        record_id="head-2",
        source_revision="rev-3",
        correction_of_record_id="old",
        effective_at="2026-07-26T00:02:00+00:00",
        as_of_eligible_at="2026-07-26T00:02:00+00:00",
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_MULTIPLE_ACTIVE_LINEAGE_HEADS,
    ):
        validate_record_collection(
            (historical, first, second),
            as_of="2026-07-26T00:03:00+00:00",
        )


def test_open_positive_reversal_rejects():
    target = _record(record_id="target")
    reversal = _record(
        record_id="reversal",
        source_revision="rev-2",
        reversal_of_record_id="target",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_CORRECTION_DIRECTION,
    ):
        validate_record_collection(
            (target, reversal),
            as_of="2026-07-26T00:02:00+00:00",
        )


def test_delimiter_collision_import_pairs_remain_distinct():
    first = _record(
        record_id="import-1",
        position_id="position-1",
        source_record_id="a:b",
        source_revision="c",
    )
    second = _record(
        record_id="import-2",
        position_id="position-2",
        source_record_id="a",
        source_revision="b:c",
    )
    validated = validate_record_collection(
        (first, second),
        as_of="2026-07-26T00:00:00+00:00",
    )
    assert {item.record_id for item in validated} == {"import-1", "import-2"}


def _generic_total_kwargs(
    *,
    candidate: ExposureCandidateV1 | None = None,
) -> dict[str, object]:
    authoritative_candidate = candidate or _candidate()
    return {
        "measure": ExposureMeasureV1.STAKE_COMMITTED,
        "currency": "USD",
        "snapshot_or_projection_id": "projection-1",
        "portfolio_id": "portfolio-1",
        "account_id": "account-1",
        "portfolio_kind": ExposurePortfolioKindV1.CASH,
        "as_of": "2026-07-26T00:00:00+00:00",
        "calculation_version": "exposure_calc_v1",
        "authoritative_records": (),
        "authoritative_candidates": (authoritative_candidate,),
    }


def test_duplicate_generic_contribution_id_rejects_globally():
    committed = _candidate_contributions()[0]
    reserved = ExposureContributionV1(
        **_clone(
            _candidate_contributions()[1],
            contribution_id=committed.contribution_id,
        )
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_DUPLICATE_CONTRIBUTION_ID,
    ):
        compute_portfolio_total_from_contributions(
            (committed, reserved),
            **_generic_total_kwargs(),
        )


def test_forged_generic_contribution_source_hash_rejects():
    forged = ExposureContributionV1(
        **_clone(
            _candidate_contributions()[0],
            source_input_hash="forged-source-hash",
        )
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CONTRIBUTION_SOURCE_HASH_MISMATCH,
    ):
        compute_portfolio_total_from_contributions(
            (forged,),
            **_generic_total_kwargs(),
        )


def test_duplicate_snapshot_record_ids_reject():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_DUPLICATE_SNAPSHOT_RECORD_REFERENCE,
    ):
        _snapshot(included_record_ids=("record-1", "record-1"))


def test_duplicate_snapshot_dimension_aggregate_keys_reject():
    aggregate = _snapshot().dimension_aggregates[0]
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_DUPLICATE_DIMENSION_AGGREGATE,
    ):
        _snapshot(dimension_aggregates=(aggregate, aggregate))


def test_duplicate_snapshot_ratio_keys_reject():
    ratio = _snapshot().concentration_ratios[0]
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_DUPLICATE_DIMENSION_RATIO,
    ):
        _snapshot(concentration_ratios=(ratio, ratio))


def test_free_form_snapshot_ratio_measure_rejects():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_DIMENSION_KEY,
    ):
        ExposureDimensionRatioV1(
            dimension_type=ExposureDimensionTypeV1.EVENT,
            dimension_key="event-1",
            measure="approved_stake",  # type: ignore[arg-type]
            value=Decimal("0.100000"),
        )


def test_snapshot_rejects_unverified_contribution_reference():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CONTRIBUTION_SOURCE_HASH_MISMATCH,
    ):
        _snapshot(
            contribution_refs=(
                ExposureContributionHashRefV1(
                    contribution_id="forged-contribution",
                    canonical_contribution_hash="forged-contribution-hash",
                ),
            )
        )


def test_historical_malformed_parlay_rejects():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_PARLAY_CARDINALITY,
    ):
        _zero_historical_record(parlay_id="parlay-without-legs")


def test_manual_recorded_real_without_provenance_rejects():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_MANUAL_SOURCE_INCOMPLETE,
    ):
        _record(
            record_kind=ExposureRecordKindV1.MANUAL_REAL_POSITION,
            portfolio_kind=ExposurePortfolioKindV1.RECORDED_REAL,
        )


def test_arbitrary_structured_reason_code_rejects():
    with pytest.raises(ValueError, match="ExposureReasonCodeV1"):
        ExposureReasonV1(
            code="APPROVED",  # type: ignore[arg-type]
            severity=ExposureReasonSeverityV1.WARNING,
            category=ExposureReasonCategoryV1.CALCULATION,
            message="not an exposure authority reason",
        )


def test_reason_order_is_canonical_across_severity_and_metadata():
    first = ExposureReasonV1(
        code=ExposureReasonCodeV1.EXP_AVAILABLE_BALANCE_ZERO,
        severity=ExposureReasonSeverityV1.INFO,
        category=ExposureReasonCategoryV1.BANKROLL_REFERENCE,
        message="same message",
        metadata=MappingProxyType({"variant": "a"}),
    )
    second = ExposureReasonV1(
        code=ExposureReasonCodeV1.EXP_AVAILABLE_BALANCE_ZERO,
        severity=ExposureReasonSeverityV1.WARNING,
        category=ExposureReasonCategoryV1.BANKROLL_REFERENCE,
        message="same message",
        metadata=MappingProxyType({"variant": "b"}),
    )
    assert (
        _projection(reasons=(first, second)).canonical_input_hash
        == _projection(reasons=(second, first)).canonical_input_hash
    )


def test_direct_projection_without_bound_objects_rejects():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_PROJECTION_OBJECT_MISMATCH,
    ):
        ExposureProjectionV1(**_clone(_projection()))


def test_factory_rejects_mutated_candidate_hash_object():
    candidate = _candidate()
    object.__setattr__(candidate, "canonical_input_hash", "fabricated-candidate-hash")
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_PROJECTION_OBJECT_MISMATCH,
    ):
        _factory_projection_for(candidate=candidate)


def test_factory_rejects_mutated_bankroll_hash_object():
    bankroll = _bankroll()
    snapshot = _snapshot(
        bankroll_reference_id=bankroll.bankroll_reference_id,
        bankroll_reference_hash=bankroll.canonical_input_hash,
        authoritative_bankroll_reference=bankroll,
    )
    object.__setattr__(
        bankroll,
        "canonical_input_hash",
        "fabricated-bankroll-hash",
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_PROJECTION_OBJECT_MISMATCH,
    ):
        _factory_projection_for(
            current_snapshot=snapshot,
            bankroll_reference=bankroll,
        )


def test_full_candidate_leg_dimensions_derive_deterministically():
    projection = _projection(affected_dimensions=None)
    supplied = {
        f"{item.dimension_type.value}:{item.dimension_key}"
        for item in projection.candidate_dimension_contributions
    }
    assert projection.affected_dimensions == tuple(sorted(supplied))
    assert {"parlay_leg:leg-1", "parlay_leg:leg-2"}.issubset(supplied)
    assert {"event:event-1", "event:event-2"}.issubset(supplied)
    assert all(
        item.position_state == ExposureRecordStateV1.PROPOSED_CANDIDATE
        for item in projection.candidate_dimension_contributions
    )


def test_ghost_candidate_leg_rejects_even_when_dimension_key_is_real():
    _, candidate, _ = _projection_evidence_objects()
    dimensionals = list(_full_dimension_contributions(candidate=candidate))
    dimensionals[1] = ExposureContributionV1(
        **_clone(dimensionals[1], leg_id="ghost-leg")
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CANDIDATE_PARLAY_EVIDENCE_MISMATCH,
    ):
        _projection(candidate_dimension_contributions=tuple(dimensionals))


def test_candidate_contribution_cannot_claim_position_lineage():
    projection = _projection()
    bad = ExposureContributionV1(
        **_clone(
            projection.candidate_top_level_contributions[0],
            position_id="position-1",
        )
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CONTRIBUTION_LINEAGE_FAILURE,
    ):
        _projection(
            candidate_top_level_contributions=(bad,)
            + projection.candidate_top_level_contributions[1:]
        )


def test_cross_object_schema_version_mismatch_rejects(monkeypatch):
    monkeypatch.setattr(
        "sports.execution.exposure_v1.contracts.SUPPORTED_SCHEMA_VERSIONS",
        frozenset({"v1", "v2"}),
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_PROJECTION_VERSION_MISMATCH,
    ):
        _factory_projection_for(candidate=_candidate(schema_version="v2"))


def test_cross_object_calculation_version_mismatch_rejects(monkeypatch):
    monkeypatch.setattr(
        "sports.execution.exposure_v1.contracts.SUPPORTED_CALCULATION_VERSIONS",
        frozenset({"exposure_calc_v1", "exposure_calc_v2"}),
    )
    candidate = _candidate()
    bankroll = _bankroll()
    snapshot = _snapshot(
        bankroll_reference_id=bankroll.bankroll_reference_id,
        bankroll_reference_hash=bankroll.canonical_input_hash,
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_PROJECTION_VERSION_MISMATCH,
    ):
        ExposureProjectionV1.from_authoritative_inputs(
            projection_id="projection-1",
            current_snapshot=snapshot,
            candidate=candidate,
            bankroll_reference=bankroll,
            candidate_top_level_contributions=_candidate_contributions(
                candidate_id=candidate.candidate_id,
                candidate_hash=candidate.canonical_input_hash,
                calculation_version="exposure_calc_v2",
            ),
            candidate_dimension_contributions=(),
            affected_dimensions=(),
        )


def test_all_versioned_input_contracts_reject_unsupported_schema():
    with pytest.raises(ExposureValidationError):
        _record(schema_version="v9")
    with pytest.raises(ExposureValidationError):
        _record(calculation_input_version="exposure_input_v9")
    with pytest.raises(ExposureValidationError):
        _candidate(schema_version="v9")
    with pytest.raises(ExposureValidationError):
        _bankroll(schema_version="v9")
    with pytest.raises(ExposureValidationError):
        _snapshot(schema_version="v9")
    projection = _projection()
    snapshot, candidate, bankroll = _projection_evidence_objects()
    with pytest.raises(ExposureValidationError):
        ExposureProjectionV1(
            **_clone(projection, schema_version="v9"),
            current_snapshot_evidence=snapshot,
            candidate_evidence=candidate,
            bankroll_evidence=bankroll,
        )


def test_same_record_id_with_different_actual_hash_rejects():
    first = _record(record_id="same-id", source_revision="rev-1")
    second = _record(record_id="same-id", source_revision="rev-2")
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_RECORD_HASH_CONFLICT,
    ):
        validate_record_collection(
            (first, second),
            as_of="2026-07-26T00:00:00+00:00",
        )


def test_valid_terminal_reversal_returns_no_current_head():
    target = _zero_historical_record(
        record_id="target",
        state=ExposureRecordStateV1.SETTLED,
    )
    reversal = _zero_historical_record(
        record_id="reversal",
        state=ExposureRecordStateV1.VOIDED,
        source_revision="rev-2",
        reversal_of_record_id="target",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    validated = validate_record_collection(
        (target, reversal),
        as_of="2026-07-26T00:02:00+00:00",
    )
    assert tuple(item.record_id for item in validated) == ()


def test_correction_record_kind_mismatch_rejects():
    target = _reservation_record(
        record_id="reservation-target",
        state=ExposureRecordStateV1.SUPERSEDED,
        stake_reserved=Decimal("0.00"),
        maximum_possible_loss=Decimal("0.00"),
        net_liability=Decimal("0.00"),
    )
    replacement = _record(
        record_id="position-replacement",
        source_revision="rev-2",
        correction_of_record_id="reservation-target",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_CORRECTION_DIRECTION,
    ):
        validate_record_collection(
            (target, replacement),
            as_of="2026-07-26T00:02:00+00:00",
        )


def test_duplicate_reservation_ids_without_lineage_reject():
    first = _reservation_record(
        record_id="reservation-a",
        source_record_id="reservation-source-a",
    )
    second = _reservation_record(
        record_id="reservation-b",
        source_record_id="reservation-source-b",
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_DUPLICATE_RESERVATION_ID,
    ):
        validate_record_collection(
            (first, second),
            as_of="2026-07-26T00:00:00+00:00",
        )


def test_duplicate_snapshot_record_hashes_reject():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_DUPLICATE_SNAPSHOT_RECORD_REFERENCE,
    ):
        _snapshot(
            included_record_ids=("record-1", "record-2"),
            included_record_hashes=MappingProxyType(
                {
                    "record-1": "same-record-hash",
                    "record-2": "same-record-hash",
                }
            ),
        )


def test_duplicate_snapshot_contribution_hashes_reject():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_DUPLICATE_CONTRIBUTION_ID,
    ):
        _snapshot(
            contribution_refs=(
                ExposureContributionHashRefV1("contribution-1", "same-hash"),
                ExposureContributionHashRefV1("contribution-2", "same-hash"),
            )
        )


def test_free_form_snapshot_aggregate_measure_rejects():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_DIMENSION_KEY,
    ):
        ExposureDimensionAggregateV1(
            dimension_type=ExposureDimensionTypeV1.EVENT,
            dimension_key="event-1",
            measure="approved_stake",  # type: ignore[arg-type]
            currency="USD",
            portfolio_kind=ExposurePortfolioKindV1.CASH,
            amount=Decimal("1.00"),
        )


def test_historical_legs_without_parent_rejects():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_PARLAY_CARDINALITY,
    ):
        _zero_historical_record(
            legs=(
                _leg(leg_id="leg-1"),
                _leg(leg_id="leg-2", event_id="event-2", market_id="market-2"),
            )
        )


def _manual_provenance(**overrides: object) -> ManualRecordedRealProvenanceV1:
    payload = {
        "external_execution_reference": "ticket-1",
        "sportsbook_account_reference": "book-account-1",
        "evidence_reference": "receipt-sha256-1",
        "recorded_by_actor_id": "operator-1",
        "recorded_at": "2026-07-26T00:00:00+00:00",
        "immutable_revision_id": "manual-revision-1",
    }
    payload.update(overrides)
    return ManualRecordedRealProvenanceV1(**payload)


def test_manual_recorded_real_with_typed_provenance_succeeds_and_hashes():
    first = _record(
        record_kind=ExposureRecordKindV1.MANUAL_REAL_POSITION,
        portfolio_kind=ExposurePortfolioKindV1.RECORDED_REAL,
        manual_provenance=_manual_provenance(),
    )
    second = _record(
        record_kind=ExposureRecordKindV1.MANUAL_REAL_POSITION,
        portfolio_kind=ExposurePortfolioKindV1.RECORDED_REAL,
        manual_provenance=_manual_provenance(evidence_reference="receipt-sha256-2"),
    )
    assert first.canonical_record_hash != second.canonical_record_hash


def test_reason_metadata_change_affects_projection_hash():
    first = ExposureReasonV1(
        code=ExposureReasonCodeV1.EXP_AVAILABLE_BALANCE_ZERO,
        severity=ExposureReasonSeverityV1.WARNING,
        category=ExposureReasonCategoryV1.BANKROLL_REFERENCE,
        message="same message",
        metadata=MappingProxyType({"variant": "a"}),
    )
    second = ExposureReasonV1(
        code=ExposureReasonCodeV1.EXP_AVAILABLE_BALANCE_ZERO,
        severity=ExposureReasonSeverityV1.WARNING,
        category=ExposureReasonCategoryV1.BANKROLL_REFERENCE,
        message="same message",
        metadata=MappingProxyType({"variant": "b"}),
    )
    assert (
        _projection(reasons=(first,)).canonical_input_hash
        != _projection(reasons=(second,)).canonical_input_hash
    )


def test_same_local_import_id_at_different_sportsbooks_remains_distinct():
    first = _record(
        record_id="book-a-record",
        position_id="book-a-position",
        source_repository="repo-a",
        source_record_id="shared-local-id",
        source_revision="rev-1",
        canonical_sportsbook_id="draftkings",
    )
    second = _record(
        record_id="book-b-record",
        position_id="book-b-position",
        source_repository="repo-b",
        source_record_id="shared-local-id",
        source_revision="rev-1",
        canonical_sportsbook_id="fanduel",
    )
    validated = validate_record_collection(
        (first, second),
        as_of="2026-07-26T00:00:00+00:00",
    )
    assert {item.record_id for item in validated} == {
        "book-a-record",
        "book-b-record",
    }


def test_version_fields_store_canonical_supported_value():
    assert _candidate(schema_version=" v1 ").schema_version == "v1"
    assert _bankroll(schema_version=" v1 ").schema_version == "v1"


def test_record_contribution_cannot_claim_proposed_candidate_state():
    record = _record()
    contribution = ExposureContributionV1(
        contribution_id="record-contribution",
        snapshot_or_projection_id="snapshot-1",
        portfolio_id="portfolio-1",
        account_id="account-1",
        record_id="record-1",
        position_id="position-1",
        order_intent_id="intent-1",
        reservation_id=None,
        candidate_id=None,
        parlay_id=None,
        leg_id=None,
        dimension_type=ExposureDimensionTypeV1.PORTFOLIO,
        dimension_key="portfolio-1",
        measure=ExposureMeasureV1.STAKE_COMMITTED,
        amount=Decimal("10.00"),
        currency="USD",
        portfolio_kind=ExposurePortfolioKindV1.CASH,
        position_state=ExposureRecordStateV1.PROPOSED_CANDIDATE,
        source_repository="sports.personal",
        calculation_version="exposure_calc_v1",
        as_of="2026-07-26T00:00:00+00:00",
        source_input_hash=record.canonical_record_hash,
        contribution_scope=ExposureContributionScopeV1.TOP_LEVEL_ECONOMIC,
        additive_to_portfolio_totals=True,
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_CONTRIBUTION_STATE,
    ):
        compute_portfolio_total_from_contributions(
            (contribution,),
            measure=ExposureMeasureV1.STAKE_COMMITTED,
            currency="USD",
            snapshot_or_projection_id="snapshot-1",
            portfolio_id="portfolio-1",
            account_id="account-1",
            portfolio_kind=ExposurePortfolioKindV1.CASH,
            as_of="2026-07-26T00:00:00+00:00",
            calculation_version="exposure_calc_v1",
            authoritative_records=(record,),
            authoritative_candidates=(),
        )


def test_snapshot_requires_complete_top_level_measure_set():
    record = _record()
    contribution = _snapshot_contribution(
        snapshot_id="snapshot-1",
        portfolio_id="portfolio-1",
        account_id="account-1",
        portfolio_kind=ExposurePortfolioKindV1.CASH,
        currency="USD",
        as_of="2026-07-26T00:00:00+00:00",
        calculation_version="exposure_calc_v1",
        record_id="record-1",
        record_hash=record.canonical_record_hash,
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INCOMPLETE_SNAPSHOT_MEASURE_SET,
    ):
        _snapshot(
            authoritative_records=(record,),
            included_record_hashes=MappingProxyType(
                {record.record_id: record.canonical_record_hash}
            ),
            authoritative_contributions=(contribution,),
            contribution_refs=(
                ExposureContributionHashRefV1(
                    contribution.contribution_id,
                    contribution.canonical_contribution_hash,
                ),
            ),
        )


def test_snapshot_totals_must_reconcile_to_bound_contributions():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_SNAPSHOT_TOTALS_MISMATCH,
    ):
        _snapshot(
            stake_committed=Decimal("11.00"),
            gross_payout=Decimal("20.00"),
        )


def test_nonzero_snapshot_cannot_omit_record_and_contribution_evidence():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_SNAPSHOT_TOTALS_MISMATCH,
    ):
        _snapshot(
            included_record_ids=(),
            included_record_hashes=MappingProxyType({}),
            contribution_refs=(),
            authoritative_contributions=(),
            counts_by_state=MappingProxyType({}),
            counts_by_record_kind=MappingProxyType({}),
            counts_by_portfolio_kind=MappingProxyType({}),
        )


def test_snapshot_duplicate_source_measure_rows_reject():
    record = _record()
    first = _snapshot_contribution(
        snapshot_id="snapshot-1",
        portfolio_id="portfolio-1",
        account_id="account-1",
        portfolio_kind=ExposurePortfolioKindV1.CASH,
        currency="USD",
        as_of="2026-07-26T00:00:00+00:00",
        calculation_version="exposure_calc_v1",
        record_id="record-1",
        record_hash=record.canonical_record_hash,
    )
    second = ExposureContributionV1(**_clone(first, contribution_id="contrib-2"))
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_DUPLICATE_TOP_LEVEL_MEASURE,
    ):
        _snapshot(
            authoritative_records=(record,),
            included_record_hashes=MappingProxyType(
                {record.record_id: record.canonical_record_hash}
            ),
            authoritative_contributions=(first, second),
            contribution_refs=tuple(
                ExposureContributionHashRefV1(
                    item.contribution_id,
                    item.canonical_contribution_hash,
                )
                for item in (first, second)
            ),
        )


def test_snapshot_record_hash_must_bind_to_actual_record():
    forged = _snapshot_contribution(
        snapshot_id="snapshot-1",
        portfolio_id="portfolio-1",
        account_id="account-1",
        portfolio_kind=ExposurePortfolioKindV1.CASH,
        currency="USD",
        as_of="2026-07-26T00:00:00+00:00",
        calculation_version="exposure_calc_v1",
        record_id="record-1",
        record_hash="caller-authored-record-hash",
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
    ):
        _snapshot(
            included_record_hashes=MappingProxyType(
                {"record-1": "caller-authored-record-hash"}
            ),
            authoritative_contributions=(forged,),
            contribution_refs=(
                ExposureContributionHashRefV1(
                    forged.contribution_id,
                    forged.canonical_contribution_hash,
                ),
            ),
        )


def test_snapshot_bankroll_hash_must_bind_to_actual_bankroll():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_BANKROLL_REFERENCE_INVALID,
    ):
        _snapshot(
            bankroll_reference_id="caller-bankroll",
            bankroll_reference_hash="caller-bankroll-hash",
        )


def test_snapshot_counts_must_use_closed_taxonomy_and_match_records():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_SNAPSHOT_COUNTS_MISMATCH,
    ):
        _snapshot(counts_by_state=MappingProxyType({"banana": 999}))


def test_projection_rejects_candidate_collision_with_snapshot_position():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CANDIDATE_COLLISION,
    ):
        _factory_projection_for(candidate=_candidate(candidate_id="position-1"))


def test_straight_candidate_dimensions_derive_from_full_candidate_identity():
    projection = _factory_projection_for()
    dimensions = {
        (item.dimension_type, item.dimension_key)
        for item in projection.candidate_dimension_contributions
    }
    assert {
        (ExposureDimensionTypeV1.LEAGUE, "wnba"),
        (ExposureDimensionTypeV1.EVENT, "event-1"),
        (ExposureDimensionTypeV1.MARKET, "market-1"),
        (ExposureDimensionTypeV1.MARKET_TYPE, "moneyline"),
        (ExposureDimensionTypeV1.PERIOD, "full_game"),
        (ExposureDimensionTypeV1.SPORTSBOOK, "draftkings"),
        (ExposureDimensionTypeV1.SETTLEMENT_HORIZON, "same_day"),
        (ExposureDimensionTypeV1.RECONCILIATION_VERSION, "recon-v1"),
        (ExposureDimensionTypeV1.STRATEGY, "strategy-1"),
        (ExposureDimensionTypeV1.STRATEGY_VERSION, "strategy-v1"),
        (ExposureDimensionTypeV1.MODEL_VERSION, "model-v1"),
        (ExposureDimensionTypeV1.CALIBRATION_VERSION, "cal-v1"),
    }.issubset(dimensions)


def test_parlay_dimensions_include_every_frozen_leg_identity_field():
    projection = _projection()
    dimensions = {
        (item.leg_id, item.dimension_type, item.dimension_key)
        for item in projection.candidate_dimension_contributions
    }
    assert {
        ("leg-1", ExposureDimensionTypeV1.LEAGUE, "wnba"),
        ("leg-1", ExposureDimensionTypeV1.MARKET_TYPE, "moneyline"),
        ("leg-1", ExposureDimensionTypeV1.PERIOD, "full_game"),
        ("leg-1", ExposureDimensionTypeV1.SPORTSBOOK, "draftkings"),
        ("leg-1", ExposureDimensionTypeV1.SETTLEMENT_HORIZON, "same_day"),
        (
            "leg-1",
            ExposureDimensionTypeV1.RECONCILIATION_VERSION,
            "recon-v1",
        ),
        (None, ExposureDimensionTypeV1.CORRELATION_GROUP, "group-1"),
    }.issubset(dimensions)


def test_generic_totals_reject_unsupported_measure_instead_of_false_zero():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_DIMENSION_KEY,
    ):
        compute_portfolio_total_from_contributions(
            _candidate_contributions(),
            **{
                **_generic_total_kwargs(),
                "measure": "approved_stake",
            },
        )


def test_record_position_contribution_cannot_claim_reservation_state():
    record = _record()
    contribution = ExposureContributionV1(
        **_clone(
            _snapshot_contribution(
                snapshot_id="snapshot-1",
                portfolio_id="portfolio-1",
                account_id="account-1",
                portfolio_kind=ExposurePortfolioKindV1.CASH,
                currency="USD",
                as_of="2026-07-26T00:00:00+00:00",
                calculation_version="exposure_calc_v1",
                record_id="record-1",
                record_hash=record.canonical_record_hash,
            ),
            position_state=ExposureRecordStateV1.PENDING_CONFIRMATION,
        )
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_CONTRIBUTION_STATE,
    ):
        compute_portfolio_total_from_contributions(
            (contribution,),
            measure=ExposureMeasureV1.STAKE_COMMITTED,
            currency="USD",
            snapshot_or_projection_id="snapshot-1",
            portfolio_id="portfolio-1",
            account_id="account-1",
            portfolio_kind=ExposurePortfolioKindV1.CASH,
            as_of="2026-07-26T00:00:00+00:00",
            calculation_version="exposure_calc_v1",
            authoritative_records=(record,),
            authoritative_candidates=(),
        )


def test_duplicate_manual_execution_provenance_rejects():
    first = _record(
        record_id="manual-1",
        record_kind=ExposureRecordKindV1.MANUAL_REAL_POSITION,
        portfolio_kind=ExposurePortfolioKindV1.RECORDED_REAL,
        position_id="manual-position-1",
        source_record_id="manual-source-1",
        manual_provenance=_manual_provenance(),
    )
    second = _record(
        record_id="manual-2",
        record_kind=ExposureRecordKindV1.MANUAL_REAL_POSITION,
        portfolio_kind=ExposurePortfolioKindV1.RECORDED_REAL,
        position_id="manual-position-2",
        source_record_id="manual-source-2",
        manual_provenance=_manual_provenance(),
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_DUPLICATE_MANUAL_EXECUTION,
    ):
        validate_record_collection(
            (first, second),
            as_of="2026-07-26T00:00:00+00:00",
        )


def test_lineage_cannot_reuse_same_canonical_import_revision():
    target = _zero_historical_record(record_id="old", source_revision="rev-1")
    replacement = _record(
        record_id="new",
        source_revision="rev-1",
        correction_of_record_id="old",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CROSS_REPOSITORY_DUPLICATE_IMPORT,
    ):
        validate_record_collection(
            (target, replacement),
            as_of="2026-07-26T00:02:00+00:00",
        )


def test_manual_provenance_cannot_be_recorded_after_eligibility_or_as_of():
    record = _record(
        record_kind=ExposureRecordKindV1.MANUAL_REAL_POSITION,
        portfolio_kind=ExposurePortfolioKindV1.RECORDED_REAL,
        manual_provenance=_manual_provenance(recorded_at="2099-01-01T00:00:00+00:00"),
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_MANUAL_SOURCE_INCOMPLETE,
    ):
        validate_record_collection(
            (record,),
            as_of="2026-07-26T00:00:00+00:00",
        )


def test_position_correction_must_preserve_frozen_wager_identity():
    target = _zero_historical_record(record_id="old", source_revision="rev-1")
    replacement = _record(
        record_id="new",
        source_revision="rev-2",
        correction_of_record_id="old",
        event_id="event-2",
        market_id="market-2",
        selection_id="selection-2",
        outcome_id="outcome-2",
        reconciliation_result_id="recon-2",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_CORRECTION_DIRECTION,
    ):
        validate_record_collection(
            (target, replacement),
            as_of="2026-07-26T00:02:00+00:00",
        )


def test_reservation_correction_must_preserve_order_and_wager_identity():
    target = _reservation_record(
        record_id="old",
        state=ExposureRecordStateV1.SUPERSEDED,
        stake_reserved=Decimal("0.00"),
        maximum_possible_loss=Decimal("0.00"),
        net_liability=Decimal("0.00"),
    )
    replacement = _reservation_record(
        record_id="new",
        source_revision="rev-2",
        correction_of_record_id="old",
        order_intent_id="intent-2",
        event_id="event-2",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_CORRECTION_DIRECTION,
    ):
        validate_record_collection(
            (target, replacement),
            as_of="2026-07-26T00:02:00+00:00",
        )


def test_parlay_correction_must_preserve_full_leg_identity():
    original_legs = (
        _leg(leg_id="leg-1"),
        _leg(
            leg_id="leg-2",
            event_id="event-2",
            market_id="market-2",
            selection_id="selection-2",
            outcome_id="outcome-2",
        ),
    )
    changed_legs = (
        _leg(leg_id="leg-1", event_id="ghost-event"),
        original_legs[1],
    )
    target = _zero_historical_record(
        record_id="old",
        source_revision="rev-1",
        parlay_id="parlay-1",
        legs=original_legs,
    )
    replacement = _record(
        record_id="new",
        source_revision="rev-2",
        correction_of_record_id="old",
        parlay_id="parlay-1",
        legs=changed_legs,
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_CORRECTION_DIRECTION,
    ):
        validate_record_collection(
            (target, replacement),
            as_of="2026-07-26T00:02:00+00:00",
        )


def test_reason_metadata_rejects_colliding_non_string_keys():
    with pytest.raises(ValueError, match="metadata keys"):
        ExposureReasonV1(
            code=ExposureReasonCodeV1.EXP_AVAILABLE_BALANCE_ZERO,
            severity=ExposureReasonSeverityV1.WARNING,
            category=ExposureReasonCategoryV1.BANKROLL_REFERENCE,
            message="invalid metadata",
            metadata=MappingProxyType({1: "integer", "1": "string"}),
        )


def test_reason_metadata_rejects_unsupported_process_specific_values():
    with pytest.raises(ValueError, match="metadata values"):
        ExposureReasonV1(
            code=ExposureReasonCodeV1.EXP_AVAILABLE_BALANCE_ZERO,
            severity=ExposureReasonSeverityV1.WARNING,
            category=ExposureReasonCategoryV1.BANKROLL_REFERENCE,
            message="invalid metadata",
            metadata=MappingProxyType({"opaque": object()}),
        )


def test_generic_totals_bind_candidate_economics_to_actual_object():
    candidate = _candidate()
    forged = ExposureContributionV1(
        **_clone(
            _candidate_contributions(candidate_hash=candidate.canonical_input_hash)[0],
            amount=Decimal("999.00"),
        )
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CANDIDATE_ECONOMICS_MISMATCH,
    ):
        compute_portfolio_total_from_contributions(
            (forged,),
            measure=ExposureMeasureV1.STAKE_COMMITTED,
            currency="USD",
            snapshot_or_projection_id="projection-1",
            portfolio_id="portfolio-1",
            account_id="account-1",
            portfolio_kind=ExposurePortfolioKindV1.CASH,
            as_of="2026-07-26T00:00:00+00:00",
            calculation_version="exposure_calc_v1",
            authoritative_records=(),
            authoritative_candidates=(candidate,),
        )


def test_generic_totals_bind_reservation_economics_to_actual_record():
    record = _reservation_record()
    forged = _snapshot_contribution(
        snapshot_id="snapshot-1",
        portfolio_id=record.portfolio_id,
        account_id=record.account_id,
        portfolio_kind=record.portfolio_kind,
        currency=record.currency,
        as_of=record.as_of_eligible_at,
        calculation_version="exposure_calc_v1",
        record_id=record.record_id,
        record_hash=record.canonical_record_hash,
        position_id=record.position_id,
        order_intent_id=record.order_intent_id,
        reservation_id=record.reservation_id,
        position_state=record.state,
        source_repository=record.source_repository,
        measure=ExposureMeasureV1.POTENTIAL_PROFIT,
        amount=Decimal("50.00"),
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_SNAPSHOT_TOTALS_MISMATCH,
    ):
        compute_portfolio_total_from_contributions(
            (forged,),
            measure=ExposureMeasureV1.POTENTIAL_PROFIT,
            currency="USD",
            snapshot_or_projection_id="snapshot-1",
            portfolio_id="portfolio-1",
            account_id="account-1",
            portfolio_kind=ExposurePortfolioKindV1.CASH,
            as_of="2026-07-26T00:00:00+00:00",
            calculation_version="exposure_calc_v1",
            authoritative_records=(record,),
            authoritative_candidates=(),
        )


def test_generic_totals_are_independent_of_global_decimal_precision():
    first_candidate = _candidate(
        candidate_id="candidate-1",
        allocation_amount=Decimal("99.00"),
        maximum_possible_loss=Decimal("99.00"),
        potential_profit=Decimal("1.00"),
        gross_payout=Decimal("100.00"),
        net_liability=Decimal("99.00"),
    )
    second_candidate = _candidate(
        candidate_id="candidate-2",
        allocation_amount=Decimal("99.00"),
        maximum_possible_loss=Decimal("99.00"),
        potential_profit=Decimal("1.00"),
        gross_payout=Decimal("100.00"),
        net_liability=Decimal("99.00"),
    )
    contributions = (
        ExposureContributionV1(
            **_clone(
                _candidate_contributions(
                    candidate_id=first_candidate.candidate_id,
                    candidate_hash=first_candidate.canonical_input_hash,
                )[0],
                amount=Decimal("99.00"),
            )
        ),
        ExposureContributionV1(
            **_clone(
                _candidate_contributions(
                    candidate_id=second_candidate.candidate_id,
                    candidate_hash=second_candidate.canonical_input_hash,
                )[0],
                contribution_id="candidate-2-committed",
                amount=Decimal("99.00"),
            )
        ),
    )
    original_precision = getcontext().prec
    try:
        getcontext().prec = 2
        total = compute_portfolio_total_from_contributions(
            contributions,
            measure=ExposureMeasureV1.STAKE_COMMITTED,
            currency="USD",
            snapshot_or_projection_id="projection-1",
            portfolio_id="portfolio-1",
            account_id="account-1",
            portfolio_kind=ExposurePortfolioKindV1.CASH,
            as_of="2026-07-26T00:00:00+00:00",
            calculation_version="exposure_calc_v1",
            authoritative_records=(),
            authoritative_candidates=(first_candidate, second_candidate),
        )
    finally:
        getcontext().prec = original_precision
    assert total == Decimal("198.00")


def test_snapshot_ghost_dimension_and_oversized_aggregate_reject():
    ghost = ExposureDimensionAggregateV1(
        dimension_type=ExposureDimensionTypeV1.EVENT,
        dimension_key="ghost-event",
        measure=ExposureMeasureV1.MAXIMUM_LOSS,
        currency="USD",
        portfolio_kind=ExposurePortfolioKindV1.CASH,
        amount=Decimal("999.00"),
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_SNAPSHOT_DIMENSION_MISMATCH,
    ):
        _snapshot(dimension_aggregates=(ghost,))


def test_snapshot_ratios_correlation_and_bankroll_percentages_are_derived():
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_SNAPSHOT_DIMENSION_MISMATCH,
    ):
        _snapshot(
            concentration_ratios=(),
            correlated_maximum_loss_by_group=MappingProxyType(
                {"ghost-group": Decimal("777.00")}
            ),
            exposure_pct_bankroll=Decimal("0.999999"),
            exposure_pct_available=Decimal("0.999999"),
        )


def test_snapshot_counts_reject_boolean_values():
    with pytest.raises(ValueError, match="nonnegative int"):
        _snapshot(counts_by_state=MappingProxyType({"open": True}))


def test_reason_decimal_and_text_metadata_hash_differ():
    decimal_reason = ExposureReasonV1(
        code=ExposureReasonCodeV1.EXP_AVAILABLE_BALANCE_ZERO,
        severity=ExposureReasonSeverityV1.WARNING,
        category=ExposureReasonCategoryV1.BANKROLL_REFERENCE,
        message="typed metadata",
        metadata=MappingProxyType({"value": Decimal("1.00")}),
    )
    text_reason = ExposureReasonV1(
        code=ExposureReasonCodeV1.EXP_AVAILABLE_BALANCE_ZERO,
        severity=ExposureReasonSeverityV1.WARNING,
        category=ExposureReasonCategoryV1.BANKROLL_REFERENCE,
        message="typed metadata",
        metadata=MappingProxyType({"value": "1.00"}),
    )
    assert decimal_reason.canonical_sort_key() != text_reason.canonical_sort_key()


def test_same_manual_ticket_with_different_revision_cannot_double_count():
    first = _record(
        record_id="manual-a",
        record_kind=ExposureRecordKindV1.MANUAL_REAL_POSITION,
        portfolio_kind=ExposurePortfolioKindV1.RECORDED_REAL,
        position_id="manual-position-a",
        source_record_id="manual-source-a",
        manual_provenance=_manual_provenance(immutable_revision_id="manual-rev-a"),
    )
    second = _record(
        record_id="manual-b",
        record_kind=ExposureRecordKindV1.MANUAL_REAL_POSITION,
        portfolio_kind=ExposurePortfolioKindV1.RECORDED_REAL,
        position_id="manual-position-b",
        source_record_id="manual-source-b",
        manual_provenance=_manual_provenance(immutable_revision_id="manual-rev-b"),
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_DUPLICATE_MANUAL_EXECUTION,
    ):
        validate_record_collection(
            (first, second),
            as_of="2026-07-26T00:00:00+00:00",
        )


def test_same_manual_ticket_new_revision_is_valid_inside_one_lineage():
    first = _record(
        record_id="manual-rev-1",
        record_kind=ExposureRecordKindV1.MANUAL_REAL_POSITION,
        portfolio_kind=ExposurePortfolioKindV1.RECORDED_REAL,
        manual_provenance=_manual_provenance(immutable_revision_id="manual-rev-1"),
    )
    second = _record(
        record_id="manual-rev-2",
        record_kind=ExposureRecordKindV1.MANUAL_REAL_POSITION,
        portfolio_kind=ExposurePortfolioKindV1.RECORDED_REAL,
        source_revision="rev-2",
        correction_of_record_id="manual-rev-1",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
        manual_provenance=_manual_provenance(
            immutable_revision_id="manual-rev-2",
            recorded_at="2026-07-26T00:01:00+00:00",
        ),
    )
    validated = validate_record_collection(
        (first, second),
        as_of="2026-07-26T00:02:00+00:00",
    )
    assert tuple(item.record_id for item in validated) == ("manual-rev-2",)


def test_append_only_open_record_can_be_corrected_without_rewriting_target():
    target = _record(record_id="open-target", source_revision="rev-1")
    replacement = _record(
        record_id="open-replacement",
        source_revision="rev-2",
        correction_of_record_id="open-target",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    validated = validate_record_collection(
        (target, replacement),
        as_of="2026-07-26T00:02:00+00:00",
    )
    assert tuple(item.record_id for item in validated) == ("open-replacement",)


def test_settled_record_can_be_voided_by_append_only_reversal():
    target = _zero_historical_record(
        record_id="settled-target",
        state=ExposureRecordStateV1.SETTLED,
        source_revision="rev-1",
    )
    reversal = _zero_historical_record(
        record_id="void-reversal",
        state=ExposureRecordStateV1.VOIDED,
        source_revision="rev-2",
        reversal_of_record_id="settled-target",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    validated = validate_record_collection(
        (target, reversal),
        as_of="2026-07-26T00:02:00+00:00",
    )
    assert tuple(item.record_id for item in validated) == ()


def test_reversal_child_must_express_voided_state():
    target = _zero_historical_record(
        record_id="settled-target",
        state=ExposureRecordStateV1.SETTLED,
    )
    invalid = _zero_historical_record(
        record_id="settled-child",
        state=ExposureRecordStateV1.SETTLED,
        source_revision="rev-2",
        reversal_of_record_id="settled-target",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_CORRECTION_DIRECTION,
    ):
        validate_record_collection(
            (target, invalid),
            as_of="2026-07-26T00:02:00+00:00",
        )


def test_same_external_revision_cannot_escape_dedup_by_mutating_canonical_ids():
    first = _record(
        record_id="import-a",
        position_id="position-a",
        source_repository="repo-a",
        source_record_id="external-ticket-1",
        source_revision="external-rev-1",
    )
    second = _record(
        record_id="import-b",
        position_id="position-b",
        source_repository="repo-b",
        source_record_id="external-ticket-1",
        source_revision="external-rev-1",
        event_id="mutated-event",
        market_id="mutated-market",
        selection_id="mutated-selection",
        outcome_id="mutated-outcome",
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CROSS_REPOSITORY_DUPLICATE_IMPORT,
    ):
        validate_record_collection(
            (first, second),
            as_of="2026-07-26T00:00:00+00:00",
        )


def test_generic_top_level_contribution_requires_authoritative_portfolio_key():
    candidate = _candidate()
    contribution = ExposureContributionV1(
        **_clone(
            _candidate_contributions(candidate_hash=candidate.canonical_input_hash)[0],
            dimension_key="other-portfolio",
        )
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_CONTRIBUTION_SCOPE,
    ):
        compute_portfolio_total_from_contributions(
            (contribution,),
            measure=ExposureMeasureV1.STAKE_COMMITTED,
            currency="USD",
            snapshot_or_projection_id="projection-1",
            portfolio_id="portfolio-1",
            account_id="account-1",
            portfolio_kind=ExposurePortfolioKindV1.CASH,
            as_of="2026-07-26T00:00:00+00:00",
            calculation_version="exposure_calc_v1",
            authoritative_records=(),
            authoritative_candidates=(candidate,),
        )


def test_generic_summation_rejects_cross_object_schema_mismatch(monkeypatch):
    monkeypatch.setattr(
        "sports.execution.exposure_v1.contracts.SUPPORTED_SCHEMA_VERSIONS",
        frozenset({"v1", "v2"}),
    )
    candidate = _candidate(schema_version="v2")
    contribution = _candidate_contributions(
        candidate_hash=candidate.canonical_input_hash
    )[0]
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_PROJECTION_VERSION_MISMATCH,
    ):
        compute_portfolio_total_from_contributions(
            (contribution,),
            measure=ExposureMeasureV1.STAKE_COMMITTED,
            currency="USD",
            snapshot_or_projection_id="projection-1",
            portfolio_id="portfolio-1",
            account_id="account-1",
            portfolio_kind=ExposurePortfolioKindV1.CASH,
            as_of="2026-07-26T00:00:00+00:00",
            calculation_version="exposure_calc_v1",
            authoritative_records=(),
            authoritative_candidates=(candidate,),
        )


def test_terminal_lineage_produces_zero_current_snapshot_without_losing_history():
    target = _zero_historical_record(
        record_id="settled-target",
        state=ExposureRecordStateV1.SETTLED,
        source_revision="rev-1",
    )
    reversal = _zero_historical_record(
        record_id="void-head",
        state=ExposureRecordStateV1.VOIDED,
        source_revision="rev-2",
        reversal_of_record_id="settled-target",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    snapshot = _snapshot(
        as_of="2026-07-26T00:02:00+00:00",
        included_record_ids=(),
        included_record_hashes=MappingProxyType({}),
        authoritative_records=(target, reversal),
        authoritative_contributions=(),
        contribution_refs=(),
        stake_committed=Decimal("0.00"),
        stake_reserved=Decimal("0.00"),
        maximum_possible_loss=Decimal("0.00"),
        potential_profit=Decimal("0.00"),
        gross_payout=Decimal("0.00"),
        net_liability=Decimal("0.00"),
        counts_by_state=MappingProxyType({}),
        counts_by_record_kind=MappingProxyType({}),
        counts_by_portfolio_kind=MappingProxyType({}),
    )
    assert snapshot.included_record_ids == ()


@pytest.mark.parametrize(
    "status",
    (
        ExposureAvailabilityStatusV1.UNAVAILABLE,
        ExposureAvailabilityStatusV1.ERROR,
    ),
)
def test_nonavailable_snapshot_cannot_be_promoted_to_available_projection(status):
    bankroll = _bankroll()
    snapshot = _snapshot(
        bankroll_reference_id=bankroll.bankroll_reference_id,
        bankroll_reference_hash=bankroll.canonical_input_hash,
        authoritative_bankroll_reference=bankroll,
        status=status,
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_STATE_CONFLICT,
    ):
        _factory_projection_for(
            current_snapshot=snapshot,
            bankroll_reference=bankroll,
        )


def test_zero_bankroll_denominators_derive_unavailable_snapshot_reasons():
    bankroll = _bankroll(
        available_balance=Decimal("0.00"),
        total_bankroll=Decimal("0.00"),
        reserved_balance=Decimal("0.00"),
    )
    snapshot = _snapshot(
        bankroll_reference_id=bankroll.bankroll_reference_id,
        bankroll_reference_hash=bankroll.canonical_input_hash,
        authoritative_bankroll_reference=bankroll,
    )
    assert snapshot.status == ExposureAvailabilityStatusV1.UNAVAILABLE
    assert {
        ExposureReasonCodeV1.EXP_BANKROLL_DENOMINATOR_ZERO,
        ExposureReasonCodeV1.EXP_AVAILABLE_BALANCE_ZERO,
    }.issubset({reason.code for reason in snapshot.reasons})


def test_projection_revalidates_snapshot_evidence_after_payload_and_hash_mutation():
    snapshot, candidate, bankroll = _projection_evidence_objects()
    object.__setattr__(snapshot, "stake_committed", Decimal("99.00"))
    object.__setattr__(snapshot, "gross_payout", Decimal("108.00"))
    object.__setattr__(
        snapshot,
        "canonical_input_hash",
        stable_hash(snapshot.hash_payload()),
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_SNAPSHOT_TOTALS_MISMATCH,
    ):
        _factory_projection_for(
            candidate=candidate,
            current_snapshot=snapshot,
            bankroll_reference=bankroll,
        )


def test_projection_revalidates_snapshot_collision_evidence_after_mutation():
    snapshot, _, bankroll = _projection_evidence_objects()
    object.__setattr__(snapshot, "authoritative_record_identities", ())
    object.__setattr__(
        snapshot,
        "canonical_input_hash",
        stable_hash(snapshot.hash_payload()),
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CANDIDATE_COLLISION,
    ):
        _factory_projection_for(
            candidate=_candidate(candidate_id="position-1"),
            current_snapshot=snapshot,
            bankroll_reference=bankroll,
        )


def test_generic_summation_rejects_candidate_collision_with_current_record():
    record = _record()
    candidate = _candidate(candidate_id=record.position_id)
    contribution = _candidate_contributions(
        candidate_id=candidate.candidate_id,
        candidate_hash=candidate.canonical_input_hash,
    )[0]
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CANDIDATE_COLLISION,
    ):
        compute_portfolio_total_from_contributions(
            (contribution,),
            measure=ExposureMeasureV1.STAKE_COMMITTED,
            currency="USD",
            snapshot_or_projection_id="projection-1",
            portfolio_id="portfolio-1",
            account_id="account-1",
            portfolio_kind=ExposurePortfolioKindV1.CASH,
            as_of="2026-07-26T00:00:00+00:00",
            calculation_version="exposure_calc_v1",
            authoritative_records=(record,),
            authoritative_candidates=(candidate,),
        )


def test_generic_candidate_contribution_requires_frozen_source_repository():
    candidate = _candidate()
    contribution = ExposureContributionV1(
        **_clone(
            _candidate_contributions(candidate_hash=candidate.canonical_input_hash)[0],
            source_repository="caller.repo",
        )
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CONTRIBUTION_SOURCE_HASH_MISMATCH,
    ):
        compute_portfolio_total_from_contributions(
            (contribution,),
            measure=ExposureMeasureV1.STAKE_COMMITTED,
            currency="USD",
            snapshot_or_projection_id="projection-1",
            portfolio_id="portfolio-1",
            account_id="account-1",
            portfolio_kind=ExposurePortfolioKindV1.CASH,
            as_of="2026-07-26T00:00:00+00:00",
            calculation_version="exposure_calc_v1",
            authoritative_records=(),
            authoritative_candidates=(candidate,),
        )


def test_terminal_lineage_is_hash_bound_and_differs_from_empty_history():
    bankroll = _bankroll(as_of="2026-07-26T00:02:00+00:00")
    target = _zero_historical_record(
        record_id="settled-target",
        state=ExposureRecordStateV1.SETTLED,
        source_revision="rev-1",
    )
    reversal = _zero_historical_record(
        record_id="void-head",
        state=ExposureRecordStateV1.VOIDED,
        source_revision="rev-2",
        reversal_of_record_id="settled-target",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    common = {
        "as_of": "2026-07-26T00:02:00+00:00",
        "bankroll_reference_id": bankroll.bankroll_reference_id,
        "bankroll_reference_hash": bankroll.canonical_input_hash,
        "authoritative_bankroll_reference": bankroll,
        "included_record_ids": (),
        "included_record_hashes": MappingProxyType({}),
        "authoritative_contributions": (),
        "contribution_refs": (),
        "stake_committed": Decimal("0.00"),
        "stake_reserved": Decimal("0.00"),
        "maximum_possible_loss": Decimal("0.00"),
        "potential_profit": Decimal("0.00"),
        "gross_payout": Decimal("0.00"),
        "net_liability": Decimal("0.00"),
        "counts_by_state": MappingProxyType({}),
        "counts_by_record_kind": MappingProxyType({}),
        "counts_by_portfolio_kind": MappingProxyType({}),
    }
    terminal = _snapshot(
        **common,
        authoritative_records=(target, reversal),
    )
    empty = _snapshot(
        **common,
        authoritative_records=(),
    )
    assert terminal.canonical_input_hash != empty.canonical_input_hash
    assert dict(terminal.authoritative_lineage_record_hashes) == {
        target.record_id: target.canonical_record_hash,
        reversal.record_id: reversal.canonical_record_hash,
    }


def test_projection_rejects_erased_terminal_lineage_evidence():
    bankroll = _bankroll(as_of="2026-07-26T00:02:00+00:00")
    target = _zero_historical_record(
        record_id="settled-target",
        state=ExposureRecordStateV1.SETTLED,
        source_revision="rev-1",
    )
    reversal = _zero_historical_record(
        record_id="void-head",
        state=ExposureRecordStateV1.VOIDED,
        source_revision="rev-2",
        reversal_of_record_id="settled-target",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    snapshot = _snapshot(
        as_of="2026-07-26T00:02:00+00:00",
        bankroll_reference_id=bankroll.bankroll_reference_id,
        bankroll_reference_hash=bankroll.canonical_input_hash,
        authoritative_bankroll_reference=bankroll,
        included_record_ids=(),
        included_record_hashes=MappingProxyType({}),
        authoritative_records=(target, reversal),
        authoritative_contributions=(),
        contribution_refs=(),
        stake_committed=Decimal("0.00"),
        stake_reserved=Decimal("0.00"),
        maximum_possible_loss=Decimal("0.00"),
        potential_profit=Decimal("0.00"),
        gross_payout=Decimal("0.00"),
        net_liability=Decimal("0.00"),
        counts_by_state=MappingProxyType({}),
        counts_by_record_kind=MappingProxyType({}),
        counts_by_portfolio_kind=MappingProxyType({}),
    )
    object.__setattr__(snapshot, "_authoritative_records_evidence", ())
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
    ):
        _factory_projection_for(
            current_snapshot=snapshot,
            candidate=_candidate(as_of="2026-07-26T00:02:00+00:00"),
            bankroll_reference=bankroll,
        )


@pytest.mark.parametrize(
    "state",
    (
        ExposureRecordStateV1.CORRECTED,
        ExposureRecordStateV1.SUPERSEDED,
    ),
)
def test_replacement_only_historical_head_requires_child(state):
    orphan = _zero_historical_record(state=state)
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_CORRECTION_DIRECTION,
    ):
        validate_record_collection(
            (orphan,),
            as_of="2026-07-26T00:00:00+00:00",
        )


def test_projection_rejects_synchronized_snapshot_status_reason_promotion():
    bankroll = _bankroll()
    warning = ExposureReasonV1(
        code=ExposureReasonCodeV1.EXP_SETTLEMENT_HORIZON_UNKNOWN,
        severity=ExposureReasonSeverityV1.WARNING,
        category=ExposureReasonCategoryV1.CALCULATION,
        message="snapshot authority is unavailable",
    )
    snapshot = _snapshot(
        bankroll_reference_id=bankroll.bankroll_reference_id,
        bankroll_reference_hash=bankroll.canonical_input_hash,
        authoritative_bankroll_reference=bankroll,
        status=ExposureAvailabilityStatusV1.UNAVAILABLE,
        reasons=(warning,),
    )
    object.__setattr__(
        snapshot,
        "status",
        ExposureAvailabilityStatusV1.AVAILABLE,
    )
    object.__setattr__(snapshot, "reasons", ())
    object.__setattr__(
        snapshot,
        "canonical_input_hash",
        stable_hash(snapshot.hash_payload()),
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_PROJECTION_OBJECT_MISMATCH,
    ):
        _factory_projection_for(
            current_snapshot=snapshot,
            bankroll_reference=bankroll,
        )


def test_generic_summation_rejects_mixed_bound_schema_generations(monkeypatch):
    monkeypatch.setattr(
        "sports.execution.exposure_v1.contracts.SUPPORTED_SCHEMA_VERSIONS",
        frozenset({"v1", "v2"}),
    )
    record = _record(schema_version="v1")
    record_contribution = next(
        contribution
        for contribution in _snapshot_contributions(
            record=record,
            snapshot_id="projection-1",
            portfolio_id="portfolio-1",
            account_id="account-1",
            portfolio_kind=ExposurePortfolioKindV1.CASH,
            currency="USD",
            as_of="2026-07-26T00:00:00+00:00",
            calculation_version="exposure_calc_v1",
        )
        if contribution.measure == ExposureMeasureV1.STAKE_COMMITTED
        and contribution.contribution_scope
        == ExposureContributionScopeV1.TOP_LEVEL_ECONOMIC
    )
    candidate = _candidate(schema_version="v2")
    candidate_contribution = ExposureContributionV1(
        **_clone(
            _candidate_contributions(candidate_hash=candidate.canonical_input_hash)[0],
            schema_version="v2",
        )
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_PROJECTION_VERSION_MISMATCH,
    ):
        compute_portfolio_total_from_contributions(
            (record_contribution, candidate_contribution),
            measure=ExposureMeasureV1.STAKE_COMMITTED,
            currency="USD",
            snapshot_or_projection_id="projection-1",
            portfolio_id="portfolio-1",
            account_id="account-1",
            portfolio_kind=ExposurePortfolioKindV1.CASH,
            as_of="2026-07-26T00:00:00+00:00",
            calculation_version="exposure_calc_v1",
            authoritative_records=(record,),
            authoritative_candidates=(candidate,),
        )


@pytest.mark.parametrize(
    "sportsbook_alias",
    ("DraftKings", "draft kings", "draft-kings"),
)
def test_import_sportsbook_alias_cannot_split_canonical_identity(
    sportsbook_alias,
):
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_MISSING_CANONICAL_IDENTITY,
    ):
        _record(canonical_sportsbook_id=sportsbook_alias)


def test_manual_ticket_sportsbook_alias_cannot_split_canonical_identity():
    provenance = _manual_provenance()
    canonical = _record(
        record_id="manual-canonical",
        record_kind=ExposureRecordKindV1.MANUAL_REAL_POSITION,
        portfolio_kind=ExposurePortfolioKindV1.RECORDED_REAL,
        canonical_sportsbook_id="draftkings",
        manual_provenance=provenance,
    )
    assert canonical.canonical_sportsbook_id == "draftkings"
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_MISSING_CANONICAL_IDENTITY,
    ):
        _record(
            record_id="manual-alias",
            position_id="manual-alias-position",
            record_kind=ExposureRecordKindV1.MANUAL_REAL_POSITION,
            portfolio_kind=ExposurePortfolioKindV1.RECORDED_REAL,
            canonical_sportsbook_id="DraftKings",
            manual_provenance=provenance,
        )


def test_projection_rejects_coordinated_terminal_lineage_erasure():
    bankroll = _bankroll(as_of="2026-07-26T00:02:00+00:00")
    target = _zero_historical_record(
        record_id="settled-target",
        state=ExposureRecordStateV1.SETTLED,
        source_revision="rev-1",
    )
    reversal = _zero_historical_record(
        record_id="void-head",
        state=ExposureRecordStateV1.VOIDED,
        source_revision="rev-2",
        reversal_of_record_id="settled-target",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    snapshot = _snapshot(
        as_of="2026-07-26T00:02:00+00:00",
        bankroll_reference_id=bankroll.bankroll_reference_id,
        bankroll_reference_hash=bankroll.canonical_input_hash,
        authoritative_bankroll_reference=bankroll,
        included_record_ids=(),
        included_record_hashes=MappingProxyType({}),
        authoritative_records=(target, reversal),
        authoritative_contributions=(),
        contribution_refs=(),
        stake_committed=Decimal("0.00"),
        stake_reserved=Decimal("0.00"),
        maximum_possible_loss=Decimal("0.00"),
        potential_profit=Decimal("0.00"),
        gross_payout=Decimal("0.00"),
        net_liability=Decimal("0.00"),
        counts_by_state=MappingProxyType({}),
        counts_by_record_kind=MappingProxyType({}),
        counts_by_portfolio_kind=MappingProxyType({}),
    )
    object.__setattr__(snapshot, "_authoritative_records_evidence", ())
    object.__setattr__(
        snapshot,
        "authoritative_lineage_record_hashes",
        MappingProxyType({}),
    )
    object.__setattr__(
        snapshot,
        "canonical_input_hash",
        stable_hash(snapshot.hash_payload()),
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
    ):
        _factory_projection_for(
            current_snapshot=snapshot,
            candidate=_candidate(as_of="2026-07-26T00:02:00+00:00"),
            bankroll_reference=bankroll,
        )


def test_projection_rejects_candidate_collision_with_terminal_position():
    bankroll = _bankroll(as_of="2026-07-26T00:02:00+00:00")
    target = _zero_historical_record(
        record_id="settled-target",
        state=ExposureRecordStateV1.SETTLED,
        source_revision="rev-1",
    )
    reversal = _zero_historical_record(
        record_id="void-head",
        state=ExposureRecordStateV1.VOIDED,
        source_revision="rev-2",
        reversal_of_record_id="settled-target",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    snapshot = _snapshot(
        as_of="2026-07-26T00:02:00+00:00",
        bankroll_reference_id=bankroll.bankroll_reference_id,
        bankroll_reference_hash=bankroll.canonical_input_hash,
        authoritative_bankroll_reference=bankroll,
        included_record_ids=(),
        included_record_hashes=MappingProxyType({}),
        authoritative_records=(target, reversal),
        authoritative_contributions=(),
        contribution_refs=(),
        stake_committed=Decimal("0.00"),
        stake_reserved=Decimal("0.00"),
        maximum_possible_loss=Decimal("0.00"),
        potential_profit=Decimal("0.00"),
        gross_payout=Decimal("0.00"),
        net_liability=Decimal("0.00"),
        counts_by_state=MappingProxyType({}),
        counts_by_record_kind=MappingProxyType({}),
        counts_by_portfolio_kind=MappingProxyType({}),
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CANDIDATE_COLLISION,
    ):
        _factory_projection_for(
            current_snapshot=snapshot,
            candidate=_candidate(
                candidate_id=target.position_id,
                as_of="2026-07-26T00:02:00+00:00",
            ),
            bankroll_reference=bankroll,
        )


def test_generic_summation_rejects_terminal_reservation_collision():
    reservation = _reservation_record(
        state=ExposureRecordStateV1.CANCELED,
        stake_reserved=Decimal("0.00"),
        maximum_possible_loss=Decimal("0.00"),
        net_liability=Decimal("0.00"),
    )
    candidate = _candidate(candidate_id=reservation.reservation_id)
    contribution = _candidate_contributions(
        candidate_id=candidate.candidate_id,
        candidate_hash=candidate.canonical_input_hash,
    )[0]
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CANDIDATE_COLLISION,
    ):
        compute_portfolio_total_from_contributions(
            (contribution,),
            measure=ExposureMeasureV1.STAKE_COMMITTED,
            currency="USD",
            snapshot_or_projection_id="projection-1",
            portfolio_id="portfolio-1",
            account_id="account-1",
            portfolio_kind=ExposurePortfolioKindV1.CASH,
            as_of="2026-07-26T00:00:00+00:00",
            calculation_version="exposure_calc_v1",
            authoritative_records=(reservation,),
            authoritative_candidates=(candidate,),
        )


def test_foreign_terminal_lineage_cannot_underwrite_local_snapshot():
    target = _zero_historical_record(
        record_id="foreign-settled",
        state=ExposureRecordStateV1.SETTLED,
        source_revision="rev-1",
        portfolio_id="foreign-portfolio",
        account_id="foreign-account",
    )
    reversal = _zero_historical_record(
        record_id="foreign-void",
        state=ExposureRecordStateV1.VOIDED,
        source_revision="rev-2",
        portfolio_id="foreign-portfolio",
        account_id="foreign-account",
        reversal_of_record_id="foreign-settled",
        effective_at="2026-07-26T00:01:00+00:00",
        as_of_eligible_at="2026-07-26T00:01:00+00:00",
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_PROJECTION_SOURCE_MISMATCH,
    ):
        _snapshot(
            as_of="2026-07-26T00:02:00+00:00",
            included_record_ids=(),
            included_record_hashes=MappingProxyType({}),
            authoritative_records=(target, reversal),
            authoritative_contributions=(),
            contribution_refs=(),
            stake_committed=Decimal("0.00"),
            stake_reserved=Decimal("0.00"),
            maximum_possible_loss=Decimal("0.00"),
            potential_profit=Decimal("0.00"),
            gross_payout=Decimal("0.00"),
            net_liability=Decimal("0.00"),
            counts_by_state=MappingProxyType({}),
            counts_by_record_kind=MappingProxyType({}),
            counts_by_portfolio_kind=MappingProxyType({}),
        )


def test_collection_rejects_coordinated_import_sportsbook_alias_mutation():
    canonical = _record(
        record_id="canonical-import",
        position_id="canonical-position",
        source_repository="repo-a",
        canonical_sportsbook_id="draftkings",
    )
    mutated = _record(
        record_id="mutated-import",
        position_id="mutated-position",
        source_repository="repo-b",
        canonical_sportsbook_id="fanduel",
    )
    object.__setattr__(
        mutated,
        "canonical_sportsbook_id",
        "DraftKings",
    )
    object.__setattr__(
        mutated,
        "canonical_record_hash",
        stable_hash(mutated.hash_payload()),
    )
    object.__setattr__(
        mutated,
        "_authoritative_origin_hash",
        _seal_authoritative_hash(mutated.canonical_record_hash),
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
    ):
        validate_record_collection(
            (canonical, mutated),
            as_of="2026-07-26T00:00:00+00:00",
        )


def test_collection_rejects_coordinated_manual_sportsbook_alias_mutation():
    provenance = _manual_provenance()
    canonical = _record(
        record_id="manual-canonical",
        source_record_id="manual-canonical-source",
        position_id="manual-canonical-position",
        record_kind=ExposureRecordKindV1.MANUAL_REAL_POSITION,
        portfolio_kind=ExposurePortfolioKindV1.RECORDED_REAL,
        canonical_sportsbook_id="draftkings",
        manual_provenance=provenance,
    )
    mutated = _record(
        record_id="manual-mutated",
        source_record_id="manual-mutated-source",
        source_revision="rev-2",
        position_id="manual-mutated-position",
        record_kind=ExposureRecordKindV1.MANUAL_REAL_POSITION,
        portfolio_kind=ExposurePortfolioKindV1.RECORDED_REAL,
        canonical_sportsbook_id="fanduel",
        manual_provenance=provenance,
    )
    object.__setattr__(
        mutated,
        "canonical_sportsbook_id",
        "DraftKings",
    )
    object.__setattr__(
        mutated,
        "canonical_record_hash",
        stable_hash(mutated.hash_payload()),
    )
    object.__setattr__(
        mutated,
        "_authoritative_origin_hash",
        _seal_authoritative_hash(mutated.canonical_record_hash),
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_FORGED_CANONICAL_HASH,
    ):
        validate_record_collection(
            (canonical, mutated),
            as_of="2026-07-26T00:00:00+00:00",
        )


def test_projection_rejects_coordinated_bankroll_evidence_rebinding():
    original = _bankroll()
    snapshot = _snapshot(
        bankroll_reference_id=original.bankroll_reference_id,
        bankroll_reference_hash=original.canonical_input_hash,
        authoritative_bankroll_reference=original,
    )
    replacement = _bankroll(
        ledger_version="ledger-v2",
        available_balance=Decimal("900.00"),
        total_bankroll=Decimal("1000.00"),
    )
    object.__setattr__(
        snapshot,
        "_authoritative_bankroll_evidence",
        replacement,
    )
    object.__setattr__(
        snapshot,
        "bankroll_reference_hash",
        replacement.canonical_input_hash,
    )
    object.__setattr__(
        snapshot,
        "exposure_pct_bankroll",
        Decimal("0.010000"),
    )
    object.__setattr__(
        snapshot,
        "exposure_pct_available",
        Decimal("0.011111"),
    )
    object.__setattr__(
        snapshot,
        "canonical_input_hash",
        stable_hash(snapshot.hash_payload()),
    )
    object.__setattr__(
        snapshot,
        "_authoritative_origin_hash",
        _seal_authoritative_hash(snapshot.canonical_input_hash),
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_PROJECTION_OBJECT_MISMATCH,
    ):
        _factory_projection_for(
            current_snapshot=snapshot,
            bankroll_reference=replacement,
        )


def test_snapshot_rejects_coordinated_invalid_bankroll_mutation():
    bankroll = _bankroll()
    object.__setattr__(
        bankroll,
        "available_balance",
        Decimal("1000.00"),
    )
    object.__setattr__(
        bankroll,
        "reserved_balance",
        Decimal("500.00"),
    )
    object.__setattr__(
        bankroll,
        "total_bankroll",
        Decimal("100.00"),
    )
    object.__setattr__(
        bankroll,
        "canonical_input_hash",
        stable_hash(bankroll.hash_payload()),
    )
    object.__setattr__(
        bankroll,
        "_authoritative_origin_hash",
        _seal_authoritative_hash(bankroll.canonical_input_hash),
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_BANKROLL_REFERENCE_INVALID,
    ):
        _snapshot(
            bankroll_reference_id=bankroll.bankroll_reference_id,
            bankroll_reference_hash=bankroll.canonical_input_hash,
            authoritative_bankroll_reference=bankroll,
            exposure_pct_bankroll=Decimal("0.100000"),
            exposure_pct_available=Decimal("0.010000"),
        )


def test_projection_rejects_coordinated_candidate_parlay_mutation():
    candidate = _candidate()
    object.__setattr__(candidate, "parlay_id", "forged-parlay")
    object.__setattr__(
        candidate,
        "canonical_input_hash",
        stable_hash(candidate.hash_payload()),
    )
    object.__setattr__(
        candidate,
        "_authoritative_origin_hash",
        _seal_authoritative_hash(candidate.canonical_input_hash),
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_PROJECTION_OBJECT_MISMATCH,
    ):
        _factory_projection_for(candidate=candidate)


def test_projection_rejects_coordinated_nonadditive_top_level_mutation():
    projection = _projection()
    contributions = projection.candidate_top_level_contributions
    for contribution in contributions:
        object.__setattr__(
            contribution,
            "additive_to_portfolio_totals",
            False,
        )
        object.__setattr__(
            contribution,
            "canonical_contribution_hash",
            stable_hash(contribution.hash_payload()),
        )
        object.__setattr__(
            contribution,
            "_authoritative_origin_hash",
            _seal_authoritative_hash(contribution.canonical_contribution_hash),
        )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_CONTRIBUTION_SCOPE,
    ):
        _projection(candidate_top_level_contributions=contributions)


def test_generic_rejects_coordinated_nonadditive_top_level_mutation():
    candidate = _candidate()
    contribution = _candidate_contributions(
        candidate_hash=candidate.canonical_input_hash
    )[0]
    object.__setattr__(
        contribution,
        "additive_to_portfolio_totals",
        False,
    )
    object.__setattr__(
        contribution,
        "canonical_contribution_hash",
        stable_hash(contribution.hash_payload()),
    )
    object.__setattr__(
        contribution,
        "_authoritative_origin_hash",
        _seal_authoritative_hash(contribution.canonical_contribution_hash),
    )
    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_INVALID_CONTRIBUTION_SCOPE,
    ):
        compute_portfolio_total_from_contributions(
            (contribution,),
            measure=ExposureMeasureV1.STAKE_COMMITTED,
            currency="USD",
            snapshot_or_projection_id="projection-1",
            portfolio_id="portfolio-1",
            account_id="account-1",
            portfolio_kind=ExposurePortfolioKindV1.CASH,
            as_of="2026-07-26T00:00:00+00:00",
            calculation_version="exposure_calc_v1",
            authoritative_records=(),
            authoritative_candidates=(candidate,),
        )


def test_collection_revalidates_resealed_terminal_state_semantics():
    record = _record()
    object.__setattr__(record, "state", ExposureRecordStateV1.SETTLED)
    object.__setattr__(
        record,
        "canonical_record_hash",
        stable_hash(record.hash_payload()),
    )
    object.__setattr__(
        record,
        "_authoritative_origin_hash",
        _seal_authoritative_hash(record.canonical_record_hash),
    )

    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CURRENT_ECONOMICS_FOR_HISTORICAL_STATE,
    ):
        validate_record_collection(
            (record,),
            as_of="2026-07-26T00:00:00+00:00",
        )


def test_projection_output_rejects_resealed_understated_totals():
    projection = _projection()
    understated = ExposureTotalsV1(
        **_clone(
            projection.projected_totals,
            maximum_possible_loss=Decimal("10.00"),
            net_liability=Decimal("10.00"),
        )
    )
    object.__setattr__(projection, "projected_totals", understated)
    object.__setattr__(
        projection,
        "canonical_input_hash",
        stable_hash(projection.hash_payload()),
    )
    object.__setattr__(
        projection,
        "_authoritative_origin_hash",
        _seal_authoritative_hash(projection.canonical_input_hash),
    )

    with pytest.raises(
        ExposureValidationError,
        match=ExposureReasonCodeV1.EXP_CANDIDATE_ECONOMICS_MISMATCH,
    ):
        validate_authoritative_projection_integrity(projection)


def test_projection_output_integrity_validator_accepts_bound_projection():
    projection = _projection()
    assert validate_authoritative_projection_integrity(projection) is projection


def test_private_authority_seals_do_not_change_public_dataclass_hashing():
    candidate = _candidate()
    before = stable_hash(candidate)
    object.__setattr__(
        candidate,
        "_authoritative_origin_hash",
        "different-process-local-seal",
    )
    assert stable_hash(candidate) == before
