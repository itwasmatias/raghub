from models.sports.features.usage import UsageFeatures
from models.sports.player import Player
from sports.features.builders.base import BaseFeatureBuilder
from sports.features.builders.usage_builder import UsageFeatureBuilder


def test_usage_builder_implements_placeholder_contract() -> None:
    builder = UsageFeatureBuilder()
    player = Player(name="Test Player", team="Test Team")

    assert isinstance(builder, BaseFeatureBuilder)
    assert builder.feature_name == "usage"

    result = builder.build(player)

    assert isinstance(result, UsageFeatures)
    assert result == UsageFeatures()
