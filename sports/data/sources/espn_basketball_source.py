from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import requests


class EspnBasketballSourceError(RuntimeError):
    """Raised when ESPN basketball data cannot be retrieved or normalized."""


class EspnBasketballSource:
    scoreboard_url = (
        "https://site.api.espn.com/apis/site/v2/sports/basketball/"
        "{league}/scoreboard?dates={date}"
    )
    summary_url = (
        "https://site.api.espn.com/apis/site/v2/sports/basketball/"
        "{league}/summary?event={event_id}"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
    }

    def __init__(
        self,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        today: Callable[[], date] | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.today = today or date.today

    def fetch_recent_game_logs(
        self,
        leagues: tuple[str, ...] = ("nba", "wnba"),
        minimum_players: int = 10,
        max_days: int = 30,
    ) -> list[dict[str, Any]]:
        unsupported = set(leagues) - {"nba", "wnba"}
        if unsupported:
            raise ValueError(
                f"unsupported ESPN basketball leagues: {sorted(unsupported)}"
            )

        rows: list[dict[str, Any]] = []
        seen_events: set[tuple[str, str]] = set()
        for offset in range(max_days):
            game_date = self.today() - timedelta(days=offset)
            date_text = game_date.strftime("%Y%m%d")
            for league in leagues:
                scoreboard = self._get_json(
                    self.scoreboard_url.format(
                        league=league,
                        date=date_text,
                    )
                )
                events = scoreboard.get("events")
                if not isinstance(events, list):
                    raise EspnBasketballSourceError(
                        "ESPN scoreboard returned an invalid response"
                    )
                for event in events:
                    if not self._is_completed(event):
                        continue
                    event_id = str(event.get("id") or "")
                    identity = (league, event_id)
                    if not event_id or identity in seen_events:
                        continue
                    seen_events.add(identity)
                    summary = self._get_json(
                        self.summary_url.format(
                            league=league,
                            event_id=event_id,
                        )
                    )
                    rows.extend(
                        self._normalize_summary(summary, event_id, league)
                    )
            if len(rows) >= minimum_players:
                break
        return rows

    def _get_json(self, url: str) -> dict[str, Any]:
        try:
            response = self.session.get(
                url,
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.Timeout as error:
            raise EspnBasketballSourceError(
                "ESPN basketball request timed out"
            ) from error
        except requests.RequestException as error:
            raise EspnBasketballSourceError(
                f"ESPN basketball HTTP request failed: {error}"
            ) from error

        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            raise EspnBasketballSourceError(
                "ESPN basketball returned invalid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise EspnBasketballSourceError(
                "ESPN basketball returned an invalid response"
            )
        return payload

    @staticmethod
    def _is_completed(event: dict[str, Any]) -> bool:
        return bool(
            event.get("status", {})
            .get("type", {})
            .get("completed", False)
        )

    def _normalize_summary(
        self,
        payload: dict[str, Any],
        event_id: str,
        league: str,
    ) -> list[dict[str, Any]]:
        boxscore = payload.get("boxscore")
        if not isinstance(boxscore, dict):
            raise EspnBasketballSourceError(
                "ESPN summary returned an invalid response"
            )
        player_groups = boxscore.get("players")
        if not isinstance(player_groups, list):
            raise EspnBasketballSourceError(
                "ESPN summary returned an invalid player box score"
            )

        game_date = self._game_date(payload)
        rows: list[dict[str, Any]] = []
        for group in player_groups:
            team = group.get("team", {})
            for statistics in group.get("statistics", []):
                names = statistics.get("names") or statistics.get("labels")
                if not isinstance(names, list):
                    continue
                for entry in statistics.get("athletes", []):
                    if entry.get("didNotPlay"):
                        continue
                    athlete = entry.get("athlete", {})
                    stats = dict(zip(names, entry.get("stats", [])))
                    rows.append(
                        self._normalized_player(
                            athlete,
                            team,
                            stats,
                            event_id,
                            game_date,
                            league,
                        )
                    )
        return rows

    def _normalized_player(
        self,
        athlete: dict[str, Any],
        team: dict[str, Any],
        stats: dict[str, Any],
        event_id: str,
        game_date: str,
        league: str,
    ) -> dict[str, Any]:
        field_goals = self._pair(stats, "fieldgoals")
        three_points = self._pair(stats, "threepointfieldgoals")
        free_throws = self._pair(stats, "freethrows")
        return {
            "league": league.upper(),
            "player_id": str(athlete.get("id") or ""),
            "player_name": str(athlete.get("displayName") or ""),
            "team_id": str(team.get("id") or ""),
            "team_abbreviation": team.get("abbreviation"),
            "team_name": team.get("displayName"),
            "game_id": event_id,
            "game_date": game_date,
            "minutes": self._value(stats, "minutes", "min"),
            "pts": self._number(self._value(stats, "points", "pts")),
            "reb": self._number(self._value(stats, "rebounds", "reb")),
            "ast": self._number(self._value(stats, "assists", "ast")),
            "fgm": field_goals[0],
            "fga": field_goals[1],
            "fg3m": three_points[0],
            "fg3a": three_points[1],
            "ftm": free_throws[0],
            "fta": free_throws[1],
        }

    @staticmethod
    def _game_date(payload: dict[str, Any]) -> str:
        competitions = payload.get("header", {}).get("competitions", [])
        if not competitions:
            return ""
        return str(competitions[0].get("date") or "")[:10]

    @classmethod
    def _value(cls, stats: dict[str, Any], *targets: str) -> Any:
        normalized_targets = {
            cls._normalize_name(target) for target in targets
        }
        for name, value in stats.items():
            if cls._normalize_name(str(name)) in normalized_targets:
                return value
        return None

    @classmethod
    def _pair(
        cls,
        stats: dict[str, Any],
        prefix: str,
    ) -> tuple[float, float]:
        normalized_prefix = cls._normalize_name(prefix)
        for name, value in stats.items():
            normalized = cls._normalize_name(str(name))
            if normalized.startswith(normalized_prefix) and "-" in str(value):
                made, attempted = str(value).split("-", maxsplit=1)
                return cls._number(made), cls._number(attempted)

        made = cls._value(stats, f"{prefix}made")
        attempted = cls._value(stats, f"{prefix}attempted")
        return cls._number(made), cls._number(attempted)

    @staticmethod
    def _normalize_name(value: str) -> str:
        return "".join(character for character in value.lower() if character.isalnum())

    @staticmethod
    def _number(value: Any) -> float:
        if value in (None, "", "--"):
            return 0.0
        return float(value)
