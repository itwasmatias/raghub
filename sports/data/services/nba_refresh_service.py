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

    def get(
        self,
        player_id: str,
        season: str,
    ) -> PlayerSeasonStats | None: ...


class RefreshStateRepository(Protocol):
    def get_last_processed_date(self, season: str) -> str | None: ...

    def save_last_processed_date(self, season: str, date: str) -> None: ...


class NbaRefreshService:
    def __init__(
        self,
        source: PlayerGameLogSource,
        repository: PlayerStatsRepository,
        state_repository: RefreshStateRepository | None = None,
    ) -> None:
        self.source = source
        self.repository = repository
        self.state_repository = state_repository

    def refresh_season(self, season: str) -> RefreshSummary:
        rows = self.source.fetch_player_game_logs(season)
        return self._save_rows(season, rows, add_to_existing=False)

    def refresh_incremental(self, season: str) -> RefreshSummary:
        if self.state_repository is None:
            raise ValueError(
                "state_repository is required for incremental refresh"
            )

        rows = self.source.fetch_player_game_logs(season)
        checkpoint = self.state_repository.get_last_processed_date(season)
        if checkpoint is None:
            new_rows = rows
        else:
            new_rows = [
                row
                for row in rows
                if str(row.get("game_date") or "") > checkpoint
            ]

        if not new_rows:
            return RefreshSummary(
                season=season,
                games_processed=0,
                players_saved=0,
            )

        summary = self._save_rows(
            season,
            new_rows,
            add_to_existing=True,
        )
        processed_dates = [
            str(row["game_date"])
            for row in new_rows
            if row.get("game_date") is not None
        ]
        if processed_dates:
            self.state_repository.save_last_processed_date(
                season,
                max(processed_dates),
            )
        return summary

    def _save_rows(
        self,
        season: str,
        rows: list[dict[str, Any]],
        *,
        add_to_existing: bool,
    ) -> RefreshSummary:
        rows_by_player: dict[str, list[Mapping[str, Any]]] = defaultdict(list)

        for row in rows:
            rows_by_player[str(row["player_id"])].append(row)

        for player_id, player_rows in rows_by_player.items():
            stats = self._aggregate_rows(player_id, season, player_rows)
            if add_to_existing:
                existing = self.repository.get(player_id, season)
                if existing is not None:
                    stats = self._add_stats(existing, stats)
            self.repository.save(stats)

        return RefreshSummary(
            season=season,
            games_processed=len(rows),
            players_saved=len(rows_by_player),
        )

    def _aggregate_rows(
        self,
        player_id: str,
        season: str,
        rows: list[Mapping[str, Any]],
    ) -> PlayerSeasonStats:
        first_row = rows[0]
        return PlayerSeasonStats(
            player_id=player_id,
            player_name=str(first_row.get("player_name") or ""),
            season=season,
            games_played=len(rows),
            points=sum(self._number(row.get("pts")) for row in rows),
            rebounds=sum(self._number(row.get("reb")) for row in rows),
            assists=sum(self._number(row.get("ast")) for row in rows),
            minutes=sum(self._minutes(row.get("minutes")) for row in rows),
            field_goals_made=sum(
                self._number(row.get("fgm")) for row in rows
            ),
            field_goals_attempted=sum(
                self._number(row.get("fga")) for row in rows
            ),
            three_points_made=sum(
                self._number(row.get("fg3m")) for row in rows
            ),
            three_points_attempted=sum(
                self._number(row.get("fg3a")) for row in rows
            ),
            free_throws_made=sum(
                self._number(row.get("ftm")) for row in rows
            ),
            free_throws_attempted=sum(
                self._number(row.get("fta")) for row in rows
            ),
        )

    @staticmethod
    def _add_stats(
        existing: PlayerSeasonStats,
        new: PlayerSeasonStats,
    ) -> PlayerSeasonStats:
        return PlayerSeasonStats(
            player_id=existing.player_id,
            player_name=new.player_name,
            season=existing.season,
            games_played=existing.games_played + new.games_played,
            points=existing.points + new.points,
            rebounds=existing.rebounds + new.rebounds,
            assists=existing.assists + new.assists,
            minutes=existing.minutes + new.minutes,
            field_goals_made=(
                existing.field_goals_made + new.field_goals_made
            ),
            field_goals_attempted=(
                existing.field_goals_attempted
                + new.field_goals_attempted
            ),
            three_points_made=(
                existing.three_points_made + new.three_points_made
            ),
            three_points_attempted=(
                existing.three_points_attempted
                + new.three_points_attempted
            ),
            free_throws_made=(
                existing.free_throws_made + new.free_throws_made
            ),
            free_throws_attempted=(
                existing.free_throws_attempted
                + new.free_throws_attempted
            ),
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
