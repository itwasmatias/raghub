from dataclasses import dataclass, field
from typing import Any


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
class AdvancedPlayerFeatures:
    ewma_points: float
    normalized_trend_score: float
    effective_field_goal_percentage: float
    true_shooting_percentage: float
    usage_rate: float
    minutes_trend: float
    shot_volume_trend: float
    assist_opportunity: float
    rebound_opportunity: float
    turnover_pressure: float
    opponent_defensive_adjustment: float
    pace_adjustment: float
    role_stability: float
    expected_minutes: float
    expected_usage: float
    replacement_opportunity: float
    bench_opportunity_score: float


@dataclass(frozen=True, slots=True)
class CompetitionProfile:
    league: str
    competition: str
    baseline: WeightedAverages
    current: WeightedAverages
    previous: WeightedAverages | None
    recent_three: WeightedAverages
    recent_five: WeightedAverages
    recent_ten: WeightedAverages
    splits: dict[str, WeightedAverages]
    advanced: AdvancedPlayerFeatures
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
    qualitative_evidence: list[dict[str, Any]] = field(default_factory=list)
