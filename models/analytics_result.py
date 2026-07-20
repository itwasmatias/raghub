from dataclasses import dataclass, field

from models.analysis_result import AnalysisResult

@dataclass
class AnalyticsResult:
    results: list[AnalysisResult] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)