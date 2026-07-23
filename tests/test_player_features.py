from models.sports.player import Player
from sports.features.player_features import PlayerFeatureExtractor


def test_player_feature_extractor_calculates_basic_features():

    player = Player(
        name="Test Player",
        team="Test Team",
        performance_history=[10, 12, 14, 16]
    )

    extractor = PlayerFeatureExtractor()

    features = extractor.extract(player)

    assert features.season_average == 13