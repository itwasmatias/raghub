from analytics.registry import AnalyzerRegistry
from sports.analyzers.breakout_detector import BreakoutDetector


def test_breakout_detector_registration():

    registry = AnalyzerRegistry()

    detector = BreakoutDetector()

    registry.register_analyzer(detector)

    result = registry.get("breakout_detector")

    assert result == detector