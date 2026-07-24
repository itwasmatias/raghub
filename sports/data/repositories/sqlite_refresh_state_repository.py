import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RefreshState:
    season: str
    last_processed_date: str


class SQLiteRefreshStateRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = database_path
        self._create_table()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _create_table(self) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS refresh_state (
                        season TEXT PRIMARY KEY,
                        last_processed_date TEXT NOT NULL
                    )
                    """
                )

    def get_last_processed_date(self, season: str) -> str | None:
        state = self.get(season)
        return state.last_processed_date if state is not None else None

    def get(self, season: str) -> RefreshState | None:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                SELECT season, last_processed_date
                FROM refresh_state
                WHERE season = ?
                """,
                (season,),
            )
            row = cursor.fetchone()

        if row is None:
            return None
        return RefreshState(
            season=str(row[0]),
            last_processed_date=str(row[1]),
        )

    def save_last_processed_date(self, season: str, date: str) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO refresh_state (season, last_processed_date)
                    VALUES (?, ?)
                    ON CONFLICT (season) DO UPDATE SET
                        last_processed_date = excluded.last_processed_date
                    """,
                    (season, date),
                )
