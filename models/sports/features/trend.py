from dataclasses import dataclass


@dataclass
class TrendFeatures:
    season_average: float
    recent_average: float
    trend: float
