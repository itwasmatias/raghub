from datetime import datetime, timedelta, timezone
from io import BytesIO

from sports.betting.engine import BettingIntelligenceEngine
from sports.betting.models import ModelPrediction, OddsQuote
from web_ui import FeatureUIHandler


NOW = datetime(2026, 1, 15, 18, tzinfo=timezone.utc)


def _quote(book: str, selection: str, price: int, age_seconds: int = 20) -> OddsQuote:
    return OddsQuote(
        event_id="nba-1",
        market="player_points",
        selection=selection,
        sportsbook=book,
        line=22.5,
        american_price=price,
        event_start=NOW + timedelta(hours=2),
        fetched_at=NOW - timedelta(seconds=age_seconds),
        source="injected-test-feed",
    )


def _assessment():
    prediction = ModelPrediction(
        event_id="nba-1",
        market="player_points",
        selection="over",
        line=22.5,
        probability=0.58,
        uncertainty=0.04,
        model_probabilities=(0.56, 0.58, 0.60),
        model_version="sip-v1",
        generated_at=NOW,
        similar_bet_sample=120,
        similar_bet_brier_score=0.20,
        reasons=("Projected minutes increased",),
        invalidators=("Starter status changes",),
    )
    quotes = [
        _quote("Book A", "over", -110),
        _quote("Book A", "under", -110),
        _quote("Book B", "over", 100),
        _quote("Book B", "under", -120),
        _quote("Old Book", "over", 120, 600),
        _quote("Old Book", "under", -140, 600),
    ]
    return BettingIntelligenceEngine().assess(prediction, quotes, as_of=NOW)


def _capture_get(handler: FeatureUIHandler, path: str) -> tuple[int, str]:
    captured: dict[str, int] = {}
    handler.path = path
    handler.wfile = BytesIO()
    handler.send_response = lambda status: captured.__setitem__("status", status)
    handler.send_header = lambda _name, _value: None
    handler.end_headers = lambda: None
    FeatureUIHandler.do_GET(handler)
    return captured["status"], handler.wfile.getvalue().decode()


def test_betting_route_renders_decision_metrics_prices_and_risk_context():
    handler = object.__new__(FeatureUIHandler)
    handler.betting_assessment_provider = lambda: [_assessment()]

    status, html = _capture_get(handler, "/betting")

    assert status == 200
    assert "Betting Intelligence" in html
    assert "Qualified edge" in html
    assert "58.0%" in html
    assert "Book B" in html
    assert "+100" in html
    assert "Projected minutes increased" in html
    assert "Starter status changes" in html
    assert "stale quotes excluded: Old Book" in html
    assert "No-vig consensus" in html


def test_betting_route_has_honest_empty_state_without_market_data():
    handler = object.__new__(FeatureUIHandler)
    handler.betting_assessment_provider = lambda: []

    status, html = _capture_get(handler, "/betting")

    assert status == 200
    assert "No market assessments yet" in html
    assert "No bet is the default" in html


def test_betting_route_separates_live_status_from_replay_best_bet_visuals():
    handler = object.__new__(FeatureUIHandler)
    handler.betting_board_provider = lambda: {
        "data_classification": "live",
        "status": "unconfigured",
        "assessments": [],
        "message": "Configure ODDS_API_KEY for the selected provider and run Refresh.",
        "exclusions": ["No live sportsbook records are available."],
        "source_health": {"status": "unconfigured"},
        "generated_at": NOW.isoformat(),
    }
    handler.betting_replay_provider = lambda: {
        "data_classification": "replay",
        "status": "ready",
        "assessments": [_assessment()],
        "ranking": {"rank": 1, "score": 8.4},
        "message": "Historical replay only.",
        "exclusions": [],
        "source_health": {"status": "replay"},
        "generated_at": NOW.isoformat(),
    }

    status, html = _capture_get(handler, "/betting")

    assert status == 200
    assert "LIVE DATA UNAVAILABLE" in html
    assert "HISTORICAL REPLAY" in html
    assert "Illustrative replay best option" in html
    assert "probability-bar" in html
    assert "SIP 58.0%" in html
    assert "Market" in html
