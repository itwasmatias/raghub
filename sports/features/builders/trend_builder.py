from models.sports.features.trend import TrendFeatures
from models.sports.player import Player
from sports.features.builders.base import BaseFeatureBuilder


class TrendFeatureBuilder(BaseFeatureBuilder[TrendFeatures]):
    """Build scoring trend features from a player's performance history."""

    recent_window = 5

    @property
    def feature_name(self) -> str:
        return "trend"

    def build(self, player: Player) -> TrendFeatures:
        history = player.performance_history or []

        if not history:
            return TrendFeatures(
                season_average=0.0,
                recent_average=0.0,
                trend=0.0,
            )

        season_average = sum(history) / len(history)
        recent_history = history[-self.recent_window :]
        recent_average = sum(recent_history) / len(recent_history)
        trend = recent_average - season_average

        return TrendFeatures(
            season_average=season_average,
            recent_average=recent_average,
            trend=trend,
        )
