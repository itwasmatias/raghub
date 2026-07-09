import unittest

from connectors.base import BaseConnector
from connectors.registry import ConnectorRegistry
from models.search_result import SearchResult


class DemoConnector(BaseConnector):
    @property
    def source_name(self) -> str:
        return "demo"

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        return []


class ConnectorRegistryTests(unittest.TestCase):
    def test_registry_registers_and_lists_custom_connectors(self):
        registry = ConnectorRegistry()
        registry.register(DemoConnector)

        self.assertTrue(registry.has("demo"))
        self.assertEqual(registry.list_sources(), ["demo"])

    def test_registry_supports_plugin_paths(self):
        registry = ConnectorRegistry()
        registry.register_plugin_path("tests.test_connector_registry")
        registry.load_plugins()

        self.assertTrue(registry.has("demo"))


if __name__ == "__main__":
    unittest.main()
