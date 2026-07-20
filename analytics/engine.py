from models.analytics_result import AnalyticsResult

class AnalyticsEngine:
    
    def __init__(self,registry):
        self.registry = registry

    def analyze(self, evidence):
        results = []

        for analyzer in self.registry.get_all():
            result = analyzer.analyze(evidence)
            results.append(result)

        return AnalyticsResult(results=results)