"""
Retrieval provider abstractions for RAGHub.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from models.retrieval_result import RetrievalResult


class RetrievalProvider(ABC):
    """
    Backend-specific retrieval implementation.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        return ""

    @abstractmethod
    def retrieve(self, query: str, limit: int = 10) -> list[RetrievalResult]:
        raise NotImplementedError
