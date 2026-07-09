"""
Normalized search result model used by all RAGHub connectors.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SearchResult:
    """
    A normalized search result returned by any connector.

    Every connector converts its native response into this model so
    the rest of RAGHub can operate independently of the source.
    """

    # Core information
    title: str
    url: str
    snippet: str
    source: str

    # Optional metadata
    identifier: str | None = None
    authors: list[str] = field(default_factory=list)
    published: str | None = None

    # Retrieval metadata
    score: float | None = None

    # Connector-specific metadata
    metadata: dict[str, Any] = field(default_factory=dict)