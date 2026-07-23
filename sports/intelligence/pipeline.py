from analytics.engine import AnalyticsEngine
from models.analytics_result import AnalyticsResult
from models.sports.features.player_feature_view import PlayerFeatureView
from models.sports.player import Player
from sports.features.facade import FeatureFacade


class SportsIntelligencePipeline:
    def __init__(
        self,
        feature_facade: FeatureFacade,
        analytics_engine: AnalyticsEngine,
    ) -> None:
        self.feature_facade = feature_facade
        self.analytics_engine = analytics_engine

    def analyze(
        self,
        player: Player,
        feature_names: list[str],
    ) -> AnalyticsResult:
        feature_view: PlayerFeatureView = self.feature_facade.build(
            player,
            feature_names,
        )
        return self.analytics_engine.analyze(feature_view)
