from datetime import datetime, timezone
from typing import Any, Protocol

from sports.data.models.basketball_dataset import BasketballDataset
from sports.data.models.bootstrap_summary import BootstrapSummary


class HistoricalGameLogSource(Protocol):
    def fetch_player_game_logs(
        self,
        season: str,
        league_id: str,
        season_type: str,
    ) -> list[dict[str, Any]]: ...


class HistoricalGameLogRepository(Protocol):
    def save_many(self, logs: list[dict[str, Any]]) -> None: ...


class HistoricalBasketballLoader:
    DATASETS: tuple[BasketballDataset, ...] = (
        BasketballDataset("NBA", "regular", "2023-24", "Regular Season"),
        BasketballDataset("NBA", "playoffs", "2023-24", "Playoffs"),
        BasketballDataset("NBA", "regular", "2024-25", "Regular Season"),
        BasketballDataset("NBA", "playoffs", "2024-25", "Playoffs"),
        BasketballDataset("NBA", "regular", "2025-26", "Regular Season"),
        BasketballDataset("NBA", "playoffs", "2025-26", "Playoffs"),
        BasketballDataset("WNBA", "regular", "2024", "Regular Season"),
        BasketballDataset("WNBA", "playoffs", "2024", "Playoffs"),
        BasketballDataset("WNBA", "regular", "2025", "Regular Season"),
        BasketballDataset("WNBA", "playoffs", "2025", "Playoffs"),
        BasketballDataset("WNBA", "regular", "2026", "Regular Season"),
        BasketballDataset("NBA", "summer_league", "2024", "Regular Season"),
        BasketballDataset("NBA", "summer_league", "2025", "Regular Season"),
        BasketballDataset("NBA", "summer_league", "2026", "Regular Season"),
    )

    def __init__(
        self,
        source: HistoricalGameLogSource,
        game_log_repository: HistoricalGameLogRepository,
    ) -> None:
        self.source = source
        self.game_log_repository = game_log_repository

    def preload(self) -> BootstrapSummary:
        loaded = 0
        games_loaded = 0
        empty: list[BasketballDataset] = []
        unsupported: list[BasketballDataset] = []
        failed: list[BasketballDataset] = []

        for dataset in self.DATASETS:
            try:
                rows = self.source.fetch_player_game_logs(
                    dataset.season,
                    self._league_id(dataset),
                    dataset.season_type,
                )
            except NotImplementedError:
                unsupported.append(dataset)
                continue
            except Exception:
                failed.append(dataset)
                continue

            if not rows:
                empty.append(dataset)
                continue

            loaded_at = datetime.now(timezone.utc).isoformat()
            self.game_log_repository.save_many(
                [
                    {
                        **row,
                        "league": dataset.league,
                        "competition": dataset.competition,
                        "season": dataset.season,
                        "season_type": dataset.season_type,
                        "source": "nba_api",
                        "loaded_at": loaded_at,
                    }
                    for row in rows
                ]
            )
            loaded += 1
            games_loaded += len(rows)

        return BootstrapSummary(
            datasets_requested=len(self.DATASETS),
            datasets_loaded=loaded,
            games_loaded=games_loaded,
            empty_datasets=empty,
            unsupported_datasets=unsupported,
            failed_datasets=failed,
        )

    @staticmethod
    def _league_id(dataset: BasketballDataset) -> str:
        if dataset.competition == "summer_league":
            return "15"
        if dataset.league == "WNBA":
            return "10"
        return "00"
