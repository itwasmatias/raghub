from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from sports.data.models.player_season_stats import PlayerSeasonStats
from sports.data.repositories.sqlite_player_stats_repository import (
    SQLitePlayerStatsRepository,
)
from sports.intelligence.models.trending_player import TrendingPlayer


class TrendingPlayerService:
    def __init__(
        self,
        repository: SQLitePlayerStatsRepository,
    ) -> None:
        self.repository = repository

    def rank(
        self,
        current_season: str,
        previous_season: str,
        game_logs: list[dict[str, Any]],
        limit: int = 10,
    ) -> list[TrendingPlayer]:
        current_stats = self.repository.list_by_season(current_season)
        previous_by_player = {
            stats.player_id: stats
            for stats in self.repository.list_by_season(previous_season)
        }
        logs_by_player: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for log in game_logs:
            logs_by_player[str(log["player_id"])].append(log)

        ranked: list[TrendingPlayer] = []
        for stats in current_stats:
            player_logs = logs_by_player.get(stats.player_id)
            if not player_logs:
                continue

            player_logs.sort(key=lambda log: str(log.get("game_date") or ""))
            recent_logs = player_logs[-5:]
            recent_five_ppg = sum(
                self._number(log.get("pts")) for log in recent_logs
            ) / len(recent_logs)
            previous = previous_by_player.get(stats.player_id)
            trend_score = recent_five_ppg - stats.points_per_game
            badge = self._badge(trend_score)

            ranked.append(
                TrendingPlayer(
                    player_id=stats.player_id,
                    player_name=stats.player_name,
                    latest_team=self._team_name(player_logs[-1]),
                    recent_five_ppg=recent_five_ppg,
                    current_season_ppg=stats.points_per_game,
                    previous_season_ppg=(
                        previous.points_per_game
                        if previous is not None
                        else None
                    ),
                    weighted_two_season_ppg=self._weighted_ppg(
                        stats,
                        previous,
                    ),
                    trend_score=trend_score,
                    badge=badge,
                    explanation=(
                        f"Recent five PPG is {recent_five_ppg:.1f}, "
                        f"{trend_score:+.1f} versus the current-season "
                        f"average of {stats.points_per_game:.1f}."
                    ),
                )
            )

        ranked.sort(key=lambda player: player.trend_score, reverse=True)
        return ranked[:limit]

    @staticmethod
    def _weighted_ppg(
        current: PlayerSeasonStats,
        previous: PlayerSeasonStats | None,
    ) -> float:
        if previous is None:
            return current.points_per_game

        games_played = current.games_played + previous.games_played
        if games_played == 0:
            return 0.0
        return (current.points + previous.points) / games_played

    @staticmethod
    def _badge(trend_score: float) -> str:
        if trend_score > 1.0:
            return "rising"
        if trend_score < -1.0:
            return "declining"
        return "stable"

    @staticmethod
    def _team_name(log: Mapping[str, Any]) -> str:
        return str(
            log.get("team_name")
            or log.get("team_abbreviation")
            or ""
        )

    @staticmethod
    def _number(value: Any) -> float:
        if value is None:
            return 0.0
        return float(value)
