from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests


LOGGER = logging.getLogger("sip.odds")


class TheOddsApiMoneylineSource:
    """The Odds API adapter for WNBA and MLB pregame full-game moneylines."""

    name = "the-odds-api"
    base_url = "https://api.the-odds-api.com/v4/sports"
    supported_books = ("draftkings", "fanduel", "betmgm")

    def __init__(
        self,
        *,
        api_key: str,
        sports: tuple[str, ...] = ("basketball_wnba", "baseball_mlb"),
        regions: str = "us",
        session: Any | None = None,
        timeout: float = 20,
        clock=None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("ODDS_API_KEY is required")
        self.api_key = api_key.strip()
        self.sports = sports
        self.regions = regions
        self.session = session or requests.Session()
        self.timeout = timeout
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.last_quota: dict[str, int | None] = {}

    def fetch(self, *, correlation_id: str = "") -> list[dict[str, Any]]:
        rows = []
        for sport in self.sports:
            LOGGER.info(
                "odds_request_start",
                extra={"sport": sport, "correlation_id": correlation_id},
            )
            response = self.session.get(
                f"{self.base_url}/{sport}/odds",
                params={
                    "apiKey": self.api_key,
                    "regions": self.regions,
                    "markets": "h2h",
                    "oddsFormat": "american",
                    "dateFormat": "iso",
                    "bookmakers": ",".join(self.supported_books),
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            self._quota(response.headers)
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError(f"{sport} odds response must be a list")
            accepted = self._normalize_payload(sport, payload)
            rows.extend(accepted)
            LOGGER.info(
                "odds_request_complete",
                extra={
                    "sport": sport,
                    "events_received": len(payload),
                    "quotes_accepted": len(accepted),
                    "correlation_id": correlation_id,
                },
            )
        return rows

    def _normalize_payload(
        self, sport: str, events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        league = "WNBA" if sport == "basketball_wnba" else "MLB"
        now = self.clock().astimezone(timezone.utc).isoformat()
        rows = []
        for event in events:
            event_id = str(event.get("id") or "")
            start = str(event.get("commence_time") or "")
            home = str(event.get("home_team") or "")
            away = str(event.get("away_team") or "")
            if not all((event_id, start, home, away)):
                continue
            for book in event.get("bookmakers") or []:
                book_key = str(book.get("key") or "").lower()
                if book_key not in self.supported_books:
                    continue
                for market in book.get("markets") or []:
                    if market.get("key") != "h2h":
                        continue
                    observed = str(
                        market.get("last_update")
                        or book.get("last_update")
                        or now
                    )
                    for outcome in market.get("outcomes") or []:
                        team = str(outcome.get("name") or "")
                        selection = (
                            "home"
                            if team == home
                            else "away" if team == away else ""
                        )
                        price = outcome.get("price")
                        if not selection or price is None:
                            continue
                        rows.append(
                            {
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
                                "venue": None,
                                "sportsbook": str(
                                    book.get("title") or book_key
                                ),
                                "market": "h2h",
                                "period": "full_game",
                                "selection": selection,
                                "selection_team": team,
                                "line": None,
                                "american_price": price,
                                "observed_at": observed,
                                "source": self.name,
                                "source_url": (
                                    f"{self.base_url}/{sport}/odds"
                                ),
                                "data_mode": "live",
                                "is_live": False,
                            }
                        )
        return rows

    def _quota(self, headers: Any) -> None:
        for label, header in (
            ("remaining", "x-requests-remaining"),
            ("used", "x-requests-used"),
            ("last", "x-requests-last"),
        ):
            value = headers.get(header)
            self.last_quota[label] = int(value) if value is not None else None
