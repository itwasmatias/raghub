from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sports.personal.normalization import MoneylineNormalizer
from sports.personal.repository import PersonalEditionRepository

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
EVENT_START = datetime(2026, 7, 27, 0, 10, tzinfo=timezone.utc)


def _row(
    *,
    source: str,
    provider_event_id: str,
    sportsbook: str,
    selection: str,
    selection_team: str,
    american_price: int,
    observed_at: datetime,
) -> dict[str, object]:
    return {
        "provider_event_id": provider_event_id,
        "league": "MLB",
        "season": "2026",
        "home_team": "NY Yankees",
        "away_team": "LA Dodgers",
        "sportsbook": sportsbook,
        "market": "h2h",
        "period": "full_game",
        "selection": selection,
        "selection_team": selection_team,
        "american_price": american_price,
        "observed_at": observed_at.isoformat(),
        "source": source,
        "source_url": "https://example.test/quote",
        "data_mode": "live",
        "is_live": False,
        "event_start": EVENT_START.isoformat(),
    }


def _provider_pair(
    *,
    source: str,
    provider_event_id: str,
    sportsbook: str = "DraftKings",
    observed_at: datetime = NOW - timedelta(seconds=30),
    home_price: int = -140,
    away_price: int = 120,
):
    return [
        _row(
            source=source,
            provider_event_id=provider_event_id,
            sportsbook=sportsbook,
            selection="home",
            selection_team="NY Yankees",
            american_price=home_price,
            observed_at=observed_at,
        ),
        _row(
            source=source,
            provider_event_id=provider_event_id,
            sportsbook=sportsbook,
            selection="away",
            selection_team="LA Dodgers",
            american_price=away_price,
            observed_at=observed_at,
        ),
    ]


def _migrations_before(version: int):
    return tuple(
        item for item in PersonalEditionRepository.MIGRATIONS if item[0] < version
    )


def test_migration7_preserves_duplicate_legacy_provider_quote_ids(
    tmp_path, monkeypatch
):
    repository = PersonalEditionRepository(tmp_path / "sip.db")
    all_migrations = PersonalEditionRepository.MIGRATIONS

    monkeypatch.setattr(repository, "MIGRATIONS", _migrations_before(7))
    repository.migrate()

    with repository.connect() as connection:
        with connection:
            payload = json.dumps({"provider_quote_id": "quote:v1:dup"}, sort_keys=True)
            connection.execute(
                """
                INSERT INTO sip_quotes (
                    canonical_event_id, sportsbook, market, period,
                    selection, observed_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "mlb:legacy:event",
                    "draftkings",
                    "moneyline",
                    "full_game",
                    "home",
                    NOW.isoformat(),
                    payload,
                ),
            )
            connection.execute(
                """
                INSERT INTO sip_quotes (
                    canonical_event_id, sportsbook, market, period,
                    selection, observed_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "mlb:legacy:event",
                    "draftkings",
                    "moneyline",
                    "full_game",
                    "away",
                    NOW.isoformat(),
                    payload,
                ),
            )

    monkeypatch.setattr(repository, "MIGRATIONS", all_migrations)
    repository.migrate()

    with repository.connect() as connection:
        rows = connection.execute(
            "SELECT id, provider_quote_id FROM sip_quotes ORDER BY id"
        ).fetchall()

    assert len(rows) == 2
    assert len({row[1] for row in rows}) == 2
    assert all(str(row[1]).startswith("quote:v1:dup") for row in rows)


def test_migration7_failure_keeps_original_sip_quotes_table(tmp_path, monkeypatch):
    repository = PersonalEditionRepository(tmp_path / "sip.db")
    all_migrations = PersonalEditionRepository.MIGRATIONS

    monkeypatch.setattr(repository, "MIGRATIONS", _migrations_before(7))
    repository.migrate()

    with repository.connect() as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO sip_quotes (
                    canonical_event_id, sportsbook, market, period,
                    selection, observed_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "mlb:legacy:event",
                    "draftkings",
                    "moneyline",
                    "full_game",
                    "home",
                    NOW.isoformat(),
                    json.dumps(
                        {"provider_quote_id": "quote:v1:legacy"}, sort_keys=True
                    ),
                ),
            )

    failing_migration_7 = (
        7,
        """
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS sip_quotes_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_event_id TEXT NOT NULL,
            sportsbook TEXT NOT NULL,
            market TEXT NOT NULL,
            period TEXT NOT NULL,
            selection TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            provider_quote_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(provider_quote_id)
        );
        INSERT INTO sip_quotes_v2 (
            id, canonical_event_id, sportsbook, market, period,
            selection, observed_at, provider_quote_id, payload_json
        )
        SELECT
            id,
            canonical_event_id,
            sportsbook,
            market,
            period,
            selection,
            observed_at,
            'legacy:' || CAST(id AS TEXT),
            payload_json
        FROM sip_quotes;
        DROP TABLE sip_quotes;
        INSERT INTO table_that_does_not_exist VALUES (1);
        ALTER TABLE sip_quotes_v2 RENAME TO sip_quotes;
        COMMIT;
        """,
    )
    monkeypatch.setattr(
        repository,
        "MIGRATIONS",
        _migrations_before(7) + (failing_migration_7,),
    )

    with pytest.raises(sqlite3.Error):
        repository.migrate()

    with repository.connect() as connection:
        sip_quotes_exists = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='sip_quotes'
            """
        ).fetchone()
        row_count = connection.execute("SELECT COUNT(*) FROM sip_quotes").fetchone()[0]

    assert sip_quotes_exists is not None
    assert row_count == 1

    monkeypatch.setattr(repository, "MIGRATIONS", all_migrations)


def test_quote_upsert_conflict_synchronizes_structural_columns(tmp_path):
    repository = PersonalEditionRepository(tmp_path / "sip.db")
    normalized = MoneylineNormalizer(maximum_age_seconds=600).normalize(
        _provider_pair(source="odds_api_io", provider_event_id="provider-a-101"),
        as_of=NOW,
    )
    original = normalized.quotes[0]
    updated = replace(
        original,
        sportsbook="fanduel",
        canonical_sportsbook_id="fanduel",
        observed_at=(NOW - timedelta(seconds=10)).isoformat(),
        source_url="https://example.test/quote-updated",
    )

    repository.save_market_snapshot(
        events=normalized.events,
        quotes=(original,),
        retrieved_at=NOW.isoformat(),
    )
    repository.save_market_snapshot(
        events=normalized.events,
        quotes=(updated,),
        retrieved_at=NOW.isoformat(),
    )

    with repository.connect() as connection:
        row = connection.execute(
            """
            SELECT sportsbook, observed_at, payload_json
            FROM sip_quotes
            WHERE provider_quote_id = ?
            """,
            (original.provider_quote_id,),
        ).fetchone()

    assert row is not None
    payload = json.loads(str(row[2]))
    assert row[0] == "fanduel"
    assert row[1] == updated.observed_at
    assert payload["sportsbook"] == "fanduel"
    assert payload["observed_at"] == updated.observed_at
