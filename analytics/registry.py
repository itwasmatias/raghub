from analytics.base import BaseAnalyzer

class AnalyzerRegistry:
    
    def __init__(self):
        self._analyzers: dict[str, BaseAnalyzer] = {}

    def register_analyzer(self, analyzer: BaseAnalyzer) -> None:
        if analyzer.analyzer_name in self._analyzers:
            raise ValueError(
                f"Analyzer '{analyzer.analyzer_name}' is already registered"
            )

        self._analyzers[analyzer.analyzer_name] = analyzer

    def get(self, analyzer_name: str) -> BaseAnalyzer:
        return self._analyzers[analyzer_name]

    def get_all(self) -> list[BaseAnalyzer]:
        return list(self._analyzers.values())
