from models.sports.player import Player
from models.sports.features.shooting import ShootingFeatures
from sports.features.builders.base import BaseFeatureBuilder
from sports.features.builders.shooting_builder import ShootingFeatureBuilder


def test_shooting_builder_implements_placeholder_contract() -> None:
    builder = ShootingFeatureBuilder()
    player = Player(name="Test Player", team="Test Team")

    assert isinstance(builder, BaseFeatureBuilder)
    assert builder.feature_name == "shooting"

    result = builder.build(player)

    assert isinstance(result, ShootingFeatures)
    assert result.effective_field_goal == 0.0
    assert result.true_shooting == 0.0
    assert result.shot_quality == 0.0
