from models.connector_result import ConnectorResult


def test_connector_result_creation():
    result = ConnectorResult(
        connector_name="wikipedia",
        query="AI",
    )

    assert result.connector_name == "wikipedia"
    assert result.query == "AI"