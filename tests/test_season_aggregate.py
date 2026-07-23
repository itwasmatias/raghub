import pytest

from sports.data.models.player_season_stats import PlayerSeasonStats
from sports.data.services.season_aggregate import combine_seasons


def test_player_season_stats_calculates_averages_and_percentages() -> None:
    stats = PlayerSeasonStats(
        player_id="player-1",
        player_name="Test Player",
        season="2024-25",
        games_played=2,
        points=30,
        rebounds=12,
        assists=8,
        minutes=60,
        field_goals_made=10,
        field_goals_attempted=20,
        three_points_made=3,
        three_points_attempted=10,
        free_throws_made=7,
        free_throws_attempted=8,
    )

    assert stats.points_per_game == 15.0
    assert stats.rebounds_per_game == 6.0
    assert stats.assists_per_game == 4.0
    assert stats.minutes_per_game == 30.0
    assert stats.field_goal_percentage == 0.5
    assert stats.three_point_percentage == 0.3
    assert stats.free_throw_percentage == 0.875


def test_player_season_stats_returns_zero_for_empty_denominators() -> None:
    stats = PlayerSeasonStats(
        player_id="player-1",
        player_name="Test Player",
        season="2024-25",
        games_played=0,
        points=0,
        rebounds=0,
        assists=0,
        minutes=0,
        field_goals_made=0,
        field_goals_attempted=0,
        three_points_made=0,
        three_points_attempted=0,
        free_throws_made=0,
        free_throws_attempted=0,
    )

    assert stats.points_per_game == 0.0
    assert stats.field_goal_percentage == 0.0
    assert stats.three_point_percentage == 0.0
    assert stats.free_throw_percentage == 0.0


def test_combine_seasons_sums_totals_before_calculating_rates() -> None:
    first = PlayerSeasonStats(
        player_id="player-1",
        player_name="Test Player",
        season="2023-24",
        games_played=10,
        points=100,
        rebounds=40,
        assists=30,
        minutes=300,
        field_goals_made=1,
        field_goals_attempted=2,
        three_points_made=1,
        three_points_attempted=2,
        free_throws_made=1,
        free_throws_attempted=2,
    )
    second = PlayerSeasonStats(
        player_id="player-1",
        player_name="Test Player",
        season="2024-25",
        games_played=20,
        points=300,
        rebounds=80,
        assists=90,
        minutes=700,
        field_goals_made=1,
        field_goals_attempted=8,
        three_points_made=1,
        three_points_attempted=8,
        free_throws_made=1,
        free_throws_attempted=8,
    )

    combined = combine_seasons([first, second])

    assert combined.player_id == "player-1"
    assert combined.player_name == "Test Player"
    assert combined.season == "combined"
    assert combined.games_played == 30
    assert combined.points == 400
    assert combined.rebounds == 120
    assert combined.assists == 120
    assert combined.minutes == 1000
    assert combined.field_goals_made == 2
    assert combined.field_goals_attempted == 10
    assert combined.three_points_made == 2
    assert combined.three_points_attempted == 10
    assert combined.free_throws_made == 2
    assert combined.free_throws_attempted == 10
    assert combined.points_per_game == pytest.approx(400 / 30)
    assert combined.field_goal_percentage == 0.2
    assert combined.three_point_percentage == 0.2
    assert combined.free_throw_percentage == 0.2


def test_combine_seasons_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="at least one season"):
        combine_seasons([])
