from models.research_result import ResearchResult


def test_research_result_creation():
    result = ResearchResult(
        query="Artificial Intelligence"
    )

    assert result.query == "Artificial Intelligence"