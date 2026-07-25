from datetime import datetime, timedelta, timezone

from sports.personal.team_history import PregameTeamFeatureBuilder, TeamGame


START = datetime(2025, 5, 1, tzinfo=timezone.utc)


def game(index, home_score, away_score):
    return TeamGame(
        league="WNBA",
        event_id=str(index),
        start_time=(START + timedelta(days=index)).isoformat(),
        season="2025",
        home_team="New York Liberty" if index % 2 == 0 else "Chicago Sky",
        away_team="Chicago Sky" if index % 2 == 0 else "New York Liberty",
        home_score=home_score,
        away_score=away_score,
        neutral_site=False,
        source_url="https://example.test",
    )


def test_training_features_are_point_in_time_and_do_not_include_outcome():
    rows = PregameTeamFeatureBuilder.training_rows([
        game(0, 90, 80),
        game(2, 75, 85),
    ])

    assert rows[0]["home_season_win_pct"] == 0.5
    assert rows[1]["home_season_win_pct"] == 1.0
    assert rows[1]["away_season_win_pct"] == 0.0
    assert rows[1]["home_rest_days"] == 1
    assert rows[1]["home_season_score_diff"] == 10.0
    assert rows[1]["away_season_score_diff"] == -10.0


def test_upcoming_features_require_both_teams_and_use_only_prior_games():
    games = [game(0, 90, 80), game(2, 75, 85)]
    features = PregameTeamFeatureBuilder.upcoming_features(
        games,
        season="2025",
        home_team="New York Liberty",
        away_team="Chicago Sky",
        event_start=(START + timedelta(days=4)).isoformat(),
    )

    assert features["home_season_win_pct"] == 0.5
    assert features["away_season_win_pct"] == 0.5
    assert features["home_rest_days"] == 1
    assert features["home_season_score_diff"] == 0.0
    assert features["away_season_score_diff"] == 0.0
    assert features["home_recent_score_diff"] == 0.0
    assert features["away_recent_score_diff"] == 0.0
