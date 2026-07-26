from datetime import datetime, timedelta, timezone

import app as app_module

from sports.personal.config import PersonalEditionSettings
from sports.personal.repository import PersonalEditionRepository
from sports.personal.service import PersonalEditionService


NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


def service(tmp_path):
    settings = PersonalEditionSettings.from_mapping(
        {
            "SIP_MODE": "live",
            "ODDS_FEED_ENABLED": "false",
            "ODDS_API_IO_API_KEY": "",
            "SPORTSGAMEODDS_API_KEY": "",
            "SIP_PERSONAL_DATABASE": str(tmp_path / "sip.db"),
            "SIP_MODEL_PATH": str(tmp_path / "model.json"),
        }
    )
    return PersonalEditionService(
        settings,
        repository=PersonalEditionRepository(settings.database_path),
        clock=lambda: NOW,
    )


def test_personal_dashboard_and_core_apis_have_honest_empty_states(tmp_path):
    flask_app = app_module.create_app(personal_service=service(tmp_path))
    client = flask_app.test_client()

    dashboard = client.get("/")
    wnba_page = client.get("/wnba")
    games = client.get("/api/sip/games")
    wnba_snapshot = client.get("/api/sip/wnba")
    choices = client.get("/api/sip/choices")
    status = client.get("/api/sip/status")

    assert dashboard.status_code == 200
    assert wnba_page.status_code == 200
    assert b"SIP v1.0" in dashboard.data
    assert b"Live WNBA Moneyline" in wnba_page.data
    assert b"NBA flagship" in dashboard.data
    assert b"No genuine upcoming events are stored" in dashboard.data
    assert games.get_json() == {"events": []}
    assert wnba_snapshot.get_json()["events"] == []
    assert choices.get_json() == {
        "qualified_choices": [],
        "no_bet_results": [],
    }
    assert status.get_json()["feed"]["status"] == "not_refreshed"
    assert status.get_json()["model"]["status"] == "MODEL_NOT_TRAINED"
    assert dashboard.headers["Cache-Control"] == "no-store, max-age=0"
    assert dashboard.headers["X-SIP-Frontend-Version"]
    assert b"NBA Moneyline Command Center" in dashboard.data
    assert b'data-league-filter="NBA"' in dashboard.data
    assert b"NBA model" in dashboard.data
    assert b"preserves the no-bet result instead of guessing" in dashboard.data
    summary = client.get("/api/sip/dashboard").get_json()["league_summary"]
    assert summary["NBA"] == {
        "upcoming_events": 0,
        "evaluations": 0,
        "qualified_choices": 0,
        "no_bet_results": 0,
        "resolved_predictions": 0,
    }


def test_personal_dashboard_marks_real_mlb_events_for_filtering(tmp_path):
    class MlbSource:
        def fetch(self, **_kwargs):
            rows = []
            for book in ("DraftKings", "FanDuel"):
                for selection, team, price in (
                    ("home", "Chicago Cubs", -125),
                    ("away", "Milwaukee Brewers", 110),
                ):
                    rows.append(
                        {
                            "provider_event_id": f"{book}-mlb-event",
                            "league": "MLB",
                            "season": "2026",
                            "event_start": (NOW + timedelta(hours=4)).isoformat(),
                            "home_team": "Chicago Cubs",
                            "away_team": "Milwaukee Brewers",
                            "sportsbook": book,
                            "market": "h2h",
                            "period": "full_game",
                            "selection": selection,
                            "selection_team": team,
                            "american_price": price,
                            "observed_at": (NOW - timedelta(seconds=20)).isoformat(),
                            "source": "contract-test",
                            "source_url": "https://example.test/mlb-odds",
                            "data_mode": "live",
                            "is_live": False,
                        }
                    )
            return rows

    settings = PersonalEditionSettings.from_mapping(
        {
            "SIP_MODE": "live",
            "ODDS_FEED_ENABLED": "true",
            "ODDS_API_IO_API_KEY": "test-key",
            "ODDS_SPORTS": "baseball_mlb",
            "SIP_PERSONAL_DATABASE": str(tmp_path / "sip.db"),
            "SIP_MODEL_PATH": str(tmp_path / "model.json"),
        }
    )
    personal = PersonalEditionService(
        settings,
        repository=PersonalEditionRepository(settings.database_path),
        source=MlbSource(),
        clock=lambda: NOW,
    )
    personal.refresh()

    dashboard = app_module.create_app(personal_service=personal).test_client().get("/")

    assert dashboard.status_code == 200
    assert b'data-league="MLB"' in dashboard.data
    assert b"Milwaukee Brewers" in dashboard.data
    assert b"Chicago Cubs" in dashboard.data
    assert b"2 complete books" in dashboard.data
    assert b"FORECAST_MISSING" in dashboard.data
    summary = (
        app_module.create_app(personal_service=personal)
        .test_client()
        .get("/api/sip/dashboard")
        .get_json()["league_summary"]["MLB"]
    )
    assert summary == {
        "upcoming_events": 1,
        "evaluations": 2,
        "qualified_choices": 0,
        "no_bet_results": 2,
        "resolved_predictions": 0,
    }


def test_manual_refresh_reports_unconfigured_without_live_fixtures(tmp_path):
    flask_app = app_module.create_app(personal_service=service(tmp_path))
    response = flask_app.test_client().post("/api/sip/refresh")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "succeeded"
    assert payload["result"]["status"] == "unconfigured"
    assert payload["result"]["evaluations"] == []


def test_betting_page_uses_personal_market_assessments(tmp_path):
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

    settings = PersonalEditionSettings.from_mapping(
        {
            "SIP_MODE": "live",
            "ODDS_FEED_ENABLED": "true",
            "ODDS_API_IO_API_KEY": "test-key",
            "SIP_PERSONAL_DATABASE": str(tmp_path / "sip.db"),
            "SIP_MODEL_PATH": str(tmp_path / "model.json"),
        }
    )
    personal = PersonalEditionService(
        settings,
        repository=PersonalEditionRepository(settings.database_path),
        source=Source(),
        clock=lambda: NOW,
    )
    personal.refresh()

    response = (
        app_module.create_app(personal_service=personal).test_client().get("/betting")
    )

    assert response.status_code == 200
    assert b"No market assessments yet" not in response.data
    assert b"wnba:20260725" in response.data
    assert b"No bet" in response.data
    assert b"FORECAST_MISSING" in response.data
    assert b"draftkings" in response.data
