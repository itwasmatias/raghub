"""
Bootstrap the built-in connector registry.
"""

from connectors.registry import ConnectorRegistry
from connectors.wikipedia.connector import WikipediaConnector


def create_registry() -> ConnectorRegistry:
    """
    Create and populate the connector registry with all built-in
    connectors.
    """
    registry = ConnectorRegistry()

    registry.register(WikipediaConnector)

    return registry