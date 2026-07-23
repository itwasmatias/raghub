from pathlib import Path

from sports.data.repositories.sqlite_player_stats_repository import (
    SQLitePlayerStatsRepository,
)
from sports.data.repositories.sqlite_refresh_state_repository import (
    SQLiteRefreshStateRepository,
)
from sports.data.services.nba_refresh_service import NbaRefreshService


class FakeNbaApiSource:
    def __init__(self, rows_by_season):
        self.rows_by_season = rows_by_season
        self.calls = []

    def fetch_player_game_logs(self, season):
        self.calls.append(season)
        return list(self.rows_by_season.get(season, []))


def make_game_log(
    game_id: str,
    game_date: str,
    points: int,
    rebounds: int,
    assists: int,
    minutes: str,
    field_goals_made: int,
    field_goals_attempted: int,
    three_points_made: int,
    three_points_attempted: int,
    free_throws_made: int,
    free_throws_attempted: int,
):
    return {
        "player_id": "player-1",
        "player_name": "Test Player One",
        "team_id": 1,
        "team_abbreviation": "T1",
        "team_name": "Team One",
        "game_id": game_id,
        "game_date": game_date,
        "minutes": minutes,
        "pts": points,
        "reb": rebounds,
        "ast": assists,
        "fgm": field_goals_made,
        "fga": field_goals_attempted,
        "fg3m": three_points_made,
        "fg3a": three_points_attempted,
        "ftm": free_throws_made,
        "fta": free_throws_attempted,
    }


def test_refresh_incremental_tracks_checkpoint_and_accumulates_new_games(
    tmp_path: Path,
):
    season = "2024-25"
    rows_by_season = {
        season: [
            make_game_log(
                game_id="game-1",
                game_date="2025-01-01",
                points=20,
                rebounds=8,
                assists=5,
                minutes="35:30",
                field_goals_made=7,
                field_goals_attempted=14,
                three_points_made=3,
                three_points_attempted=7,
                free_throws_made=3,
                free_throws_attempted=4,
            ),
            make_game_log(
                game_id="game-2",
                game_date="2025-01-03",
                points=18,
                rebounds=7,
                assists=4,
                minutes="35:30",
                field_goals_made=6,
                field_goals_attempted=13,
                three_points_made=2,
                three_points_attempted=5,
                free_throws_made=4,
                free_throws_attempted=5,
            ),
        ]
    }
    source = FakeNbaApiSource(rows_by_season)
    stats_repository = SQLitePlayerStatsRepository(tmp_path / "stats.db")
    state_repository = SQLiteRefreshStateRepository(tmp_path / "state.db")
    service = NbaRefreshService(source, stats_repository, state_repository)

    first_summary = service.refresh_incremental(season)

    first_stats = stats_repository.get("player-1", season)
    first_state = state_repository.get(season)

    assert first_summary.season == season
    assert first_summary.games_processed == 2
    assert first_summary.players_saved == 1
    assert first_stats is not None
    assert first_stats.games_played == 2
    assert first_stats.points == 38
    assert first_stats.rebounds == 15
    assert first_stats.assists == 9
    assert first_stats.minutes == 71.0
    assert first_stats.field_goals_made == 13
    assert first_stats.field_goals_attempted == 27
    assert first_stats.three_points_made == 5
    assert first_stats.three_points_attempted == 12
    assert first_stats.free_throws_made == 7
    assert first_stats.free_throws_attempted == 9
    assert first_state is not None
    assert first_state.last_processed_date == "2025-01-03"

    rows_by_season[season] = [
        make_game_log(
            game_id="game-1",
            game_date="2025-01-01",
            points=20,
            rebounds=8,
            assists=5,
            minutes="35:30",
            field_goals_made=7,
            field_goals_attempted=14,
            three_points_made=3,
            three_points_attempted=7,
            free_throws_made=3,
            free_throws_attempted=4,
        ),
        make_game_log(
            game_id="game-2",
            game_date="2025-01-03",
            points=18,
            rebounds=7,
            assists=4,
            minutes="35:30",
            field_goals_made=6,
            field_goals_attempted=13,
            three_points_made=2,
            three_points_attempted=5,
            free_throws_made=4,
            free_throws_attempted=5,
        ),
        make_game_log(
            game_id="game-3",
            game_date="2025-01-05",
            points=14,
            rebounds=6,
            assists=3,
            minutes="35:30",
            field_goals_made=5,
            field_goals_attempted=11,
            three_points_made=1,
            three_points_attempted=4,
            free_throws_made=3,
            free_throws_attempted=4,
        ),
    ]

    second_summary = service.refresh_incremental(season)

    second_stats = stats_repository.get("player-1", season)
    second_state = state_repository.get(season)

    assert second_summary.season == season
    assert second_summary.games_processed == 1
    assert second_summary.players_saved == 1
    assert second_stats is not None
    assert second_stats.games_played == 3
    assert second_stats.points == 52
    assert second_stats.rebounds == 21
    assert second_stats.assists == 12
    assert second_stats.minutes == 106.5
    assert second_stats.field_goals_made == 18
    assert second_stats.field_goals_attempted == 38
    assert second_stats.three_points_made == 6
    assert second_stats.three_points_attempted == 16
    assert second_stats.free_throws_made == 10
    assert second_stats.free_throws_attempted == 13
    assert second_state is not None
    assert second_state.last_processed_date == "2025-01-05"

    third_summary = service.refresh_incremental(season)

    third_stats = stats_repository.get("player-1", season)
    third_state = state_repository.get(season)

    assert third_summary.season == season
    assert third_summary.games_processed == 0
    assert third_summary.players_saved == 0
    assert third_stats == second_stats
    assert third_state == second_state
