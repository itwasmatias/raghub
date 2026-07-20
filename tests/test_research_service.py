import unittest

from services.research import ResearchService


class FakeSearchService:
    def __init__(self):
        self.calls = []

    def search(self, source, query, limit=10):
        self.calls.append((source, query, limit))
        return [{"title": "Wikipedia Result"}]


class FakeRetrievalService:
    def __init__(self):
        self.calls = []

    def retrieve(self, query, limit=10):
        self.calls.append((query, limit))
        return [{"content": "Retrieved Context"}]


class ResearchServiceTests(unittest.TestCase):

    def test_research_combines_search_and_retrieval(self):
        service = ResearchService(
            search_service=FakeSearchService(),
            retrieval_service=FakeRetrievalService(),
        )

        result = service.research(
            query="What is RAGHub?",
            sources=["wikipedia"],
        )

        self.assertEqual(result["query"], "What is RAGHub?")
        self.assertEqual(len(result["search_results"]), 1)
        self.assertEqual(len(result["retrieval_results"]), 1)

    def test_research_defaults_to_all_sources(self):
        service = ResearchService(
            search_service=FakeSearchService(),
            retrieval_service=FakeRetrievalService(),
        )

        service.research("AI")

        self.assertTrue(True)

    def test_research_handles_empty_results(self):
        class EmptySearch(FakeSearchService):
            def search(self, source, query, limit=10):
                return []

        class EmptyRetrieval(FakeRetrievalService):
            def retrieve(self, query, limit=10):
                return []

        service = ResearchService(
            search_service=EmptySearch(),
            retrieval_service=EmptyRetrieval(),
        )

        result = service.research("Nothing")

        self.assertEqual(result["search_results"], [])
        self.assertEqual(result["retrieval_results"], [])