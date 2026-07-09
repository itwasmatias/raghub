"""
Search orchestration service.
"""

from connectors import create_registry
from models.search_result import SearchResult


class SearchService:
    """
    Coordinates searches across one or more connectors.
    """

    def __init__(self):
        self.registry = create_registry()

    def search(
        self,
        source: str,
        query: str,
        limit: int = 10,
    ) -> list[SearchResult]:
        """
        Execute a search using the specified connector.
        """
        connector = self.registry.get(source)

        return connector.search(
            query=query,
            limit=limit,
        )
