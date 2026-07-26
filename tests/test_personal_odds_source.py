from datetime import datetime, timezone

import pytest
import requests

from sports.data.sources.the_odds_api_moneyline_source import (
    TheOddsApiMoneylineSource,
)

NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


class Response:
    def __init__(self, payload, *, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {
            "x-requests-remaining": "99",
            "x-requests-used": "1",
            "x-requests-last": "1",
        }

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Session:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        if not self.responses:
            raise AssertionError("unexpected call with no prepared response")
        return self.responses.pop(0)


def sports_catalog():
    return [
        {
            "sport": "basketball",
            "league": "wnba",
            "sport_name": "Basketball",
            "league_name": "WNBA",
        },
        {
            "sport": "baseball",
            "league": "mlb",
            "sport_name": "Baseball",
            "league_name": "MLB",
        },
        {
            "sport": "basketball",
            "league": "nba",
            "sport_name": "Basketball",
            "league_name": "NBA",
        },
    ]


def bookmakers_catalog():
    return {"bookmakers": ["BetMGM", "DraftKings"], "count": 2}


def active_bookmakers_catalog():
    return [
        {"name": "DraftKings", "active": True},
        {"name": "FanDuel", "active": True},
        {"name": "BetMGM", "active": True},
    ]


def events_payload(
    event_id,
    *,
    home,
    away,
    sport_slug,
    league_slug,
    league_name,
    start="2026-07-25T03:00:00Z",
):
    return [
        {
            "id": event_id,
            "date": start,
            "home_team": home,
            "away_team": away,
            "sport": {"slug": sport_slug, "name": sport_slug.title()},
            "league": {"slug": league_slug, "name": league_name},
        }
    ]


def odds_payload(event_id, *, home, away, include_betmgm=False):
    books = {
        "DraftKings": [
            {
                "name": "ML",
                "updatedAt": "2026-07-25T00:00:00Z",
                "odds": [
                    {"home": -130, "away": 110},
                ],
            }
        ],
        "FanDuel": [
            {
                "name": "ML",
                "updatedAt": "2026-07-25T00:00:00Z",
                "odds": [
                    {"home": -128, "away": 108},
                ],
            }
        ],
    }
    if include_betmgm:
        books["BetMGM"] = [
            {
                "name": "ML",
                "updatedAt": "2026-07-25T00:00:00Z",
                "odds": [
                    {"home": -127, "away": 107},
                ],
            }
        ]
    return {
        "id": event_id,
        "commence_time": "2026-07-25T03:00:00Z",
        "home_team": home,
        "away_team": away,
        "bookmakers": books,
        "bookmakerIds": list(books.keys()),
    }


def test_event_discovery_calls_provider_sports_and_events_endpoints():
    session = Session(
        responses=[
            Response(bookmakers_catalog()),
            Response(sports_catalog()),
            Response(
                events_payload(
                    "wnba-1",
                    home="New York Liberty",
                    away="Chicago Sky",
                    sport_slug="basketball",
                    league_slug="usa-wnba",
                    league_name="USA - WNBA",
                )
            ),
            Response(
                events_payload(
                    "wnba-1",
                    home="New York Liberty",
                    away="Chicago Sky",
                    sport_slug="basketball",
                    league_slug="usa-wnba",
                    league_name="USA - WNBA",
                )
            ),
            Response(
                odds_payload(
                    "wnba-1",
                    home="New York Liberty",
                    away="Chicago Sky",
                )
            ),
        ]
    )
    source = TheOddsApiMoneylineSource(
        api_key="secret",
        sports=("basketball_wnba",),
        session=session,
        clock=lambda: NOW,
    )

    rows = source.fetch(correlation_id="contract-1")

    assert rows
    assert session.calls[0][0].endswith("/v3/bookmakers/selected")
    assert session.calls[1][0].endswith("/v3/sports")
    assert session.calls[2][0].endswith("/v3/events")
    assert session.calls[2][1]["params"]["sport"] == "basketball"
    assert "league" not in session.calls[2][1]["params"]
    assert session.calls[2][1]["params"]["status"] == "pending"
    assert session.calls[3][1]["params"]["league"] == "usa-wnba"
    assert session.calls[4][0].endswith("/v3/odds")
    assert source.last_stats["resolved_slugs"]["basketball_wnba"] == {
        "sport": "basketball",
        "league": "usa-wnba",
    }


def test_event_id_propagates_to_event_level_odds_call_parameters():
    session = Session(
        responses=[
            Response(bookmakers_catalog()),
            Response(sports_catalog()),
            Response(
                events_payload(
                    "event-abc-123",
                    home="New York Liberty",
                    away="Chicago Sky",
                    sport_slug="basketball",
                    league_slug="usa-wnba",
                    league_name="USA - WNBA",
                )
            ),
            Response(
                events_payload(
                    "event-abc-123",
                    home="New York Liberty",
                    away="Chicago Sky",
                    sport_slug="basketball",
                    league_slug="usa-wnba",
                    league_name="USA - WNBA",
                )
            ),
            Response(
                odds_payload(
                    "event-abc-123",
                    home="New York Liberty",
                    away="Chicago Sky",
                )
            ),
        ]
    )
    source = TheOddsApiMoneylineSource(
        api_key="secret",
        sports=("basketball_wnba",),
        session=session,
        clock=lambda: NOW,
    )

    source.fetch()

    odds_call = [call for call in session.calls if call[0].endswith("/v3/odds")][0]
    assert odds_call[0].endswith("/v3/odds")
    assert odds_call[1]["params"]["eventId"] == "event-abc-123"
    assert odds_call[1]["params"]["apiKey"] == "secret"


def test_event_level_odds_request_uses_two_account_bookmakers():
    session = Session(
        responses=[
            Response(bookmakers_catalog()),
            Response(sports_catalog()),
            Response(
                events_payload(
                    "wnba-2",
                    home="New York Liberty",
                    away="Chicago Sky",
                    sport_slug="basketball",
                    league_slug="usa-wnba",
                    league_name="USA - WNBA",
                )
            ),
            Response(
                events_payload(
                    "wnba-2",
                    home="New York Liberty",
                    away="Chicago Sky",
                    sport_slug="basketball",
                    league_slug="usa-wnba",
                    league_name="USA - WNBA",
                )
            ),
            Response(
                odds_payload(
                    "wnba-2",
                    home="New York Liberty",
                    away="Chicago Sky",
                )
            ),
        ]
    )
    source = TheOddsApiMoneylineSource(
        api_key="secret",
        sports=("basketball_wnba",),
        bookmakers=("fanduel", "draftkings"),
        session=session,
        clock=lambda: NOW,
    )

    source.fetch()

    params = [call for call in session.calls if call[0].endswith("/v3/odds")][0][1][
        "params"
    ]
    assert params["bookmakers"] == "BetMGM,DraftKings"
    assert "sport" not in params


def test_multiple_bookmaker_rows_are_parsed_for_full_game_moneyline():
    session = Session(
        responses=[
            Response(bookmakers_catalog()),
            Response(sports_catalog()),
            Response(
                events_payload(
                    "mlb-1",
                    home="Los Angeles Dodgers",
                    away="New York Mets",
                    sport_slug="baseball",
                    league_slug="usa-mlb",
                    league_name="USA - MLB",
                )
            ),
            Response(
                events_payload(
                    "mlb-1",
                    home="Los Angeles Dodgers",
                    away="New York Mets",
                    sport_slug="baseball",
                    league_slug="usa-mlb",
                    league_name="USA - MLB",
                )
            ),
            Response(
                odds_payload(
                    "mlb-1",
                    home="Los Angeles Dodgers",
                    away="New York Mets",
                )
            ),
        ]
    )
    source = TheOddsApiMoneylineSource(
        api_key="secret",
        sports=("baseball_mlb",),
        session=session,
        clock=lambda: NOW,
    )

    rows = source.fetch()

    assert len(rows) == 4
    assert {row["sportsbook"] for row in rows} == {"DraftKings", "FanDuel"}
    assert {row["selection"] for row in rows} == {"home", "away"}
    assert {row["league"] for row in rows} == {"MLB"}


def test_missing_event_id_is_counted_and_prevents_odds_calls():
    session = Session(
        responses=[
            Response(bookmakers_catalog()),
            Response(sports_catalog()),
            Response(
                [
                    {
                        "commence_time": "2026-07-25T03:00:00Z",
                        "home_team": "New York Liberty",
                        "away_team": "Chicago Sky",
                    }
                ]
            ),
            Response(
                [
                    {
                        "commence_time": "2026-07-25T03:00:00Z",
                        "home_team": "New York Liberty",
                        "away_team": "Chicago Sky",
                    }
                ]
            ),
        ]
    )
    source = TheOddsApiMoneylineSource(
        api_key="secret",
        sports=("basketball_wnba",),
        session=session,
        clock=lambda: NOW,
    )

    rows = source.fetch()

    assert rows == []
    assert source.last_stats["missing_event_id"] == 1
    assert len(session.calls) == 3


def test_empty_event_list_sets_unavailable_error():
    session = Session(
        responses=[
            Response(bookmakers_catalog()),
            Response(sports_catalog()),
            Response([]),
            Response([]),
        ]
    )
    source = TheOddsApiMoneylineSource(
        api_key="secret",
        sports=("basketball_wnba",),
        session=session,
        clock=lambda: NOW,
    )

    rows = source.fetch()

    assert rows == []
    assert "basketball_wnba" in source.last_errors
    assert "empty event list" in source.last_errors["basketball_wnba"].lower()


def test_event_with_no_odds_is_tracked_without_crashing():
    session = Session(
        responses=[
            Response(bookmakers_catalog()),
            Response(sports_catalog()),
            Response(
                events_payload(
                    "mlb-2",
                    home="Los Angeles Dodgers",
                    away="New York Mets",
                    sport_slug="baseball",
                    league_slug="usa-mlb",
                    league_name="USA - MLB",
                )
            ),
            Response(
                events_payload(
                    "mlb-2",
                    home="Los Angeles Dodgers",
                    away="New York Mets",
                    sport_slug="baseball",
                    league_slug="usa-mlb",
                    league_name="USA - MLB",
                )
            ),
            Response([]),
        ]
    )
    source = TheOddsApiMoneylineSource(
        api_key="secret",
        sports=("baseball_mlb",),
        session=session,
        clock=lambda: NOW,
    )

    rows = source.fetch()

    assert rows == []
    assert source.last_stats["events_without_odds"] == 1
    assert source.last_stats["odds_requests_made"] == 1


def test_incomplete_sportsbook_quotes_are_returned_honestly_for_downstream_checks():
    payload = [
        {
            "id": "mlb-3",
            "commence_time": "2026-07-25T03:00:00Z",
            "home_team": "Los Angeles Dodgers",
            "away_team": "New York Mets",
            "bookmakers": [
                {
                    "key": "draftkings",
                    "title": "DraftKings",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Los Angeles Dodgers", "price": -130},
                                {"name": "New York Mets", "price": 110},
                            ],
                        }
                    ],
                },
                {
                    "key": "fanduel",
                    "title": "FanDuel",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Los Angeles Dodgers", "price": -128}
                            ],
                        }
                    ],
                },
            ],
        }
    ]
    session = Session(
        responses=[
            Response(bookmakers_catalog()),
            Response(sports_catalog()),
            Response(
                events_payload(
                    "mlb-3",
                    home="Los Angeles Dodgers",
                    away="New York Mets",
                    sport_slug="baseball",
                    league_slug="usa-mlb",
                    league_name="USA - MLB",
                )
            ),
            Response(
                events_payload(
                    "mlb-3",
                    home="Los Angeles Dodgers",
                    away="New York Mets",
                    sport_slug="baseball",
                    league_slug="usa-mlb",
                    league_name="USA - MLB",
                )
            ),
            Response(payload),
        ]
    )
    source = TheOddsApiMoneylineSource(
        api_key="secret",
        sports=("baseball_mlb",),
        session=session,
        clock=lambda: NOW,
    )

    rows = source.fetch()

    assert len(rows) == 3
    assert {row["sportsbook"] for row in rows} == {"DraftKings", "FanDuel"}


def test_credential_secrecy_redacts_api_key_in_errors():
    session = Session(
        responses=[
            Response(bookmakers_catalog()),
            Response(sports_catalog()),
            Response(
                {"message": "api key secret is invalid"},
                status_code=401,
            ),
        ]
    )
    source = TheOddsApiMoneylineSource(
        api_key="secret",
        sports=("basketball_wnba",),
        session=session,
        clock=lambda: NOW,
    )

    source.fetch()

    assert "secret" not in str(source.last_errors)
    assert "[REDACTED]" in str(source.last_errors)


def test_wnba_and_mlb_filtering_via_discovered_slugs():
    session = Session(
        responses=[
            Response(bookmakers_catalog()),
            Response(sports_catalog()),
            Response(
                events_payload(
                    "wnba-4",
                    home="New York Liberty",
                    away="Chicago Sky",
                    sport_slug="basketball",
                    league_slug="usa-wnba",
                    league_name="USA - WNBA",
                )
            ),
            Response(
                events_payload(
                    "wnba-4",
                    home="New York Liberty",
                    away="Chicago Sky",
                    sport_slug="basketball",
                    league_slug="usa-wnba",
                    league_name="USA - WNBA",
                )
            ),
            Response(
                odds_payload(
                    "wnba-4",
                    home="New York Liberty",
                    away="Chicago Sky",
                )
            ),
            Response(
                events_payload(
                    "mlb-4",
                    home="Los Angeles Dodgers",
                    away="New York Mets",
                    sport_slug="baseball",
                    league_slug="usa-mlb",
                    league_name="USA - MLB",
                )
            ),
            Response(
                events_payload(
                    "mlb-4",
                    home="Los Angeles Dodgers",
                    away="New York Mets",
                    sport_slug="baseball",
                    league_slug="usa-mlb",
                    league_name="USA - MLB",
                )
            ),
            Response(
                odds_payload(
                    "mlb-4",
                    home="Los Angeles Dodgers",
                    away="New York Mets",
                )
            ),
        ]
    )
    source = TheOddsApiMoneylineSource(
        api_key="secret",
        sports=("basketball_wnba", "baseball_mlb"),
        session=session,
        clock=lambda: NOW,
    )

    rows = source.fetch(correlation_id="contract-mlb-wnba")

    assert {row["league"] for row in rows} == {"WNBA", "MLB"}
    assert source.last_stats["discovered_event_counts"]["basketball_wnba"] == 1
    assert source.last_stats["discovered_event_counts"]["baseball_mlb"] == 1


def test_moneyline_source_propagates_timeout_without_fixture_fallback():
    source = TheOddsApiMoneylineSource(
        api_key="secret",
        session=Session(error=requests.Timeout("provider timed out")),
    )
    with pytest.raises(requests.Timeout):
        source.fetch()
