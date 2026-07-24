from collections import defaultdict
from collections.abc import Mapping
import math
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
        third_season = self._previous_season(previous_season)
        third_by_player = (
            {
                stats.player_id: stats
                for stats in self.repository.list_by_season(third_season)
            }
            if third_season is not None
            else {}
        )
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
            third = third_by_player.get(stats.player_id)
            playoff_ppg = self._playoff_ppg(player_logs)
            playoff_delta_ppg = (
                playoff_ppg - stats.points_per_game if playoff_ppg is not None else None
            )
            volatility_score = self._volatility(player_logs)
            consistency_score = self._consistency(volatility_score)
            trend_score = (
                recent_five_ppg
                - stats.points_per_game
                + (playoff_delta_ppg * 0.25 if playoff_delta_ppg is not None else 0.0)
            )
            badge = self._badge(trend_score)

            ranked.append(
                TrendingPlayer(
                    player_id=stats.player_id,
                    player_name=stats.player_name,
                    latest_team=self._team_name(player_logs[-1]),
                    recent_five_ppg=recent_five_ppg,
                    current_season_ppg=stats.points_per_game,
                    previous_season_ppg=(
                        previous.points_per_game if previous is not None else None
                    ),
                    weighted_two_season_ppg=self._weighted_ppg(
                        stats,
                        previous,
                    ),
                    weighted_three_season_ppg=self._weighted_ppg(
                        stats,
                        previous,
                        third,
                    ),
                    playoff_ppg=playoff_ppg,
                    playoff_delta_ppg=playoff_delta_ppg,
                    volatility_score=volatility_score,
                    consistency_score=consistency_score,
                    trend_score=trend_score,
                    badge=badge,
                    explanation=(
                        f"Recent five PPG is {recent_five_ppg:.1f}, "
                        f"weighted three-season baseline is "
                        f"{self._weighted_ppg(stats, previous, third):.1f}, "
                        f"playoff delta is "
                        f"{(playoff_delta_ppg if playoff_delta_ppg is not None else 0.0):+.1f}, "
                        f"consistency is {consistency_score:.1f}, and total "
                        f"trend score is {trend_score:+.1f}."
                    ),
                )
            )

        ranked.sort(key=lambda player: player.trend_score, reverse=True)
        return ranked[:limit]

    @staticmethod
    def _weighted_ppg(
        current: PlayerSeasonStats,
        previous: PlayerSeasonStats | None,
        third: PlayerSeasonStats | None = None,
    ) -> float:
        seasons = [current]
        if previous is not None:
            seasons.append(previous)
        if third is not None:
            seasons.append(third)

        games_played = sum(season.games_played for season in seasons)
        if games_played == 0:
            return 0.0
        return sum(season.points for season in seasons) / games_played

    @staticmethod
    def _badge(trend_score: float) -> str:
        if trend_score > 1.0:
            return "rising"
        if trend_score < -1.0:
            return "declining"
        return "stable"

    @staticmethod
    def _team_name(log: Mapping[str, Any]) -> str:
        return str(log.get("team_name") or log.get("team_abbreviation") or "")

    @staticmethod
    def _number(value: Any) -> float:
        if value is None:
            return 0.0
        return float(value)

    @staticmethod
    def _previous_season(season: str) -> str | None:
        if "-" not in season:
            return None
        first, second = season.split("-", maxsplit=1)
        if (
            len(first) != 4
            or len(second) != 2
            or not first.isdigit()
            or not second.isdigit()
        ):
            return None
        start = int(first) - 1
        end = int(second) - 1
        if end < 0:
            end += 100
        return f"{start}-{end:02d}"

    @classmethod
    def _playoff_ppg(cls, logs: list[Mapping[str, Any]]) -> float | None:
        playoff_points = [
            cls._number(log.get("pts"))
            for log in logs
            if str(log.get("competition") or "").lower() == "playoffs"
            or "playoff" in str(log.get("season_type") or "").lower()
        ]
        if not playoff_points:
            return None
        return sum(playoff_points) / len(playoff_points)

    @classmethod
    def _volatility(cls, logs: list[Mapping[str, Any]]) -> float:
        values = [cls._number(log.get("pts")) for log in logs]
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        return math.sqrt(variance)

    @staticmethod
    def _consistency(volatility_score: float) -> float:
        return 100.0 / (1.0 + max(0.0, volatility_score))
