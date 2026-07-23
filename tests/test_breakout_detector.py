from sports.analyzers.breakout_detector import BreakoutDetector
from models.sports.player_features import PlayerFeatures


def test_breakout_detector_name():
    
    detector = BreakoutDetector()

    assert detector.analyzer_name == "breakout_detector"

def test_detects_breakout_candidate():

    detector = BreakoutDetector()

    features = PlayerFeatures(
        season_average=15,
        recent_average=25,
        trend=10
    )

    result = detector.analyze(features)

    assert result.analyzer == "breakout_detector"
    assert result.metadata["breakout_score"] > 0