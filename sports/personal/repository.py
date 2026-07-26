from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sports.personal.models import (
    CanonicalEvent,
    CompleteBookMarket,
    FeedHealth,
    ModelMetadata,
    MoneylineForecast,
    NormalizedMoneylineQuote,
    QualificationResult,
    ResolvedPredictionRecord,
)


class PersonalEditionRepository:
    MIGRATIONS = (
        (
            1,
            """
            CREATE TABLE IF NOT EXISTS sip_events (
                canonical_id TEXT PRIMARY KEY,
                league TEXT NOT NULL,
                start_time TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sip_quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_event_id TEXT NOT NULL,
                sportsbook TEXT NOT NULL,
                market TEXT NOT NULL,
                period TEXT NOT NULL,
                selection TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE (
                    canonical_event_id, sportsbook, market, period,
                    selection, observed_at
                )
            );
            CREATE INDEX IF NOT EXISTS idx_sip_quotes_event_time
                ON sip_quotes(canonical_event_id, observed_at);
            CREATE TABLE IF NOT EXISTS sip_forecasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_event_id TEXT NOT NULL,
                market TEXT NOT NULL,
                period TEXT NOT NULL,
                selection TEXT NOT NULL,
                model_version TEXT NOT NULL,
                forecast_timestamp TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE (
                    canonical_event_id, market, period, selection,
                    model_version, forecast_timestamp
                )
            );
            CREATE TABLE IF NOT EXISTS sip_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_event_id TEXT NOT NULL,
                selection TEXT NOT NULL,
                status TEXT NOT NULL,
                evaluated_at TEXT NOT NULL,
                data_mode TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sip_evaluations_latest
                ON sip_evaluations(canonical_event_id, evaluated_at DESC);
            CREATE TABLE IF NOT EXISTS sip_feed_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sip_job_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                error TEXT
            );
            """,
        ),
        (
            2,
            """
            CREATE TABLE IF NOT EXISTS sip_resolved_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_event_id TEXT NOT NULL,
                selection TEXT NOT NULL,
                model_version TEXT NOT NULL,
                forecast_timestamp TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE (
                    canonical_event_id, selection, model_version,
                    forecast_timestamp
                )
            );
            CREATE INDEX IF NOT EXISTS idx_sip_resolved_predictions_event
                ON sip_resolved_predictions(canonical_event_id, forecast_timestamp);
            """,
        ),
    )

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def migrate(self) -> list[int]:
        applied = []
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sip_schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                existing = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT version FROM sip_schema_migrations"
                    )
                }
                for version, sql in self.MIGRATIONS:
                    if version in existing:
                        continue
                    connection.executescript(sql)
                    connection.execute(
                        "INSERT INTO sip_schema_migrations(version) VALUES (?)",
                        (version,),
                    )
                    applied.append(version)
        return applied

    def save_market_snapshot(
        self,
        *,
        events: tuple[CanonicalEvent, ...],
        quotes: tuple[NormalizedMoneylineQuote, ...],
        retrieved_at: str,
    ) -> None:
        self.migrate()
        with closing(self.connect()) as connection:
            with connection:
                for event in events:
                    connection.execute(
                        """
                        INSERT INTO sip_events VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(canonical_id) DO UPDATE SET
                            league=excluded.league,
                            start_time=excluded.start_time,
                            payload_json=excluded.payload_json,
                            updated_at=excluded.updated_at
                        """,
                        (
                            event.canonical_id,
                            event.league,
                            event.start_time,
                            json.dumps(asdict(event), sort_keys=True),
                            retrieved_at,
                        ),
                    )
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO sip_quotes (
                        canonical_event_id, sportsbook, market, period,
                        selection, observed_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            quote.canonical_event_id,
                            quote.sportsbook,
                            quote.market,
                            quote.period,
                            quote.selection,
                            quote.observed_at,
                            json.dumps(asdict(quote), sort_keys=True),
                        )
                        for quote in quotes
                    ],
                )

    def save_forecasts(
        self, forecasts: tuple[MoneylineForecast, ...] | list[MoneylineForecast]
    ) -> None:
        self.migrate()
        with closing(self.connect()) as connection:
            with connection:
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO sip_forecasts (
                        canonical_event_id, market, period, selection,
                        model_version, forecast_timestamp, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            item.canonical_event_id,
                            item.market,
                            item.period,
                            item.selection,
                            item.model_version,
                            item.forecast_timestamp,
                            json.dumps(asdict(item), sort_keys=True),
                        )
                        for item in forecasts
                    ],
                )

    def save_evaluation(self, result: QualificationResult, *, data_mode: str) -> int:
        self.migrate()
        payload = asdict(result)
        payload["reason_codes"] = [
            getattr(item, "value", str(item)) for item in result.reason_codes
        ]
        with closing(self.connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO sip_evaluations (
                        canonical_event_id, selection, status,
                        evaluated_at, data_mode, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.canonical_event_id,
                        result.selection,
                        result.status,
                        result.evaluated_at,
                        data_mode,
                        json.dumps(payload, sort_keys=True),
                    ),
                )
                return int(cursor.lastrowid)

    def save_feed_health(self, health: FeedHealth) -> None:
        self.migrate()
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO sip_feed_state VALUES (1, ?)
                    ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json
                    """,
                    (json.dumps(asdict(health), sort_keys=True),),
                )

    def load_feed_health(self) -> FeedHealth | None:
        self.migrate()
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM sip_feed_state WHERE id = 1"
            ).fetchone()
        return FeedHealth(**json.loads(row[0])) if row else None

    def list_events(self) -> list[CanonicalEvent]:
        self.migrate()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM sip_events ORDER BY start_time"
            ).fetchall()
        return [
            CanonicalEvent(
                **{
                    **json.loads(row[0]),
                    "provider_event_ids": tuple(
                        json.loads(row[0])["provider_event_ids"]
                    ),
                    "source_urls": tuple(json.loads(row[0])["source_urls"]),
                }
            )
            for row in rows
        ]

    def list_quotes(self) -> list[NormalizedMoneylineQuote]:
        self.migrate()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM sip_quotes ORDER BY observed_at"
            ).fetchall()
        return [NormalizedMoneylineQuote(**json.loads(row[0])) for row in rows]

    def list_forecasts(self) -> list[MoneylineForecast]:
        self.migrate()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM sip_forecasts ORDER BY forecast_timestamp"
            ).fetchall()
        values = []
        for (payload_text,) in rows:
            payload = json.loads(payload_text)
            metadata = payload["metadata"]
            metadata["training_period"] = tuple(metadata["training_period"])
            metadata["validation_period"] = tuple(metadata["validation_period"])
            payload["metadata"] = ModelMetadata(**metadata)
            payload["contributing_factors"] = tuple(payload["contributing_factors"])
            payload["missing_feature_warnings"] = tuple(
                payload["missing_feature_warnings"]
            )
            values.append(MoneylineForecast(**payload))
        return values

    def latest_evaluations(self) -> list[dict[str, Any]]:
        self.migrate()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM sip_evaluations
                WHERE id IN (
                    SELECT MAX(id) FROM sip_evaluations
                    GROUP BY canonical_event_id, selection
                )
                ORDER BY evaluated_at DESC
                """
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def start_job(self, name: str, started_at: str) -> int:
        self.migrate()
        with closing(self.connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO sip_job_runs (
                        job_name, started_at, status, attempts
                    ) VALUES (?, ?, 'running', 1)
                    """,
                    (name, started_at),
                )
                return int(cursor.lastrowid)

    def finish_job(
        self,
        job_id: int,
        *,
        finished_at: str,
        status: str,
        attempts: int,
        error: str | None,
    ) -> None:
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    UPDATE sip_job_runs
                    SET finished_at=?, status=?, attempts=?, error=?
                    WHERE id=?
                    """,
                    (finished_at, status, attempts, error, job_id),
                )

    def recent_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        self.migrate()
        with closing(self.connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT * FROM sip_job_runs
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_resolved_predictions(
        self,
        rows: tuple[ResolvedPredictionRecord, ...] | list[ResolvedPredictionRecord],
    ) -> None:
        if not rows:
            return
        self.migrate()
        with closing(self.connect()) as connection:
            with connection:
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO sip_resolved_predictions (
                        canonical_event_id, selection, model_version,
                        forecast_timestamp, payload_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            item.canonical_event_id,
                            item.selection,
                            item.model_version,
                            item.forecast_timestamp,
                            json.dumps(asdict(item), sort_keys=True),
                        )
                        for item in rows
                    ],
                )

    def list_resolved_predictions(
        self,
    ) -> list[ResolvedPredictionRecord]:
        self.migrate()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM sip_resolved_predictions
                ORDER BY forecast_timestamp
                """
            ).fetchall()
        return [ResolvedPredictionRecord(**json.loads(row[0])) for row in rows]

    def resolved_prediction_keys(self) -> set[tuple[str, str, str, str]]:
        self.migrate()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT canonical_event_id, selection, model_version, forecast_timestamp
                FROM sip_resolved_predictions
                """
            ).fetchall()
        return {(str(a), str(b), str(c), str(d)) for a, b, c, d in rows}
