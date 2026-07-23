from analytics.domains.sports.analyzers.player_trend import PlayerTrendAnalyzer
from analytics.engine import AnalyticsEngine
from analytics.registry import AnalyzerRegistry
from models.analytics_result import AnalyticsResult
from models.sports.player import Player
from sports.intelligence.pipeline import SportsIntelligencePipeline
from sports.features.builders.trend_builder import TrendFeatureBuilder
from sports.features.facade import FeatureFacade
from sports.features.registry import FeatureRegistry


def test_sports_intelligence_pipeline_returns_rising_trend_result():
    feature_registry = FeatureRegistry()
    feature_registry.register_builder(TrendFeatureBuilder())
    facade = FeatureFacade(feature_registry)

    analyzer_registry = AnalyzerRegistry()
    analyzer_registry.register_analyzer(PlayerTrendAnalyzer())
    engine = AnalyticsEngine(analyzer_registry)

    pipeline = SportsIntelligencePipeline(facade, engine)

    player = Player(
        name="Test Player",
        team="Test Team",
        performance_history=[5, 5, 5, 10, 12, 14],
    )

    result = pipeline.analyze(player, ["trend"])

    assert isinstance(result, AnalyticsResult)
    assert len(result.results) == 1
    assert result.results[0].metadata["trend"] == "rising"
