from dataclasses import asdict

from models.sports.player import Player
from models.sports.features.player_feature_view import PlayerFeatureView
from models.sports.features.shooting import ShootingFeatures
from models.sports.features.trend import TrendFeatures
from models.sports.features.usage import UsageFeatures
from sports.features.builders.base import BaseFeatureBuilder
from sports.features.builders.shooting_builder import ShootingFeatureBuilder
from sports.features.builders.trend_builder import TrendFeatureBuilder
from sports.features.builders.usage_builder import UsageFeatureBuilder
from sports.features.facade import FeatureFacade
from sports.features.registry import FeatureRegistry


def test_feature_facade_builds_trend_features():
    registry = FeatureRegistry()
    registry.register_builder(TrendFeatureBuilder())

    player = Player(
        name="Test Player",
        team="Test Team",
        performance_history=[5, 10, 15, 20, 25, 30],
    )

    facade = FeatureFacade(registry)
    view = facade.build(player, ["trend"])

    assert isinstance(view, PlayerFeatureView)
    assert isinstance(view.trend, TrendFeatures)
    assert view.trend.season_average == 17.5
    assert view.trend.recent_average == 20.0
    assert view.trend.trend == 2.5
    assert view.shooting is None
    assert view.usage is None


def test_feature_facade_builds_zero_trend_for_empty_history():
    registry = FeatureRegistry()
    registry.register_builder(TrendFeatureBuilder())

    player = Player(name="Test Player", team="Test Team")
    view = FeatureFacade(registry).build(player, ["trend"])

    assert view.trend == TrendFeatures(
        season_average=0.0,
        recent_average=0.0,
        trend=0.0,
    )


def test_feature_facade_preserves_a_future_builder_result():
    expected = ShootingFeatures(effective_field_goal=0.55)

    class ShootingProbeBuilder(BaseFeatureBuilder[ShootingFeatures]):
        @property
        def feature_name(self) -> str:
            return "shooting"

        def build(self, player: Player) -> ShootingFeatures:
            return expected

    registry = FeatureRegistry()
    registry.register_builder(ShootingProbeBuilder())

    player = Player(name="Test Player", team="Test Team")
    view = FeatureFacade(registry).build(player, ["shooting"])

    assert view.shooting is expected
    assert view.trend is None
    assert view.usage is None


def test_feature_facade_keeps_unknown_plugin_fields_extensible():
    class CustomProbeBuilder(BaseFeatureBuilder[str]):
        @property
        def feature_name(self) -> str:
            return "custom_metric"

        def build(self, player: Player) -> str:
            return f"{player.name}:ready"

    registry = FeatureRegistry()
    registry.register_builder(CustomProbeBuilder())

    player = Player(name="Test Player", team="Test Team")
    view = FeatureFacade(registry).build(player, ["custom_metric"])

    assert view.custom_metric == "Test Player:ready"
    assert view.extra_features == {"custom_metric": "Test Player:ready"}
    assert asdict(view)["extra_features"] == {"custom_metric": "Test Player:ready"}


def test_feature_facade_builds_trend_shooting_and_usage_features():
    registry = FeatureRegistry()
    registry.register_builder(TrendFeatureBuilder())
    registry.register_builder(ShootingFeatureBuilder())
    registry.register_builder(UsageFeatureBuilder())

    player = Player(
        name="Test Player",
        team="Test Team",
        performance_history=[10, 12, 15, 18, 20],
    )
    view = FeatureFacade(registry).build(player, ["trend", "shooting", "usage"])

    assert isinstance(view.trend, TrendFeatures)
    assert isinstance(view.shooting, ShootingFeatures)
    assert isinstance(view.usage, UsageFeatures)
