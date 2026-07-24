from datetime import date

import requests

import pytest

from sports.application.nba_demo_runtime import BasketballDemoRuntime
from sports.data.sources.espn_basketball_source import (
    EspnBasketballSource,
    EspnBasketballSourceError,
)


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(url)
        return self.responses[url]


def summary(event_id: str, player_count: int = 10) -> dict:
    athletes = [
        {
            "athlete": {"id": str(index), "displayName": f"Player {index}"},
            "stats": ["30", str(10 + index), "5", "3", "4-8", "1-3", "1-2"],
        }
        for index in range(player_count)
    ]
    return {
        "header": {
            "id": event_id,
            "competitions": [{"date": "2026-01-05T00:00Z"}],
        },
        "boxscore": {
            "players": [
                {
                    "team": {
                        "id": "1",
                        "abbreviation": "TST",
                        "displayName": "Test Team",
                    },
                    "statistics": [
                        {
                            "names": [
                                "minutes",
                                "points",
                                "rebounds",
                                "assists",
                                "fieldGoalsMade-fieldGoalsAttempted",
                                (
                                    "threePointFieldGoalsMade-"
                                    "threePointFieldGoalsAttempted"
                                ),
                                "freeThrowsMade-freeThrowsAttempted",
                            ],
                            "athletes": athletes,
                        }
                    ],
                }
            ]
        },
    }


def test_searches_completed_games_and_normalizes_player_rows() -> None:
    scoreboard_url = EspnBasketballSource.scoreboard_url.format(
        league="nba",
        date="20260105",
    )
    summary_url = EspnBasketballSource.summary_url.format(
        league="nba",
        event_id="event-1",
    )
    session = FakeSession(
        {
            scoreboard_url: FakeResponse(
                {
                    "events": [
                        {
                            "id": "event-1",
                            "status": {"type": {"completed": True}},
                        },
                        {
                            "id": "live",
                            "status": {"type": {"completed": False}},
                        },
                    ]
                }
            ),
            summary_url: FakeResponse(summary("event-1")),
        }
    )
    source = EspnBasketballSource(
        session=session,
        today=lambda: date(2026, 1, 5),
    )

    rows = source.fetch_recent_game_logs(
        leagues=("nba",),
        minimum_players=10,
        max_days=1,
    )

    assert len(rows) == 10
    assert rows[0] == {
        "league": "NBA",
        "player_id": "0",
        "player_name": "Player 0",
        "team_id": "1",
        "team_abbreviation": "TST",
        "team_name": "Test Team",
        "game_id": "event-1",
        "game_date": "2026-01-05",
        "minutes": "30",
        "pts": 10.0,
        "reb": 5.0,
        "ast": 3.0,
        "fgm": 4.0,
        "fga": 8.0,
        "fg3m": 1.0,
        "fg3a": 3.0,
        "ftm": 1.0,
        "fta": 2.0,
    }
    assert summary_url in session.calls


def test_supports_wnba_and_walks_backward() -> None:
    today_url = EspnBasketballSource.scoreboard_url.format(
        league="wnba",
        date="20260105",
    )
    prior_url = EspnBasketballSource.scoreboard_url.format(
        league="wnba",
        date="20260104",
    )
    event_url = EspnBasketballSource.summary_url.format(
        league="wnba",
        event_id="w1",
    )
    session = FakeSession(
        {
            today_url: FakeResponse({"events": []}),
            prior_url: FakeResponse(
                {
                    "events": [
                        {
                            "id": "w1",
                            "status": {"type": {"completed": True}},
                        }
                    ]
                }
            ),
            event_url: FakeResponse(summary("w1", 1)),
        }
    )

    rows = EspnBasketballSource(
        session=session,
        today=lambda: date(2026, 1, 5),
    ).fetch_recent_game_logs(
        leagues=("wnba",),
        minimum_players=1,
        max_days=2,
    )

    assert len(rows) == 1
    assert session.calls[:2] == [today_url, prior_url]


@pytest.mark.parametrize(
    "response",
    [requests.Timeout("slow"), FakeResponse(ValueError("bad json"))],
)
def test_raises_clear_source_errors(response: object) -> None:
    class ErrorSession:
        def get(self, url: str, **kwargs: object) -> FakeResponse:
            if isinstance(response, Exception):
                raise response
            return response

    message = "timed out" if isinstance(response, Exception) else "invalid"
    with pytest.raises(EspnBasketballSourceError, match=message):
        EspnBasketballSource(
            session=ErrorSession(),
            today=lambda: date(2026, 1, 5),
        ).fetch_recent_game_logs(max_days=1)


def test_runtime_falls_back_to_espn_when_cdn_is_empty(tmp_path) -> None:
    class TimedOutStats:
        def fetch_player_game_logs(self, season: str) -> list[dict]:
            raise requests.Timeout("timed out")

    class EmptyCdn:
        def fetch_recent_game_logs(self, limit: int = 40) -> list[dict]:
            return []

    class FakeEspn:
        def fetch_recent_game_logs(self, limit: int = 40) -> list[dict]:
            rows = summary("espn-game")["boxscore"]["players"][0]["statistics"][0][
                "athletes"
            ]
            return [
                {
                    "league": "NBA",
                    "player_id": row["athlete"]["id"],
                    "player_name": row["athlete"]["displayName"],
                    "team_id": "1",
                    "team_abbreviation": "TST",
                    "team_name": "Test Team",
                    "game_id": "espn-game",
                    "game_date": "2026-01-05",
                    "minutes": row["stats"][0],
                    "pts": float(row["stats"][1]),
                    "reb": float(row["stats"][2]),
                    "ast": float(row["stats"][3]),
                    "fgm": 4.0,
                    "fga": 8.0,
                    "fg3m": 1.0,
                    "fg3a": 3.0,
                    "ftm": 1.0,
                    "fta": 2.0,
                }
                for row in rows
            ]

    runtime = BasketballDemoRuntime(
        database_path=tmp_path / "demo.db",
        source=TimedOutStats(),
        cdn_source=EmptyCdn(),
        espn_source=FakeEspn(),
    )

    runtime.load_history()

    logs = runtime.game_log_repository.list_by_season(runtime.current_season)
    assert len(logs) == 10
    assert all(log["source"] == "espn_web" for log in logs)
    message = runtime.get_load_status_message() or ""
    assert "ESPN public web data" in message
    assert "games" in message
    assert "players" in message

    details = runtime.get_load_status_details()
    assert details is not None
    assert details["success"] is True
    assert details["source_label"] == "ESPN public web data"
    assert details["players_loaded"] == 10
