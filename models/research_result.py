from dataclasses import dataclass, field

from models.connector_result import ConnectorResult
from models.evidence import Evidence


@dataclass(slots=True)
class ResearchResult:
    query: str

    connector_results: list[ConnectorResult] = field(
        default_factory=list
    )

    merged_evidence: list[Evidence] = field(
        default_factory=list
    )

    citations: list[str] = field(
        default_factory=list
    )

    statistics: dict = field(
        default_factory=dict
    )

    elapsed_time: float = 0.0