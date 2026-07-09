import unittest
from time import sleep

from services.retrieve import RetrievalService


class RetrievalTTLTests(unittest.TestCase):
    def test_cache_expires_after_ttl(self):
        class CountingProvider:
            name = "counting"

            def __init__(self):
                self.calls = 0

            def retrieve(self, query, limit=10):
                self.calls += 1
                return []

        provider = CountingProvider()
        service = RetrievalService(providers=[provider], ttl_seconds=1)

        service.retrieve("test")
        sleep(1.1)
        service.retrieve("test")

        self.assertEqual(provider.calls, 2)


if __name__ == "__main__":
    unittest.main()
