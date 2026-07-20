from models.analytics_result import AnalyticsResult
from models.analysis_result import AnalysisResult


def test_analytics_result_creation():

    analysis = AnalysisResult(
        analyzer="trend",
        summary="market increasing",
        confidence=0.85,
    )

    result = AnalyticsResult(
        results=[analysis]
    )

    assert len(result.results) == 1
    assert result.results[0] is analysis

def test_analytics_result_metadata():

    result = AnalyticsResult(
        metadata={"analyzers_run": 3}
    )

    assert result.metadata["analyzers_run"] == 3