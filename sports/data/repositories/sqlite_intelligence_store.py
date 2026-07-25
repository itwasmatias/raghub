from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


class SQLiteIntelligenceStore:
    """Local normalized store that retains the last valid provider snapshot."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        self._create_tables()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _create_tables(self) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS intelligence_records (
                        dataset TEXT NOT NULL,
                        canonical_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        retrieved_at TEXT NOT NULL,
                        freshness TEXT NOT NULL,
                        PRIMARY KEY(dataset, canonical_id)
                    );
                    CREATE TABLE IF NOT EXISTS ingestion_state (
                        dataset TEXT PRIMARY KEY,
                        provider TEXT,
                        status TEXT NOT NULL,
                        last_attempt_at TEXT NOT NULL,
                        last_successful_refresh TEXT,
                        last_error TEXT,
                        received INTEGER NOT NULL DEFAULT 0,
                        changed INTEGER NOT NULL DEFAULT 0,
                        cached_records INTEGER NOT NULL DEFAULT 0
                    );
                    """
                )

    def upsert_records(
        self,
        dataset: str,
        rows: list[dict[str, Any]],
        *,
        provider: str,
        retrieved_at: str,
        freshness: str = "current",
    ) -> tuple[int, int, int]:
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            canonical_id = str(row.get("canonical_id") or "").strip()
            if not canonical_id:
                raise ValueError(
                    f"{dataset} row is missing a canonical_id"
                )
            unique[canonical_id] = dict(row)
        changed = 0
        with closing(self._connect()) as connection:
            with connection:
                for canonical_id, row in unique.items():
                    payload = json.dumps(row, sort_keys=True, separators=(",", ":"))
                    content_hash = hashlib.sha256(payload.encode()).hexdigest()
                    existing = connection.execute(
                        """
                        SELECT content_hash FROM intelligence_records
                        WHERE dataset = ? AND canonical_id = ?
                        """,
                        (dataset, canonical_id),
                    ).fetchone()
                    if existing is None or str(existing[0]) != content_hash:
                        changed += 1
                    connection.execute(
                        """
                        INSERT INTO intelligence_records (
                            dataset, canonical_id, payload_json, content_hash,
                            provider, retrieved_at, freshness
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(dataset, canonical_id) DO UPDATE SET
                            payload_json=excluded.payload_json,
                            content_hash=excluded.content_hash,
                            provider=excluded.provider,
                            retrieved_at=excluded.retrieved_at,
                            freshness=excluded.freshness
                        """,
                        (
                            dataset,
                            canonical_id,
                            payload,
                            content_hash,
                            provider,
                            retrieved_at,
                            freshness,
                        ),
                    )
        return len(rows), len(unique), changed

    def list_records(self, dataset: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT canonical_id, payload_json, provider,
                       retrieved_at, freshness
                FROM intelligence_records
                WHERE dataset = ?
                ORDER BY canonical_id
                """,
                (dataset,),
            ).fetchall()
        values = []
        for canonical_id, payload, provider, retrieved_at, freshness in rows:
            record = dict(json.loads(str(payload)))
            record["canonical_id"] = str(canonical_id)
            record["_provenance"] = {
                "provider": str(provider),
                "retrieved_at": str(retrieved_at),
                "freshness": str(freshness),
            }
            values.append(record)
        return values

    def record_state(
        self,
        dataset: str,
        *,
        provider: str | None,
        status: str,
        attempted_at: str,
        successful_at: str | None,
        error: str | None,
        received: int,
        changed: int,
    ) -> None:
        cached = self.count_records(dataset)
        with closing(self._connect()) as connection:
            with connection:
                prior = connection.execute(
                    """
                    SELECT last_successful_refresh
                    FROM ingestion_state WHERE dataset = ?
                    """,
                    (dataset,),
                ).fetchone()
                preserved_success = (
                    successful_at
                    or (str(prior[0]) if prior and prior[0] else None)
                )
                connection.execute(
                    """
                    INSERT INTO ingestion_state (
                        dataset, provider, status, last_attempt_at,
                        last_successful_refresh, last_error, received,
                        changed, cached_records
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(dataset) DO UPDATE SET
                        provider=excluded.provider,
                        status=excluded.status,
                        last_attempt_at=excluded.last_attempt_at,
                        last_successful_refresh=excluded.last_successful_refresh,
                        last_error=excluded.last_error,
                        received=excluded.received,
                        changed=excluded.changed,
                        cached_records=excluded.cached_records
                    """,
                    (
                        dataset,
                        provider,
                        status,
                        attempted_at,
                        preserved_success,
                        error,
                        received,
                        changed,
                        cached,
                    ),
                )

    def last_successful_refresh(self, dataset: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT last_successful_refresh
                FROM ingestion_state WHERE dataset = ?
                """,
                (dataset,),
            ).fetchone()
        return str(row[0]) if row and row[0] else None

    def source_health(self) -> dict[str, dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT dataset, provider, status, last_attempt_at,
                       last_successful_refresh, last_error, received,
                       changed, cached_records
                FROM ingestion_state ORDER BY dataset
                """
            ).fetchall()
        return {
            str(row[0]): {
                "provider": row[1],
                "status": row[2],
                "last_attempt_at": row[3],
                "last_successful_refresh": row[4],
                "last_error": row[5],
                "received": row[6],
                "changed": row[7],
                "cached_records": row[8],
            }
            for row in rows
        }

    def count_records(self, dataset: str) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM intelligence_records WHERE dataset = ?",
                (dataset,),
            ).fetchone()
        return int(row[0])

    def total_records(self) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM intelligence_records"
            ).fetchone()
        return int(row[0])

