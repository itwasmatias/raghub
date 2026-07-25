from datetime import datetime, timezone

import pytest
import requests

from sports.data.sources.sports_game_odds_moneyline_source import (
    SportsGameOddsMoneylineSource,
)


NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Session:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return Response(self.payload)


def payload():
    bookmaker = {
        "draftkings": {
            "odds": "-130",
            "available": True,
            "lastUpdatedAt": "2026-07-25T00:00:00Z",
        },
        "fanduel": {"odds": "-125", "available": True},
        "betmgm": {"odds": "-128", "available": True},
        "unknown": {"odds": "-120", "available": True},
    }
    return {
        "success": True,
        "data": [{
            "eventID": "sgo-1",
            "status": {
                "startsAt": "2026-07-25T03:00:00Z",
                "started": False,
                "live": False,
                "completed": False,
                "cancelled": False,
            },
            "teams": {
                "home": {"names": {"long": "New York Liberty"}},
                "away": {"names": {"long": "Chicago Sky"}},
            },
            "odds": {
                "points-home-game-ml-home": {
                    "periodID": "game",
                    "betTypeID": "ml",
                    "byBookmaker": bookmaker,
                },
                "points-away-game-ml-away": {
                    "periodID": "game",
                    "betTypeID": "ml",
                    "byBookmaker": {
                        key: {**value, "odds": "+110"}
                        for key, value in bookmaker.items()
                    },
                },
            },
        }],
    }


def test_source_authenticates_with_header_and_normalizes_complete_books():
    session = Session(payload())
    source = SportsGameOddsMoneylineSource(
        api_key="secret",
        leagues=("WNBA",),
        session=session,
        clock=lambda: NOW,
    )

    assert source.authenticate()["authenticated"] is True
    rows = source.fetch(correlation_id="request-1")

    assert len(rows) == 6
    assert {row["sportsbook"] for row in rows} == {
        "draftkings", "fanduel", "betmgm"
    }
    assert {row["selection"] for row in rows} == {"home", "away"}
    assert len({row["observed_at"] for row in rows}) == 1
    assert all(
        call[1]["headers"]["x-api-key"] == "secret" for call in session.calls
    )
    assert "apiKey" not in session.calls[-1][1]["params"]


def test_source_honors_configured_base_url_timeout_and_request_limit():
    session = Session(payload())
    source = SportsGameOddsMoneylineSource(
        api_key="secret",
        leagues=("WNBA",),
        base_url="https://api.sportsgameodds.com/v2",
        request_timeout_seconds=15,
        max_events_per_request=10,
        session=session,
        clock=lambda: NOW,
    )

    source.fetch()

    url, request = session.calls[0]
    assert url == "https://api.sportsgameodds.com/v2/events"
    assert request["timeout"] == 15
    assert request["params"]["limit"] == 10
    assert request["headers"]["x-api-key"] == "secret"
    assert "secret" not in str(request["params"])


def test_source_rejects_failed_payload_and_propagates_timeout():
    failed = SportsGameOddsMoneylineSource(
        api_key="secret",
        session=Session({"success": False, "error": "invalid key"}),
    )
    with pytest.raises(ValueError, match="invalid key"):
        failed.fetch()
    timed_out = SportsGameOddsMoneylineSource(
        api_key="secret",
        session=Session(error=requests.Timeout("timeout")),
    )
    with pytest.raises(requests.Timeout):
        timed_out.fetch()


def test_source_records_unavailable_league_without_mislabeling_data():
    class PartialSession(Session):
        def get(self, url, **kwargs):
            if kwargs.get("params", {}).get("leagueID") == "WNBA":
                return Response({
                    "success": False,
                    "error": "The leagueID WNBA is unavailable at your current subscription tier. Upgrade to unlock",
                }, status_code=400)
            return Response(payload())

    source = SportsGameOddsMoneylineSource(
        api_key="secret",
        leagues=("WNBA", "MLB"),
        session=PartialSession(),
        clock=lambda: NOW,
    )

    rows = source.fetch()

    assert len(rows) == 6
    assert {row["league"] for row in rows} == {"MLB"}
    assert "WNBA" in source.last_errors
