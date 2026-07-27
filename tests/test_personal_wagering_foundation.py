from __future__ import annotations

from decimal import Decimal

import pytest

from sports.personal.repository import PersonalEditionRepository
from sports.personal.wagering import (
    BankrollAccount,
    DisabledExecutionGateway,
    LedgerEntry,
    LedgerEntryType,
    SettlementOutcome,
    SettlementRecord,
    ValidationCode,
    WagerStatus,
    WagerKind,
    WagerLeg,
    WagerTicket,
    WagerValidationError,
    build_parlay_wager_ticket,
    build_single_wager_ticket,
    money,
    now_iso,
    validate_wager_ticket,
)


def _account() -> BankrollAccount:
    now = now_iso()
    return BankrollAccount(
        account_id="acct-1",
        currency="USD",
        balance_usd=Decimal("1000.00"),
        available_usd=Decimal("1000.00"),
        held_usd=Decimal("0.00"),
        status="active",
        created_at=now,
        updated_at=now,
        metadata={"owner": "test"},
    )


def test_wagering_migrations_are_applied_after_existing_versions(tmp_path):
    repository = PersonalEditionRepository(tmp_path / "sip.db")

    first = repository.migrate()
    second = repository.migrate()

    assert first == [1, 2, 3, 4, 5, 6, 7]
    assert second == []


def test_bankroll_ledger_round_trip_and_idempotency(tmp_path):
    repository = PersonalEditionRepository(tmp_path / "sip.db")
    repository.save_bankroll_account(_account())

    now = now_iso()
    entry = LedgerEntry(
        entry_id="entry-1",
        account_id="acct-1",
        entry_type=LedgerEntryType.DEBIT,
        amount_usd=Decimal("-25.00"),
        balance_after_usd=Decimal("975.00"),
        available_after_usd=Decimal("975.00"),
        held_after_usd=Decimal("0.00"),
        reference_kind="wager",
        reference_id="wager-1",
        idempotency_key="ledger:wager-1:debit",
        created_at=now,
        note="Reserve stake",
    )

    repository.append_ledger_entry(entry)
    account = repository.get_bankroll_account("acct-1")
    rows = repository.list_ledger_entries("acct-1")

    assert account is not None
    assert account.balance_usd == Decimal("975.00")
    assert rows[0]["idempotency_key"] == "ledger:wager-1:debit"

    with pytest.raises(ValueError, match="duplicate ledger idempotency key"):
        repository.append_ledger_entry(entry)


def test_wager_validation_rejects_invalid_parlay_structure():
    now = now_iso()
    ticket = WagerTicket(
        wager_id="wager-invalid-parlay",
        account_id="acct-1",
        kind=WagerKind.PARLAY,
        status=WagerStatus.VALIDATED,
        stake_usd=money("20"),
        potential_profit_usd=money("22"),
        potential_payout_usd=money("42"),
        execution_mode="paper_only",
        idempotency_key="parlay:single-leg",
        placed_at=now,
        updated_at=now,
        legs=(
            WagerLeg(
                leg_id="leg-1",
                canonical_event_id="wnba:1",
                market="moneyline",
                period="full_game",
                selection="home",
                sportsbook="draftkings",
                american_odds=110,
            ),
        ),
        validation_notes=(),
        metadata={},
    )

    with pytest.raises(WagerValidationError) as caught:
        validate_wager_ticket(ticket)

    assert any(
        issue.code == ValidationCode.PARLAY_NEEDS_TWO_LEGS
        for issue in caught.value.issues
    )


def test_parlay_ticket_persistence_and_leg_round_trip(tmp_path):
    repository = PersonalEditionRepository(tmp_path / "sip.db")
    repository.save_bankroll_account(_account())

    legs = (
        WagerLeg(
            leg_id="leg-a",
            canonical_event_id="wnba:2026:storm:sky",
            market="moneyline",
            period="full_game",
            selection="home",
            sportsbook="draftkings",
            american_odds=120,
            model_probability=Decimal("0.58"),
            market_probability=Decimal("0.52"),
            correlation_group="grp-1",
        ),
        WagerLeg(
            leg_id="leg-b",
            canonical_event_id="mlb:2026:mets:dodgers",
            market="moneyline",
            period="full_game",
            selection="away",
            sportsbook="fanduel",
            american_odds=145,
            model_probability=Decimal("0.46"),
            market_probability=Decimal("0.41"),
            correlation_group="grp-2",
        ),
    )
    ticket = build_parlay_wager_ticket(
        account_id="acct-1",
        stake_usd="15",
        legs=legs,
        idempotency_key="parlay:wnba-mlb:15",
    )

    wager_id = repository.save_wager_ticket(ticket)
    replay = repository.get_wager_ticket(wager_id)

    assert replay is not None
    assert replay.kind == WagerKind.PARLAY
    assert len(replay.legs) == 2
    assert replay.legs[0].canonical_event_id == "wnba:2026:storm:sky"


def test_settlement_foundation_updates_wager_state_and_audit(tmp_path):
    repository = PersonalEditionRepository(tmp_path / "sip.db")
    repository.save_bankroll_account(_account())

    ticket = build_single_wager_ticket(
        account_id="acct-1",
        canonical_event_id="wnba:2026:storm:sky",
        sportsbook="draftkings",
        selection="home",
        american_odds=120,
        stake_usd="10",
        idempotency_key="single:storm:home:10",
    )
    wager_id = repository.save_wager_ticket(ticket)

    settlement = SettlementRecord(
        settlement_id="settlement-1",
        wager_id=wager_id,
        outcome=SettlementOutcome.WIN,
        payout_usd=Decimal("22.00"),
        profit_usd=Decimal("12.00"),
        settled_at=now_iso(),
        source="manual-settlement-test",
        notes="foundation check",
    )
    repository.save_settlement(settlement)

    replay = repository.get_wager_ticket(wager_id)
    audits = repository.list_audit_events(
        entity_kind="settlement", entity_id="settlement-1"
    )

    assert replay is not None
    assert replay.status.value == "settled"
    assert audits
    assert audits[0]["payload"]["outcome"] == "win"


def test_execution_gateway_stays_disabled_until_controls_are_proven():
    ticket = build_single_wager_ticket(
        account_id="acct-1",
        canonical_event_id="wnba:2026:storm:sky",
        sportsbook="draftkings",
        selection="home",
        american_odds=120,
        stake_usd="10",
        idempotency_key="single:disabled-check",
    )

    with pytest.raises(
        RuntimeError, match="Real-money sportsbook execution is disabled"
    ):
        DisabledExecutionGateway().place_wager(ticket)
