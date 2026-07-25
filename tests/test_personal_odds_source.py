from datetime import datetime, timezone

import pytest
import requests

from sports.data.sources.the_odds_api_moneyline_source import (
    TheOddsApiMoneylineSource,
)


NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


class Response:
    headers = {"x-requests-remaining": "99"}

    def __init__(self, payload):
        self.payload = payload

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
    return [{
        "id": "event-a",
        "commence_time": "2026-07-25T03:00:00Z",
        "home_team": "New York Liberty",
        "away_team": "Chicago Sky",
        "bookmakers": [{
            "key": "draftkings",
            "title": "DraftKings",
            "last_update": "2026-07-25T00:00:00Z",
            "markets": [{"key": "h2h", "outcomes": [
                {"name": "New York Liberty", "price": -130},
                {"name": "Chicago Sky", "price": 110},
            ]}],
        }],
    }]


def test_moneyline_source_loads_wnba_and_mlb_and_preserves_books():
    session = Session(payload())
    source = TheOddsApiMoneylineSource(
        api_key="secret",
        session=session,
        clock=lambda: NOW,
    )
    rows = source.fetch(correlation_id="test-1")

    assert len(session.calls) == 2
    assert all(call[1]["params"]["markets"] == "h2h" for call in session.calls)
    assert all("apiKey" in call[1]["params"] for call in session.calls)
    assert len(rows) == 4
    assert {row["league"] for row in rows} == {"WNBA", "MLB"}
    assert {row["selection"] for row in rows} == {"home", "away"}
    assert all(row["data_mode"] == "live" for row in rows)


def test_moneyline_source_propagates_timeout_without_fixture_fallback():
    source = TheOddsApiMoneylineSource(
        api_key="secret",
        session=Session(error=requests.Timeout("provider timed out")),
    )
    with pytest.raises(requests.Timeout):
        source.fetch()
