"""
Bootstrap the built-in connector registry.
"""

from connectors.registry import ConnectorRegistry
from connectors.pgvector.connector import PgVectorConnector
from connectors.wikipedia.connector import WikipediaConnector


def create_registry() -> ConnectorRegistry:
    """
    Create and populate the connector registry with all built-in
    connectors.
    """
    registry = ConnectorRegistry()

    registry.register(PgVectorConnector)
    registry.register(WikipediaConnector)

    return registry
