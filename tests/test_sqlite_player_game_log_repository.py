from pathlib import Path

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

    repository.save_many(
        [make_log("p1", "g1", "2025-01-01", points=25)]
    )

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
