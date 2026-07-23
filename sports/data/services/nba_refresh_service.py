from collections import defaultdict
from collections.abc import Mapping
from typing import Any, Protocol

from sports.data.models.player_season_stats import PlayerSeasonStats
from sports.data.models.refresh_summary import RefreshSummary


class PlayerGameLogSource(Protocol):
    def fetch_player_game_logs(
        self,
        season: str,
    ) -> list[dict[str, Any]]: ...


class PlayerStatsRepository(Protocol):
    def save(self, stats: PlayerSeasonStats) -> None: ...


class NbaRefreshService:
    def __init__(
        self,
        source: PlayerGameLogSource,
        repository: PlayerStatsRepository,
    ) -> None:
        self.source = source
        self.repository = repository

    def refresh_season(self, season: str) -> RefreshSummary:
        rows = self.source.fetch_player_game_logs(season)
        rows_by_player: dict[str, list[Mapping[str, Any]]] = defaultdict(list)

        for row in rows:
            rows_by_player[str(row["player_id"])].append(row)

        for player_id, player_rows in rows_by_player.items():
            first_row = player_rows[0]
            stats = PlayerSeasonStats(
                player_id=player_id,
                player_name=str(first_row.get("player_name") or ""),
                season=season,
                games_played=len(player_rows),
                points=sum(self._number(row.get("pts")) for row in player_rows),
                rebounds=sum(
                    self._number(row.get("reb")) for row in player_rows
                ),
                assists=sum(
                    self._number(row.get("ast")) for row in player_rows
                ),
                minutes=sum(
                    self._minutes(row.get("minutes")) for row in player_rows
                ),
                field_goals_made=sum(
                    self._number(row.get("fgm")) for row in player_rows
                ),
                field_goals_attempted=sum(
                    self._number(row.get("fga")) for row in player_rows
                ),
                three_points_made=sum(
                    self._number(row.get("fg3m")) for row in player_rows
                ),
                three_points_attempted=sum(
                    self._number(row.get("fg3a")) for row in player_rows
                ),
                free_throws_made=sum(
                    self._number(row.get("ftm")) for row in player_rows
                ),
                free_throws_attempted=sum(
                    self._number(row.get("fta")) for row in player_rows
                ),
            )
            self.repository.save(stats)

        return RefreshSummary(
            season=season,
            games_processed=len(rows),
            players_saved=len(rows_by_player),
        )

    @staticmethod
    def _number(value: Any) -> float:
        if value is None:
            return 0.0
        return float(value)

    @staticmethod
    def _minutes(value: Any) -> float:
        if value is None:
            return 0.0
        if isinstance(value, str):
            if not value:
                return 0.0
            if ":" in value:
                minute_text, second_text = value.split(":", maxsplit=1)
                return float(minute_text) + float(second_text) / 60
        return float(value)
