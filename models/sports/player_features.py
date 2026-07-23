from dataclasses import dataclass


@dataclass(slots=True)
class PlayerFeatures:

    season_average: float

    recent_average: float = 0.0

    trend: float = 0.0

    variance: float = 0.0

    ewma: float = 0.0