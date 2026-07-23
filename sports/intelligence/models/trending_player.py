from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TrendingPlayer:
    player_id: str
    player_name: str
    latest_team: str
    recent_five_ppg: float
    current_season_ppg: float
    previous_season_ppg: float | None
    weighted_two_season_ppg: float
    trend_score: float
    badge: str
    explanation: str
