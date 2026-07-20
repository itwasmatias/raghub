from dataclasses import dataclass, field
from typing import Any
from models.evidence import Evidence

@dataclass(slots=True)
class ConnectorResult:
    connector_name: str
    query: str
    results: list[Evidence] = field(default_factory=list)
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)