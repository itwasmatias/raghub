from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sports.betting.engine import BettingIntelligenceEngine
from sports.betting.models import BettingPolicy, ModelPrediction
from sports.betting.normalization import MarketNormalizer
from sports.data.repositories.sqlite_lifecycle_repository import (
    SQLiteLifecycleRepository,
)
from sports.intelligence.lifecycle_service import IntelligenceLifecycleService
from sports.intelligence.nba_opportunity_loop import NBAPlayerOpportunityLoop
from sports.intelligence.opportunity_ranking import OpportunityRankingEngine


class NBAReplayDemo:
    """Deterministic, clearly labeled replay of the NBA flagship workflow."""

    NOW = datetime(2026, 1, 15, 18, tzinfo=timezone.utc)

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def run(self) -> dict[str, object]:
        lifecycle = IntelligenceLifecycleService(
            SQLiteLifecycleRepository(self.database_path)
        )
        situation_id = "demo:historical:nba-chi-ny:example-guard"
        if lifecycle.get_situation(situation_id) is not None:
            # The demo database is disposable; use a unique replay identity while
            # retaining the stable scenario label.
            situation_id = (
                f"{situation_id}:{len(lifecycle.repository.list_situation_ids()) + 1}"
            )
        normalizer = MarketNormalizer.release_candidate_defaults(
            players={"example guard": "nba:player:example-guard"}
        )
        normalized = normalizer.normalize(self._raw_quotes(), as_of=self.NOW)
        prediction = ModelPrediction(
            event_id="nba:game:20260115:chi:nyk",
            market="player_points",
            selection="over",
            line=18.5,
            probability=0.62,
            uncertainty=0.04,
            model_probabilities=(0.60, 0.62, 0.64),
            model_version="player-opportunity-v1",
            generated_at=self.NOW,
            similar_bet_sample=80,
            similar_bet_brier_score=0.19,
            reasons=(
                "Expected minutes increased from 24.0 to 33.0.",
                "Recent scoring was above the season baseline.",
                "The player already held stable rotation minutes.",
            ),
            invalidators=(
                "The player is not in the confirmed starting lineup.",
                "A minutes restriction is announced.",
            ),
            canonical_player_id="nba:player:example-guard",
            outcome_definition="over_under",
            forecast_type="player_points_over",
            team_id="nba:team:chi",
            player_role="rotation_guard",
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
        loop_result = loop.run(
            situation_id=situation_id,
            player_id="nba:player:example-guard",
            player_name="Example Guard",
            status_signal={
                "report": "Starting guard was ruled out before the game.",
                "source": "Historical official injury report",
                "url": "https://example.com/replay/official-injury-report",
                "reported_at": self.NOW.isoformat(),
                "reliability": 0.98,
            },
            player_context={
                "current_minutes": 24.0,
                "projected_minutes": 33.0,
                "current_usage_rate": 0.19,
                "projected_usage_rate": 0.25,
                "recent_points_average": 17.8,
                "season_points_average": 14.2,
                "previous_season_points_average": 12.9,
                "weighted_points_average": 15.1,
                "stable_rotation_minutes": True,
                "coaching_context": (
                    "Qualitative replay evidence: the coach said the bench "
                    "rotation would tighten."
                ),
                "source": "Historical normalized NBA game logs",
                "url": "https://example.com/replay/nba-game-logs",
                "retrieved_at": self.NOW.isoformat(),
                "reliability": 0.9,
            },
            prediction=prediction,
            quotes=normalized.rankable_quotes,
            as_of=self.NOW,
        )
        situation = loop_result["situation"]
        forecast_id = str(situation.forecasts[0]["id"])
        lifecycle.record_outcome(
            situation_id,
            {
                "forecast_id": forecast_id,
                "occurred": True,
                "actual_points": 22,
                "actual_minutes": 34,
                "expected_minutes": 33,
                "actual_usage_rate": 0.26,
                "expected_usage_rate": 0.25,
                "closing_line": 20.0,
                "opening_line": 18.5,
                "market_moved_in_forecast_direction": True,
                "notes": "Historical replay result: player scored 22 points.",
            },
            occurred_at=(self.NOW + timedelta(hours=6)).isoformat(),
        )
        lifecycle.record_lesson(
            situation_id,
            (
                "In this replay, stable prior rotation minutes plus a confirmed "
                "availability change were useful early indicators; one case is "
                "not enough to generalize."
            ),
            occurred_at=(self.NOW + timedelta(hours=6, minutes=2)).isoformat(),
        )
        completed = lifecycle.get_situation(situation_id)
        if completed is None:  # pragma: no cover - repository contract guard
            raise RuntimeError("Replay situation could not be reconstructed")
        assessment = loop_result["assessment"]
        ranking = OpportunityRankingEngine().rank(
            [
                {
                    "id": situation_id,
                    "assessment": assessment,
                    "data_completeness": 0.95,
                    "evidence_quality": 0.92,
                    "source_independence": 0.9,
                    "market_freshness": 0.98,
                    "role_stability": 0.85,
                    "injury_certainty": 0.95,
                    "model_reliability": 0.81,
                    "liquidity": 0.7,
                    "positive_factors": [
                        "Expected minutes increased by 9.0.",
                        "Recent scoring exceeded the season baseline.",
                        "Three complete sportsbook markets were available.",
                    ],
                    "risks": [
                        "Historical replay sample is illustrative.",
                        "Starting role was uncertain when the first forecast was made.",
                    ],
                }
            ]
        )[0]
        quality = lifecycle.evaluate_forecasts()
        return {
            "version": "1.0.0-rc1",
            "workflow": "NBA Player Opportunity Intelligence",
            "data_classification": "replay",
            "scenario_notice": (
                "This deterministic replay uses labeled historical-style fixture "
                "data and never places a wager."
            ),
            "situation": asdict(completed),
            "sportsbooks": sorted(
                {quote.sportsbook for quote in normalized.rankable_quotes}
            ),
            "market_exclusions": normalized.exclusions,
            "assessment": {
                "decision": assessment.decision.value,
                "expected_return": assessment.expected_return,
                "probability_edge": assessment.probability_edge,
                "best_sportsbook": (
                    assessment.best_quote.sportsbook
                    if assessment.best_quote
                    else None
                ),
                "rejection_reasons": list(assessment.rejection_reasons),
            },
            "market_assessment": asdict(assessment),
            "ranking": asdict(ranking),
            "forecast_evaluation": quality,
            "reasoning_trace": lifecycle.timeline(situation_id),
            "claim_graph": lifecycle.claim_graph(situation_id),
            "research_memory": lifecycle.build_research_memory(),
        }

    def _raw_quotes(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        prices = {
            "DraftKings": {"Over": -105, "Under": -115},
            "FanDuel": {"Over": 100, "Under": -120},
            "BetMGM": {"Over": -110, "Under": -110},
        }
        for book, outcomes in prices.items():
            for outcome, price in outcomes.items():
                rows.append(
                    {
                        "provider_event_id": "replay-chi-ny",
                        "home_team": "New York Knicks",
                        "away_team": "Chicago Bulls",
                        "event_start": (
                            self.NOW + timedelta(hours=3)
                        ).isoformat(),
                        "player": "Example Guard",
                        "market": "Player Points",
                        "outcome": outcome,
                        "outcome_definition": "over_under",
                        "line": 18.5,
                        "american_price": price,
                        "sportsbook": book,
                        "fetched_at": (
                            self.NOW - timedelta(seconds=30)
                        ).isoformat(),
                        "source": f"{book} historical replay feed",
                        "source_url": (
                            f"https://example.com/replay/{book.lower()}"
                        ),
                        "status": "active",
                    }
                )
        return rows
