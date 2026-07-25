from datetime import datetime, timezone

from sports.data.sources.the_odds_api_source import TheOddsApiSource


class FakeResponse:
    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = headers or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_odds_api_source_fetches_events_then_normalizes_player_props() -> None:
    session = FakeSession(
        [
            FakeResponse(
                [
                    {
                        "id": "event-1",
                        "commence_time": "2026-07-24T21:00:00Z",
                        "home_team": "New York Knicks",
                        "away_team": "Chicago Bulls",
                    }
                ]
            ),
            FakeResponse(
                {
                    "id": "event-1",
                    "commence_time": "2026-07-24T21:00:00Z",
                    "home_team": "New York Knicks",
                    "away_team": "Chicago Bulls",
                    "bookmakers": [
                        {
                            "key": "draftkings",
                            "title": "DraftKings",
                            "markets": [
                                {
                                    "key": "player_points",
                                    "last_update": "2026-07-24T18:00:00Z",
                                    "outcomes": [
                                        {
                                            "name": "Over",
                                            "description": "Example Guard",
                                            "price": -105,
                                            "point": 18.5,
                                        },
                                        {
                                            "name": "Under",
                                            "description": "Example Guard",
                                            "price": -115,
                                            "point": 18.5,
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                },
                headers={
                    "x-requests-remaining": "499",
                    "x-requests-used": "1",
                    "x-requests-last": "1",
                },
            ),
        ]
    )
    source = TheOddsApiSource(
        api_key="secret",
        session=session,
        timeout=8,
        bookmakers=("draftkings", "fanduel", "betmgm"),
        clock=lambda: datetime(2026, 7, 24, 18, tzinfo=timezone.utc),
    )

    rows = source.fetch_changed("sportsbook_markets", since=None)

    assert len(rows) == 2
    assert rows[0]["sportsbook"] == "DraftKings"
    assert rows[0]["player"] == "Example Guard"
    assert rows[0]["market"] == "player_points"
    assert rows[0]["canonical_id"].startswith("odds:event-1:")
    assert rows[0]["source_url"].endswith("/event-1/odds")
    assert source.quota["remaining"] == 499
    assert session.calls[0][0].endswith("/basketball_nba/events")
    assert session.calls[1][1]["params"]["markets"] == "player_points"
    assert session.calls[1][1]["params"]["bookmakers"] == (
        "draftkings,fanduel,betmgm"
    )
    assert all(call[1]["timeout"] == 8 for call in session.calls)


def test_odds_api_source_requires_key_and_only_supports_market_dataset() -> None:
    try:
        TheOddsApiSource(api_key="")
        raise AssertionError("expected missing key failure")
    except ValueError as error:
        assert "API key" in str(error)

    source = TheOddsApiSource(api_key="secret", session=FakeSession([]))
    try:
        source.fetch_changed("players", since=None)
        raise AssertionError("expected dataset failure")
    except ValueError as error:
        assert "sportsbook_markets" in str(error)
