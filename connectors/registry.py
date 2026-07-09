"""
Connector registry for RAGHub.

Provides a central location for registering and retrieving
knowledge source connectors.
"""

from connectors.base import BaseConnector


class ConnectorRegistry:
    """
    Registry of available connectors.
    """

    def __init__(self):
        self._connectors: dict[str, type[BaseConnector]] = {}
        self._plugin_paths: list[str] = []

    def register(self, connector_class: type[BaseConnector]) -> None:
        """
        Register a connector class.
        """
        connector = connector_class()
        self._connectors[connector.source_name] = connector_class

    def register_instance(self, connector: BaseConnector) -> None:
        """
        Register an already-instantiated connector.
        """
        self._connectors[connector.source_name] = type(connector)

    def register_plugin_path(self, plugin_path: str) -> None:
        """
        Register a dotted module path that may expose connectors.
        """
        if plugin_path not in self._plugin_paths:
            self._plugin_paths.append(plugin_path)

    def load_plugins(self) -> None:
        """
        Import plugin modules from the registered paths and register any
        connector classes they expose.
        """
        for plugin_path in self._plugin_paths:
            module = __import__(plugin_path, fromlist=["*"])
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if isinstance(attr, type) and issubclass(attr, BaseConnector) and attr is not BaseConnector:
                    self.register(attr)

    def get(self, source_name: str) -> BaseConnector:
        """
        Create and return a connector instance.
        """
        connector_class = self._connectors[source_name]
        return connector_class()
    
    def search(
        self,
        source_name: str,
        query: str,
        limit: int = 10,
    ):
        """
        Search a single connector.
        """
        connector = self.get(source_name)
        return connector.search(query, limit=limit)

    def search_selected(
        self,
        sources: list[str],
        query: str,
        limit: int = 10,
    ):
        """
        Search multiple connectors and return results.
        """
        results = []
        for source in sources:
            if not self.has(source):
                continue


            connector = self.get(source)
            results.extend(connector.search(query,limit=limit))

        return results
    
    def search_all(
        self,
        query: str,
        limit: int = 10,
    ):
        """
        Search every registered connector.
        """
        return self.search_selected(
            self.list_sources(),
            query,
            limit,
        )

    def has(self, source_name: str) -> bool:
        """
        Return True when a connector is registered for the given source.
        """
        return source_name in self._connectors

    def list_sources(self) -> list[str]:
        """
        Return all registered connector names.
        """
        return sorted(self._connectors.keys())
