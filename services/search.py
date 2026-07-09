"""
Search orchestration service.
"""

from connectors import create_registry
from models.search_result import SearchResult
from services.ranking import RankingService


class SearchService:
    """
    Coordinates searches across one or more connectors.
    """

    def __init__(
        self,
        registry=None,
        ranking_service: RankingService | None = None,
        plugin_paths: list[str] | None = None,
    ):
        self.registry = registry or create_registry()
        if plugin_paths:
            for plugin_path in plugin_paths:
                self.registry.register_plugin_path(plugin_path)
            self.registry.load_plugins()
        self.ranking_service = ranking_service or RankingService()

    def search(
        self,
        source: str,
        query: str,
        limit: int = 10,
    ) -> list[SearchResult]:
        """
        Execute a search using the specified connector and rank the results.
        """
        if not self.registry.has(source):
            self.registry.load_plugins()
        if not self.registry.has(source):
            raise ValueError(f"Unsupported source: {source}")

        connector = self.registry.get(source)
        results = connector.search(
            query=query,
            limit=limit,
        )
        return self.ranking_service.rank(results)
