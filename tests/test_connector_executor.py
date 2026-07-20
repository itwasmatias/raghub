from services.connector_executor import ConnectorExecutor
from tests.fakes.fake_connector import FakeConnector

def test_execute_single_connector():
    executor = ConnectorExecutor()

    connector = FakeConnector("wikipedia")

    results = executor.execute(
        [connector],
        "Artificial Intelligence",
    )

    assert len(results) == 1
    assert results[0].connector_name == "wikipedia"

def test_execute_multiple_connectors():
    executor = ConnectorExecutor()

    connectors = [
        FakeConnector("wiki"),
        FakeConnector("github"),
        FakeConnector("pubmed"),
    ]

    results = executor.execute(
        connectors,
        "AI",
    )

    assert len(results) == 3