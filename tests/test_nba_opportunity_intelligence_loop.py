from datetime import datetime, timedelta, timezone
from pathlib import Path

from sports.betting.engine import BettingIntelligenceEngine
from sports.betting.models import BettingPolicy, Decision, ModelPrediction, OddsQuote
from sports.data.repositories.sqlite_lifecycle_repository import (
    SQLiteLifecycleRepository,
)
from sports.intelligence.lifecycle_service import IntelligenceLifecycleService
from sports.intelligence.nba_opportunity_loop import NBAPlayerOpportunityLoop


NOW = datetime(2026, 7, 24, 18, tzinfo=timezone.utc)


def _quote(book: str, selection: str, price: int) -> OddsQuote:
    return OddsQuote(
        event_id="nba-chi-ny-1",
        market="player_points",
        selection=selection,
        sportsbook=book,
        line=18.5,
        american_price=price,
        event_start=NOW + timedelta(hours=3),
        fetched_at=NOW - timedelta(seconds=30),
        source=f"{book.lower()}-licensed-feed",
        source_url=f"https://example.com/{book.lower()}/licensed-feed",
    )


def test_nba_player_opportunity_loop_runs_observe_through_monitor(
    tmp_path: Path,
) -> None:
    lifecycle = IntelligenceLifecycleService(
        SQLiteLifecycleRepository(tmp_path / "lifecycle.db")
    )
    loop = NBAPlayerOpportunityLoop(
        lifecycle,
        BettingIntelligenceEngine(
            BettingPolicy(
                minimum_edge=0.01,
                minimum_expected_return=0.01,
                minimum_similar_sample=20,
            )
        ),
    )
    prediction = ModelPrediction(
        event_id="nba-chi-ny-1",
        market="player_points",
        selection="over",
        line=18.5,
        probability=0.62,
        uncertainty=0.04,
        model_probabilities=(0.60, 0.62, 0.64),
        model_version="player-opportunity-v1",
        generated_at=NOW,
        similar_bet_sample=80,
        similar_bet_brier_score=0.19,
        reasons=("Minutes projection increased from 24 to 33.",),
        invalidators=("Player is not in the confirmed starting lineup.",),
    )
    quotes = [
        _quote("DraftKings", "over", -105),
        _quote("DraftKings", "under", -115),
        _quote("FanDuel", "over", 100),
        _quote("FanDuel", "under", -120),
        _quote("BetMGM", "over", -110),
        _quote("BetMGM", "under", -110),
    ]

    result = loop.run(
        situation_id="nba:player-7:nba-chi-ny-1",
        player_id="player-7",
        player_name="Example Guard",
        status_signal={
            "report": "Starting guard ruled out.",
            "source": "Official NBA injury report",
            "url": "https://example.com/official-injury-report",
            "reported_at": NOW.isoformat(),
            "reliability": 0.98,
        },
        player_context={
            "current_minutes": 24.0,
            "projected_minutes": 33.0,
            "current_usage_rate": 0.19,
            "projected_usage_rate": 0.25,
            "recent_points_average": 17.8,
            "season_points_average": 14.2,
            "stable_rotation_minutes": True,
            "coaching_context": "Coach said the bench rotation will tighten.",
            "source": "normalized NBA game logs",
            "url": "https://example.com/nba-game-logs",
            "retrieved_at": NOW.isoformat(),
        },
        prediction=prediction,
        quotes=quotes,
        as_of=NOW,
    )

    assert result["assessment"].decision is Decision.QUALIFIED
    assert result["assessment"].best_quote.sportsbook == "FanDuel"
    situation = result["situation"]
    assert situation.current_stage.value == "monitor"
    assert len(situation.evidence_collected) >= 5
    assert situation.active_hypotheses[0]["supporting_evidence"]
    assert situation.forecasts[0]["probability"] == 0.62
    assert any("FanDuel" in action for action in situation.recommended_actions)
    assert situation.monitoring_rules
    assert result["claim_graph"]["nodes"]


def test_nba_loop_abstains_when_books_do_not_have_complete_markets(
    tmp_path: Path,
) -> None:
    lifecycle = IntelligenceLifecycleService(
        SQLiteLifecycleRepository(tmp_path / "lifecycle.db")
    )
    loop = NBAPlayerOpportunityLoop(lifecycle, BettingIntelligenceEngine())
    prediction = ModelPrediction(
        event_id="nba-chi-ny-2",
        market="player_points",
        selection="over",
        line=18.5,
        probability=0.62,
        uncertainty=0.04,
        model_probabilities=(0.60, 0.62, 0.64),
        model_version="player-opportunity-v1",
        generated_at=NOW,
        similar_bet_sample=80,
        similar_bet_brier_score=0.19,
    )
    incomplete = [_quote("DraftKings", "over", -105)]

    result = loop.run(
        situation_id="nba:player-8:nba-chi-ny-2",
        player_id="player-8",
        player_name="Second Guard",
        status_signal={
            "report": "Starter is questionable.",
            "source": "Official NBA injury report",
            "url": "https://example.com/report-2",
            "reported_at": NOW.isoformat(),
        },
        player_context={
            "current_minutes": 20,
            "projected_minutes": 28,
            "current_usage_rate": 0.18,
            "projected_usage_rate": 0.22,
            "recent_points_average": 13,
            "season_points_average": 12,
            "stable_rotation_minutes": True,
            "source": "normalized NBA game logs",
            "url": "https://example.com/logs-2",
            "retrieved_at": NOW.isoformat(),
        },
        prediction=prediction,
        quotes=incomplete,
        as_of=NOW,
    )

    assert result["assessment"].decision is Decision.NO_BET
    assert any(
        "insufficient fresh books with complete markets" in reason
        for reason in result["assessment"].rejection_reasons
    )
    assert any(
        "complete sportsbook markets" in gap
        for gap in result["situation"].evidence_gaps
    )
