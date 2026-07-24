from collections.abc import Iterable

from sports.data.models.basketball_dataset import BasketballDataset
from sports.data.repositories.sqlite_player_game_log_repository import (
    SQLitePlayerGameLogRepository,
)
from sports.intelligence.models.data_health import (
    DataHealthReport,
    DatasetHealth,
)


class DataHealthService:
    def __init__(
        self,
        repository: SQLitePlayerGameLogRepository,
        expected_datasets: Iterable[BasketballDataset],
    ) -> None:
        self.repository = repository
        self.expected_datasets = list(expected_datasets)

    def inspect(
        self,
        failed_sources: list[str] | None = None,
        timed_out_sources: list[str] | None = None,
    ) -> DataHealthReport:
        datasets: list[DatasetHealth] = []
        for expected in self.expected_datasets:
            rows = [
                row
                for row in self.repository.list_by_season(expected.season)
                if row.get("league") == expected.league
                and row.get("competition") == expected.competition
                and row.get("season_type") == expected.season_type
            ]
            datasets.append(
                DatasetHealth(
                    dataset=expected,
                    games=len(
                        {
                            str(row["game_id"])
                            for row in rows
                            if row.get("game_id") is not None
                        }
                    ),
                    players=len(
                        {
                            str(row["player_id"])
                            for row in rows
                            if row.get("player_id") is not None
                        }
                    ),
                    source=self._latest(rows, "source"),
                    last_loaded_at=self._latest(rows, "loaded_at"),
                )
            )
        return DataHealthReport(
            datasets=datasets,
            failed_sources=list(failed_sources or []),
            timed_out_sources=list(timed_out_sources or []),
        )

    @staticmethod
    def _latest(rows: list[dict], key: str) -> str | None:
        values = [str(row[key]) for row in rows if row.get(key)]
        return max(values) if values else None
