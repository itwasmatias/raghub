from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class BasketballDataset:
    league: Literal["NBA", "WNBA"]
    competition: Literal["regular", "playoffs", "summer_league"]
    season: str
    season_type: str
