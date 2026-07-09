"""
PubMed retrieval provider.
"""

from __future__ import annotations

import requests

from models.retrieval_result import RetrievalResult
from services.providers import RetrievalProvider


class PubMedProvider(RetrievalProvider):
    """
    Retrieve simple PubMed-style results from the public E-utilities endpoint.
    """

    name = "pubmed"

    def retrieve(self, query: str, limit: int = 10) -> list[RetrievalResult]:
        params = {
            "db": "pubmed",
            "retmode": "json",
            "rettype": "abstract",
            "tool": "raghub",
            "email": "raghub@example.com",
            "term": query,
            "retmax": limit,
        }

        try:
            response = requests.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                params=params,
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return []

        id_list = payload.get("esearchresult", {}).get("idlist", [])

        results = []
        for article_id in id_list:
            results.append(
                RetrievalResult(
                    document_id=article_id,
                    chunk_index=0,
                    content=f"PubMed article {article_id}",
                    score=0.7,
                    metadata={"provider": self.name, "article_id": article_id},
                )
            )

        return results
