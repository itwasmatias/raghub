from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from sports.intelligence.models.lifecycle import LifecycleStage, SituationEvent


class SQLiteLifecycleRepository:
    """Append-only persistence for intelligence lifecycle events."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = database_path
        self._create_table()

    def _create_table(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS situation_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        situation_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        occurred_at TEXT NOT NULL,
                        recorded_at TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        UNIQUE(situation_id, sequence)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS situation_events_lookup
                    ON situation_events(situation_id, sequence)
                    """
                )

    def append(self, event: SituationEvent) -> SituationEvent:
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                last_sequence = connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0)
                    FROM situation_events
                    WHERE situation_id = ?
                    """,
                    (event.situation_id,),
                ).fetchone()[0]
                next_sequence = int(last_sequence) + 1
                cursor = connection.execute(
                    """
                    INSERT INTO situation_events (
                        situation_id, sequence, occurred_at, recorded_at,
                        stage, event_type, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.situation_id,
                        next_sequence,
                        event.occurred_at,
                        event.recorded_at,
                        event.stage.value,
                        event.event_type,
                        json.dumps(event.payload, sort_keys=True),
                    ),
                )
        return SituationEvent(
            id=int(cursor.lastrowid),
            situation_id=event.situation_id,
            sequence=next_sequence,
            occurred_at=event.occurred_at,
            recorded_at=event.recorded_at,
            stage=event.stage,
            event_type=event.event_type,
            payload=dict(event.payload),
        )

    def list_events(self, situation_id: str) -> list[SituationEvent]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT id, situation_id, sequence, occurred_at, recorded_at,
                       stage, event_type, payload_json
                FROM situation_events
                WHERE situation_id = ?
                ORDER BY sequence
                """,
                (situation_id,),
            ).fetchall()
        return [
            SituationEvent(
                id=int(row[0]),
                situation_id=str(row[1]),
                sequence=int(row[2]),
                occurred_at=str(row[3]),
                recorded_at=str(row[4]),
                stage=LifecycleStage(str(row[5])),
                event_type=str(row[6]),
                payload=dict(json.loads(str(row[7]))),
            )
            for row in rows
        ]

    def list_situation_ids(self) -> list[str]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT situation_id
                FROM situation_events
                GROUP BY situation_id
                ORDER BY MIN(id)
                """
            ).fetchall()
        return [str(row[0]) for row in rows]

    def replace_event(self, _event: SituationEvent) -> None:
        raise NotImplementedError(
            "Situation history is append-only; append a superseding event instead."
        )

