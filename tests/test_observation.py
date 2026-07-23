from datetime import datetime

from sports.research.models.observation import Observation


def test_observation_creation():

    timestamp = datetime.now()
    
    observation = Observation(
        subject="Test Player",
        description="Improved after role change",
        source="NBA Data",
        timestamp=timestamp
    )

    assert observation.subject == "Test Player"
    assert observation.description == "Improved after role change"
    assert observation.source == "NBA Data"
    assert observation.timestamp == timestamp