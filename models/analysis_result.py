from dataclasses import dataclass, field


@dataclass
class AnalysisResult:
    """
    Normalized result returned by analyzers.
    """

    analyzer: str
    summary: str
    confidence: float = 0.0
    findings: list[str] = field(default_factory=list)
    evidence: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)