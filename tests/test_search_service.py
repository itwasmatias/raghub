import unittest

from connectors.base import BaseConnector
from models.search_result import SearchResult
from services.search import SearchService


class DemoConnector(BaseConnector):
    @property
    def source_name(self) -> str:
        return "demo"

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        return [SearchResult(title="demo", url="", snippet="demo", source=self.source_name)]


class SearchServiceTests(unittest.TestCase):
    def test_search_service_uses_registered_connector(self):
        class FakeRegistry:
            def __init__(self):
                self.loaded = False
                self._connectors = {"demo": DemoConnector}

            def has(self, source_name):
                return source_name in self._connectors

            def get(self, source_name):
                return self._connectors[source_name]()

            def register_plugin_path(self, plugin_path):
                self.loaded = True

            def load_plugins(self):
                self.loaded = True

        registry = FakeRegistry()
        service = SearchService(registry=registry)
        results = service.search("demo", "hello")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source, "demo")


if __name__ == "__main__":
    unittest.main()
