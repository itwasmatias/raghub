from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class WeightedAverages:
    games: int
    points: float
    rebounds: float
    assists: float
    minutes: float
    field_goal_percentage: float
    three_point_percentage: float
    free_throw_percentage: float


@dataclass(frozen=True, slots=True)
class VolatilityProfile:
    standard_deviation: float
    coefficient_of_variation: float
    consistency_score: float
    hot_streak_games: int
    cold_streak_games: int
    unusually_strong_games: int
    unusually_weak_games: int


@dataclass(frozen=True, slots=True)
class RoleChangeProfile:
    minutes_change: float
    usage_change: float
    starter_change: str | None
    team_changed: bool
    injury_opportunity: bool
    short_appearance_success: bool
    signals: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CompetitionProfile:
    league: str
    competition: str
    baseline: WeightedAverages
    current: WeightedAverages
    previous: WeightedAverages | None
    recent_five: WeightedAverages
    recent_ten: WeightedAverages
    volatility: VolatilityProfile
    role_change: RoleChangeProfile


@dataclass(frozen=True, slots=True)
class PlayerIntelligence:
    player_id: str
    player_name: str
    profiles: dict[str, CompetitionProfile]
    playoff_vs_regular_ppg: float | None
    explanation: str
    evidence: list[str]
    latest_team: str = ""
