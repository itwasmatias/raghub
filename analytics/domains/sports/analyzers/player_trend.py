from analytics.base import BaseAnalyzer
from models.analysis_result import AnalysisResult
from models.sports.features.player_feature_view import PlayerFeatureView


class PlayerTrendAnalyzer(BaseAnalyzer):

    @property
    def analyzer_name(self) -> str:
        return "sports_player_trend"

    def analyze(self, research_result) -> AnalysisResult:
        
        if research_result is None:
            return AnalysisResult(
                analyzer=self.analyzer_name,
                summary="Player trend analysis pending.",
                confidence=0.0,
                metadata={}
            )

        if isinstance(research_result, PlayerFeatureView):
            trend_features = research_result.trend
            trend_score = trend_features.trend

            if trend_score > 0:
                trend = "rising"
            elif trend_score < 0:
                trend = "declining"
            else:
                trend = "stable"

            return AnalysisResult(
                analyzer=self.analyzer_name,
                summary=f"Player performance trend: {trend}",
                confidence=0.5,
                metadata={
                    "trend": trend,
                    "trend_score": trend_score,
                    "season_average": trend_features.season_average,
                    "recent_average": trend_features.recent_average,
                },
            )

        history = research_result.performance_history
        season_average = sum(history) / len(history)

        recent_values = history[-3:]
        recent_average = sum(recent_values) / len(recent_values)

        playoff_history = getattr(
            research_result,
            "playoff_history",
            []
        )
        features_present = 1

        if playoff_history:
            features_present += 1

        feature_completeness = features_present / 2

        confidence = 0.5

        if playoff_history:
            confidence += 0.1

        playoff_average = (
            sum(playoff_history) / len(playoff_history)
            if playoff_history
            else None
        )

        if history[-1] > history[0]:
            trend = "rising"

        elif history[-1] < history[0]:
            trend = "declining"

        else:
            trend = "stable"
            
        trend_score = history[-1] - history[0]
        first_value = history[0]

        normalized_trend_score = (
            (history[-1] - first_value) / first_value
            if first_value != 0
            else 0.0
        )
        recent_values = history[-3:]
        recent_average = sum(recent_values) / len(recent_values)
        mean = season_average

        variance = sum(
            (value - mean) ** 2 for value in history
        ) / len(history)

        volatility = variance ** 0.5
        
        alpha = 0.5

        ewma = history[0]

        for value in history[1:]:
            ewma = alpha * value + (1 - alpha) * ewma

        return AnalysisResult(
            analyzer=self.analyzer_name,
            summary=f"Player performance trend: {trend}",
            confidence=confidence,
            metadata={
                "trend": trend,
                "trend_score": trend_score,
                "ewma": ewma,
                "recent_average": recent_average,
                "season_average": season_average,
                "playoff_average": playoff_average,
                "normalized_trend_score": normalized_trend_score,
                "volatility": volatility,
                "feature_completeness": feature_completeness,
            }
        )
