from models.analysis_result import AnalysisResult

def test_analysis_result_creation():

    result = AnalysisResult(
        analyzer="trend",
        summary="market increasing",
        confidence=0.85,
    )

    assert result.analyzer == "trend"
    assert result.confidence == 0.85
    assert result.summary == "market increasing"
    assert result.findings == []
    assert result.evidence ==[]