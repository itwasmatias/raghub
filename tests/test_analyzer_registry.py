import pytest

from analytics.base import BaseAnalyzer
from analytics.registry import AnalyzerRegistry

class FakeAnalyzer(BaseAnalyzer):
    
    def __init__(self,name):
        self._name = name

    @property
    def analyzer_name(self):
        return self._name

    def analyze(self, evidence):
        return evidence

def test_register_analyzer():
       
        registry = AnalyzerRegistry()
        analyzer = FakeAnalyzer("fake")
       
        registry.register_analyzer(analyzer)
       
        assert registry.get("fake") is analyzer

def test_get_all_analyzers():

    registry = AnalyzerRegistry()

    analyzer1 = FakeAnalyzer("trend")
    analyzer2 = FakeAnalyzer("entities")

    registry.register_analyzer(analyzer1)
    registry.register_analyzer(analyzer2)

    assert registry.get_all() == [analyzer1, analyzer2]

def test_duplicate_analyzer_registration():

    registry = AnalyzerRegistry()
    
    analyzer1 = FakeAnalyzer("trend")
    analyzer2 = FakeAnalyzer("trend")

    registry.register_analyzer(analyzer1)

    with pytest.raises(ValueError):
        registry.register_analyzer(analyzer2)