"""
PostgreSQL/pgvector retrieval provider.
"""

from __future__ import annotations

from config import Config
from models.retrieval_result import RetrievalResult
from services.providers import RetrievalProvider

try:
    from database import get_connection
except ImportError:  # pragma: no cover - optional dependency
    def get_connection():
        raise RuntimeError("Database connection is unavailable")


class PgVectorProvider(RetrievalProvider):
    """
    Retrieve chunks from the local pgvector-backed PostgreSQL store.
    """

    name = "pgvector"

    def _normalize_rows(self, rows: list[tuple | dict]) -> list[RetrievalResult]:
        normalized = []
        for row in rows:
            if isinstance(row, dict):
                row_data = row
            else:
                row_data = {
                    "document_id": row[0],
                    "chunk_index": row[1],
                    "content": row[2],
                    "distance": row[3] if len(row) > 3 else None,
                }

            distance = row_data.get("distance")
            score = 0.0
            if distance is not None:
                try:
                    score = max(0.0, 1.0 - float(distance))
                except (TypeError, ValueError):
                    score = 0.0

            normalized.append(
                RetrievalResult(
                    document_id=row_data.get("document_id"),
                    chunk_index=row_data.get("chunk_index", 0),
                    content=row_data.get("content", ""),
                    score=score,
                    metadata={"provider": self.name},
                )
            )

        return normalized

    def retrieve(self, query: str, limit: int = 10) -> list[RetrievalResult]:
        from services.embedding_client import embed

        query_embedding = embed(query)

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        document_id,
                        chunk_index,
                        content,
                        embedding <=> %s::vector AS distance
                    FROM chunks
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (
                        query_embedding,
                        query_embedding,
                        limit or Config.TOP_K,
                    ),
                )

                rows = cur.fetchall()

        return self._normalize_rows(rows)
