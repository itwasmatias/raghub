import sqlite3
from contextlib import closing
from pathlib import Path

from sports.data.models.player_season_stats import PlayerSeasonStats


class SQLitePlayerStatsRepository:
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
                    CREATE TABLE IF NOT EXISTS player_season_stats (
                        player_id TEXT NOT NULL,
                        player_name TEXT NOT NULL,
                        season TEXT NOT NULL,
                        games_played INTEGER NOT NULL,
                        points REAL NOT NULL,
                        rebounds REAL NOT NULL,
                        assists REAL NOT NULL,
                        minutes REAL NOT NULL,
                        field_goals_made REAL NOT NULL,
                        field_goals_attempted REAL NOT NULL,
                        three_points_made REAL NOT NULL,
                        three_points_attempted REAL NOT NULL,
                        free_throws_made REAL NOT NULL,
                        free_throws_attempted REAL NOT NULL,
                        PRIMARY KEY (player_id, season)
                    )
                    """
                )

    def save(self, stats: PlayerSeasonStats) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO player_season_stats (
                        player_id,
                        player_name,
                        season,
                        games_played,
                        points,
                        rebounds,
                        assists,
                        minutes,
                        field_goals_made,
                        field_goals_attempted,
                        three_points_made,
                        three_points_attempted,
                        free_throws_made,
                        free_throws_attempted
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (player_id, season) DO UPDATE SET
                        player_name = excluded.player_name,
                        games_played = excluded.games_played,
                        points = excluded.points,
                        rebounds = excluded.rebounds,
                        assists = excluded.assists,
                        minutes = excluded.minutes,
                        field_goals_made = excluded.field_goals_made,
                        field_goals_attempted =
                            excluded.field_goals_attempted,
                        three_points_made = excluded.three_points_made,
                        three_points_attempted =
                            excluded.three_points_attempted,
                        free_throws_made = excluded.free_throws_made,
                        free_throws_attempted =
                            excluded.free_throws_attempted
                    """,
                    (
                        stats.player_id,
                        stats.player_name,
                        stats.season,
                        stats.games_played,
                        stats.points,
                        stats.rebounds,
                        stats.assists,
                        stats.minutes,
                        stats.field_goals_made,
                        stats.field_goals_attempted,
                        stats.three_points_made,
                        stats.three_points_attempted,
                        stats.free_throws_made,
                        stats.free_throws_attempted,
                    ),
                )

    def get(
        self,
        player_id: str,
        season: str,
    ) -> PlayerSeasonStats | None:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                SELECT
                    player_id,
                    player_name,
                    season,
                    games_played,
                    points,
                    rebounds,
                    assists,
                    minutes,
                    field_goals_made,
                    field_goals_attempted,
                    three_points_made,
                    three_points_attempted,
                    free_throws_made,
                    free_throws_attempted
                FROM player_season_stats
                WHERE player_id = ? AND season = ?
                """,
                (player_id, season),
            )
            row = cursor.fetchone()

        return self._from_row(row) if row is not None else None

    def list_by_season(self, season: str) -> list[PlayerSeasonStats]:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                SELECT
                    player_id,
                    player_name,
                    season,
                    games_played,
                    points,
                    rebounds,
                    assists,
                    minutes,
                    field_goals_made,
                    field_goals_attempted,
                    three_points_made,
                    three_points_attempted,
                    free_throws_made,
                    free_throws_attempted
                FROM player_season_stats
                WHERE season = ?
                ORDER BY player_id
                """,
                (season,),
            )
            rows = cursor.fetchall()

        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: tuple[object, ...]) -> PlayerSeasonStats:
        return PlayerSeasonStats(
            player_id=str(row[0]),
            player_name=str(row[1]),
            season=str(row[2]),
            games_played=int(row[3]),
            points=float(row[4]),
            rebounds=float(row[5]),
            assists=float(row[6]),
            minutes=float(row[7]),
            field_goals_made=float(row[8]),
            field_goals_attempted=float(row[9]),
            three_points_made=float(row[10]),
            three_points_attempted=float(row[11]),
            free_throws_made=float(row[12]),
            free_throws_attempted=float(row[13]),
        )
