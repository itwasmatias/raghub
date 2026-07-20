"""
Normalized retrieval result model for document chunks.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RetrievalResult:
    """
    Normalized representation of a retrieved chunk.
    """
    content: str

    source: str = "unknown"
    document_id: int | str | None = None
    chunk_index: int = 0
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    