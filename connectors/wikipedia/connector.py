"""
Wikipedia connector.

Retrieves search results from the Wikipedia API and converts them
into RAGHub's normalized SearchResult objects.
"""
import re
import requests

from connectors.base import BaseConnector
from models.search_result import SearchResult


class WikipediaConnector(BaseConnector):
    """Connector for Wikipedia."""
    def _clean_snippet(self, snippet: str) -> str:
        """
        Remove Wikipedia HTML highlight tags from snippets.
        """

        snippet = re.sub(
            r"<span class=\"searchmatch\">",
            "",
            snippet,
        )

        snippet = snippet.replace("</span>", "")

        return snippet
    @property
    def source_name(self) -> str:
        return "wikipedia"

    def search(
        self,
        query: str,
        limit: int = 10,
    ) -> list[SearchResult]:
        """
        Search Wikipedia and return normalized SearchResult objects.
        """

        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "format": "json",
            "srlimit": limit,
        }

        headers = {
            "User-Agent": "RAGHub/2.0 (educational project)"
        }

        response = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params=params,
            headers=headers,
            timeout=10,
        )

        response.raise_for_status()

        data = response.json()

        results = []

        for item in data.get("query", {}).get("search", []):
            title = item["title"]

            url = (
                "https://en.wikipedia.org/wiki/"
                + title.replace(" ", "_")
            )

            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=self._clean_snippet(
                        item.get("snippet", "")
                    ),
                    source=self.source_name,
                )
            )

        return results