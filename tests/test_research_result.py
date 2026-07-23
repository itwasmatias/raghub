from sports.research.models.research_result import ResearchResult


def test_research_result_creation():

    result = ResearchResult(
        experiment_name="Coaching Development Study",
        finding="Some teams improve player outcomes.",
        confidence=0.60,
        supporting_evidence=[
            "Player development history",
            "Performance changes"
        ]
    )

    assert result.experiment_name == "Coaching Development Study"
    assert result.confidence == 0.60
    assert len(result.supporting_evidence) == 2