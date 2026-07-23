from dataclasses import dataclass, field

from sports.data.models.basketball_dataset import BasketballDataset


@dataclass(frozen=True, slots=True)
class BootstrapSummary:
    datasets_requested: int
    datasets_loaded: int
    games_loaded: int
    empty_datasets: list[BasketballDataset] = field(default_factory=list)
    unsupported_datasets: list[BasketballDataset] = field(
        default_factory=list
    )
    failed_datasets: list[BasketballDataset] = field(default_factory=list)
