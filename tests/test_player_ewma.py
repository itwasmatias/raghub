from models.sports.player import Player
from analytics.domains.sports.analyzers.player_trend import PlayerTrendAnalyzer


def test_player_trend_calculates_ewma():

    analyzer = PlayerTrendAnalyzer()

    player = Player(
        name="Test Player",
        team="Test Team",
        performance_history=[
            10,
            12,
            15
        ]
    )

    result = analyzer.analyze(player)

    assert result.metadata["ewma"] > 12