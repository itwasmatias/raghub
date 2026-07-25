from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from sports.betting.models import ClosingEvaluation, MarketAssessment, OddsQuote


class SQLiteBettingRepository:
    """Append-only market snapshots plus recommendation audit records."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        self._create_tables()

    def _create_tables(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS odds_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL, market_name TEXT NOT NULL,
                    selection_name TEXT NOT NULL, sportsbook TEXT NOT NULL,
                    line REAL, american_price INTEGER NOT NULL,
                    event_start TEXT NOT NULL, fetched_at TEXT NOT NULL,
                    source TEXT NOT NULL, designation TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_odds_market_time
                    ON odds_snapshots(event_id, market_name, line, fetched_at);
                CREATE TABLE IF NOT EXISTS betting_recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL, event_id TEXT NOT NULL,
                    market_name TEXT NOT NULL, selection_name TEXT NOT NULL,
                    line REAL, sportsbook TEXT NOT NULL, american_price INTEGER NOT NULL,
                    model_probability REAL NOT NULL, market_probability REAL NOT NULL,
                    expected_return REAL NOT NULL, model_version TEXT NOT NULL,
                    assessment_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS betting_outcomes (
                    recommendation_id INTEGER PRIMARY KEY,
                    evaluated_at TEXT NOT NULL, closing_line REAL,
                    closing_price INTEGER, won INTEGER, realized_return REAL,
                    FOREIGN KEY(recommendation_id) REFERENCES betting_recommendations(id)
                );
                """
            )

    def add_quotes(self, quotes: list[OddsQuote]) -> None:
        rows = [
            (
                q.event_id, q.market, q.selection, q.sportsbook, q.line,
                q.american_price, q.event_start.isoformat(), q.fetched_at.isoformat(),
                q.source, q.designation,
            )
            for q in quotes
        ]
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.executemany(
                    """INSERT INTO odds_snapshots (
                        event_id, market_name, selection_name, sportsbook, line,
                        american_price, event_start, fetched_at, source, designation
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    rows,
                )

    def quote_history(
        self,
        *,
        event_id: str,
        market: str,
        selection: str,
        sportsbook: str | None = None,
    ) -> list[dict[str, object]]:
        query = """
            SELECT event_id, market_name, selection_name, sportsbook, line,
                   american_price, event_start, fetched_at, source, designation
            FROM odds_snapshots
            WHERE event_id = ? AND market_name = ? AND selection_name = ?
        """
        parameters: list[object] = [event_id, market, selection]
        if sportsbook is not None:
            query += " AND sportsbook = ?"
            parameters.append(sportsbook)
        query += " ORDER BY fetched_at ASC, id ASC"
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(query, parameters)]

    def save_recommendation(self, assessment: MarketAssessment) -> int:
        if not assessment.qualified or assessment.best_quote is None:
            raise ValueError("only qualified assessments can become recommendations")
        quote = assessment.best_quote
        payload = {
            "confidence_interval": assessment.confidence_interval,
            "model_agreement": assessment.model_agreement,
            "warnings": assessment.warnings,
            "reasons": assessment.prediction.reasons,
            "invalidators": assessment.prediction.invalidators,
        }
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                cursor = connection.execute(
                    """INSERT INTO betting_recommendations (
                        created_at, event_id, market_name, selection_name, line,
                        sportsbook, american_price, model_probability,
                        market_probability, expected_return, model_version,
                        assessment_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        datetime.now(timezone.utc).isoformat(), quote.event_id, quote.market,
                        quote.selection, quote.line, quote.sportsbook, quote.american_price,
                        assessment.prediction.probability, assessment.consensus_probability,
                        assessment.expected_return, assessment.prediction.model_version,
                        json.dumps(payload),
                    ),
                )
                return int(cursor.lastrowid)

    def evaluate(
        self,
        recommendation_id: int,
        *,
        closing_quote: OddsQuote | None,
        won: bool | None,
    ) -> ClosingEvaluation:
        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                "SELECT line, american_price FROM betting_recommendations WHERE id = ?",
                (recommendation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"recommendation {recommendation_id} was not found")
            suggested_line, suggested_price = row
            suggested_implied = _implied(int(suggested_price))
            closing_implied = (
                closing_quote.implied_probability if closing_quote is not None else None
            )
            # Lower implied probability means SIP obtained the better payout.
            beat_close = (
                suggested_implied < closing_implied if closing_implied is not None else None
            )
            line_delta = (
                closing_quote.line - suggested_line
                if closing_quote is not None
                and closing_quote.line is not None
                and suggested_line is not None
                else None
            )
            realized = (
                (_decimal(int(suggested_price)) - 1.0 if won else -1.0)
                if won is not None
                else None
            )
            with connection:
                connection.execute(
                    """INSERT INTO betting_outcomes VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(recommendation_id) DO UPDATE SET
                        evaluated_at=excluded.evaluated_at,
                        closing_line=excluded.closing_line,
                        closing_price=excluded.closing_price,
                        won=excluded.won,
                        realized_return=excluded.realized_return""",
                    (
                        recommendation_id, datetime.now(timezone.utc).isoformat(),
                        closing_quote.line if closing_quote else None,
                        closing_quote.american_price if closing_quote else None,
                        None if won is None else int(won), realized,
                    ),
                )
        return ClosingEvaluation(
            recommendation_id, beat_close, suggested_implied, closing_implied,
            line_delta, won, realized,
        )


def _decimal(price: int) -> float:
    return 1.0 + (price / 100.0 if price > 0 else 100.0 / abs(price))


def _implied(price: int) -> float:
    return 1.0 / _decimal(price)
