from sports.research.models.hypothesis import Hypothesis


def test_hypothesis_creation():

    hypothesis = Hypothesis(
        title="Trade Motivation Effect",
        statement="Players may improve after a trade due to increased motivation.",
        rationale="A new environment may increase focus and effort.",
        confidence=0.35
    )

    assert hypothesis.title == "Trade Motivation Effect"
    assert hypothesis.statement.startswith("Players may improve")
    assert hypothesis.rationale.startswith("A new environment")
    assert hypothesis.confidence == 0.35