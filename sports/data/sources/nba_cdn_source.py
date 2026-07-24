import re
from datetime import datetime
from typing import Any

import requests


class NbaCdnSourceError(RuntimeError):
    """Raised when official NBA CDN data cannot be loaded."""


class NbaCdnSource:
    site_url = "https://www.nba.com/"
    schedule_url = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json"
    boxscore_url = (
        "https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{game_id}.json"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.nba.com/",
        "Origin": "https://www.nba.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }

    def __init__(
        self,
        session: requests.Session | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self._session_primed = False

    def fetch_recent_game_logs(
        self,
        limit: int = 40,
    ) -> list[dict[str, Any]]:
        schedule = self._get_json(self.schedule_url)
        completed = self._completed_games(schedule)
        selected = completed[-limit:] if limit > 0 else []

        rows: list[dict[str, Any]] = []
        for _, game_id in selected:
            payload = self._get_json(self.boxscore_url.format(game_id=game_id))
            rows.extend(self._normalize_boxscore(payload))
        return rows

    def _get_json(self, url: str) -> dict[str, Any]:
        try:
            response = self._request_json_url(url)
            if response.status_code == 403 and not self._session_primed:
                self._prime_session()
                response = self._request_json_url(url)

            response.raise_for_status()
        except requests.Timeout as error:
            raise NbaCdnSourceError("NBA CDN request timed out") from error
        except requests.RequestException as error:
            raise NbaCdnSourceError(f"NBA CDN HTTP request failed: {error}") from error

        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            raise NbaCdnSourceError("NBA CDN returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise NbaCdnSourceError("NBA CDN returned an invalid payload")
        return payload

    def _request_json_url(self, url: str) -> requests.Response:
        return self.session.get(
            url,
            headers=self.headers,
            timeout=self.timeout,
        )

    def _prime_session(self) -> None:
        self._session_primed = True
        try:
            self.session.get(
                self.site_url,
                headers={
                    "User-Agent": self.headers["User-Agent"],
                    "Accept": "text/html,application/xhtml+xml",
                },
                timeout=self.timeout,
            )
        except requests.RequestException:
            # Best effort: the retry may still succeed in some network setups.
            return

    @staticmethod
    def _completed_games(
        payload: dict[str, Any],
    ) -> list[tuple[str, str]]:
        game_dates = payload.get("leagueSchedule", {}).get("gameDates", [])
        completed: list[tuple[str, str]] = []
        for date_entry in game_dates:
            date_value = str(date_entry.get("gameDate") or "")
            for game in date_entry.get("games", []):
                if game.get("gameStatus") != 3:
                    continue
                sort_value = str(
                    game.get("gameDateTimeUTC") or game.get("gameTimeUTC") or date_value
                )
                completed.append((sort_value, str(game["gameId"])))
        completed.sort()
        return completed

    def _normalize_boxscore(
        self,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        game = payload.get("game", {})
        game_id = str(game.get("gameId") or "")
        game_date = self._game_date(game)
        rows: list[dict[str, Any]] = []

        for side in ("homeTeam", "awayTeam"):
            team = game.get(side, {})
            team_name = " ".join(
                part
                for part in (
                    str(team.get("teamCity") or ""),
                    str(team.get("teamName") or ""),
                )
                if part
            )
            for player in team.get("players", []):
                stats = player.get("statistics") or {}
                if not stats:
                    continue
                rows.append(
                    {
                        "player_id": player.get("personId"),
                        "player_name": self._player_name(player),
                        "team_id": team.get("teamId"),
                        "team_abbreviation": team.get("teamTricode"),
                        "team_name": team_name,
                        "game_id": game_id,
                        "game_date": game_date,
                        "minutes": self._minutes(stats.get("minutes")),
                        "pts": stats.get("points"),
                        "reb": stats.get("reboundsTotal"),
                        "ast": stats.get("assists"),
                        "fgm": stats.get("fieldGoalsMade"),
                        "fga": stats.get("fieldGoalsAttempted"),
                        "fg3m": stats.get("threePointersMade"),
                        "fg3a": stats.get("threePointersAttempted"),
                        "ftm": stats.get("freeThrowsMade"),
                        "fta": stats.get("freeThrowsAttempted"),
                    }
                )
        return rows

    @staticmethod
    def _game_date(game: dict[str, Any]) -> str:
        value = str(game.get("gameTimeUTC") or game.get("gameTimeLocal") or "")
        return value[:10]

    @staticmethod
    def _player_name(player: dict[str, Any]) -> str:
        if player.get("name"):
            return str(player["name"])
        return " ".join(
            part
            for part in (
                str(player.get("firstName") or ""),
                str(player.get("familyName") or ""),
            )
            if part
        )

    @staticmethod
    def _minutes(value: Any) -> Any:
        if not isinstance(value, str) or not value.startswith("PT"):
            return value
        match = re.fullmatch(
            r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?",
            value,
        )
        if match is None:
            return value
        hours, minutes, seconds = match.groups()
        total_minutes = int(hours or 0) * 60 + int(minutes or 0)
        total_seconds = float(seconds or 0)
        return f"{total_minutes}:{int(total_seconds):02d}"
