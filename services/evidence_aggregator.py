from models.connector_result import ConnectorResult
from models.evidence import Evidence


class EvidenceAggregator:
    """Merge evidence returned by multiple connectors."""

    @staticmethod
    def aggregate(
        connector_results: list[ConnectorResult],
    ) -> list[Evidence]:
        merged: list[Evidence] = []

        for connector in connector_results:
            merged.extend(connector.results)

        return merged