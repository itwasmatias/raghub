from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import requests


class TheOddsApiSource:
    """Licensed-feed adapter for current NBA player-prop markets."""

    base_url = "https://api.the-odds-api.com/v4/sports/basketball_nba"
    name = "the-odds-api"

    def __init__(
        self,
        *,
        api_key: str,
        session: Any | None = None,
        timeout: float = 20,
        bookmakers: tuple[str, ...] = (
            "draftkings",
            "fanduel",
            "betmgm",
        ),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("The Odds API key is required.")
        self.api_key = api_key.strip()
        self.session = session or requests.Session()
        self.timeout = timeout
        self.bookmakers = bookmakers
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.quota: dict[str, int | None] = {
            "remaining": None,
            "used": None,
            "last": None,
        }

    def fetch_changed(
        self, dataset: str, since: str | None
    ) -> list[dict[str, Any]]:
        if dataset != "sportsbook_markets":
            raise ValueError(
                "TheOddsApiSource only supports sportsbook_markets."
            )
        events_url = f"{self.base_url}/events"
        events_response = self.session.get(
            events_url,
            params={
                "apiKey": self.api_key,
                "dateFormat": "iso",
            },
            timeout=self.timeout,
        )
        events_response.raise_for_status()
        events = events_response.json()
        rows: list[dict[str, Any]] = []
        for event in events:
            event_id = str(event.get("id") or "")
            if not event_id:
                continue
            odds_url = f"{self.base_url}/events/{event_id}/odds"
            response = self.session.get(
                odds_url,
                params={
                    "apiKey": self.api_key,
                    "regions": "us",
                    "markets": "player_points",
                    "oddsFormat": "american",
                    "dateFormat": "iso",
                    "bookmakers": ",".join(self.bookmakers),
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            self._record_quota(response.headers)
            rows.extend(self._normalize_event(response.json(), odds_url))
        return rows

    def _normalize_event(
        self, event: dict[str, Any], source_url: str
    ) -> list[dict[str, Any]]:
        event_id = str(event.get("id") or "")
        event_start = str(event.get("commence_time") or "")
        home = str(event.get("home_team") or "")
        away = str(event.get("away_team") or "")
        rows = []
        for book in event.get("bookmakers") or []:
            book_key = str(book.get("key") or "")
            book_title = str(book.get("title") or book_key)
            if book_key not in self.bookmakers:
                continue
            for market in book.get("markets") or []:
                if market.get("key") != "player_points":
                    continue
                fetched_at = str(
                    market.get("last_update")
                    or self.clock().astimezone(timezone.utc).isoformat()
                )
                for outcome in market.get("outcomes") or []:
                    player = str(outcome.get("description") or "")
                    selection = str(outcome.get("name") or "")
                    line = outcome.get("point")
                    price = outcome.get("price")
                    if not player or line is None or price is None:
                        continue
                    identity = ":".join(
                        (
                            "odds",
                            event_id,
                            book_key,
                            "player_points",
                            self._slug(player),
                            selection.lower(),
                            str(line),
                        )
                    )
                    rows.append(
                        {
                            "canonical_id": identity,
                            "provider_event_id": event_id,
                            "home_team": home,
                            "away_team": away,
                            "event_start": event_start,
                            "player": player,
                            "market": "player_points",
                            "outcome": selection,
                            "outcome_definition": "over_under",
                            "line": float(line),
                            "american_price": int(price),
                            "sportsbook": book_title,
                            "sportsbook_key": book_key,
                            "fetched_at": fetched_at,
                            "source": self.name,
                            "source_url": source_url,
                            "data_classification": "live",
                            "status": "active",
                            "updated_at": fetched_at,
                        }
                    )
        return rows

    def _record_quota(self, headers: Any) -> None:
        mapping = {
            "remaining": "x-requests-remaining",
            "used": "x-requests-used",
            "last": "x-requests-last",
        }
        for key, header in mapping.items():
            value = headers.get(header)
            self.quota[key] = int(value) if value is not None else None

    @staticmethod
    def _slug(value: str) -> str:
        return "-".join(
            "".join(
                character.lower() if character.isalnum() else " "
                for character in value
            ).split()
        )
