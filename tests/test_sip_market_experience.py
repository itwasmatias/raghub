from datetime import datetime, timedelta, timezone

import app as app_module

from sports.personal.config import PersonalEditionSettings
from sports.personal.repository import PersonalEditionRepository
from sports.personal.service import PersonalEditionService


NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


class Source:
    def fetch(self, **_kwargs):
        rows = []
        for book in ("DraftKings", "FanDuel"):
            for selection, team, price in (
                ("home", "New York Liberty", -130),
                ("away", "Chicago Sky", 110),
            ):
                rows.append(
                    {
                        "provider_event_id": f"{book}-event",
                        "league": "WNBA",
                        "season": "2026",
                        "event_start": (NOW + timedelta(hours=3)).isoformat(),
                        "home_team": "New York Liberty",
                        "away_team": "Chicago Sky",
                        "sportsbook": book,
                        "market": "h2h",
                        "period": "full_game",
                        "selection": selection,
                        "selection_team": team,
                        "american_price": price,
                        "observed_at": (NOW - timedelta(seconds=30)).isoformat(),
                        "source": "contract-test",
                        "source_url": "https://example.test/odds",
                        "data_mode": "live",
                        "is_live": False,
                    }
                )
        return rows


def _service(tmp_path, *, with_feed=False):
    settings = PersonalEditionSettings.from_mapping(
        {
            "SIP_MODE": "live",
            "ODDS_FEED_ENABLED": "true" if with_feed else "false",
            "ODDS_API_IO_API_KEY": "test-key" if with_feed else "",
            "SPORTSGAMEODDS_API_KEY": "",
            "SIP_PERSONAL_DATABASE": str(tmp_path / "sip.db"),
            "SIP_MODEL_PATH": str(tmp_path / "model.json"),
        }
    )
    service = PersonalEditionService(
        settings,
        repository=PersonalEditionRepository(settings.database_path),
        source=Source() if with_feed else None,
        clock=lambda: NOW,
    )
    if with_feed:
        service.refresh()
    return service


def test_market_pages_render_with_explicit_unavailable_state(tmp_path):
    app = app_module.create_app(personal_service=_service(tmp_path, with_feed=False))
    client = app.test_client()

    assert client.get("/sip/markets").status_code == 200
    assert client.get("/sip/portfolio").status_code == 200
    assert client.get("/sip/activity").status_code == 200

    markets = client.get("/api/sip/markets").get_json()
    assert markets["has_persisted_data"] is False
    assert "No persisted market records" in markets["unavailable_state"]


def test_market_feed_and_order_intent_recording_flow(tmp_path):
    app = app_module.create_app(personal_service=_service(tmp_path, with_feed=True))
    client = app.test_client()

    markets_payload = client.get("/api/sip/markets").get_json()
    assert markets_payload["has_persisted_data"] is True
    market = markets_payload["markets"][0]
    outcome = market["outcomes"][0]

    intent = client.post(
        "/api/sip/order-intents",
        json={
            "market_id": market["id"],
            "outcome_id": outcome["id"],
            "mode": "practice",
            "stake": "25",
            "requested_odds": int(outcome.get("best_odds") or -110),
            "accepted_odds": int(outcome.get("best_odds") or -110),
        },
    )
    payload = intent.get_json()

    assert intent.status_code == 200
    assert payload["status"] == "recorded"
    assert payload["execution"] == "disabled"

    portfolio = client.get("/api/sip/portfolio").get_json()
    assert portfolio["open_positions"] >= 1

    activity = client.get("/api/sip/activity").get_json()
    assert activity["events"]
