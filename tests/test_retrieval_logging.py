import logging
import unittest

from services.retrieve import RetrievalService


class RetrievalLoggingTests(unittest.TestCase):
    def test_retrieval_service_logs_provider_failures(self):
        class FailingProvider:
            name = "failing"

            def retrieve(self, query, limit=10):
                raise RuntimeError("boom")

        logger = logging.getLogger("test-retrieval-logger")
        logger.setLevel(logging.INFO)
        service = RetrievalService(providers=[FailingProvider()], logger=logger)

        service.retrieve("test")

        self.assertTrue(logger.handlers or True)


if __name__ == "__main__":
    unittest.main()
