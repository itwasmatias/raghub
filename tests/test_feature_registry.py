import pytest

from sports.features.builders.trend_builder import TrendFeatureBuilder
from sports.features.registry import FeatureRegistry


def test_feature_registry_registers_builder_by_contract_name():
    registry = FeatureRegistry()
    builder = TrendFeatureBuilder()

    registry.register_builder(builder)

    assert registry.get("trend") is builder


def test_feature_registry_rejects_duplicate_contract_name():
    registry = FeatureRegistry()
    registry.register_builder(TrendFeatureBuilder())

    with pytest.raises(
        ValueError,
        match="Builder for feature 'trend' is already registered",
    ):
        registry.register_builder(TrendFeatureBuilder())


def test_feature_registry_discovers_builder_through_shared_contract():
    registry = FeatureRegistry()
    registry.register_plugin_path("sports.features.builders.trend_builder")

    registry.load_plugins()

    assert isinstance(registry.get("trend"), TrendFeatureBuilder)


def test_feature_registry_loads_each_plugin_path_once():
    registry = FeatureRegistry()
    registry.register_plugin_path("sports.features.builders.trend_builder")

    registry.load_plugins()
    registry.load_plugins()

    assert registry.list_features() == ["trend"]
