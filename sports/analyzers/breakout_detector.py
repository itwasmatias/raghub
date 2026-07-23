from analytics.base import BaseAnalyzer
from models.analysis_result import AnalysisResult
from models.sports.player_features import PlayerFeatures


class BreakoutDetector(BaseAnalyzer):

    @property
    def analyzer_name(self):
        return "breakout_detector"


    def analyze(self, features: PlayerFeatures):

        performance_signal = 0

        if features.season_average:
            performance_signal = (
                features.recent_average -
                features.season_average
            ) / features.season_average


        breakout_score = performance_signal


        return AnalysisResult(
            analyzer=self.analyzer_name,
            summary="Breakout candidate analysis completed",
            confidence=min(
                max(breakout_score, 0),
                1
            ),
            metadata={
                "breakout_score": breakout_score,
                "performance_signal": performance_signal,
                "trend": features.trend
            }
        )