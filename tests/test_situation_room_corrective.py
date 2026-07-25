from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import create_app
from intelligence.repository import SituationRoomRepository
from intelligence.service import SituationRoomService


NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


def _forecast(evidence_id: str) -> dict:
    return {
        "forecast_id": "fred-cpi-forecast",
        "question": "Will CPI inflation remain at or above 3% by October 2026?",
        "domain": "macroeconomics",
        "outcome_options": ["Yes", "No"],
        "created_at": NOW.isoformat(),
        "resolution_deadline": (NOW + timedelta(days=100)).isoformat(),
        "resolution_criteria": "Resolve Yes when official FRED CPIAUCSL data is at least 3% year over year.",
        "resolution_source": {
            "name": "Federal Reserve Bank of St. Louis FRED",
            "url": "https://fred.stlouisfed.org/series/CPIAUCSL",
        },
        "prior_probability": 0.5,
        "current_probability": 0.64,
        "base_rate_explanation": "Experimental persistence rule anchored to the latest official CPI trend.",
        "supporting_evidence": [
            {"evidence_id": evidence_id, "explanation": "Latest CPI remains above 3%."}
        ],
        "contradicting_evidence": [],
        "major_assumptions": ["No discontinuity in the CPI series."],
        "update_triggers": ["New CPIAUCSL observation"],
        "calibration_status": "experimental",
        "calibration_note": "Calibration unavailable: insufficient resolved outcomes",
        "model_version": "fred-cpi-persistence-v1",
        "resolution_status": "open",
        "previous_probability": 0.5,
        "probability_change": 0.14,
        "why_probability_changed": "The latest official CPI observation remains elevated.",
        "increase_triggers": ["CPI accelerates"],
        "decrease_triggers": ["CPI falls below 3%"],
        "last_updated_at": NOW.isoformat(),
        "forecast_horizon": "100 days",
    }


def test_persisted_forecast_and_evidence_are_retrievable_from_same_repository(
    tmp_path,
):
    repository = SituationRoomRepository(tmp_path / "situation-room.db")
    evidence = repository.upsert_evidence(
        {
            "title": "FRED CPI observation",
            "source": "FRED",
            "summary": "Official CPI series refreshed.",
            "url": "https://fred.stlouisfed.org/series/CPIAUCSL",
            "observed_at": NOW.isoformat(),
        }
    )
    repository.upsert_forecast(_forecast(evidence["evidence_id"]))

    assert repository.count_evidence() == len(repository.list_evidence()) == 1
    assert repository.count_forecasts() == len(repository.list_forecasts()) == 1
    stored = repository.get_forecast("fred-cpi-forecast")
    assert stored["current_probability"] == 0.64
    assert stored["supporting_evidence"][0]["evidence_id"] == evidence["evidence_id"]
    assert repository.get_forecast_history("fred-cpi-forecast")


def test_forecast_apis_and_dashboard_render_openable_real_records(tmp_path):
    repository = SituationRoomRepository(tmp_path / "situation-room.db")
    evidence = repository.upsert_evidence(
        {
            "title": "FRED CPI observation",
            "source": "FRED",
            "summary": "Official CPI series refreshed.",
            "url": "https://fred.stlouisfed.org/series/CPIAUCSL",
            "observed_at": NOW.isoformat(),
        }
    )
    repository.upsert_forecast(_forecast(evidence["evidence_id"]))
    service = SituationRoomService(repository=repository)
    app = create_app(situation_room_service=service)
    client = app.test_client()

    listing = client.get("/api/forecasts")
    detail = client.get("/api/forecasts/fred-cpi-forecast")
    history = client.get("/api/forecasts/fred-cpi-forecast/history")
    page = client.get("/situation-room")

    assert listing.status_code == detail.status_code == history.status_code == 200
    assert listing.get_json()["count"] == 1
    assert detail.get_json()["forecast"]["resolution_criteria"]
    assert detail.get_json()["forecast"]["calibration_status"] == "experimental"
    assert history.get_json()["history"]
    html = page.get_data(as_text=True)
    assert html.index("Intelligence Brief") < html.index("Active Forecasts")
    assert html.index("Active Forecasts") < html.index("System Health")
    assert "Evidence Explorer" in html
    assert 'id="edgeList"' not in html


def test_missing_forecast_and_evidence_return_structured_empty_states(tmp_path):
    service = SituationRoomService(
        repository=SituationRoomRepository(tmp_path / "empty.db")
    )
    client = create_app(situation_room_service=service).test_client()

    forecasts = client.get("/api/forecasts").get_json()
    missing = client.get("/api/evidence/not-found")

    assert forecasts["count"] == 0
    assert "why" in forecasts["empty_state"]
    assert missing.status_code == 404
    assert missing.get_json()["status"] == "evidence_unavailable"


def test_global_feed_relevance_filters_noise_language_and_duplicates():
    articles = [
        {
            "title": "NBA finals ratings rise",
            "url": "https://sports.example/nba",
            "language": "English",
            "domain": "sports.example",
        },
        {
            "title": "Central bank signals policy-rate decision",
            "url": "https://news.example/policy",
            "language": "English",
            "domain": "news.example",
        },
        {
            "title": "Central bank signals policy rate decision",
            "url": "https://wire.example/duplicate",
            "language": "English",
            "domain": "wire.example",
        },
        {
            "title": "El banco central cambia las tasas",
            "url": "https://news.example/es",
            "language": "Spanish",
            "domain": "news.example",
        },
    ]

    retained, quarantined = SituationRoomService._filter_global_articles(
        articles, query="policy"
    )

    assert len(retained) == 1
    assert "Central bank" in retained[0]["title"]
    assert len(quarantined) == 3


def test_autonomy_cycle_persists_readable_objective_and_findings(tmp_path):
    repository = SituationRoomRepository(tmp_path / "cycles.db")
    service = SituationRoomService(repository=repository)
    service.get_snapshot = lambda **_kwargs: {
        "generated_at": NOW.isoformat(),
        "alerts": [{"title": "CPI remains elevated"}],
        "risk_profiles": [],
        "active_forecasts": [{"forecast_id": "fred-cpi-forecast"}],
        "source_status": [{"source": "fred", "status": "live"}],
    }

    result = service.run_autonomy_cycle(force=True)
    latest = service.get_latest_autonomy_cycle()

    assert result["status"] == "ok"
    assert latest["objective"]
    assert latest["actions_attempted"]
    assert latest["findings"]
    assert "forecasts_considered" in latest
    assert latest["cycle_status"] == "completed"
