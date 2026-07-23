from __future__ import annotations

from collections.abc import Callable
from typing import Any


class NbaApiSource:
    def __init__(
        self,
        endpoint_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._endpoint_factory = endpoint_factory or self._default_endpoint_factory

    def fetch_player_game_logs(
        self,
        season: str,
        league_id: str | None = None,
        season_type: str | None = None,
    ) -> list[dict[str, Any]]:
        endpoint_arguments: dict[str, Any] = {"season_nullable": season}
        if league_id is not None:
            endpoint_arguments["league_id_nullable"] = league_id
        if season_type is not None:
            endpoint_arguments["season_type_nullable"] = season_type

        endpoint = self._endpoint_factory(**endpoint_arguments)
        response_dict = endpoint.get_dict()
        return self._normalize_player_game_logs(response_dict)

    def _default_endpoint_factory(self, **kwargs: Any) -> Any:
        from nba_api.stats.endpoints.playergamelogs import PlayerGameLogs

        return PlayerGameLogs(**kwargs)

    def _normalize_player_game_logs(
        self,
        response_dict: dict[str, Any],
    ) -> list[dict[str, Any]]:
        data_sets = self._extract_data_sets(response_dict)
        player_game_logs = self._select_player_game_logs_data_set(data_sets)
        if not player_game_logs:
            return []

        headers = player_game_logs.get("headers", [])
        rows = player_game_logs.get("rowSet", [])

        normalized_rows: list[dict[str, Any]] = []
        for raw_row in rows:
            row_map = dict(zip(headers, raw_row))
            normalized_rows.append(
                {
                    "player_id": row_map.get("PLAYER_ID"),
                    "player_name": row_map.get("PLAYER_NAME"),
                    "team_id": row_map.get("TEAM_ID"),
                    "team_abbreviation": row_map.get("TEAM_ABBREVIATION"),
                    "team_name": row_map.get("TEAM_NAME"),
                    "game_id": row_map.get("GAME_ID"),
                    "game_date": row_map.get("GAME_DATE"),
                    "minutes": row_map.get("MIN"),
                    "pts": row_map.get("PTS"),
                    "reb": row_map.get("REB"),
                    "ast": row_map.get("AST"),
                    "fgm": row_map.get("FGM"),
                    "fga": row_map.get("FGA"),
                    "fg3m": row_map.get("FG3M"),
                    "fg3a": row_map.get("FG3A"),
                    "ftm": row_map.get("FTM"),
                    "fta": row_map.get("FTA"),
                }
            )

        return normalized_rows

    def _extract_data_sets(
        self,
        response_dict: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if "resultSets" in response_dict:
            data_sets = response_dict["resultSets"]
        elif "resultSet" in response_dict:
            data_sets = response_dict["resultSet"]
        elif {"headers", "rowSet"}.issubset(response_dict):
            data_sets = [response_dict]
        else:
            data_sets = []

        if isinstance(data_sets, dict):
            return [data_sets]

        return list(data_sets)

    def _select_player_game_logs_data_set(
        self,
        data_sets: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not data_sets:
            return None

        for data_set in data_sets:
            if data_set.get("name") == "PlayerGameLogs":
                return data_set

        return data_sets[0]
