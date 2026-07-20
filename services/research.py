"""
Research orchestration service.

Coordinates searches across one or more connectors
and prepares normalized research results.
"""
from connectors import create_registry
from services.search import SearchService
from services.retrieve import RetrievalService


class ResearchService:
    """
    High-level orchestration layer for multi-source research.
    """

    def __init__(
        self,
        search_service=None,
        retrieval_service=None,
        registry=None,
    ):
        self.registry = registry or create_registry()
        self.search_service = search_service or SearchService(
            registry=self.registry
        )

        self.retrieval_service = (
            retrieval_service or RetrievalService()
        )
        
    def research(
        self,
        query: str,
        sources: list[str] | None = None,
        limit: int = 10,
    ):
        """
        Execute a research request.
        """
        if sources is None:
            sources = ["wikipedia"]

        search_results = []

        for source in sources:
            search_results.extend(
                self.search_service.search(
                    source=source,
                    query=query,
                    limit=limit,
                )
            )

        retrieval_results = self.retrieval_service.retrieve(
            query=query,
            limit=limit,
        )

        return {
            "query": query,
            "sources": sources,
            "search_results": search_results,
            "retrieval_results": retrieval_results,
        }


        """
        Execute a research request.

        (Implementation will come in the next step.)
        """
        raise NotImplementedError
