from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SituationRoomRepository:
    """Small durable store for inspectable Situation Room records."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path = "data/situation_room.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def migrate(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS situation_room_schema (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS situation_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    persisted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS situation_forecasts (
                    forecast_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS situation_forecast_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    forecast_id TEXT NOT NULL,
                    probability REAL NOT NULL,
                    changed_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_ids_json TEXT NOT NULL,
                    FOREIGN KEY (forecast_id)
                        REFERENCES situation_forecasts(forecast_id)
                );
                CREATE TABLE IF NOT EXISTS situation_autonomy_cycles (
                    cycle_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS situation_quarantine (
                    item_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO situation_room_schema(version, applied_at)
                VALUES (?, ?)
                """,
                (self.SCHEMA_VERSION, self._now()),
            )

    def upsert_evidence(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = dict(payload)
        evidence_id = str(record.get("evidence_id") or self._stable_id(
            "evidence",
            str(record.get("source") or ""),
            str(record.get("url") or ""),
            str(record.get("title") or ""),
        ))
        record["evidence_id"] = evidence_id
        observed_at = str(record.get("observed_at") or self._now())
        record["observed_at"] = observed_at
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO situation_evidence(
                    evidence_id, payload_json, observed_at, persisted_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(evidence_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    observed_at=excluded.observed_at
                """,
                (
                    evidence_id,
                    json.dumps(record, sort_keys=True),
                    observed_at,
                    self._now(),
                ),
            )
        return record

    def list_evidence(self) -> list[dict[str, Any]]:
        return self._list_payloads(
            "SELECT payload_json FROM situation_evidence ORDER BY observed_at DESC"
        )

    def get_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        return self._one_payload(
            "SELECT payload_json FROM situation_evidence WHERE evidence_id=?",
            (evidence_id,),
        )

    def count_evidence(self) -> int:
        return self._count("situation_evidence")

    def upsert_forecast(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = dict(payload)
        forecast_id = str(record["forecast_id"])
        evidence_references = [
            item
            for group in (
                record.get("supporting_evidence") or [],
                record.get("contradicting_evidence") or [],
            )
            for item in group
            if item.get("evidence_id")
        ]
        if not evidence_references and not record.get("scheduled_data_update"):
            raise ValueError(
                "A forecast update requires persisted evidence or a documented scheduled-data update."
            )
        now = self._now()
        existing = self.get_forecast(forecast_id)
        created_at = str(
            existing.get("created_at")
            if existing
            else record.get("created_at") or now
        )
        updated_at = str(record.get("last_updated_at") or now)
        record["created_at"] = created_at
        record["last_updated_at"] = updated_at
        probability = float(record["current_probability"])
        previous = (
            float(existing["current_probability"])
            if existing is not None
            else float(record.get("prior_probability") or probability)
        )
        record["previous_probability"] = previous
        record["probability_change"] = round(probability - previous, 4)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO situation_forecasts(
                    forecast_id, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(forecast_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    forecast_id,
                    json.dumps(record, sort_keys=True),
                    created_at,
                    updated_at,
                ),
            )
            last = connection.execute(
                """
                SELECT probability FROM situation_forecast_history
                WHERE forecast_id=? ORDER BY id DESC LIMIT 1
                """,
                (forecast_id,),
            ).fetchone()
            if last is None or abs(float(last["probability"]) - probability) > 1e-9:
                evidence_ids = [
                    str(item.get("evidence_id"))
                    for item in record.get("supporting_evidence") or []
                    if item.get("evidence_id")
                ]
                connection.execute(
                    """
                    INSERT INTO situation_forecast_history(
                        forecast_id, probability, changed_at, reason,
                        evidence_ids_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        forecast_id,
                        probability,
                        updated_at,
                        str(record.get("why_probability_changed") or "Forecast created."),
                        json.dumps(evidence_ids),
                    ),
                )
        return record

    def list_forecasts(self) -> list[dict[str, Any]]:
        records = self._list_payloads(
            """
            SELECT payload_json FROM situation_forecasts
            ORDER BY updated_at DESC
            """
        )
        for record in records:
            record["probability_history"] = self.get_forecast_history(
                str(record["forecast_id"])
            )
        return records

    def get_forecast(self, forecast_id: str) -> dict[str, Any] | None:
        return self._one_payload(
            "SELECT payload_json FROM situation_forecasts WHERE forecast_id=?",
            (forecast_id,),
        )

    def get_forecast_history(self, forecast_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT probability, changed_at, reason, evidence_ids_json
                FROM situation_forecast_history
                WHERE forecast_id=? ORDER BY id ASC
                """,
                (forecast_id,),
            ).fetchall()
        return [
            {
                "probability": float(row["probability"]),
                "changed_at": row["changed_at"],
                "reason": row["reason"],
                "evidence_ids": json.loads(row["evidence_ids_json"]),
            }
            for row in rows
        ]

    def count_forecasts(self) -> int:
        return self._count("situation_forecasts")

    def save_autonomy_cycle(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = dict(payload)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO situation_autonomy_cycles(
                    cycle_id, payload_json, started_at, completed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    record["cycle_id"],
                    json.dumps(record, sort_keys=True),
                    record["started_at"],
                    record.get("completed_at"),
                ),
            )
        return record

    def get_latest_autonomy_cycle(self) -> dict[str, Any] | None:
        return self._one_payload(
            """
            SELECT payload_json FROM situation_autonomy_cycles
            ORDER BY started_at DESC LIMIT 1
            """
        )

    def quarantine(self, payload: dict[str, Any], reason: str) -> None:
        record = dict(payload)
        item_id = self._stable_id(
            "quarantine",
            str(record.get("url") or ""),
            str(record.get("title") or ""),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO situation_quarantine(
                    item_id, payload_json, reason, retrieved_at
                ) VALUES (?, ?, ?, ?)
                """,
                (item_id, json.dumps(record, sort_keys=True), reason, self._now()),
            )

    def _list_payloads(self, query: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(query).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def _one_payload(
        self, query: str, parameters: tuple[Any, ...] = ()
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def _count(self, table: str) -> int:
        with self._connect() as connection:
            row = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        return int(row["count"])

    @staticmethod
    def _stable_id(prefix: str, *parts: str) -> str:
        digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]
        return f"{prefix}-{digest}"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
