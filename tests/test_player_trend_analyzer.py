from analytics.domains.sports.analyzers.player_trend import PlayerTrendAnalyzer
from models.analysis_result import AnalysisResult
from models.sports.features.player_feature_view import PlayerFeatureView
from models.sports.features.trend import TrendFeatures
from models.sports.player import Player


class FakeResearchResult:
    def __init__(self, performance_history, playoff_history=None):
        self.performance_history = performance_history
        self.playoff_history = playoff_history or []


def test_player_trend_analyzer_returns_analysis_result():
    analyzer = PlayerTrendAnalyzer()

    result = analyzer.analyze(None)

    assert isinstance(result, AnalysisResult)


def test_player_trend_detects_improving_performance():
    analyzer = PlayerTrendAnalyzer()

    player = Player(
        name="Test Player", team="Test Team", performance_history=[10, 12, 15]
    )

    result = analyzer.analyze(player)

    assert result.metadata["trend"] == "rising"


def test_player_trend_detects_declining_performance():

    analyzer = PlayerTrendAnalyzer()

    player = Player(
        name="Test Player", team="Test Team", performance_history=[15, 12, 10]
    )

    result = analyzer.analyze(player)

    assert result.metadata["trend"] == "declining"


def test_player_trend_calculates_trend_score():

    analyzer = PlayerTrendAnalyzer()

    player = Player(
        name="Test Player", team="Test Team", performance_history=[10, 12, 15]
    )

    result = analyzer.analyze(player)

    assert result.metadata["trend_score"] == 5


def test_player_trend_includes_recent_average():

    player = FakeResearchResult(performance_history=[10, 12, 14, 16])

    analyzer = PlayerTrendAnalyzer()

    result = analyzer.analyze(player)

    assert result.metadata["recent_average"] == 14


def test_player_trend_includes_performance_windows():

    player = FakeResearchResult(
        performance_history=[10, 12, 14, 16], playoff_history=[20, 22]
    )

    analyzer = PlayerTrendAnalyzer()

    result = analyzer.analyze(player)

    assert result.metadata["season_average"] == 13
    assert result.metadata["recent_average"] == 14
    assert result.metadata["playoff_average"] == 21


def test_player_trend_calculates_normalized_trend_score():

    analyzer = PlayerTrendAnalyzer()

    player = Player(
        name="Test Player", team="Test Team", performance_history=[10, 15, 20]
    )

    result = analyzer.analyze(player)

    assert result.metadata["normalized_trend_score"] == 1.0


def test_player_trend_calculates_volatility():

    analyzer = PlayerTrendAnalyzer()

    player = Player(
        name="Test Player", team="Test Team", performance_history=[10, 12, 14, 16]
    )

    result = analyzer.analyze(player)

    assert result.metadata["volatility"] > 0


def test_player_trend_confidence_increases_with_playoff_data():

    analyzer = PlayerTrendAnalyzer()

    player = FakeResearchResult(
        performance_history=[10, 12, 14, 16], playoff_history=[20, 22]
    )

    result = analyzer.analyze(player)

    assert result.confidence > 0.5


def test_player_trend_calculates_feature_completeness():

    analyzer = PlayerTrendAnalyzer()

    player = FakeResearchResult(
        performance_history=[10, 12, 14, 16], playoff_history=[20, 22]
    )

    result = analyzer.analyze(player)

    assert result.metadata["feature_completeness"] > 0


def test_player_trend_analyzes_player_feature_view_trend_features():

    analyzer = PlayerTrendAnalyzer()

    feature_view = PlayerFeatureView(
        trend=TrendFeatures(
            season_average=12.0,
            recent_average=16.0,
            trend=4.0,
        )
    )

    result = analyzer.analyze(feature_view)

    assert result.metadata["trend"] == "rising"
    assert result.metadata["trend_score"] == 4.0
    assert result.metadata["season_average"] == 12.0
    assert result.metadata["recent_average"] == 16.0
