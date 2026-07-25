from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sports.betting.engine import BettingIntelligenceEngine
from sports.betting.models import BettingPolicy, ModelPrediction
from sports.betting.normalization import MarketNormalizer
from sports.intelligence.opportunity_ranking import OpportunityRankingEngine
from sports.release.demo import NBAReplayDemo
from sports.release.health import SystemHealthService
from sports.release.settings import ReleaseSettings
from app import create_app


NOW = datetime(2026, 7, 24, 18, tzinfo=timezone.utc)


def _raw_quote(
    book: str,
    outcome: str,
    price: int,
    *,
    fetched_at: datetime | None = None,
    player: str = "Example Guard",
    status: str = "active",
) -> dict[str, object]:
    return {
        "provider_event_id": "provider-chi-ny",
        "home_team": "New York Knicks",
        "away_team": "Chicago Bulls",
        "event_start": (NOW + timedelta(hours=3)).isoformat(),
        "player": player,
        "market": "Player Points",
        "outcome": outcome,
        "outcome_definition": "over_under",
        "line": 18.5,
        "american_price": price,
        "sportsbook": book,
        "fetched_at": (fetched_at or NOW).isoformat(),
        "source": f"{book} licensed feed",
        "source_url": f"https://example.com/{book.lower()}",
        "status": status,
    }


def test_market_normalization_matches_canonical_event_player_and_market() -> None:
    normalizer = MarketNormalizer(
        team_aliases={
            "new york knicks": "nba:team:nyk",
            "chicago bulls": "nba:team:chi",
        },
        player_aliases={"example guard": "nba:player:example-guard"},
        sportsbook_aliases={
            "draftkings": "draftkings",
            "fanduel": "fanduel",
            "betmgm": "betmgm",
        },
        maximum_age_seconds=180,
    )
    result = normalizer.normalize(
        [
            _raw_quote("DraftKings", "Over", -105),
            _raw_quote("DraftKings", "Under", -115),
            _raw_quote("FanDuel", "Over", 100),
            _raw_quote("FanDuel", "Under", -120),
            _raw_quote("BetMGM", "Over", -110, status="suspended"),
            _raw_quote("BetMGM", "Under", -110),
            _raw_quote("UnknownBook", "Over", -105),
            _raw_quote(
                "FanDuel",
                "Over",
                -105,
                player="Unmatched Player",
            ),
        ],
        as_of=NOW,
    )

    assert len(result.rankable_quotes) == 4
    assert {quote.sportsbook for quote in result.rankable_quotes} == {
        "draftkings",
        "fanduel",
    }
    assert all(
        quote.event_id == "nba:game:20260724:chi:nyk"
        for quote in result.rankable_quotes
    )
    assert all(
        quote.canonical_player_id == "nba:player:example-guard"
        for quote in result.rankable_quotes
    )
    reasons = " ".join(result.exclusions)
    assert "BetMGM quote is suspended" in reasons
    assert "sportsbook identity could not be resolved" in reasons
    assert "player identity could not be resolved" in reasons


def test_opportunity_ranking_is_explainable_and_penalizes_uncertainty() -> None:
    normalizer = MarketNormalizer.release_candidate_defaults(
        players={"example guard": "nba:player:example-guard"}
    )
    normalized = normalizer.normalize(
        [
            _raw_quote("DraftKings", "Over", -105),
            _raw_quote("DraftKings", "Under", -115),
            _raw_quote("FanDuel", "Over", 100),
            _raw_quote("FanDuel", "Under", -120),
            _raw_quote("BetMGM", "Over", -110),
            _raw_quote("BetMGM", "Under", -110),
        ],
        as_of=NOW,
    )
    prediction = ModelPrediction(
        event_id="nba:game:20260724:chi:nyk",
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
        canonical_player_id="nba:player:example-guard",
        outcome_definition="over_under",
    )
    assessment = BettingIntelligenceEngine(
        BettingPolicy(
            minimum_edge=0.01,
            minimum_expected_return=0.01,
            minimum_similar_sample=20,
        )
    ).assess(prediction, normalized.rankable_quotes, as_of=NOW)

    ranking = OpportunityRankingEngine().rank(
        [
            {
                "id": "opportunity-1",
                "assessment": assessment,
                "data_completeness": 0.95,
                "evidence_quality": 0.92,
                "source_independence": 0.9,
                "market_freshness": 0.98,
                "role_stability": 0.85,
                "injury_certainty": 0.8,
                "model_reliability": 0.81,
                "liquidity": 0.7,
                "positive_factors": [
                    "Expected minutes increased by 7.5.",
                    "Three sportsbooks are currently available.",
                ],
                "risks": ["Starting lineup is not officially confirmed."],
            }
        ]
    )[0]

    assert 0 < ranking.score <= 100
    assert ranking.rank == 1
    assert ranking.explanation
    assert "Expected minutes increased" in ranking.explanation[0]
    assert ranking.risks
    assert "guarantee" not in " ".join(ranking.explanation).lower()


def test_system_health_returns_actionable_degraded_diagnostic() -> None:
    health = SystemHealthService(
        probes={
            "database": lambda: {
                "status": "healthy",
                "latency_ms": 4,
                "last_successful_refresh": NOW.isoformat(),
            },
            "graph": lambda: {
                "status": "unavailable",
                "cause": "graph service returned HTTP 502",
                "last_successful_refresh": "2026-07-24T14:41:00+00:00",
                "cached_available": True,
                "retry_after_seconds": 45,
                "request_id": "graph-20260724-1842",
            },
        }
    ).snapshot()

    assert health["status"] == "degraded"
    graph = health["components"]["graph"]
    assert graph["cause"] == "graph service returned HTTP 502"
    assert graph["cached_available"] is True
    assert graph["retry_after_seconds"] == 45
    assert graph["request_id"]


def test_production_settings_fail_safely_and_demo_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RAGHUB_SECRET_KEY", raising=False)
    monkeypatch.setenv("RAGHUB_ENV", "production")

    with pytest.raises(ValueError, match="RAGHUB_SECRET_KEY"):
        ReleaseSettings.from_environment()

    monkeypatch.setenv("RAGHUB_ENV", "development")
    monkeypatch.setenv("RAGHUB_DEMO_READ_ONLY", "true")
    settings = ReleaseSettings.from_environment()
    assert settings.demo_read_only is True


def test_replay_demo_closes_observe_to_learn_without_live_network(
    tmp_path: Path,
) -> None:
    result = NBAReplayDemo(tmp_path / "demo.db").run()

    assert result["data_classification"] == "replay"
    assert result["workflow"] == "NBA Player Opportunity Intelligence"
    assert result["situation"]["current_stage"] == "learn"
    assert result["situation"]["final_outcome"]["actual_points"] == 22
    assert result["forecast_evaluation"]["brier_score"] >= 0
    assert "player-opportunity-v1" in result["forecast_evaluation"]["by_model"]
    assert "player_points" in result["forecast_evaluation"]["by_market"]
    assert result["forecast_evaluation"]["minutes_error"]["count"] == 1
    assert result["forecast_evaluation"]["usage_error"]["count"] == 1
    assert result["forecast_evaluation"]["sample_size_warning"]
    assert result["situation"]["lessons_learned"]
    assert result["ranking"]["rank"] == 1
    assert len(result["sportsbooks"]) >= 2
    assert result["reasoning_trace"]


def test_release_routes_render_demo_and_actionable_health(tmp_path: Path) -> None:
    demo = NBAReplayDemo(tmp_path / "route-demo.db")
    health_service = SystemHealthService(
        probes={"database": lambda: {"status": "healthy", "latency_ms": 2}}
    )
    client = create_app(
        health_service=health_service,
        replay_demo_factory=lambda: demo,
    ).test_client()

    health = client.get("/api/system/health")
    demo_api = client.get("/api/demo/nba-opportunity")
    demo_page = client.get("/demo/nba")

    assert health.status_code == 200
    assert health.get_json()["components"]["database"]["latency_ms"] == 2
    assert demo_api.get_json()["data_classification"] == "replay"
    assert demo_page.status_code == 200
    assert b"HISTORICAL REPLAY" in demo_page.data
    assert b"Observe" in demo_page.data
    assert b"Learn" in demo_page.data


def test_read_only_demo_mode_blocks_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAGHUB_ENV", "development")
    monkeypatch.setenv("RAGHUB_DEMO_READ_ONLY", "true")
    client = create_app().test_client()

    response = client.post("/refresh")

    assert response.status_code == 403
    assert response.get_json()["error"] == (
        "This deployment is in read-only demo mode."
    )
    assert response.headers["X-Request-ID"]
