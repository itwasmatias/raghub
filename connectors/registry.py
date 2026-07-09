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

    def register(
        self,
        connector_class: type[BaseConnector],
    ) -> None:
        """
        Register a connector class.
        """
        connector = connector_class()
        self._connectors[connector.source_name] = connector_class

    def get(
        self,
        source_name: str,
    ) -> BaseConnector:
        """
        Create and return a connector instance.
        """
        connector_class = self._connectors[source_name]
        return connector_class()

    def list_sources(self) -> list[str]:
        """
        Return all registered connector names.
        """
        return sorted(self._connectors.keys())
