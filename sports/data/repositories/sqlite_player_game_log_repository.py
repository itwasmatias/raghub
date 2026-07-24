import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any


class SQLitePlayerGameLogRepository:
    _columns = """
        league, competition, season, season_type, player_id, player_name,
        team_id, team_abbreviation, team_name, game_id, game_date, minutes,
        pts, reb, ast, fgm, fga, fg3m, fg3a, ftm, fta, source, loaded_at
    """

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
                    CREATE TABLE IF NOT EXISTS player_game_logs (
                        league TEXT NOT NULL DEFAULT '',
                        competition TEXT NOT NULL DEFAULT '',
                        season TEXT NOT NULL,
                        season_type TEXT NOT NULL DEFAULT '',
                        player_id TEXT NOT NULL,
                        player_name TEXT NOT NULL,
                        team_id INTEGER,
                        team_abbreviation TEXT,
                        team_name TEXT,
                        game_id TEXT NOT NULL,
                        game_date TEXT NOT NULL,
                        minutes NUMERIC,
                        pts REAL,
                        reb REAL,
                        ast REAL,
                        fgm REAL,
                        fga REAL,
                        fg3m REAL,
                        fg3a REAL,
                        ftm REAL,
                        fta REAL,
                        source TEXT,
                        loaded_at TEXT,
                        PRIMARY KEY (
                            league, competition, season, season_type,
                            player_id, game_id
                        )
                    )
                    """
                )

    def save_many(
        self,
        logs: Iterable[Mapping[str, Any]],
    ) -> None:
        values = [self._to_values(log) for log in logs]
        if not values:
            return

        with closing(self._connect()) as connection:
            with connection:
                connection.executemany(
                    f"""
                    INSERT INTO player_game_logs ({self._columns})
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?
                    )
                    ON CONFLICT (
                        league, competition, season, season_type,
                        player_id, game_id
                    ) DO UPDATE SET
                        player_name = excluded.player_name,
                        team_id = excluded.team_id,
                        team_abbreviation = excluded.team_abbreviation,
                        team_name = excluded.team_name,
                        game_date = excluded.game_date,
                        minutes = excluded.minutes,
                        pts = excluded.pts,
                        reb = excluded.reb,
                        ast = excluded.ast,
                        fgm = excluded.fgm,
                        fga = excluded.fga,
                        fg3m = excluded.fg3m,
                        fg3a = excluded.fg3a,
                        ftm = excluded.ftm,
                        fta = excluded.fta,
                        source = excluded.source,
                        loaded_at = excluded.loaded_at
                    """,
                    values,
                )

    def list_by_season(self, season: str) -> list[dict[str, Any]]:
        return self._list(
            f"""
            SELECT {self._columns}
            FROM player_game_logs
            WHERE season = ?
            ORDER BY game_date, player_id, game_id, league, competition
            """,
            (season,),
        )

    def list_by_player(
        self,
        player_id: str,
        season: str,
    ) -> list[dict[str, Any]]:
        return self._list(
            f"""
            SELECT {self._columns}
            FROM player_game_logs
            WHERE player_id = ? AND season = ?
            ORDER BY game_date, game_id, league, competition
            """,
            (player_id, season),
        )

    def _list(
        self,
        query: str,
        parameters: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _to_values(log: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            log.get("league", ""),
            log.get("competition", ""),
            log["season"],
            log.get("season_type", ""),
            log["player_id"],
            log["player_name"],
            log.get("team_id"),
            log.get("team_abbreviation"),
            log.get("team_name"),
            log["game_id"],
            log["game_date"],
            log.get("minutes"),
            log.get("pts"),
            log.get("reb"),
            log.get("ast"),
            log.get("fgm"),
            log.get("fga"),
            log.get("fg3m"),
            log.get("fg3a"),
            log.get("ftm"),
            log.get("fta"),
            log.get("source"),
            log.get("loaded_at"),
        )

    @staticmethod
    def _from_row(row: tuple[object, ...]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "player_id": row[4],
            "player_name": row[5],
            "season": row[2],
            "team_id": row[6],
            "team_abbreviation": row[7],
            "team_name": row[8],
            "game_id": row[9],
            "game_date": row[10],
            "minutes": row[11],
            "pts": row[12],
            "reb": row[13],
            "ast": row[14],
            "fgm": row[15],
            "fga": row[16],
            "fg3m": row[17],
            "fg3a": row[18],
            "ftm": row[19],
            "fta": row[20],
        }
        if row[0]:
            result.update(
                {
                    "league": row[0],
                    "competition": row[1],
                    "season_type": row[3],
                    "source": row[21],
                    "loaded_at": row[22],
                }
            )
        return result
