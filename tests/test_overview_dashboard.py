from __future__ import annotations

from copy import deepcopy

from app import create_app
from services.overview import OverviewService


class FakeSettings:
    odds_provider = "odds_api_io"


class FakePersonalService:
    settings = FakeSettings()

    def __init__(self, payload):
        self.payload = payload
        self.snapshot_calls = 0

    def snapshot(self):
        self.snapshot_calls += 1
        return deepcopy(self.payload)

    def refresh(self):
        raise AssertionError("overview must not refresh providers")


class FakeSituationRepository:
    def __init__(self, *, forecasts=None, evidence=None):
        self._forecasts = forecasts or []
        self._evidence = evidence or []

    def list_forecasts(self):
        return deepcopy(self._forecasts)

    def list_evidence(self):
        return deepcopy(self._evidence)

    def count_forecasts(self):
        return len(self._forecasts)

    def count_evidence(self):
        return len(self._evidence)

    def get_latest_autonomy_cycle(self):
        return None


class FakeSituationRoomService:
    def __init__(self, repository):
        self.repository = repository

    def get_snapshot(self, **_kwargs):
        raise AssertionError("overview must not call live Situation Room providers")


class FakeComputeRepository:
    def health(self):
        return {
            "status": "healthy",
            "registered_workers": 1,
            "online_workers": 1,
            "queued_jobs": 0,
        }


class FakeRuntime:
    def get_dashboard_snapshot(self, **_kwargs):
        return {}


def sports_snapshot():
    return {
        "generated_at": "2026-07-25T14:00:00+00:00",
        "mode": "live",
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
                "WNBA": {
                    "status": "ready",
                    "calibration_status": "calibrated",
                    "version": "wnba-v1",
                },
                "MLB": {
                    "status": "MODEL_NOT_TRAINED",
                    "calibration_status": "not_trained",
                    "detail": "No validated artifact.",
                },
            },
        },
        "league_summary": {
            "WNBA": {
                "upcoming_events": 1,
                "evaluations": 2,
                "qualified_choices": 1,
                "no_bet_results": 1,
                "resolved_predictions": 1,
            },
            "MLB": {
                "upcoming_events": 0,
                "evaluations": 0,
                "qualified_choices": 0,
                "no_bet_results": 0,
                "resolved_predictions": 0,
            },
        },
        "events": [
            {
                "canonical_id": "wnba:2026-07-25:sea-chi",
                "league": "WNBA",
                "away_team_name": "Seattle",
                "home_team_name": "Chicago",
                "start_time": "2026-07-25T20:00:00+00:00",
                "complete_books": 2,
                "quotes": [
                    {
                        "sportsbook": "book-a",
                        "observed_at": "2026-07-25T13:59:00+00:00",
                    }
                ],
                "forecasts": [{"model_version": "wnba-v1"}],
            }
        ],
        "evaluations": [
            {
                "canonical_event_id": "wnba:2026-07-25:sea-chi",
                "selection": "home",
                "qualified": True,
                "status": "qualified",
                "reason_codes": [],
                "best_sportsbook": "book-a",
                "best_price": 115,
                "model_probability": 0.58,
                "market_probability": 0.52,
                "edge": 0.06,
                "expected_value": 0.08,
                "data_quality": 0.9,
                "evaluated_at": "2026-07-25T13:59:00+00:00",
                "evidence": ["Two complete books."],
            },
            {
                "canonical_event_id": "wnba:2026-07-25:sea-chi",
                "selection": "away",
                "qualified": False,
                "status": "no_bet",
                "reason_codes": ["EDGE_BELOW_THRESHOLD"],
                "best_sportsbook": "book-b",
                "best_price": -120,
                "model_probability": 0.42,
                "market_probability": 0.48,
                "edge": -0.06,
                "expected_value": -0.07,
                "data_quality": 0.9,
                "evaluated_at": "2026-07-25T13:59:00+00:00",
                "evidence": ["Edge gate failed."],
            },
        ],
        "resolved_predictions": [
            {
                "canonical_event_id": "wnba:resolved",
                "league": "WNBA",
                "correct": True,
                "brier_contribution": 0.16,
                "log_loss_contribution": 0.51,
                "model_version": "wnba-v1",
                "resolved_at": "2026-07-24T22:00:00+00:00",
            }
        ],
        "wnba_performance": {
            "resolved_predictions": 1,
            "correct_predictions": 1,
            "accuracy": 1.0,
            "brier_score": 0.16,
            "log_loss": 0.51,
        },
        "scheduler": {
            "running": False,
            "last_run_at": "2026-07-25T13:59:00+00:00",
            "next_run_at": None,
            "recent_jobs": [],
        },
    }


def build_service(payload=None, *, forecasts=None, evidence=None):
    personal = FakePersonalService(payload or sports_snapshot())
    situation = FakeSituationRoomService(
        FakeSituationRepository(forecasts=forecasts, evidence=evidence)
    )
    service = OverviewService(
        personal_service=personal,
        situation_room_service=situation,
        compute_repository=FakeComputeRepository(),
    )
    return service, personal


def test_overview_contract_separates_leagues_and_preserves_decisions():
    service, personal = build_service()

    payload = service.snapshot()

    assert payload["schema_version"] == "overview.v1"
    assert set(payload["sports"]) == {"WNBA", "MLB"}
    decisions = payload["sports"]["WNBA"]["predictions"][0]["decisions"]
    assert [item["status"] for item in decisions] == ["qualified", "no_bet"]
    assert payload["sports"]["MLB"]["counts"]["upcoming_events"] == {
        "status": "available",
        "value": 0,
    }
    assert payload["sports"]["MLB"]["performance"]["status"] == "unavailable"
    assert payload["sports"]["MLB"]["performance"]["sample_size"] == 0
    assert payload["sports"]["MLB"]["performance"]["accuracy"] is None
    assert personal.snapshot_calls == 1


def test_overview_maps_stale_and_error_without_discarding_persisted_values():
    snapshot = sports_snapshot()
    snapshot["feed"].update(
        {
            "status": "degraded",
            "freshness": "stale",
            "error": "Provider rate limit.",
        }
    )
    service, _ = build_service(snapshot)

    payload = service.snapshot()

    assert payload["sports"]["WNBA"]["status"] == "stale"
    assert payload["sports"]["WNBA"]["provider"]["status"] == "error"
    assert payload["sports"]["WNBA"]["provider"]["freshness"] == "stale"
    assert payload["sports"]["WNBA"]["counts"]["upcoming_events"]["value"] == 1
    assert payload["notices"][0]["status"] == "stale"


def test_overview_marks_counts_unavailable_when_feed_never_refreshed():
    snapshot = sports_snapshot()
    snapshot["feed"].update(
        {
            "configured": False,
            "status": "not_refreshed",
            "freshness": "unavailable",
            "last_successful_refresh": None,
        }
    )
    snapshot["league_summary"]["MLB"]["upcoming_events"] = 0
    service, _ = build_service(snapshot)

    mlb = service.snapshot()["sports"]["MLB"]

    assert mlb["counts"]["upcoming_events"] == {
        "status": "unavailable",
        "value": None,
    }
    assert mlb["provider"]["status"] == "unavailable"


def test_overview_uses_only_persisted_situation_room_records():
    forecasts = [
        {
            "forecast_id": "forecast-1",
            "question": "Will CPI remain above 3%?",
            "current_probability": 0.61,
            "probability_change": 0.03,
            "resolution_deadline": "2026-10-01T00:00:00+00:00",
            "calibration_status": "experimental",
            "last_updated_at": "2026-07-25T12:00:00+00:00",
            "supporting_evidence": [{"evidence_id": "evidence-1"}],
            "contradicting_evidence": [],
        }
    ]
    evidence = [
        {
            "evidence_id": "evidence-1",
            "title": "Official CPI release",
            "source": "FRED",
            "url": "https://example.test/evidence",
            "observed_at": "2026-07-25T12:00:00+00:00",
        }
    ]
    service, _ = build_service(forecasts=forecasts, evidence=evidence)

    payload = service.snapshot()

    assert payload["situation_room"]["status"] == "available"
    assert payload["situation_room"]["active_forecasts"][0]["forecast_id"] == "forecast-1"
    assert payload["situation_room"]["briefs"] == []
    assert payload["situation_room"]["briefs_status"] == "unavailable"
    assert payload["evidence"]["persisted_records"] == {
        "status": "available",
        "value": 1,
    }
    assert payload["evidence"]["supporting_links"]["value"] == 1
    assert payload["evidence"]["contradicting_links"]["value"] == 0


def test_overview_sections_fail_independently():
    class BrokenSituationRepository(FakeSituationRepository):
        def list_forecasts(self):
            raise RuntimeError("database unavailable")

    personal = FakePersonalService(sports_snapshot())
    service = OverviewService(
        personal_service=personal,
        situation_room_service=FakeSituationRoomService(BrokenSituationRepository()),
        compute_repository=FakeComputeRepository(),
    )

    payload = service.snapshot()

    assert payload["sports"]["WNBA"]["status"] == "ready"
    assert payload["situation_room"]["status"] == "error"
    assert payload["evidence"]["status"] == "error"
    assert payload["system_health"]["compute"]["status"] == "healthy"
    assert payload["status"] == "partial"


def test_overview_routes_render_without_changing_home(tmp_path):
    service, _ = build_service()
    app = create_app(
        runtime=FakeRuntime(),
        personal_service=FakePersonalService(sports_snapshot()),
        situation_room_service=FakeSituationRoomService(FakeSituationRepository()),
        overview_service=service,
    )
    client = app.test_client()

    api_response = client.get("/api/overview")
    page_response = client.get("/overview")

    assert api_response.status_code == 200
    assert api_response.get_json()["schema_version"] == "overview.v1"
    assert page_response.status_code == 200
    html = page_response.get_data(as_text=True)
    assert "Unified Overview" in html
    assert "/static/overview.css" in html
    assert "/static/overview.js" in html
    home_rule = next(rule for rule in app.url_map.iter_rules() if rule.rule == "/")
    assert home_rule.endpoint == "home"


def test_overview_page_excludes_legacy_synthetic_metrics():
    service, _ = build_service()
    app = create_app(
        runtime=FakeRuntime(),
        personal_service=FakePersonalService(sports_snapshot()),
        situation_room_service=FakeSituationRoomService(FakeSituationRepository()),
        overview_service=service,
    )

    html = app.test_client().get("/overview").get_data(as_text=True)

    for forbidden in (
        "SIP Confidence Index",
        "Market Efficiency",
        "Featured Matchup",
        "Last 10 Games",
        "Integrations 4",
    ):
        assert forbidden not in html
