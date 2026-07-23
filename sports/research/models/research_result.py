from dataclasses import dataclass


@dataclass
class ResearchResult:
    experiment_name: str
    finding: str
    confidence: float
    supporting_evidence: list[str]