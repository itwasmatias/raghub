"""
Wikipedia connector.

Retrieves search results from the Wikipedia API and converts them
into RAGHub's normalized SearchResult objects.
"""
import re
import requests

from connectors.base import BaseConnector
from models.retrieval_result import RetrievalResult
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

    def _fetch_results(
        self,
        query: str,
        limit: int,
    ) -> list[dict]:
        """
        Fetch raw search results from the Wikipedia API.
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

        try:
            response = requests.get(
                "https://en.wikipedia.org/w/api.php",
                params=params,
                headers=headers,
                timeout=10,
            )

            response.raise_for_status()

            data = response.json()

            return data.get("query", {}).get("search", [])

        except Exception:
            return []

    def search(
        self,
        query: str,
        limit: int = 10,
    ) -> list[SearchResult]:
        """
        Search Wikipedia and return normalized SearchResult objects.
        """
        raw_results = self._fetch_results(query, limit)

        results = []
        
        for item in raw_results:
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
    def search(
        self,
        query: str,
        limit: int = 10,
    ) -> list[SearchResult]:
        """
        Search Wikipedia and return normalized SearchResult objects.
        """
        raw_results = self._fetch_results(query, limit)
        
        results = []

        for item in raw_results:
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

    def retrieve(
        self,
        query: str,
        limit: int = 5,
    ) -> list[RetrievalResult]:
        """
        Retrieve Wikipedia content formatted for LLM context.
        """
        results: list[RetrievalResult] = []
        
        search_results = self.search(query, limit=limit)
        print(f"DEBUG: Found {len(search_results)} search results for query '{query}'.")
        if search_results is None:
            print("DEBUG: search() returned None!")
            return[]

        for item in search_results:
            results.append(
                RetrievalResult(
                    content=item.snippet,
                    source=item.source,
                    metadata={"url": item.url, "title": item.title, "source":item.source, "provider": "wikipedia"}
                )
            )
       
        return results   
 