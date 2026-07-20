class ConnectorExecutor:
    """Execute one or more connectors."""

    def execute(self, connectors, query: str):
        results = []

        for connector in connectors:
            results.append(connector.search(query))

        return results