from models.sports.player import Player


def test_player_has_performance_history():
    player = Player(
        name="Test Player",
        team="Test Team",
        performance_history=[
            10,
            12,
            15
        ]
    )

    assert player.performance_history == [
        10,
        12,
        15
    ]