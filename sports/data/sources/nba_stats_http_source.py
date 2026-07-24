from typing import Any

import requests


class NbaStatsHttpSourceError(RuntimeError):
    """Raised when NBA Stats game logs cannot be retrieved or decoded."""


class NbaStatsHttpSource:
    endpoint = "https://stats.nba.com/stats/playergamelogs"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.nba.com/",
        "Origin": "https://www.nba.com",
        "Accept": "application/json, text/plain, */*",
    }

    def __init__(
        self,
        session: requests.Session | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout

    def fetch_player_game_logs(
        self,
        season: str,
        league_id: str,
        season_type: str,
    ) -> list[dict[str, Any]]:
        try:
            response = self.session.get(
                self.endpoint,
                params={
                    "Season": season,
                    "LeagueID": league_id,
                    "SeasonType": season_type,
                },
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.Timeout as error:
            raise NbaStatsHttpSourceError(
                "NBA Stats request timed out"
            ) from error
        except requests.RequestException as error:
            raise NbaStatsHttpSourceError(
                f"NBA Stats HTTP request failed: {error}"
            ) from error

        try:
            payload = response.json()
        except (ValueError, TypeError) as error:
            raise NbaStatsHttpSourceError(
                "NBA Stats returned invalid JSON"
            ) from error

        if not isinstance(payload, dict):
            raise NbaStatsHttpSourceError(
                "NBA Stats returned invalid JSON payload"
            )
        return self._normalize_player_game_logs(payload)

    def _normalize_player_game_logs(
        self,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        result_sets = self._result_sets(payload)
        if not result_sets:
            return []

        result_set = next(
            (
                item
                for item in result_sets
                if item.get("name") == "PlayerGameLogs"
            ),
            result_sets[0],
        )
        headers = result_set.get("headers", [])
        rows = result_set.get("rowSet", [])

        normalized: list[dict[str, Any]] = []
        for raw_row in rows:
            row = dict(zip(headers, raw_row))
            normalized.append(
                {
                    "player_id": row.get("PLAYER_ID"),
                    "player_name": row.get("PLAYER_NAME"),
                    "team_id": row.get("TEAM_ID"),
                    "team_abbreviation": row.get("TEAM_ABBREVIATION"),
                    "team_name": row.get("TEAM_NAME"),
                    "game_id": row.get("GAME_ID"),
                    "game_date": row.get("GAME_DATE"),
                    "minutes": row.get("MIN"),
                    "pts": row.get("PTS"),
                    "reb": row.get("REB"),
                    "ast": row.get("AST"),
                    "fgm": row.get("FGM"),
                    "fga": row.get("FGA"),
                    "fg3m": row.get("FG3M"),
                    "fg3a": row.get("FG3A"),
                    "ftm": row.get("FTM"),
                    "fta": row.get("FTA"),
                }
            )
        return normalized

    @staticmethod
    def _result_sets(
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if "resultSets" in payload:
            result_sets = payload["resultSets"]
        elif "resultSet" in payload:
            result_sets = payload["resultSet"]
        elif {"headers", "rowSet"}.issubset(payload):
            result_sets = [payload]
        else:
            return []

        if isinstance(result_sets, dict):
            return [result_sets]
        if not isinstance(result_sets, list):
            return []
        return [
            item for item in result_sets if isinstance(item, dict)
        ]
