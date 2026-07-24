import requests

import pytest

from sports.data.sources.nba_stats_http_source import (
    NbaStatsHttpSource,
    NbaStatsHttpSourceError,
)


class FakeResponse:
    def __init__(
        self,
        payload: object = None,
        error: Exception | None = None,
    ) -> None:
        self.payload = payload
        self.error = error

    def raise_for_status(self) -> None:
        if self.error is not None:
            raise self.error

    def json(self) -> object:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def payload(envelope: str = "resultSets") -> dict[str, object]:
    result = {
        "name": "PlayerGameLogs",
        "headers": [
            "PLAYER_ID",
            "PLAYER_NAME",
            "TEAM_ID",
            "TEAM_ABBREVIATION",
            "TEAM_NAME",
            "GAME_ID",
            "GAME_DATE",
            "MIN",
            "PTS",
            "REB",
            "AST",
            "FGM",
            "FGA",
            "FG3M",
            "FG3A",
            "FTM",
            "FTA",
        ],
        "rowSet": [
            [
                2544,
                "LeBron James",
                1610612747,
                "LAL",
                "Los Angeles Lakers",
                "game-1",
                "2025-01-01",
                "35:30",
                27,
                8,
                9,
                10,
                18,
                2,
                5,
                5,
                6,
            ]
        ],
    }
    return {envelope: [result]}


@pytest.mark.parametrize("envelope", ["resultSets", "resultSet"])
def test_fetches_and_normalizes_player_game_logs(envelope: str) -> None:
    session = FakeSession(FakeResponse(payload(envelope)))
    source = NbaStatsHttpSource(session=session, timeout=12.5)

    rows = source.fetch_player_game_logs(
        "2024-25",
        "00",
        "Regular Season",
    )

    call = session.calls[0]
    assert call["url"] == NbaStatsHttpSource.endpoint
    assert call["params"] == {
        "Season": "2024-25",
        "LeagueID": "00",
        "SeasonType": "Regular Season",
    }
    assert call["timeout"] == 12.5
    headers = call["headers"]
    assert isinstance(headers, dict)
    assert {"User-Agent", "Referer", "Origin", "Accept"} <= headers.keys()
    assert rows == [
        {
            "player_id": 2544,
            "player_name": "LeBron James",
            "team_id": 1610612747,
            "team_abbreviation": "LAL",
            "team_name": "Los Angeles Lakers",
            "game_id": "game-1",
            "game_date": "2025-01-01",
            "minutes": "35:30",
            "pts": 27,
            "reb": 8,
            "ast": 9,
            "fgm": 10,
            "fga": 18,
            "fg3m": 2,
            "fg3a": 5,
            "ftm": 5,
            "fta": 6,
        }
    ]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (requests.Timeout("slow"), "timed out"),
        (
            FakeResponse(
                {},
                requests.HTTPError("503 Server Error"),
            ),
            "HTTP request failed",
        ),
        (FakeResponse(ValueError("bad json")), "invalid JSON"),
    ],
)
def test_raises_clear_source_errors(
    response: FakeResponse | Exception,
    message: str,
) -> None:
    source = NbaStatsHttpSource(session=FakeSession(response))

    with pytest.raises(NbaStatsHttpSourceError, match=message):
        source.fetch_player_game_logs("2024-25", "00", "Regular Season")
