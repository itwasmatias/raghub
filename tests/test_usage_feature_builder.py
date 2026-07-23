from models.sports.features.usage import UsageFeatures
from models.sports.player import Player
from sports.features.builders.base import BaseFeatureBuilder
from sports.features.builders.usage_builder import UsageFeatureBuilder


def test_feature_name() -> None:
    builder = UsageFeatureBuilder()

    assert isinstance(builder, BaseFeatureBuilder)
    assert builder.feature_name == "usage"


def test_build_returns_zeroed_usage_features() -> None:
    builder = UsageFeatureBuilder()
    player = Player(name="Test Player", team="Test Team")

    result = builder.build(player)

    assert isinstance(result, UsageFeatures)
    assert result.minutes_change == 0.0
    assert result.usage_change == 0.0
    assert result.role_change == 0.0
