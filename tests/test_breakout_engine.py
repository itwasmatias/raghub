from analytics.engine import AnalyticsEngine
from analytics.registry import AnalyzerRegistry
from sports.analyzers.breakout_detector import BreakoutDetector
from models.sports.player_features import PlayerFeatures

def test_breakout_detector_runs_through_engine():

    registry = AnalyzerRegistry()

    detector = BreakoutDetector()

    registry.register_analyzer(detector)

    engine = AnalyticsEngine(registry)


    features = PlayerFeatures(
        season_average=15,
        recent_average=25,
        trend=10
    )

    result = engine.analyze(features)


    assert len(result.results) == 1

    assert result.results[0].analyzer == "breakout_detector"