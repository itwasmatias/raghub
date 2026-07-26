from __future__ import annotations

from copy import deepcopy

from app import create_app


class FakeRuntime:
    def refresh(self):
        return None

    def load_history(self):
        return None

    def get_dashboard_snapshot(self, **_kwargs):
        return {}


class FakePersonalService:
    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.refresh_calls = 0

    def snapshot(self):
        return deepcopy(self._snapshot)

    def refresh(self):
        self.refresh_calls += 1
        raise AssertionError("experience routes must not trigger refresh")

    def wnba_snapshot(self):
        return {
            "feed": self._snapshot["feed"],
            "league_summary": self._snapshot["league_summary"]["WNBA"],
            "events": [
                event for event in self._snapshot["events"] if event["league"] == "WNBA"
            ],
            "evaluations": [
                row
                for row in self._snapshot["evaluations"]
                if row["canonical_event_id"].startswith("wnba:")
            ],
            "wnba_performance": self._snapshot["wnba_performance"],
            "resolved_predictions": self._snapshot["resolved_predictions"],
            "generated_at": self._snapshot["generated_at"],
        }

    def betting_board(self):
        return {
            "status": "ready",
            "assessments": [],
            "generated_at": self._snapshot["generated_at"],
            "source_health": self._snapshot["feed"],
            "message": "No market assessments yet",
        }


class FakeSituationRepository:
    def count_forecasts(self):
        return 0

    def count_evidence(self):
        return 0


class FakeSituationRoomService:
    repository = FakeSituationRepository()

    def get_latest_autonomy_cycle(self):
        return None


class FakeComputeRepository:
    def health(self):
        return {
            "status": "healthy",
            "registered_workers": 0,
            "online_workers": 0,
            "queued_jobs": 0,
        }


def base_snapshot():
    return {
        "version": "1.0.0",
        "edition": "Personal Edition",
        "mode": "live",
        "supported_leagues": ["NBA", "WNBA", "MLB"],
        "feed": {
            "configured": True,
            "enabled": True,
            "status": "healthy",
            "last_attempt_at": "2026-07-25T13:59:00+00:00",
            "last_successful_refresh": "2026-07-25T13:59:00+00:00",
            "events_received": 2,
            "complete_books": 4,
            "freshness": "fresh",
            "error": None,
        },
        "model": {
            "status": "PARTIALLY_TRAINED",
            "calibration_status": "partial",
            "leagues": {
                "NBA": {
                    "calibration_status": "not_trained",
                    "status": "MODEL_NOT_TRAINED",
                },
                "WNBA": {
                    "calibration_status": "calibrated",
                    "status": "ready",
                    "version": "wnba-moneyline-baseline-v1",
                },
                "MLB": {
                    "calibration_status": "calibrated",
                    "status": "ready",
                    "version": "mlb-moneyline-baseline-v1",
                },
            },
        },
        "league_summary": {
            "NBA": {
                "upcoming_events": 0,
                "evaluations": 0,
                "qualified_choices": 0,
                "no_bet_results": 0,
                "resolved_predictions": 0,
            },
            "WNBA": {
                "upcoming_events": 1,
                "evaluations": 2,
                "qualified_choices": 1,
                "no_bet_results": 1,
                "resolved_predictions": 0,
            },
            "MLB": {
                "upcoming_events": 1,
                "evaluations": 2,
                "qualified_choices": 1,
                "no_bet_results": 1,
                "resolved_predictions": 0,
            },
        },
        "events": [
            {
                "canonical_id": "wnba:20260725:sea:chi",
                "league": "WNBA",
                "away_team_name": "Seattle Storm",
                "home_team_name": "Chicago Sky",
                "start_time": "2026-07-25T20:00:00+00:00",
                "venue": "Wintrust Arena",
                "complete_books": 2,
                "quotes": [
                    {
                        "sportsbook": "draftkings",
                        "selection": "home",
                        "american_price": 115,
                        "observed_at": "2026-07-25T13:58:00+00:00",
                    },
                    {
                        "sportsbook": "draftkings",
                        "selection": "away",
                        "american_price": -120,
                        "observed_at": "2026-07-25T13:58:00+00:00",
                    },
                ],
                "season_context": {
                    "current_season": "Available",
                    "previous_season": "Available",
                },
                "forecasts": [
                    {
                        "model_version": "wnba-moneyline-baseline-v1",
                        "contributing_factors": ["pace", "rest"],
                    }
                ],
            },
            {
                "canonical_id": "mlb:20260725:nym:lad",
                "league": "MLB",
                "away_team_name": "New York Mets",
                "home_team_name": "Los Angeles Dodgers",
                "start_time": "2026-07-25T23:10:00+00:00",
                "venue": "Dodger Stadium",
                "complete_books": 1,
                "quotes": [
                    {
                        "sportsbook": "fanduel",
                        "selection": "home",
                        "american_price": -130,
                        "observed_at": "2026-07-25T13:58:00+00:00",
                    }
                ],
                "season_context": {
                    "current_season": "Unavailable",
                    "previous_season": "Unavailable",
                },
                "forecasts": [
                    {
                        "model_version": "mlb-moneyline-baseline-v1",
                        "contributing_factors": ["bullpen", "park"],
                    }
                ],
            },
        ],
        "evaluations": [
            {
                "canonical_event_id": "wnba:20260725:sea:chi",
                "selection": "home",
                "qualified": True,
                "status": "qualified",
                "reason_codes": [],
                "best_sportsbook": "draftkings",
                "best_price": 115,
                "model_probability": 0.58,
                "market_probability": 0.52,
                "edge": 0.06,
                "expected_value": 0.08,
                "data_quality": 0.91,
                "evaluated_at": "2026-07-25T13:59:00+00:00",
                "evidence": ["Two complete books."],
                "diagnostics": {
                    "odds_feed": {"passed": True},
                    "complete_books": {"actual": 2, "required": 2},
                    "event_pairing": {"passed": True},
                    "forecast": {"passed": True},
                    "calibration": {"passed": True},
                    "data_quality": {"actual": 0.91, "required": 0.75},
                    "edge": {"required": 0.03},
                    "next_actions": [],
                },
            },
            {
                "canonical_event_id": "wnba:20260725:sea:chi",
                "selection": "away",
                "qualified": False,
                "status": "no_bet",
                "reason_codes": ["EDGE_BELOW_THRESHOLD"],
                "best_sportsbook": "fanduel",
                "best_price": -120,
                "model_probability": 0.42,
                "market_probability": 0.48,
                "edge": -0.06,
                "expected_value": -0.07,
                "data_quality": 0.91,
                "evaluated_at": "2026-07-25T13:59:00+00:00",
                "evidence": ["Edge gate failed."],
                "diagnostics": {
                    "odds_feed": {"passed": True},
                    "complete_books": {"actual": 2, "required": 2},
                    "event_pairing": {"passed": True},
                    "forecast": {"passed": True},
                    "calibration": {"passed": True},
                    "data_quality": {"actual": 0.91, "required": 0.75},
                    "edge": {"required": 0.03},
                    "next_actions": ["No-bet: edge too small."],
                },
            },
        ],
        "qualified_choices": [],
        "no_bet_results": [],
        "thresholds": {
            "minimum_model_edge": 0.03,
            "minimum_data_quality": 0.75,
            "minimum_complete_books": 2,
            "odds_max_age_seconds": 600,
            "forecast_max_age_seconds": 3600,
        },
        "scheduler": {
            "running": False,
            "last_run_at": "2026-07-25T13:59:00+00:00",
            "next_run_at": None,
            "recent_jobs": [],
        },
        "resolved_predictions": [],
        "wnba_performance": {
            "resolved_predictions": 0,
            "correct_predictions": 0,
            "accuracy": None,
            "brier_score": None,
            "log_loss": None,
        },
        "research": {
            "player_intelligence_route": "/player",
            "availability": "NBA player intelligence is the primary research path.",
        },
        "generated_at": "2026-07-25T14:00:00+00:00",
    }


def make_client(snapshot=None):
    service = FakePersonalService(snapshot or base_snapshot())
    app = create_app(
        personal_service=service,
        situation_room_service=FakeSituationRoomService(),
        compute_repository=FakeComputeRepository(),
    )
    return app.test_client(), service


def test_experience_page_has_plain_language_explanations_and_help():
    client, _ = make_client()

    html = client.get("/").get_data(as_text=True)

    assert "SIP estimates this team wins" in html
    assert "The sportsbook price implies approximately" in html
    assert "The estimated advantage is" in html
    assert "No-bet: the estimated advantage is too small." in html
    assert "No-bet: fewer than two complete sportsbook quotes are available." in html
    assert "How to read this card" in html


def test_practice_calculator_label_and_no_place_bet_button():
    client, _ = make_client()

    html_home = client.get("/").get_data(as_text=True)
    html_betting = client.get("/betting").get_data(as_text=True)

    label = "Practice Bet Calculator"
    assert label in html_home
    assert "estimates hypothetical returns only" in html_home
    assert "does not place a wager" in html_home
    assert label in html_betting
    assert "Place Bet" not in html_home
    assert "Place Bet" not in html_betting


def test_practice_calculator_api_uses_backend_calculator_only():
    client, service = make_client()

    response = client.post(
        "/api/sip/practice-wager",
        json={
            "american_odds": 110,
            "wager_amount_usd": "100",
            "model_probability": "0.58",
            "market_implied_probability": "0.52",
        },
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["potential_profit_usd"] == "110.00"
    assert payload["total_return_usd"] == "210.00"
    assert payload["break_even_probability"] == "0.4762"
    assert payload["expected_profit_usd"] == "21.80"
    assert payload["expected_return_percentage"] == "21.80"
    assert service.refresh_calls == 0


def test_practice_calculator_validation_errors():
    client, _ = make_client()

    zero_odds = client.post(
        "/api/sip/practice-wager",
        json={"american_odds": 0, "wager_amount_usd": "10"},
    )
    bad_probability = client.post(
        "/api/sip/practice-wager",
        json={
            "american_odds": 110,
            "wager_amount_usd": "10",
            "model_probability": "1.2",
        },
    )

    assert zero_odds.status_code == 400
    assert "cannot be zero" in zero_odds.get_json()["error"]
    assert bad_probability.status_code == 400
    assert "between 0 and 1" in bad_probability.get_json()["error"]


def test_stale_quote_and_missing_data_warning_are_visible():
    snapshot = base_snapshot()
    snapshot["feed"]["freshness"] = "stale"
    snapshot["feed"]["error"] = "Quotes are stale"
    snapshot["events"][1]["forecasts"] = []

    client, _ = make_client(snapshot)
    html = client.get("/").get_data(as_text=True)

    assert "Stale quote warning" in html
    assert "Important missing information" in html


def test_color_independent_status_labels_and_no_synthetic_metrics():
    client, _ = make_client()
    html = client.get("/").get_data(as_text=True)

    assert "Qualified analytical choice" in html
    assert "No-bet result" in html
    for forbidden in (
        "SIP Confidence Index",
        "Market Efficiency",
        "Featured Matchup",
        "Last 10 Games",
        "fallback 50% hypothesis support",
        "All Systems Operational",
    ):
        assert forbidden not in html


def test_mobile_layout_has_no_horizontal_overflow():
    css = open("static/sip_experience.css", encoding="utf-8").read()

    assert "overflow-x: hidden" in css or "overflow-x:hidden" in css
    assert "max-width: 100%" in css or "max-width:100%" in css


def test_existing_forecast_and_qualification_values_remain_unchanged():
    snapshot = base_snapshot()
    client, _ = make_client(snapshot)

    before = deepcopy(snapshot["evaluations"])
    dashboard = client.get("/api/sip/dashboard").get_json()
    client.get("/")
    client.get("/betting")
    after_dashboard = client.get("/api/sip/dashboard").get_json()

    assert dashboard["evaluations"] == before
    assert after_dashboard["evaluations"] == before
