import unittest

from services.retrieve import RetrievalService


class RetrievalProviderSelectionTests(unittest.TestCase):
    def test_retrieval_service_skips_failing_providers(self):
        class FailingProvider:
            name = "failing"

            def retrieve(self, query, limit=10):
                raise RuntimeError("boom")

        class WorkingProvider:
            name = "working"

            def retrieve(self, query, limit=10):
                return []

        service = RetrievalService(providers=[FailingProvider(), WorkingProvider()])
        results = service.retrieve("test")

        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
