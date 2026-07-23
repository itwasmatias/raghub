from sports.research.engine.research_engine import ResearchEngine
from sports.research.models.observation import Observation
from sports.research.models.hypothesis import Hypothesis


def test_research_engine_stores_observations():

    engine = ResearchEngine()

    observation = Observation(
        subject="Test Player",
        description="Performance increased after role change",
        source="NBA Data",
        timestamp="2026-07-22"
    )

    engine.add_observation(observation)

    assert len(engine.observations) == 1
    assert engine.observations[0].subject == "Test Player"

def test_research_engine_stores_hypotheses():

    engine = ResearchEngine()

    hypothesis = Hypothesis(
        title="Trade Motivation Effect",
        statement="Players may improve after feeling undervalued.",
        rationale="New opportunity can increase motivation.",
        confidence=0.60
    )

    engine.add_hypothesis(hypothesis)

    assert len(engine.hypotheses) == 1
    assert engine.hypotheses[0].title == "Trade Motivation Effect"