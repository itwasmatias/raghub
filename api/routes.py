try:
    from fastapi import FastAPI, Query
except ImportError:  # pragma: no cover - optional dependency
    class Query:  # type: ignore[override]
        def __init__(self, default=None, alias=None):
            self.default = default
            self.alias = alias

    class FastAPI:  # type: ignore[override]
        def __init__(self):
            self.routes = []

        def get(self, path):
            def decorator(func):
                self.routes.append((path, func))
                return func
            return decorator

from services.answer import AnswerService
from services.ranking import RankingService
from services.retrieve import RetrievalService
from services.search import SearchService

router = FastAPI()


@router.get("/health")
def health_check():
    return {"status": "healthy", "project": "raghub"}


@router.get("/search")
def search(
    q: str = Query(..., alias="q"),
    source: str = "wikipedia",
    limit: int = 10,
    source_weight: float | None = None,
    recency_boost: float | None = None,
    reliability_boost: float | None = None,
    tie_breaker: str | None = None,
):
    policy = {}
    if source_weight is not None:
        policy["source_weights"] = {source: source_weight}
    if recency_boost is not None:
        policy["recency_boost"] = recency_boost
    if reliability_boost is not None:
        policy["reliability_boost"] = reliability_boost
    if tie_breaker is not None:
        policy["tie_breaker"] = tie_breaker

    search_service = SearchService(
        ranking_service=RankingService(policy=policy) if policy else None,
    )
    results = search_service.search(source=source, query=q, limit=limit)

    return {
        "query": q,
        "source": source,
        "results": [
            {
                "title": result.title,
                "url": result.url,
                "snippet": result.snippet,
                "source": result.source,
                "score": result.score,
                "content_type": result.content_type,
                "metadata": result.metadata,
            }
            for result in results
        ],
    }


@router.get("/retrieve")
def retrieve(
    query: str = Query(..., alias="q"),
    providers: str | None = None,
):
    provider_names = [name.strip() for name in providers.split(",") if name.strip()] if providers else None
    retrieval_service = RetrievalService(provider_names=provider_names)
    results = retrieval_service.retrieve(query)

    return {
        "query": query,
        "providers": provider_names or ["pgvector"],
        "results": [
            {
                "document_id": result.document_id,
                "chunk_index": result.chunk_index,
                "content": result.content,
                "score": result.score,
                "metadata": result.metadata,
            }
            for result in results
        ],
    }


@router.get("/combined")
def combined(
    q: str = Query(..., alias="q"),
    source: str = "wikipedia",
    limit: int = 10,
):
    search_service = SearchService()
    retrieval_service = RetrievalService()

    search_results = search_service.search(source=source, query=q, limit=limit)
    retrieval_results = retrieval_service.retrieve(q)

    return {
        "query": q,
        "search": {
            "results": [
                {
                    "title": result.title,
                    "url": result.url,
                    "snippet": result.snippet,
                    "source": result.source,
                    "score": result.score,
                    "content_type": result.content_type,
                    "metadata": result.metadata,
                }
                for result in search_results
            ]
        },
        "retrieve": {
            "results": [
                {
                    "document_id": result.document_id,
                    "chunk_index": result.chunk_index,
                    "content": result.content,
                    "score": result.score,
                    "metadata": result.metadata,
                }
                for result in retrieval_results
            ]
        },
    }


@router.get("/answer")
def answer(
    q: str = Query(..., alias="q"),
    source: str = "wikipedia",
    limit: int = 10,
    providers: str | None = None,
):
    provider_names = [name.strip() for name in providers.split(",") if name.strip()] if providers else None
    retrieval_service = RetrievalService(provider_names=provider_names)
    answer_service = AnswerService(retrieval_service=retrieval_service)
    return answer_service.answer(query=q, source=source, limit=limit)
