import unittest

from models.search_result import SearchResult
from services.ranking import RankingService
from services.search import SearchService


class RankingServiceTests(unittest.TestCase):
    def test_ranks_results_with_source_weighting_and_normalized_scores(self):
        results = [
            SearchResult(
                title="Wikipedia low",
                url="https://example.com/w1",
                snippet="low",
                source="wikipedia",
                score=0.5,
            ),
            SearchResult(
                title="Wikipedia high",
                url="https://example.com/w2",
                snippet="high",
                source="wikipedia",
                score=1.0,
            ),
            SearchResult(
                title="Other high",
                url="https://example.com/o1",
                snippet="other",
                source="other",
                score=1.0,
            ),
        ]

        ranked = RankingService(
            source_weights={"wikipedia": 0.8, "other": 1.2}
        ).rank(results)

        self.assertEqual(ranked[0].title, "Other high")
        self.assertEqual(ranked[1].title, "Wikipedia high")
        self.assertEqual(ranked[2].title, "Wikipedia low")
        self.assertEqual(ranked[0].score, 1.0)
        self.assertAlmostEqual(ranked[1].score, 0.8)
        self.assertAlmostEqual(ranked[2].score, 0.4)

    def test_applies_policy_boosts_and_tie_breaking(self):
        results = [
            SearchResult(
                title="Zulu",
                url="https://example.com/z",
                snippet="z",
                source="wikipedia",
                score=0.5,
                published="2024-01-01",
                metadata={"reliability": 0.2},
            ),
            SearchResult(
                title="Alpha",
                url="https://example.com/a",
                snippet="a",
                source="wikipedia",
                score=0.5,
                published="2024-01-02",
                metadata={"reliability": 0.9},
            ),
        ]

        ranked = RankingService(
            policy={
                "source_weights": {"wikipedia": 1.0},
                "recency_boost": 0.1,
                "reliability_boost": 0.1,
                "tie_breaker": "title",
            }
        ).rank(results)

        self.assertEqual(ranked[0].title, "Alpha")
        self.assertEqual(ranked[1].title, "Zulu")
        self.assertGreater(ranked[0].score, ranked[1].score)


class SearchServiceTests(unittest.TestCase):
    def test_search_service_applies_ranking_before_returning_results(self):
        class FakeConnector:
            def search(self, query, limit=10):
                return [
                    SearchResult(
                        title="Low score",
                        url="https://example.com/low",
                        snippet="low",
                        source="wikipedia",
                        score=0.2,
                    )
                ]

        class FakeRegistry:
            def has(self, source_name):
                return True

            def get(self, source_name):
                return FakeConnector()

        service = SearchService(
            registry=FakeRegistry(),
            ranking_service=RankingService(source_weights={"wikipedia": 1.0}),
        )

        results = service.search(source="wikipedia", query="test", limit=5)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Low score")
        self.assertAlmostEqual(results[0].score, 0.2)


if __name__ == "__main__":
    unittest.main()
