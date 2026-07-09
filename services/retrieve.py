import logging
import os
from time import time

from config import Config
from models.retrieval_result import RetrievalResult
from services.providers import RetrievalProvider
from services.providers_arxiv import ArxivProvider
from services.providers_pgvector import PgVectorProvider
from services.providers_pubmed import PubMedProvider
from services.providers_wikipedia import WikipediaProvider


class RetrievalService:
    _cache: dict[tuple, tuple[float, list[RetrievalResult]]] = {}
    """
    Orchestrate retrieval across one or more providers.
    """

    def __init__(
        self,
        providers: list[RetrievalProvider | str] | None = None,
        provider_names: list[str] | None = None,
        ttl_seconds: int | None = None,
        logger: logging.Logger | None = None,
    ):
        configured = providers or []
        if provider_names:
            configured = provider_names
        elif os.getenv("RAGHUB_RETRIEVAL_PROVIDERS"):
            configured = [name.strip() for name in os.getenv("RAGHUB_RETRIEVAL_PROVIDERS", "").split(",") if name.strip()]
        elif getattr(Config, "RETRIEVAL_PROVIDERS", None):
            configured = [name.strip() for name in str(Config.RETRIEVAL_PROVIDERS).split(",") if name.strip()]

        if providers is not None:
            self.providers = self._build_providers(list(providers))
        else:
            self.providers = self._build_providers(configured)
        if providers is not None and not providers:
            self.providers = []
        self.ttl_seconds = ttl_seconds
        self.logger = logger or logging.getLogger(__name__)

    def _build_providers(self, providers: list[RetrievalProvider | str | object]) -> list[object]:
        built = []
        for provider in providers:
            if isinstance(provider, RetrievalProvider):
                built.append(provider)
            elif isinstance(provider, str):
                provider_name = provider.lower()
                if provider_name == "pgvector":
                    built.append(PgVectorProvider())
                elif provider_name == "wikipedia":
                    built.append(WikipediaProvider())
                elif provider_name == "pubmed":
                    built.append(PubMedProvider())
                elif provider_name == "arxiv":
                    built.append(ArxivProvider())
                else:
                    raise ValueError(f"Unsupported provider: {provider}")
            elif hasattr(provider, "retrieve") and callable(getattr(provider, "retrieve")):
                built.append(provider)
            else:
                raise TypeError(f"Unsupported provider type: {type(provider)}")
        return built or [PgVectorProvider()]

    def _deduplicate_results(self, results: list[RetrievalResult]) -> list[RetrievalResult]:
        seen: set[tuple[object, object, str]] = set()
        deduped: list[RetrievalResult] = []
        for result in results:
            key = (
                result.document_id,
                result.chunk_index,
                result.content,
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(result)
        return deduped

    def retrieve(self, query: str, limit: int = 10) -> list[RetrievalResult]:
        """
        Retrieve results from each configured provider and flatten them.
        Failing providers are skipped so the overall retrieval remains resilient.
        """
        cache_key = (
            query,
            limit,
            tuple(getattr(provider, "name", type(provider).__name__) for provider in self.providers),
        )
        if cache_key in self._cache:
            cached_at, cached_results = self._cache[cache_key]
            if self.ttl_seconds is None or (time() - cached_at) <= self.ttl_seconds:
                self.logger.debug("retrieval cache hit for query=%s", query)
                return cached_results
            del self._cache[cache_key]

        results: list[RetrievalResult] = []
        for provider in self.providers:
            provider_name = getattr(provider, "name", type(provider).__name__)
            self.logger.debug("retrieval provider invoked name=%s query=%s", provider_name, query)
            try:
                provider_results = provider.retrieve(query, limit=limit)
                results.extend(provider_results)
            except Exception as exc:
                self.logger.warning("retrieval provider failed name=%s error=%s", provider_name, exc)
                continue

        deduped_results = self._deduplicate_results(results)
        deduped_results.sort(key=lambda item: (item.score or 0.0), reverse=True)
        self._cache[cache_key] = (time(), deduped_results)
        return deduped_results


def retrieve(query: str):
    """
    Backward-compatible wrapper around RetrievalService.retrieve.
    """
    return RetrievalService().retrieve(query)
