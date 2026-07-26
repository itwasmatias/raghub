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


def _service(tmp_path):
    settings = PersonalEditionSettings.from_mapping(
        {
            "SIP_MODE": "live",
            "ODDS_FEED_ENABLED": "true",
            "ODDS_API_IO_API_KEY": "test-key",
            "SPORTSGAMEODDS_API_KEY": "",
            "SIP_PERSONAL_DATABASE": str(tmp_path / "sip.db"),
            "SIP_MODEL_PATH": str(tmp_path / "model.json"),
        }
    )
    service = PersonalEditionService(
        settings,
        repository=PersonalEditionRepository(settings.database_path),
        source=Source(),
        clock=lambda: NOW,
    )
    service.refresh()
    return service


def _first_market_and_outcome(client):
    payload = client.get("/api/sip/markets").get_json()
    market = payload["markets"][0]
    outcome = market["outcomes"][0]
    return market, outcome


def test_practice_execution_order_auto_submits_and_accepts(tmp_path):
    app = app_module.create_app(personal_service=_service(tmp_path))
    client = app.test_client()
    market, outcome = _first_market_and_outcome(client)

    response = client.post(
        "/api/sip/execution/orders",
        json={
            "market_id": market["id"],
            "outcome_id": outcome["id"],
            "stake": "20",
            "requested_odds": int(outcome.get("best_odds") or -110),
            "accepted_odds": int(outcome.get("best_odds") or -110),
            "execution_mode": "practice",
            "sportsbook": "draftkings",
            "auto_submit": True,
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["order"]["state"] == "accepted"
    assert payload["receipts"]


def test_manual_execution_requires_confirmation_then_accepts(tmp_path):
    app = app_module.create_app(personal_service=_service(tmp_path))
    client = app.test_client()
    market, outcome = _first_market_and_outcome(client)

    create = client.post(
        "/api/sip/execution/orders",
        json={
            "market_id": market["id"],
            "outcome_id": outcome["id"],
            "stake": "20",
            "requested_odds": int(outcome.get("best_odds") or -110),
            "accepted_odds": int(outcome.get("best_odds") or -110),
            "execution_mode": "manual_record",
            "sportsbook": "fanduel",
            "auto_submit": False,
        },
    ).get_json()

    assert create["order"]["state"] == "pending_confirmation"
    order_id = create["order"]["id"]

    confirmed = client.post(
        f"/api/sip/execution/orders/{order_id}/confirm",
        json={"actor_id": "user-test", "note": "confirmed", "auto_submit": True},
    ).get_json()

    assert confirmed["order"]["state"] == "accepted"


def test_prefilled_link_execution_waits_for_external_confirmation(tmp_path):
    app = app_module.create_app(personal_service=_service(tmp_path))
    client = app.test_client()
    market, outcome = _first_market_and_outcome(client)

    create = client.post(
        "/api/sip/execution/orders",
        json={
            "market_id": market["id"],
            "outcome_id": outcome["id"],
            "stake": "20",
            "requested_odds": int(outcome.get("best_odds") or -110),
            "accepted_odds": int(outcome.get("best_odds") or -110),
            "execution_mode": "prefilled_link",
            "sportsbook": "draftkings",
            "auto_submit": False,
        },
    ).get_json()

    order_id = create["order"]["id"]

    submit = client.post(
        f"/api/sip/execution/orders/{order_id}/confirm",
        json={"actor_id": "user-test", "auto_submit": True},
    ).get_json()

    assert submit["order"]["state"] == "awaiting_external_confirmation"
    assert submit["order"]["prefilled_url"]

    finalized = client.post(
        f"/api/sip/execution/orders/{order_id}/external-confirmation",
        json={"accepted": True, "external_reference": "book-123"},
    ).get_json()

    assert finalized["order"]["state"] == "accepted"
    assert finalized["order"]["provider_reference"] == "book-123"


def test_direct_auto_execution_stays_blocked_without_authorized_api(tmp_path):
    app = app_module.create_app(personal_service=_service(tmp_path))
    client = app.test_client()
    market, outcome = _first_market_and_outcome(client)

    response = client.post(
        "/api/sip/execution/orders",
        json={
            "market_id": market["id"],
            "outcome_id": outcome["id"],
            "stake": "20",
            "requested_odds": int(outcome.get("best_odds") or -110),
            "accepted_odds": int(outcome.get("best_odds") or -110),
            "execution_mode": "direct_auto",
            "sportsbook": "draftkings",
            "auto_submit": True,
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["order"]["state"] == "failed"
    assert payload["order"]["failure_code"] == "direct_not_authorized"
