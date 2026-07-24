import json
import sqlite3
from contextlib import closing
from pathlib import Path

from sports.intelligence.models.autonomy import (
    AlertOutcome,
    IntelligenceAlert,
    ResearchLifecycle,
    Watch,
)


class SQLiteAutonomyRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = database_path
        self._create_tables()

    def _create_tables(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS watches (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        target_type TEXT NOT NULL,
                        target_value TEXT NOT NULL,
                        condition_name TEXT NOT NULL,
                        active INTEGER NOT NULL DEFAULT 1,
                        UNIQUE(target_type, target_value, condition_name)
                    );
                    CREATE TABLE IF NOT EXISTS intelligence_alerts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        watch_id INTEGER NOT NULL,
                        player_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        signal TEXT NOT NULL,
                        baseline_value REAL NOT NULL,
                        observed_value REAL NOT NULL,
                        confidence REAL NOT NULL,
                        evidence_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS alert_outcomes (
                        alert_id INTEGER PRIMARY KEY,
                        evaluated_at TEXT NOT NULL,
                        continued INTEGER NOT NULL,
                        role_grew INTEGER NOT NULL,
                        correct INTEGER NOT NULL,
                        confidence_error REAL NOT NULL,
                        notes TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS research_lifecycles (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        observation TEXT NOT NULL,
                        hypothesis TEXT NOT NULL,
                        experiment TEXT NOT NULL,
                        outcome TEXT,
                        learned_knowledge TEXT
                    );
                    """
                )

    def save_watch(self, watch: Watch) -> Watch:
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO watches (
                        target_type, target_value, condition_name, active
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(target_type, target_value, condition_name)
                    DO UPDATE SET active = excluded.active
                    """,
                    (
                        watch.target_type,
                        watch.target_value,
                        watch.condition,
                        int(watch.active),
                    ),
                )
                row = connection.execute(
                    """
                    SELECT id, target_type, target_value, condition_name, active
                    FROM watches
                    WHERE target_type = ? AND target_value = ?
                      AND condition_name = ?
                    """,
                    (watch.target_type, watch.target_value, watch.condition),
                ).fetchone()
        return Watch(int(row[0]), str(row[1]), str(row[2]), str(row[3]), bool(row[4]))

    def list_active_watches(self) -> list[Watch]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT id, target_type, target_value, condition_name, active
                FROM watches WHERE active = 1 ORDER BY id
                """
            ).fetchall()
        return [
            Watch(int(row[0]), str(row[1]), str(row[2]), str(row[3]), bool(row[4]))
            for row in rows
        ]

    def save_alert(self, alert: IntelligenceAlert) -> IntelligenceAlert:
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO intelligence_alerts (
                        watch_id, player_id, created_at, signal,
                        baseline_value, observed_value, confidence, evidence_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        alert.watch_id,
                        alert.player_id,
                        alert.created_at,
                        alert.signal,
                        alert.baseline_value,
                        alert.observed_value,
                        alert.confidence,
                        json.dumps(alert.evidence),
                    ),
                )
                alert_id = int(cursor.lastrowid)
        return IntelligenceAlert(
            alert_id,
            alert.watch_id,
            alert.player_id,
            alert.created_at,
            alert.signal,
            alert.baseline_value,
            alert.observed_value,
            alert.confidence,
            alert.evidence,
        )

    def save_outcome(self, outcome: AlertOutcome) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO alert_outcomes VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(alert_id) DO UPDATE SET
                        evaluated_at=excluded.evaluated_at,
                        continued=excluded.continued,
                        role_grew=excluded.role_grew,
                        correct=excluded.correct,
                        confidence_error=excluded.confidence_error,
                        notes=excluded.notes
                    """,
                    (
                        outcome.alert_id,
                        outcome.evaluated_at,
                        int(outcome.continued),
                        int(outcome.role_grew),
                        int(outcome.correct),
                        outcome.confidence_error,
                        outcome.notes,
                    ),
                )

    def get_outcome(self, alert_id: int) -> AlertOutcome | None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT alert_id, evaluated_at, continued, role_grew,
                       correct, confidence_error, notes
                FROM alert_outcomes WHERE alert_id = ?
                """,
                (alert_id,),
            ).fetchone()
        if row is None:
            return None
        return AlertOutcome(
            int(row[0]),
            str(row[1]),
            bool(row[2]),
            bool(row[3]),
            bool(row[4]),
            float(row[5]),
            str(row[6]),
        )

    def list_alerts(self) -> list[IntelligenceAlert]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT id, watch_id, player_id, created_at, signal,
                       baseline_value, observed_value, confidence, evidence_json
                FROM intelligence_alerts ORDER BY id
                """
            ).fetchall()
        return [
            IntelligenceAlert(
                int(row[0]),
                int(row[1]),
                str(row[2]),
                str(row[3]),
                str(row[4]),
                float(row[5]),
                float(row[6]),
                float(row[7]),
                list(json.loads(str(row[8]))),
            )
            for row in rows
        ]

    def save_lifecycle(self, lifecycle: ResearchLifecycle) -> ResearchLifecycle:
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                if lifecycle.id is None:
                    cursor = connection.execute(
                        """
                        INSERT INTO research_lifecycles (
                            observation, hypothesis, experiment,
                            outcome, learned_knowledge
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            lifecycle.observation,
                            lifecycle.hypothesis,
                            lifecycle.experiment,
                            lifecycle.outcome,
                            lifecycle.learned_knowledge,
                        ),
                    )
                    lifecycle_id = int(cursor.lastrowid)
                else:
                    connection.execute(
                        """
                        UPDATE research_lifecycles SET
                            observation=?, hypothesis=?, experiment=?,
                            outcome=?, learned_knowledge=?
                        WHERE id=?
                        """,
                        (
                            lifecycle.observation,
                            lifecycle.hypothesis,
                            lifecycle.experiment,
                            lifecycle.outcome,
                            lifecycle.learned_knowledge,
                            lifecycle.id,
                        ),
                    )
                    lifecycle_id = lifecycle.id
        return ResearchLifecycle(
            lifecycle_id,
            lifecycle.observation,
            lifecycle.hypothesis,
            lifecycle.experiment,
            lifecycle.outcome,
            lifecycle.learned_knowledge,
        )

    def list_outcomes(self) -> list[AlertOutcome]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT alert_id, evaluated_at, continued, role_grew,
                       correct, confidence_error, notes
                FROM alert_outcomes
                ORDER BY alert_id
                """
            ).fetchall()
        return [
            AlertOutcome(
                int(row[0]),
                str(row[1]),
                bool(row[2]),
                bool(row[3]),
                bool(row[4]),
                float(row[5]),
                str(row[6]),
            )
            for row in rows
        ]

    def list_lifecycles(self) -> list[ResearchLifecycle]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT id, observation, hypothesis, experiment,
                       outcome, learned_knowledge
                FROM research_lifecycles
                ORDER BY id
                """
            ).fetchall()
        return [
            ResearchLifecycle(
                int(row[0]),
                str(row[1]),
                str(row[2]),
                str(row[3]),
                str(row[4]) if row[4] is not None else None,
                str(row[5]) if row[5] is not None else None,
            )
            for row in rows
        ]
