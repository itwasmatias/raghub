"""
PgVector connector.

Adapts the existing pgvector retrieval provider to the connector
interface while the provider layer remains in place.
"""

from __future__ import annotations

from connectors.base import BaseConnector
from models.retrieval_result import RetrievalResult
from models.search_result import SearchResult
from services.providers import RetrievalProvider
from services.providers_pgvector import PgVectorProvider


class PgVectorConnector(BaseConnector):
    """
    Connector for PostgreSQL/pgvector retrieval.
    """

    def __init__(self, provider: RetrievalProvider | None = None):
        self.provider = provider or PgVectorProvider()

    @property
    def source_name(self) -> str:
        return self.provider.name

    @property
    def name(self) -> str:
        return self.source_name

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        results = []
        for item in self.retrieve(query, limit=limit):
            document_id = str(item.document_id)
            metadata = dict(item.metadata or {})
            metadata.setdefault("chunk_index", item.chunk_index)
            results.append(
                SearchResult(
                    title=document_id,
                    url="",
                    snippet=item.content,
                    source=self.source_name,
                    identifier=document_id,
                    content_type="document_chunk",
                    score=item.score,
                    metadata=metadata,
                )
            )

        return results

    def retrieve(self, query: str, limit: int = 10) -> list[RetrievalResult]:
        return self.provider.retrieve(query, limit=limit)
