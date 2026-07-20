from models.connector_result import ConnectorResult
from models.evidence import Evidence
from services.evidence_aggregator import EvidenceAggregator


def test_aggregate_multiple_connectors():
    wiki = ConnectorResult(
        connector_name="wikipedia",
        query="AI",
        results=[
            Evidence(
                title="A",
                snippet="",
                content="",
                source="wikipedia",
            )
        ],
    )

    github = ConnectorResult(
        connector_name="github",
        query="AI",
        results=[
            Evidence(
                title="B",
                snippet="",
                content="",
                source="github",
            )
        ],
    )

    merged = EvidenceAggregator.aggregate(
        [wiki, github]
    )

    assert len(merged) == 2

def test_aggregate_empty():
    merged = EvidenceAggregator.aggregate([])

    assert merged == []   