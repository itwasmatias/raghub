from collections.abc import Iterable

from sports.data.models.player_season_stats import PlayerSeasonStats


def combine_seasons(
    stats: Iterable[PlayerSeasonStats],
) -> PlayerSeasonStats:
    seasons = list(stats)
    if not seasons:
        raise ValueError("stats must contain at least one season")

    first = seasons[0]
    return PlayerSeasonStats(
        player_id=first.player_id,
        player_name=first.player_name,
        season="combined",
        games_played=sum(item.games_played for item in seasons),
        points=sum(item.points for item in seasons),
        rebounds=sum(item.rebounds for item in seasons),
        assists=sum(item.assists for item in seasons),
        minutes=sum(item.minutes for item in seasons),
        field_goals_made=sum(item.field_goals_made for item in seasons),
        field_goals_attempted=sum(
            item.field_goals_attempted for item in seasons
        ),
        three_points_made=sum(item.three_points_made for item in seasons),
        three_points_attempted=sum(
            item.three_points_attempted for item in seasons
        ),
        free_throws_made=sum(item.free_throws_made for item in seasons),
        free_throws_attempted=sum(
            item.free_throws_attempted for item in seasons
        ),
    )
