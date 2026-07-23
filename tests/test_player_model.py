from models.sports.player import Player


def test_player_model_creates_player():
    player = Player(
        name="Test Player",
        team="Test Team"
    )

    assert player.name == "Test Player"
    assert player.team == "Test Team"