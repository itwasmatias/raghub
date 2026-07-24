from pathlib import Path
import sqlite3

from sports.data.repositories.sqlite_player_game_log_repository import (
    SQLitePlayerGameLogRepository,
)


def make_log(
    player_id: str,
    game_id: str,
    game_date: str,
    *,
    season: str = "2024-25",
    points: int = 20,
    minutes: str | float = "35:30",
) -> dict[str, object]:
    return {
        "player_id": player_id,
        "player_name": f"Player {player_id}",
        "season": season,
        "team_id": 1,
        "team_abbreviation": "TST",
        "team_name": "Test Team",
        "game_id": game_id,
        "game_date": game_date,
        "minutes": minutes,
        "pts": points,
        "reb": 8,
        "ast": 5,
        "fgm": 7,
        "fga": 14,
        "fg3m": 3,
        "fg3a": 7,
        "ftm": 3,
        "fta": 4,
    }


def test_save_many_and_list_by_season(tmp_path: Path) -> None:
    repository = SQLitePlayerGameLogRepository(tmp_path / "logs.db")
    first = make_log("p1", "g1", "2025-01-01")
    second = make_log("p2", "g2", "2025-01-02", minutes=12.5)

    repository.save_many([first, second])

    assert repository.list_by_season("2024-25") == [first, second]


def test_save_many_upserts_without_duplicates(tmp_path: Path) -> None:
    repository = SQLitePlayerGameLogRepository(tmp_path / "logs.db")
    repository.save_many([make_log("p1", "g1", "2025-01-01")])

    repository.save_many([make_log("p1", "g1", "2025-01-01", points=25)])

    results = repository.list_by_season("2024-25")
    assert len(results) == 1
    assert results[0]["pts"] == 25


def test_list_by_player_filters_season_and_is_chronological(
    tmp_path: Path,
) -> None:
    repository = SQLitePlayerGameLogRepository(tmp_path / "logs.db")
    repository.save_many(
        [
            make_log("p1", "g2", "2025-01-03"),
            make_log("p2", "g3", "2025-01-02"),
            make_log("p1", "g1", "2025-01-01"),
            make_log(
                "p1",
                "old",
                "2024-01-01",
                season="2023-24",
            ),
        ]
    )

    results = repository.list_by_player("p1", "2024-25")

    assert [log["game_id"] for log in results] == ["g1", "g2"]


def test_repository_migrates_legacy_schema_missing_new_columns(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy_logs.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE player_game_logs (
                season TEXT NOT NULL,
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
                PRIMARY KEY (player_id, game_id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO player_game_logs (
                season, player_id, player_name, team_id, team_abbreviation,
                team_name, game_id, game_date, minutes, pts, reb, ast,
                fgm, fga, fg3m, fg3a, ftm, fta
            ) VALUES (
                '2024-25', 'p1', 'Legacy Player', 1, 'TST',
                'Legacy Team', 'legacy-g1', '2025-01-01', '30:00', 18, 7, 5,
                6, 12, 2, 5, 4, 5
            )
            """
        )

    repository = SQLitePlayerGameLogRepository(database_path)

    season_logs = repository.list_by_season("2024-25")
    assert len(season_logs) == 1
    assert season_logs[0]["player_id"] == "p1"
    assert season_logs[0]["game_id"] == "legacy-g1"

    repository.save_many(
        [
            {
                **make_log("p2", "g2", "2025-01-02"),
                "league": "NBA",
                "competition": "regular",
                "season_type": "Regular Season",
                "source": "test",
                "loaded_at": "2026-07-23T19:20:00+00:00",
            }
        ]
    )
    updated_logs = repository.list_by_season("2024-25")
    assert len(updated_logs) == 2
