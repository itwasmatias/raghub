import unittest

from models.retrieval_result import RetrievalResult
from services.retrieve import RetrievalService


class RetrievalCacheTests(unittest.TestCase):
    def test_retrieval_service_caches_results_for_repeated_calls(self):
        class CountingProvider:
            name = "counting"

            def __init__(self):
                self.calls = 0

            def retrieve(self, query, limit=10):
                self.calls += 1
                return [
                    RetrievalResult(
                        document_id="doc-1",
                        chunk_index=0,
                        content="cached content",
                        score=0.95,
                    )
                ]

        provider = CountingProvider()
        service = RetrievalService(providers=[provider])

        first = service.retrieve("test query")
        second = service.retrieve("test query")

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(provider.calls, 1)


if __name__ == "__main__":
    unittest.main()
