"""
Base connector interface for RAGHub.

Every knowledge source connector must inherit from BaseConnector
and implement the required methods.
"""
from models.search_result import SearchResult
from abc import ABC, abstractmethod


class BaseConnector(ABC):
    """
    Abstract base class for all RAGHub connectors.
    """

    @property
    @abstractmethod
    def source_name(self) -> str:
        """
        Human-readable identifier for this connector.
        Example:
            wikipedia
            pubmed
            openalex
        """
        pass

    @abstractmethod
    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        """
        Search the external knowledge source.

        Args:
            query:
                User search query.

            limit:
                Maximum number of results.

        Returns:
            list[SearchResult]:
            A list of normalized search results.
        """
        pass

    def health_check(self) -> bool:
        """
        Verify connector availability.

        Override if the connector can perform
        a meaningful connectivity check.
        """
        return True
