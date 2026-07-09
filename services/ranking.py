"""
Ranking utilities for search results.
"""

from __future__ import annotations

from datetime import datetime

from models.search_result import SearchResult


class RankingService:
    """
    Sort search results, apply source weighting, and normalize scores.
    """

    def __init__(
        self,
        source_weights: dict[str, float] | None = None,
        policy: dict[str, object] | None = None,
    ):
        self.source_weights = source_weights or {}
        self.policy = policy or {}

    def _normalize_score(self, score: float | None) -> float:
        if score is None:
            return 0.0
        return max(0.0, min(1.0, float(score)))

    def _recency_boost(self, result: SearchResult) -> float:
        boost = float(self.policy.get("recency_boost", 0.0))
        if boost <= 0 or not result.published:
            return 0.0

        try:
            published = datetime.fromisoformat(result.published)
            current = datetime.now()
            age_days = max(0, (current - published).days)
            return boost / (1 + age_days)
        except ValueError:
            return 0.0

    def _reliability_boost(self, result: SearchResult) -> float:
        boost = float(self.policy.get("reliability_boost", 0.0))
        if boost <= 0:
            return 0.0

        metadata = result.metadata or {}
        reliability = metadata.get("reliability")
        if reliability is None:
            return 0.0

        try:
            return boost * self._normalize_score(float(reliability))
        except (TypeError, ValueError):
            return 0.0

    def _apply_source_weight(self, result: SearchResult) -> float:
        weights = self.policy.get("source_weights") or self.source_weights or {}
        weight = weights.get(result.source, 1.0)
        return weight * self._normalize_score(result.score)

    def _score_result(self, result: SearchResult) -> float:
        base_score = self._apply_source_weight(result)
        normalized = self._normalize_score(base_score)
        return normalized + self._recency_boost(result) + self._reliability_boost(result)

    def rank(self, results: list[SearchResult]) -> list[SearchResult]:
        """
        Rank and normalize results using the configured policy.
        """
        ranked = []
        for result in results:
            ranked.append(
                SearchResult(
                    title=result.title,
                    url=result.url,
                    snippet=result.snippet,
                    source=result.source,
                    identifier=result.identifier,
                    authors=result.authors,
                    published=result.published,
                    content_type=result.content_type,
                    score=self._score_result(result),
                    metadata=result.metadata,
                )
            )

        tie_breaker = self.policy.get("tie_breaker", "score")
        if tie_breaker == "title":
            ranked.sort(
                key=lambda item: (
                    -item.score,
                    item.title.lower(),
                )
            )
        else:
            ranked.sort(key=lambda item: item.score, reverse=True)

        return ranked
