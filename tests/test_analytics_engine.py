from models.analysis_result import AnalysisResult
from analytics.base import BaseAnalyzer
from analytics.engine import AnalyticsEngine
from analytics.registry import AnalyzerRegistry


class FakeAnalyzer(BaseAnalyzer):
    
    def __init__(self, name="fake"):
        self._name = name

    @property
    def analyzer_name(self):
        return self._name

    def analyze(self, evidence):
        return AnalysisResult(
            analyzer=self.analyzer_name,
            summary="analysis complete",
            confidence=1.0,
        )

def test_engine_runs_analyzer():
    
    registry = AnalyzerRegistry()
    analyzer = FakeAnalyzer()
    
    registry.register_analyzer(analyzer)

    engine = AnalyticsEngine(registry)
    result = engine.analyze([])

    assert len(result.results) == 1
    assert result.results[0].summary == "analysis complete"

def test_engine_runs_multiple_analyzers():

    registry = AnalyzerRegistry()

    analyzer1 = FakeAnalyzer("trend")
    analyzer2 = FakeAnalyzer("sentiment")

    registry.register_analyzer(analyzer1)
    registry.register_analyzer(analyzer2)

    engine = AnalyticsEngine(registry)

    result = engine.analyze([])

    assert len(result.results) == 2

def test_engine_with_no_analyzers():

    registry = AnalyzerRegistry()

    engine = AnalyticsEngine(registry)

    result = engine.analyze([])

    assert len(result.results) == 0

def test_engine_preserves_analyzer_results():

    registry = AnalyzerRegistry()

    analyzer1 = FakeAnalyzer("trend")
    analyzer2 = FakeAnalyzer("sentiment")

    registry.register_analyzer(analyzer1)
    registry.register_analyzer(analyzer2)

    engine = AnalyticsEngine(registry)

    result = engine.analyze([])

    names = [
        item.analyzer
        for item in result.results
    ]

    assert "trend" in names
    assert "sentiment" in names