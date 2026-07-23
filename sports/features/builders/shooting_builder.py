from models.sports.player import Player
from models.sports.features.shooting import ShootingFeatures
from sports.features.builders.base import BaseFeatureBuilder


class ShootingFeatureBuilder(BaseFeatureBuilder[ShootingFeatures]):
    """Build placeholder shooting features until metrics are wired."""

    @property
    def feature_name(self) -> str:
        return "shooting"

    def build(self, player: Player) -> ShootingFeatures:
        return ShootingFeatures(
            effective_field_goal=0.0,
            true_shooting=0.0,
            shot_quality=0.0,
        )
