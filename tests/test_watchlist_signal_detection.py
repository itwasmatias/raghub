from sports.intelligence.signal_detection import (
    SignalDetectionService,
    SignalObservation,
    WatchlistDefinition,
)


def test_watchlist_defines_sources_thresholds_escalation_and_actions() -> None:
    watchlist = WatchlistDefinition(
        id="nba-lineup-watch",
        entities=("player-7", "team-chi"),
        topics=("player availability", "starting lineup", "player props"),
        signals=("injury_status", "starter_status", "player_points_line"),
        data_sources=("official injury report", "licensed sportsbook feed"),
        expected_update_frequency="15 minutes until game start",
        significance_threshold=1.0,
        escalation_rules=("Escalate contradictions from primary sources.",),
        related_situation_ids=("nba:player-7:game-1",),
        triggered_actions=("retrieve_lineup", "revise_forecast"),
    )

    assert watchlist.data_sources
    assert watchlist.significance_threshold == 1.0
    assert watchlist.related_situation_ids
    assert watchlist.triggered_actions == ("retrieve_lineup", "revise_forecast")


def test_signal_detection_distinguishes_repetition_confirmation_and_change() -> None:
    service = SignalDetectionService()
    watchlist = WatchlistDefinition(
        id="prop-watch",
        entities=("player-7",),
        topics=("player props",),
        signals=("player_points_line",),
        data_sources=("DraftKings", "FanDuel"),
        expected_update_frequency="5 minutes",
        significance_threshold=1.0,
        escalation_rules=("Escalate line moves of at least one point.",),
        related_situation_ids=("nba:player-7:game-1",),
        triggered_actions=("revise_forecast",),
    )
    first = SignalObservation(
        entity="player-7",
        signal="player_points_line",
        value=16.5,
        observed_at="2026-07-24T10:00:00+00:00",
        source="DraftKings",
    )
    repeated = SignalObservation(
        entity="player-7",
        signal="player_points_line",
        value=16.5,
        observed_at="2026-07-24T10:02:00+00:00",
        source="DraftKings",
    )
    confirmed = SignalObservation(
        entity="player-7",
        signal="player_points_line",
        value=16.5,
        observed_at="2026-07-24T10:03:00+00:00",
        source="FanDuel",
    )
    moved = SignalObservation(
        entity="player-7",
        signal="player_points_line",
        value=18.0,
        observed_at="2026-07-24T10:20:00+00:00",
        source="DraftKings",
    )

    assert service.evaluate(watchlist, first, []).classification == "new_information"
    assert service.evaluate(watchlist, repeated, [first]).classification == "repeated_reporting"
    assert service.evaluate(watchlist, confirmed, [first]).classification == "confirmation"
    movement = service.evaluate(watchlist, moved, [first, confirmed])
    assert movement.classification == "meaningful_change"
    assert movement.significant is True
    assert movement.triggered_actions == ("revise_forecast",)
