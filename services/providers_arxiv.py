"""
arXiv retrieval provider.
"""

from __future__ import annotations

import requests

from models.retrieval_result import RetrievalResult
from services.providers import RetrievalProvider


class ArxivProvider(RetrievalProvider):
    """
    Retrieve simple arXiv-style results from the public API.
    """

    name = "arxiv"

    def retrieve(self, query: str, limit: int = 10) -> list[RetrievalResult]:
        params = {
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": limit,
            "format": "json",
        }

        try:
            response = requests.get(
                "http://export.arxiv.org/api/query",
                params=params,
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return []

        entries = payload.get("feed", {}).get("entry", [])

        results = []
        for entry in entries:
            title = entry.get("title", "")
            results.append(
                RetrievalResult(
                    document_id=entry.get("id", title),
                    chunk_index=0,
                    content=title,
                    score=0.7,
                    metadata={"provider": self.name, "title": title},
                )
            )

        return results
