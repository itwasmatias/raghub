from dataclasses import dataclass, field

from sports.data.models.basketball_dataset import BasketballDataset


@dataclass(frozen=True, slots=True)
class DatasetHealth:
    dataset: BasketballDataset
    games: int
    players: int
    source: str | None
    last_loaded_at: str | None

    @property
    def loaded(self) -> bool:
        return self.games > 0 and self.players > 0


@dataclass(frozen=True, slots=True)
class DataHealthReport:
    datasets: list[DatasetHealth]
    failed_sources: list[str] = field(default_factory=list)
    timed_out_sources: list[str] = field(default_factory=list)

    @property
    def loaded_datasets(self) -> list[DatasetHealth]:
        return [item for item in self.datasets if item.loaded]

    @property
    def missing_datasets(self) -> list[DatasetHealth]:
        return [item for item in self.datasets if not item.loaded]

    @property
    def games(self) -> int:
        return sum(item.games for item in self.loaded_datasets)

    @property
    def players(self) -> int:
        return sum(item.players for item in self.loaded_datasets)

    @property
    def last_successful_refresh(self) -> str | None:
        values = [
            item.last_loaded_at
            for item in self.loaded_datasets
            if item.last_loaded_at
        ]
        return max(values) if values else None

    @property
    def confidence_ready(self) -> bool:
        return (
            bool(self.loaded_datasets)
            and not self.missing_datasets
            and not self.failed_sources
            and not self.timed_out_sources
        )
