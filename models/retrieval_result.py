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

    document_id: int | str
    chunk_index: int
    content: str
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
