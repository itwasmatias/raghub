from abc import ABC, abstractmethod

from models.analysis_result import AnalysisResult
from models.evidence import Evidence


class BaseAnalyzer(ABC):
    """
    Base class for all RAGHub analyzers.
    """

    @property
    @abstractmethod
    def analyzer_name(self) -> str:
        """
        Human-readable analyzer name.
        """
        pass

    @abstractmethod
    def analyze(
        self,
        evidence: list[Evidence],
    ) -> AnalysisResult:
        """
        Analyze evidence and return a normalized analysis result.
        """
        pass