import requests

import pytest

from sports.application.nba_demo_runtime import BasketballDemoRuntime
from sports.data.sources.nba_cdn_source import (
    NbaCdnSource,
    NbaCdnSourceError,
)


class FakeResponse:
    def __init__(
        self,
        payload: dict,
        error: Exception | None = None,
        status_code: int = 200,
    ) -> None:
        self.payload = payload
        self.error = error
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.error is not None:
            raise self.error

    def json(self) -> dict:
        return self.payload


class FakeSession:
    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self.responses[url]


def boxscore(game_id: str, points: int) -> dict:
    return {
        "game": {
            "gameId": game_id,
            "gameTimeUTC": "2026-01-02T01:00:00Z",
            "homeTeam": {
                "teamId": 1,
                "teamTricode": "HOM",
                "teamName": "Home",
                "teamCity": "City",
                "players": [
                    {
                        "personId": 10,
                        "name": "Real Player",
                        "statistics": {
                            "minutes": "PT35M30.00S",
                            "points": points,
                            "reboundsTotal": 8,
                            "assists": 5,
                            "fieldGoalsMade": 7,
                            "fieldGoalsAttempted": 14,
                            "threePointersMade": 3,
                            "threePointersAttempted": 7,
                            "freeThrowsMade": 3,
                            "freeThrowsAttempted": 4,
                        },
                    }
                ],
            },
            "awayTeam": {
                "teamId": 2,
                "teamTricode": "AWY",
                "teamName": "Away",
                "teamCity": "Town",
                "players": [],
            },
        }
    }


def test_fetches_latest_completed_games_and_normalizes_players() -> None:
    schedule = {
        "leagueSchedule": {
            "gameDates": [
                {
                    "gameDate": "01/01/2026 00:00:00",
                    "games": [
                        {"gameId": "old", "gameStatus": 3},
                        {"gameId": "live", "gameStatus": 2},
                    ],
                },
                {
                    "gameDate": "01/02/2026 00:00:00",
                    "games": [{"gameId": "new", "gameStatus": 3}],
                },
            ]
        }
    }
    responses = {
        NbaCdnSource.schedule_url: FakeResponse(schedule),
        NbaCdnSource.boxscore_url.format(game_id="new"): FakeResponse(
            boxscore("new", 20)
        ),
    }
    session = FakeSession(responses)

    rows = NbaCdnSource(session=session, timeout=9).fetch_recent_game_logs(limit=1)

    assert len(rows) == 1
    assert rows[0] == {
        "player_id": 10,
        "player_name": "Real Player",
        "team_id": 1,
        "team_abbreviation": "HOM",
        "team_name": "City Home",
        "game_id": "new",
        "game_date": "2026-01-02",
        "minutes": "35:30",
        "pts": 20,
        "reb": 8,
        "ast": 5,
        "fgm": 7,
        "fga": 14,
        "fg3m": 3,
        "fg3a": 7,
        "ftm": 3,
        "fta": 4,
    }
    assert all(call[1]["timeout"] == 9 for call in session.calls)
    assert all(
        {"User-Agent", "Referer", "Origin", "Accept"} <= call[1]["headers"].keys()
        for call in session.calls
    )


def test_wraps_request_failures() -> None:
    class TimeoutSession:
        def get(self, url: str, **kwargs: object) -> FakeResponse:
            raise requests.Timeout("slow")

    with pytest.raises(NbaCdnSourceError, match="timed out"):
        NbaCdnSource(session=TimeoutSession()).fetch_recent_game_logs()


def test_retries_after_403_by_priming_nba_session() -> None:
    schedule = {
        "leagueSchedule": {
            "gameDates": [
                {
                    "gameDate": "01/02/2026 00:00:00",
                    "games": [{"gameId": "new", "gameStatus": 3}],
                }
            ]
        }
    }

    class PrimingSession:
        def __init__(self) -> None:
            self.schedule_calls = 0

        def get(self, url: str, **kwargs: object) -> FakeResponse:
            if url == NbaCdnSource.site_url:
                return FakeResponse({}, status_code=200)

            if url == NbaCdnSource.schedule_url:
                self.schedule_calls += 1
                if self.schedule_calls == 1:
                    return FakeResponse(
                        {},
                        error=requests.HTTPError("403 Client Error"),
                        status_code=403,
                    )
                return FakeResponse(schedule)

            if url == NbaCdnSource.boxscore_url.format(game_id="new"):
                return FakeResponse(boxscore("new", 20))

            raise AssertionError(f"unexpected URL: {url}")

    rows = NbaCdnSource(session=PrimingSession()).fetch_recent_game_logs(limit=1)

    assert len(rows) == 1
    assert rows[0]["game_id"] == "new"


def test_runtime_uses_official_sample_after_stats_timeout(tmp_path) -> None:
    class TimedOutStatsSource:
        def fetch_player_game_logs(self, season: str) -> list[dict]:
            raise requests.Timeout("historical request timed out")

    class FakeCdnSource:
        def fetch_recent_game_logs(self, limit: int = 40) -> list[dict]:
            return NbaCdnSource(
                session=FakeSession(
                    {
                        NbaCdnSource.schedule_url: FakeResponse(
                            {
                                "leagueSchedule": {
                                    "gameDates": [
                                        {
                                            "gameDate": "01/02/2026",
                                            "games": [
                                                {
                                                    "gameId": "new",
                                                    "gameStatus": 3,
                                                }
                                            ],
                                        }
                                    ]
                                }
                            }
                        ),
                        NbaCdnSource.boxscore_url.format(game_id="new"): FakeResponse(
                            boxscore("new", 20)
                        ),
                    }
                )
            ).fetch_recent_game_logs(limit)

    runtime = BasketballDemoRuntime(
        database_path=tmp_path / "demo.db",
        source=TimedOutStatsSource(),
        cdn_source=FakeCdnSource(),
    )

    runtime.load_history()

    logs = runtime.game_log_repository.list_by_season(runtime.current_season)
    stats = runtime.stats_repository.get("10", runtime.current_season)
    assert len(logs) == 1
    assert logs[0]["source"] == "nba-cdn"
    assert stats is not None
    assert stats.points == 20
    assert stats.minutes == 35.5
    message = runtime.get_load_status_message() or ""
    assert "Official NBA CDN" in message
    assert "games" in message
    assert "players" in message

    details = runtime.get_load_status_details()
    assert details is not None
    assert details["success"] is True
    assert details["source_label"] == "Official NBA CDN"
    assert details["games_loaded"] == 1
    assert details["players_loaded"] == 1
