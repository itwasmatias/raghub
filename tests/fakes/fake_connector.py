from models.connector_result import ConnectorResult
from models.evidence import Evidence


class FakeConnector:
    def __init__(self, name: str = "fake"):
        self.name = name

    def search(self, query: str) -> ConnectorResult:
        return ConnectorResult(
            connector_name=self.name,
            query=query,
            results=[
                Evidence(
                    title="Fake Result",
                    snippet="Fake Snippet",
                    content="Fake Content",
                    source=self.name,
                )
            ],
        )