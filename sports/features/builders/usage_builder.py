from models.sports.features.usage import UsageFeatures
from models.sports.player import Player
from sports.features.builders.base import BaseFeatureBuilder


class UsageFeatureBuilder(BaseFeatureBuilder[UsageFeatures]):
    @property
    def feature_name(self) -> str:
        return "usage"

    def build(self, player: Player) -> UsageFeatures:
        return UsageFeatures(
            minutes_change=0.0,
            usage_change=0.0,
            role_change=0.0,
        )
