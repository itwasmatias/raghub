import unittest
from unittest.mock import patch

from connectors.pgvector.connector import PgVectorConnector
from models.retrieval_result import RetrievalResult
from services.providers_arxiv import ArxivProvider
from services.providers_pgvector import PgVectorProvider
from services.providers_pubmed import PubMedProvider
from services.providers_wikipedia import WikipediaProvider
from services.retrieve import RetrievalService


class RetrievalServiceTests(unittest.TestCase):
    def test_pgvector_provider_normalizes_internal_chunks(self):
        provider = PgVectorProvider()

        normalized = provider._normalize_rows(
            [
                {
                    "document_id": 7,
                    "chunk_index": 3,
                    "content": "Example chunk",
                    "distance": 0.42,
                }
            ]
        )

        self.assertEqual(len(normalized), 1)
        self.assertIsInstance(normalized[0], RetrievalResult)
        self.assertEqual(normalized[0].document_id, 7)
        self.assertEqual(normalized[0].chunk_index, 3)
        self.assertEqual(normalized[0].content, "Example chunk")
        self.assertAlmostEqual(normalized[0].score, 0.58)

    def test_wikipedia_provider_normalizes_results(self):
        class FakeWikipediaConnector:
            def retrieve(self, query, limit=10):
                self.query = query
                self.limit = limit
                return [
                    RetrievalResult(
                        document_id="raghub",
                        chunk_index=0,
                        content="RAGHub retrieval fixture",
                        score=0.8,
                        metadata={"provider": "wikipedia"},
                    )
                ]

        with patch(
            "services.providers_wikipedia.WikipediaConnector",
            FakeWikipediaConnector,
        ):
            results = WikipediaProvider().retrieve("raghub", limit=2)

        self.assertTrue(results)
        self.assertIsInstance(results[0], RetrievalResult)
        self.assertIn("provider", results[0].metadata)

    def test_pubmed_provider_normalizes_results(self):
        provider = PubMedProvider()
        results = provider.retrieve("raghub", limit=2)

        self.assertIsInstance(results, list)
        self.assertTrue(all(isinstance(item, RetrievalResult) for item in results))

    def test_arxiv_provider_normalizes_results(self):
        provider = ArxivProvider()
        results = provider.retrieve("raghub", limit=2)

        self.assertIsInstance(results, list)
        self.assertTrue(all(isinstance(item, RetrievalResult) for item in results))

    def test_retrieval_service_uses_configured_providers(self):
        class FakeProvider:
            name = "fake"

            def retrieve(self, query, limit=10):
                return [
                    RetrievalResult(
                        document_id="fake-doc",
                        chunk_index=0,
                        content="fake content",
                        score=0.9,
                    )
                ]

        service = RetrievalService(providers=[FakeProvider()])
        results = service.retrieve("test query")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].document_id, "fake-doc")
        self.assertEqual(results[0].content, "fake content")
        self.assertAlmostEqual(results[0].score, 0.9)

    def test_retrieval_service_accepts_provider_names(self):
        service = RetrievalService(provider_names=["wikipedia"])

        self.assertEqual(len(service.providers), 1)
        self.assertEqual(service.providers[0].name, "wikipedia")

    def test_pgvector_connector_delegates_to_existing_provider_logic(self):
        class FakeProvider:
            name = "pgvector"

            def __init__(self):
                self.calls = []

            def retrieve(self, query, limit=10):
                self.calls.append((query, limit))
                return [
                    RetrievalResult(
                        document_id="doc-1",
                        chunk_index=0,
                        content="connector content",
                        score=0.88,
                        metadata={"provider": self.name},
                    )
                ]

        provider = FakeProvider()
        connector = PgVectorConnector(provider=provider)
        results = connector.retrieve("test query", limit=3)

        self.assertEqual(connector.source_name, "pgvector")
        self.assertEqual(connector.name, "pgvector")
        self.assertEqual(provider.calls, [("test query", 3)])
        self.assertEqual(results[0].metadata, {"provider": "pgvector"})

    def test_pgvector_connector_search_returns_search_results(self):
        class FakeProvider:
            name = "pgvector"

            def retrieve(self, query, limit=10):
                return [
                    RetrievalResult(
                        document_id="doc-1",
                        chunk_index=2,
                        content="searchable chunk",
                        score=0.77,
                        metadata={"provider": self.name},
                    )
                ]

        connector = PgVectorConnector(provider=FakeProvider())
        results = connector.search("test query", limit=3)

        self.assertEqual(results[0].title, "doc-1")
        self.assertEqual(results[0].snippet, "searchable chunk")
        self.assertEqual(results[0].source, "pgvector")
        self.assertEqual(results[0].content_type, "document_chunk")
        self.assertEqual(results[0].metadata["chunk_index"], 2)

    def test_retrieval_service_resolves_pgvector_from_registry(self):
        class FakeRegistry:
            def __init__(self):
                self.connector = PgVectorConnector(provider=PgVectorProvider())

            def get(self, source_name):
                if source_name != "pgvector":
                    raise KeyError(source_name)
                return self.connector

        registry = FakeRegistry()
        service = RetrievalService(provider_names=["pgvector"], registry=registry)

        self.assertIs(service.providers[0], registry.connector)


if __name__ == "__main__":
    unittest.main()
