from pathlib import Path
from typing import Protocol

from sports.data.models.bootstrap_summary import BootstrapSummary
from sports.data.models.refresh_summary import RefreshSummary
from sports.data.repositories.sqlite_player_game_log_repository import (
    SQLitePlayerGameLogRepository,
)
from sports.data.repositories.sqlite_player_stats_repository import (
    SQLitePlayerStatsRepository,
)
from sports.data.repositories.sqlite_refresh_state_repository import (
    SQLiteRefreshStateRepository,
)
from sports.data.services.historical_basketball_loader import (
    HistoricalBasketballLoader,
)
from sports.data.services.nba_refresh_service import NbaRefreshService
from sports.data.sources.nba_api_source import NbaApiSource
from sports.intelligence.models.trending_player import TrendingPlayer
from sports.intelligence.trending_player_service import TrendingPlayerService


class HistoryLoader(Protocol):
    def preload(self) -> BootstrapSummary: ...


class IncrementalRefreshService(Protocol):
    def refresh_incremental(self, season: str) -> RefreshSummary: ...


class PlayerRankingService(Protocol):
    def rank(
        self,
        current_season: str,
        previous_season: str,
        game_logs: list[dict],
        limit: int = 10,
    ) -> list[TrendingPlayer]: ...


class BasketballDemoRuntime:
    def __init__(
        self,
        database_path: str | Path = Path("data/sip_basketball.db"),
        *,
        source: NbaApiSource | None = None,
        player_stats_repository: SQLitePlayerStatsRepository | None = None,
        game_log_repository: SQLitePlayerGameLogRepository | None = None,
        refresh_state_repository: SQLiteRefreshStateRepository | None = None,
        historical_loader: HistoryLoader | None = None,
        refresh_service: IncrementalRefreshService | None = None,
        trending_service: PlayerRankingService | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

        self.source = source or NbaApiSource()
        self.player_stats_repository = (
            player_stats_repository
            or SQLitePlayerStatsRepository(self.database_path)
        )
        self.game_log_repository = (
            game_log_repository
            or SQLitePlayerGameLogRepository(self.database_path)
        )
        self.refresh_state_repository = (
            refresh_state_repository
            or SQLiteRefreshStateRepository(self.database_path)
        )
        self.historical_loader = historical_loader or HistoricalBasketballLoader(
            self.source,
            self.game_log_repository,
        )
        self.refresh_service = refresh_service or NbaRefreshService(
            self.source,
            self.player_stats_repository,
            self.refresh_state_repository,
            game_log_repository=self.game_log_repository,
        )
        self.trending_service = trending_service or TrendingPlayerService(
            self.player_stats_repository
        )

    def load_history(self) -> BootstrapSummary:
        return self.historical_loader.preload()

    def refresh(self) -> list[RefreshSummary]:
        summaries: list[RefreshSummary] = []
        for season in self._historical_seasons():
            if not self.game_log_repository.list_by_season(season):
                continue
            summaries.append(
                self.refresh_service.refresh_incremental(season)
            )
        return summaries

    def get_trending_players(
        self,
        current_season: str = "2025-26",
        previous_season: str = "2024-25",
        limit: int = 10,
    ) -> list[TrendingPlayer]:
        game_logs = self.game_log_repository.list_by_season(current_season)
        if not game_logs:
            return []
        return self.trending_service.rank(
            current_season,
            previous_season,
            game_logs,
            limit,
        )

    @staticmethod
    def _historical_seasons() -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                dataset.season
                for dataset in HistoricalBasketballLoader.DATASETS
            )
        )
