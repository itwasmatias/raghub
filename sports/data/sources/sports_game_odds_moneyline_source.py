from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests


LOGGER = logging.getLogger("sip.odds")


class SportsGameOddsMoneylineSource:
    """SportsGameOdds v2 pregame full-event moneyline adapter."""

    name = "sports-game-odds"
    default_base_url = "https://api.sportsgameodds.com/v2"
    supported_books = ("draftkings", "fanduel", "betmgm")
    supported_leagues = ("WNBA", "MLB")
    moneyline_ids = {
        "home": "points-home-game-ml-home",
        "away": "points-away-game-ml-away",
    }

    def __init__(
        self,
        *,
        api_key: str,
        leagues: tuple[str, ...] = supported_leagues,
        base_url: str = default_base_url,
        request_timeout_seconds: float = 15,
        max_events_per_request: int = 10,
        session: Any | None = None,
        timeout: float | None = None,
        clock=None,
        page_limit: int = 5,
    ) -> None:
        if not api_key.strip():
            raise ValueError("ODDS_API_KEY is required")
        unsupported = set(leagues) - set(self.supported_leagues)
        if unsupported:
            raise ValueError(f"unsupported SportsGameOdds leagues: {sorted(unsupported)}")
        self.api_key = api_key.strip()
        self.leagues = leagues
        normalized_base_url = base_url.strip().rstrip("/")
        self.base_url = (
            normalized_base_url
            if normalized_base_url.endswith("/events")
            else f"{normalized_base_url}/events"
        )
        self.session = session or requests.Session()
        self.timeout = timeout if timeout is not None else request_timeout_seconds
        if max_events_per_request <= 0:
            raise ValueError("ODDS_MAX_EVENTS_PER_REQUEST must be positive")
        self.max_events_per_request = max_events_per_request
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.page_limit = page_limit
        self.last_errors: dict[str, str] = {}

    def authenticate(self) -> dict[str, Any]:
        response = self.session.get(
            self.base_url,
            headers={"x-api-key": self.api_key, "Accept": "application/json"},
            params={"leagueID": "MLB", "oddsAvailable": "true", "limit": 1},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise ValueError(self._error(payload))
        return {"authenticated": True, "events_accessible": len(payload.get("data") or [])}

    def fetch(self, *, correlation_id: str = "") -> list[dict[str, Any]]:
        rows = []
        self.last_errors = {}
        for league in self.leagues:
            cursor = None
            for page in range(self.page_limit):
                params = {
                    "leagueID": league,
                    "oddsAvailable": "true",
                    "oddID": ",".join(self.moneyline_ids.values()),
                    "includeOpposingOdds": "true",
                    "includeAltLines": "false",
                    "limit": self.max_events_per_request,
                }
                if cursor:
                    params["cursor"] = cursor
                LOGGER.info(
                    "odds_request_start",
                    extra={"sport": league, "correlation_id": correlation_id},
                )
                response = self.session.get(
                    self.base_url,
                    headers={
                        "x-api-key": self.api_key,
                        "Accept": "application/json",
                    },
                    params=params,
                    timeout=self.timeout,
                )
                try:
                    payload = response.json()
                except (TypeError, ValueError):
                    payload = None
                if response.status_code >= 400:
                    message = self._error(payload)
                    if response.status_code == 400 and "subscription tier" in message:
                        self.last_errors[league] = message
                        LOGGER.warning(
                            "odds_league_unavailable",
                            extra={"sport": league, "correlation_id": correlation_id},
                        )
                        break
                    response.raise_for_status()
                if not isinstance(payload, dict) or payload.get("success") is not True:
                    raise ValueError(self._error(payload))
                events = payload.get("data") or []
                if not isinstance(events, list):
                    raise ValueError("SportsGameOdds data must be a list")
                accepted = self._normalize_payload(league, events)
                rows.extend(accepted)
                LOGGER.info(
                    "odds_request_complete",
                    extra={
                        "sport": league,
                        "events_received": len(events),
                        "quotes_accepted": len(accepted),
                        "correlation_id": correlation_id,
                    },
                )
                cursor = payload.get("nextCursor")
                if not cursor:
                    break
            if league in self.last_errors:
                continue
            if cursor:
                LOGGER.warning(
                    "odds_page_limit_reached",
                    extra={"sport": league, "correlation_id": correlation_id},
                )
        return rows

    def _normalize_payload(
        self, league: str, events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        retrieved_at = self.clock().astimezone(timezone.utc).isoformat()
        rows = []
        for event in events:
            status = event.get("status") or {}
            if (
                status.get("started")
                or status.get("live")
                or status.get("completed")
                or status.get("cancelled")
            ):
                continue
            start = str(status.get("startsAt") or event.get("startsAt") or "")
            teams = event.get("teams") or {}
            home = self._team_name(teams.get("home") or {})
            away = self._team_name(teams.get("away") or {})
            event_id = str(event.get("eventID") or "")
            if not all((event_id, start, home, away)):
                continue
            odds = event.get("odds") or {}
            for selection, odd_id in self.moneyline_ids.items():
                odd = odds.get(odd_id) or {}
                if (
                    odd.get("periodID") not in (None, "game")
                    or odd.get("betTypeID") not in (None, "ml")
                    or odd.get("started")
                    or odd.get("ended")
                    or odd.get("cancelled")
                ):
                    continue
                selected_team = home if selection == "home" else away
                for book, quote in (odd.get("byBookmaker") or {}).items():
                    book_key = str(book).lower()
                    if book_key not in self.supported_books:
                        continue
                    if not quote.get("available", False):
                        continue
                    try:
                        price = int(str(quote["odds"]).replace("+", ""))
                    except (KeyError, TypeError, ValueError):
                        continue
                    rows.append({
                        "provider_event_id": event_id,
                        "league": league,
                        "season": str(
                            datetime.fromisoformat(
                                start.replace("Z", "+00:00")
                            ).year
                        ),
                        "event_start": start,
                        "home_team": home,
                        "away_team": away,
                        "venue": (event.get("venue") or {}).get("name"),
                        "sportsbook": book_key,
                        "market": "h2h",
                        "period": "full_game",
                        "selection": selection,
                        "selection_team": selected_team,
                        "line": None,
                        "american_price": price,
                        "observed_at": retrieved_at,
                        "provider_updated_at": quote.get("lastUpdatedAt"),
                        "source": self.name,
                        "source_url": self.base_url,
                        "data_mode": "live",
                        "is_live": False,
                    })
        return rows

    @staticmethod
    def _team_name(team: dict[str, Any]) -> str:
        names = team.get("names") or {}
        return str(names.get("long") or team.get("name") or "")

    @staticmethod
    def _error(payload: Any) -> str:
        if isinstance(payload, dict):
            return f"SportsGameOdds request failed: {payload.get('error') or 'unknown error'}"
        return "SportsGameOdds returned invalid JSON"
