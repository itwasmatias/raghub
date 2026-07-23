from pathlib import Path

from sports.data.models.player_season_stats import PlayerSeasonStats
from sports.data.repositories.sqlite_player_stats_repository import (
    SQLitePlayerStatsRepository,
)


def make_stats(
    player_id: str = "player-1",
    player_name: str = "Test Player",
    season: str = "2024-25",
    points: float = 100.0,
) -> PlayerSeasonStats:
    return PlayerSeasonStats(
        player_id=player_id,
        player_name=player_name,
        season=season,
        games_played=10,
        points=points,
        rebounds=40,
        assists=30,
        minutes=300,
        field_goals_made=35,
        field_goals_attempted=80,
        three_points_made=10,
        three_points_attempted=30,
        free_throws_made=20,
        free_throws_attempted=25,
    )


def test_save_and_get_round_trip(tmp_path: Path) -> None:
    repository = SQLitePlayerStatsRepository(tmp_path / "stats.db")
    expected = make_stats()

    repository.save(expected)

    assert repository.get("player-1", "2024-25") == expected


def test_save_upserts_by_player_and_season(tmp_path: Path) -> None:
    repository = SQLitePlayerStatsRepository(tmp_path / "stats.db")
    repository.save(make_stats(points=100))

    repository.save(make_stats(player_name="Updated Player", points=250))

    result = repository.get("player-1", "2024-25")
    assert result is not None
    assert result.player_name == "Updated Player"
    assert result.points == 250
    assert len(repository.list_by_season("2024-25")) == 1


def test_get_returns_none_when_stats_are_missing(tmp_path: Path) -> None:
    repository = SQLitePlayerStatsRepository(tmp_path / "stats.db")

    assert repository.get("missing", "2024-25") is None


def test_list_by_season_returns_only_matching_players(
    tmp_path: Path,
) -> None:
    repository = SQLitePlayerStatsRepository(tmp_path / "stats.db")
    repository.save(make_stats(player_id="player-2", player_name="Second"))
    repository.save(make_stats(player_id="player-1", player_name="First"))
    repository.save(
        make_stats(
            player_id="player-3",
            player_name="Other Season",
            season="2023-24",
        )
    )

    results = repository.list_by_season("2024-25")

    assert [item.player_id for item in results] == ["player-1", "player-2"]
