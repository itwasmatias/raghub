from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Evidence:
    title: str
    snippet: str
    content: str
    source: str
    url: str | None = None
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)