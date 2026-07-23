from sports.data.models.player_season_stats import PlayerSeasonStats
from sports.data.repositories.sqlite_player_stats_repository import (
    SQLitePlayerStatsRepository,
)
from sports.data.services.nba_refresh_service import NbaRefreshService


class FakeNbaApiSource:
    def __init__(self, rows_by_season):
        self.rows_by_season = rows_by_season
        self.calls = []

    def fetch_player_game_logs(self, season):
        self.calls.append(season)
        return list(self.rows_by_season.get(season, []))


def test_refresh_season_aggregates_logs_and_persists_results(tmp_path):
    season = "2024-25"
    rows_by_season = {
        season: [
            {
                "player_id": "player-1",
                "player_name": "Test Player One",
                "team_id": 1,
                "team_abbreviation": "T1",
                "team_name": "Team One",
                "game_id": "game-1",
                "game_date": "2025-01-01",
                "minutes": "35:30",
                "pts": 20,
                "reb": 8,
                "ast": 5,
                "fgm": 7,
                "fga": 14,
                "fg3m": 3,
                "fg3a": 7,
                "ftm": 3,
                "fta": 4,
            },
            {
                "player_id": "player-1",
                "player_name": "Test Player One",
                "team_id": 1,
                "team_abbreviation": "T1",
                "team_name": "Team One",
                "game_id": "game-2",
                "game_date": "2025-01-03",
                "minutes": "35:30",
                "pts": 18,
                "reb": 7,
                "ast": 4,
                "fgm": 6,
                "fga": 13,
                "fg3m": 2,
                "fg3a": 5,
                "ftm": 4,
                "fta": 5,
            },
            {
                "player_id": "player-2",
                "player_name": "Test Player Two",
                "team_id": 2,
                "team_abbreviation": "T2",
                "team_name": "Team Two",
                "game_id": "game-3",
                "game_date": "2025-01-02",
                "minutes": "12:00",
                "pts": 11,
                "reb": 3,
                "ast": 2,
                "fgm": 4,
                "fga": 9,
                "fg3m": 1,
                "fg3a": 3,
                "ftm": 2,
                "fta": 2,
            },
        ]
    }
    source = FakeNbaApiSource(rows_by_season)
    repository = SQLitePlayerStatsRepository(tmp_path / "player_stats.db")
    service = NbaRefreshService(source, repository)

    summary = service.refresh_season(season)

    assert source.calls == [season]
    assert summary.season == season
    assert summary.games_processed == 3
    assert summary.players_saved == 2

    player_one = repository.get("player-1", season)
    player_two = repository.get("player-2", season)

    assert player_one is not None
    assert player_one == PlayerSeasonStats(
        player_id="player-1",
        player_name="Test Player One",
        season=season,
        games_played=2,
        points=38,
        rebounds=15,
        assists=9,
        minutes=71.0,
        field_goals_made=13,
        field_goals_attempted=27,
        three_points_made=5,
        three_points_attempted=12,
        free_throws_made=7,
        free_throws_attempted=9,
    )
    assert player_one.minutes == 71.0
    assert player_one.points_per_game == 19.0

    assert player_two is not None
    assert player_two == PlayerSeasonStats(
        player_id="player-2",
        player_name="Test Player Two",
        season=season,
        games_played=1,
        points=11,
        rebounds=3,
        assists=2,
        minutes=12.0,
        field_goals_made=4,
        field_goals_attempted=9,
        three_points_made=1,
        three_points_attempted=3,
        free_throws_made=2,
        free_throws_attempted=2,
    )

    second_summary = service.refresh_season(season)
    refreshed_player_one = repository.get("player-1", season)
    refreshed_player_two = repository.get("player-2", season)

    assert second_summary.season == season
    assert second_summary.games_processed == 3
    assert second_summary.players_saved == 2
    assert refreshed_player_one == player_one
    assert refreshed_player_two == player_two
