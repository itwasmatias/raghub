"""
Wikipedia retrieval provider.
"""

from __future__ import annotations

import re

import requests

from models.retrieval_result import RetrievalResult
from services.providers import RetrievalProvider


class WikipediaProvider(RetrievalProvider):
    """
    Retrieve results from the Wikipedia API as normalized retrieval results.
    """

    name = "wikipedia"

    def _clean_snippet(self, snippet: str) -> str:
        snippet = re.sub(r"<span class=\"searchmatch\">", "", snippet)
        return snippet.replace("</span>", "")

    def retrieve(self, query: str, limit: int = 10) -> list[RetrievalResult]:
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "format": "json",
            "srlimit": limit,
        }

        headers = {"User-Agent": "RAGHub/2.0 (educational project)"}
        try:
            response = requests.get(
                "https://en.wikipedia.org/w/api.php",
                params=params,
                headers=headers,
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
        except Exception:
            return []

        results = []
        for item in data.get("query", {}).get("search", []):
            title = item.get("title", "")
            results.append(
                RetrievalResult(
                    document_id=title,
                    chunk_index=0,
                    content=self._clean_snippet(item.get("snippet", "")) or title,
                    score=0.8,
                    metadata={"provider": self.name, "title": title},
                )
            )

        return results
