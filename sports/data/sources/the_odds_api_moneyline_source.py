from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import requests


LOGGER = logging.getLogger("sip.odds")


class TheOddsApiMoneylineSource:
    """The Odds API adapter for NBA, WNBA, and MLB full-game moneylines."""

    name = "the-odds-api"
    base_url = "https://api.odds-api.io/v3"
    supported_books = ("draftkings", "fanduel", "betmgm")
    sport_aliases = {
        "basketball_nba": ("basketball", "nba"),
        "basketball_wnba": ("basketball", "wnba"),
        "baseball_mlb": ("baseball", "mlb"),
    }
    league_names = {
        "basketball_nba": "NBA",
        "basketball_wnba": "WNBA",
        "baseball_mlb": "MLB",
    }

    def __init__(
        self,
        *,
        api_key: str,
        sports: tuple[str, ...] = ("basketball_nba",),
        regions: str = "us",
        bookmakers: tuple[str, ...] | None = None,
        max_events_per_sport: int = 10,
        cache_ttl_seconds: int = 600,
        event_window_hours: int = 72,
        session: Any | None = None,
        timeout: float = 20,
        clock=None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("ODDS_API_IO_API_KEY is required")
        self.api_key = api_key.strip()
        self.sports = sports
        self.regions = regions
        requested_books = bookmakers or self.supported_books[:2]
        canonical_books: list[str] = []
        for book in requested_books:
            normalized = str(book).strip().lower()
            if normalized in self.supported_books and normalized not in canonical_books:
                canonical_books.append(normalized)
        self.bookmakers = tuple(canonical_books[:2]) or self.supported_books[:2]
        self.max_events_per_sport = max(1, max_events_per_sport)
        self.cache_ttl_seconds = max(60, cache_ttl_seconds)
        self.event_window_hours = max(6, event_window_hours)
        self.session = session or requests.Session()
        self.timeout = timeout
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.last_quota: dict[str, int | None] = {}
        self.last_errors: dict[str, str] = {}
        self.last_stats: dict[str, Any] = {
            "resolved_slugs": {},
            "discovered_event_counts": {},
            "bookmakers": [],
            "odds_requests_made": 0,
            "complete_two_book_events": 0,
            "events_without_odds": 0,
            "missing_event_id": 0,
            "unsupported_bookmaker_rows": 0,
        }
        self._discovery_cache: dict[str, Any] | None = None
        self._events_cache: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self._odds_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._bookmaker_names: tuple[str, ...] = self.supported_books[:2]

    def authenticate(self) -> dict[str, Any]:
        catalog = self._discover_catalog()
        slug_map = self._resolve_requested_slugs(catalog)
        enabled = sorted(slug_map)
        unavailable = sorted(sport for sport in self.sports if sport not in slug_map)
        return {
            "authenticated": True,
            "enabled_sports": enabled,
            "unavailable_sports": unavailable,
            "resolved_slugs": {
                sport: {
                    "sport": values["sport_slug"],
                    "league": values["league_slug"],
                }
                for sport, values in slug_map.items()
            },
        }

    def fetch(self, *, correlation_id: str = "") -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        self.last_errors = {}
        self.last_stats = {
            "resolved_slugs": {},
            "discovered_event_counts": {},
            "bookmakers": [],
            "odds_requests_made": 0,
            "complete_two_book_events": 0,
            "events_without_odds": 0,
            "missing_event_id": 0,
            "unsupported_bookmaker_rows": 0,
        }
        try:
            bookmaker_names = self._discover_bookmakers()
            self._bookmaker_names = bookmaker_names
            self.last_stats["bookmakers"] = list(bookmaker_names)
            catalog = self._discover_catalog()
            slug_map = self._resolve_requested_slugs(catalog)
        except ValueError as error:
            self.last_errors["discovery"] = self._sanitize(
                f"Odds-API.io discovery failed: {error}"
            )
            return rows

        for sport in self.sports:
            resolved = slug_map.get(sport)
            if resolved is None:
                self.last_errors[sport] = self._sanitize(
                    f"Odds-API.io sport slug discovery failed for {sport}."
                )
                continue

            sport_slug = resolved["sport_slug"]
            league_slug = resolved["league_slug"]
            self.last_stats["resolved_slugs"][sport] = {
                "sport": sport_slug,
                "league": league_slug,
            }

            LOGGER.info(
                "odds_request_start",
                extra={"sport": sport, "correlation_id": correlation_id},
            )
            event_ids = self._discover_event_ids(
                sport=sport,
                sport_slug=sport_slug,
                league_slug=None,
            )
            self.last_stats["discovered_event_counts"][sport] = len(event_ids)

            if not event_ids:
                if sport not in self.last_errors:
                    self.last_errors[sport] = self._sanitize(
                        "Odds-API.io returned an empty event list for the requested window."
                    )
                LOGGER.warning(
                    "odds_sport_unavailable",
                    extra={"sport": sport, "correlation_id": correlation_id},
                )
                continue

            rows_before = len(rows)
            no_odds = 0
            complete_two_book_events = 0
            for event_id in event_ids[: self.max_events_per_sport]:
                odds_payload = self._fetch_event_odds(event_id)
                if not odds_payload:
                    no_odds += 1
                    continue
                normalized = self._normalize_event_odds(
                    sport=sport,
                    event_id=event_id,
                    payload=odds_payload,
                )
                rows.extend(normalized)
                books_for_event = {
                    str(item.get("sportsbook") or "").strip().lower()
                    for item in normalized
                    if item.get("sportsbook")
                }
                if len(books_for_event) >= 2:
                    complete_two_book_events += 1

            self.last_stats["events_without_odds"] += no_odds
            self.last_stats["complete_two_book_events"] += complete_two_book_events

            accepted = rows[rows_before:]
            LOGGER.info(
                "odds_request_complete",
                extra={
                    "sport": sport,
                    "events_received": len(event_ids),
                    "quotes_accepted": len(accepted),
                    "correlation_id": correlation_id,
                },
            )
        return rows

    def _discover_catalog(self) -> list[dict[str, str | None]]:
        cached = self._read_cache(self._discovery_cache)
        if cached is not None:
            return cached

        response = self.session.get(
            f"{self.base_url}/sports",
            params={"apiKey": self.api_key},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            message = self._response_detail(response)
            raise ValueError(f"/sports failed: {message}")
        self._quota(response.headers)
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Odds-API.io /sports response must be a list")

        catalog: list[dict[str, str | None]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            sport_slug = self._pick(
                item,
                "slug",
                "sport",
                "sport_slug",
                "sportSlug",
                "sport_key",
                "sportKey",
            )
            league_slug = self._pick(
                item,
                "league",
                "league_slug",
                "leagueSlug",
                "league_key",
                "leagueKey",
            )
            sport_name = self._pick(item, "name", "sport_name", "sportName")
            league_name = self._pick(item, "league_name", "leagueName")
            if not sport_slug:
                continue
            catalog.append(
                {
                    "sport": sport_slug,
                    "league": league_slug or None,
                    "sport_name": sport_name or sport_slug,
                    "league_name": league_name or league_slug,
                }
            )

        self._discovery_cache = self._wrap_cache(catalog)
        return catalog

    def _resolve_requested_slugs(
        self, catalog: list[dict[str, str | None]]
    ) -> dict[str, dict[str, str]]:
        resolved: dict[str, dict[str, str]] = {}
        for sport in self.sports:
            requested_sport, requested_league_hint = self.sport_aliases.get(sport, ("", ""))
            for item in catalog:
                catalog_sport = str(item.get("sport") or "")
                sport_name = self._normalize_text(
                    " ".join(
                        (
                            catalog_sport,
                            str(item.get("sport_name") or ""),
                            str(item.get("name") or ""),
                        )
                    )
                )
                if requested_sport and catalog_sport != requested_sport and requested_sport not in sport_name:
                    continue
                resolved[sport] = {
                    "sport_slug": catalog_sport,
                    "league_slug": requested_league_hint,
                }
                break
        return resolved

    def _discover_event_ids(
        self,
        *,
        sport: str,
        sport_slug: str,
        league_slug: str | None,
    ) -> list[str]:
        now = self.clock().astimezone(timezone.utc)
        window_start = (now - timedelta(hours=2)).isoformat()
        window_end = (now + timedelta(hours=self.event_window_hours)).isoformat()
        cache_key = (sport_slug, league_slug or "", window_start[:13], window_end[:13])
        cached = self._read_cache(self._events_cache.get(cache_key))
        if cached is not None:
            return cached

        requested_hints = self.sport_aliases.get(sport, ("", ""))
        requested_league_hint = requested_hints[1]
        params = {
            "apiKey": self.api_key,
            "sport": sport_slug,
            "status": "pending",
            "from": window_start,
            "to": window_end,
        }
        initial_response = self.session.get(
            f"{self.base_url}/events",
            params=params,
            timeout=self.timeout,
        )
        if initial_response.status_code >= 400:
            self.last_errors[sport] = self._sanitize(
                f"Odds-API.io events request failed: {self._response_detail(initial_response)}"
            )
            if initial_response.status_code == 429:
                self.last_errors[sport] = self._sanitize(
                    "Odds-API.io events request hit rate limits."
                )
            return []

        self._quota(initial_response.headers)
        initial_payload = initial_response.json()
        if not isinstance(initial_payload, list):
            self.last_errors[sport] = self._sanitize(
                "Odds-API.io /events response must be a list."
            )
            return []

        resolved_league_slug = league_slug
        if not resolved_league_slug and requested_league_hint:
            resolved_league_slug = self._discover_league_slug(
                initial_payload,
                requested_league_hint,
            )
            if resolved_league_slug:
                self.last_stats["resolved_slugs"][sport] = {
                    "sport": sport_slug,
                    "league": resolved_league_slug,
                }

        payload = initial_payload
        if resolved_league_slug:
            scoped_cache_key = (
                sport_slug,
                resolved_league_slug,
                window_start[:13],
                window_end[:13],
            )
            cached_scoped = self._read_cache(self._events_cache.get(scoped_cache_key))
            if cached_scoped is not None:
                return cached_scoped
            scoped_params = dict(params)
            scoped_params["league"] = resolved_league_slug
            scoped_response = self.session.get(
                f"{self.base_url}/events",
                params=scoped_params,
                timeout=self.timeout,
            )
            if scoped_response.status_code >= 400:
                self.last_errors[sport] = self._sanitize(
                    f"Odds-API.io events request failed: {self._response_detail(scoped_response)}"
                )
                if scoped_response.status_code == 429:
                    self.last_errors[sport] = self._sanitize(
                        "Odds-API.io events request hit rate limits."
                    )
                return []
            self._quota(scoped_response.headers)
            scoped_payload = scoped_response.json()
            if not isinstance(scoped_payload, list):
                self.last_errors[sport] = self._sanitize(
                    "Odds-API.io /events response must be a list."
                )
                return []
            payload = scoped_payload
            cache_key = scoped_cache_key

        event_ids: list[str] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            event_id = str(
                item.get("id") or item.get("eventId") or item.get("eventID") or ""
            ).strip()
            if not event_id:
                self.last_stats["missing_event_id"] += 1
                continue
            if event_id not in event_ids:
                event_ids.append(event_id)

        self._events_cache[cache_key] = self._wrap_cache(event_ids)
        return event_ids

    def _discover_league_slug(
        self,
        events: list[dict[str, Any]],
        league_hint: str,
    ) -> str:
        candidates: dict[str, int] = {}
        for item in events:
            if not isinstance(item, dict):
                continue
            league = item.get("league")
            league_slug = ""
            league_name = ""
            if isinstance(league, dict):
                league_slug = str(league.get("slug") or "").strip()
                league_name = str(league.get("name") or "").strip()
            elif isinstance(league, str):
                league_slug = league.strip()
            text = self._normalize_text(" ".join((league_slug, league_name)))
            if league_hint and league_hint not in text:
                continue
            if league_slug:
                candidates[league_slug] = candidates.get(league_slug, 0) + 1
        if not candidates:
            return ""
        return sorted(candidates.items(), key=lambda item: (-item[1], len(item[0])))[0][0]

    def _fetch_event_odds(self, event_id: str) -> list[dict[str, Any]]:
        cache_key = (event_id, ",".join(self._bookmaker_names))
        cached = self._read_cache(self._odds_cache.get(cache_key))
        if cached is not None:
            return cached

        response = self.session.get(
            f"{self.base_url}/odds",
            params={
                "apiKey": self.api_key,
                "eventId": event_id,
                "bookmakers": ",".join(self._bookmaker_names),
            },
            timeout=self.timeout,
        )
        self.last_stats["odds_requests_made"] += 1
        if response.status_code >= 400:
            detail = self._response_detail(response)
            if response.status_code == 429:
                detail = "Rate limit exceeded"
            self.last_errors[event_id] = self._sanitize(
                f"Odds-API.io odds request failed: {detail}"
            )
            return []
        self._quota(response.headers)
        payload = response.json()
        if isinstance(payload, dict):
            self._odds_cache[cache_key] = self._wrap_cache(payload)
            return [payload]
        if not isinstance(payload, list):
            self.last_errors[event_id] = self._sanitize(
                "Odds-API.io /odds response must be a list or object."
            )
            return []
        self._odds_cache[cache_key] = self._wrap_cache(payload)
        return payload

    def _discover_bookmakers(self) -> tuple[str, ...]:
        response = self.session.get(
            f"{self.base_url}/bookmakers/selected",
            params={"apiKey": self.api_key},
            timeout=self.timeout,
        )
        if response.status_code < 400:
            self._quota(response.headers)
            payload = response.json()
            if isinstance(payload, dict):
                selected = payload.get("bookmakers")
                if isinstance(selected, list):
                    active_names: list[str] = []
                    for name in selected:
                        normalized = self._normalize_text(name)
                        name_text = str(name).strip()
                        if (
                            normalized in self.supported_books
                            and name_text not in active_names
                        ):
                            active_names.append(name_text)
                    if active_names:
                        return tuple(active_names[:2])

        response = self.session.get(
            f"{self.base_url}/bookmakers",
            params={"apiKey": self.api_key},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise ValueError(f"/bookmakers failed: {self._response_detail(response)}")
        self._quota(response.headers)
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Odds-API.io /bookmakers response must be a list")
        active_names: list[str] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            if not bool(item.get("active")):
                continue
            name = str(item.get("name") or item.get("key") or "").strip()
            if not name:
                continue
            normalized = self._normalize_text(name)
            if normalized in self.supported_books and name not in active_names:
                active_names.append(name)
        if not active_names:
            raise ValueError("Odds-API.io /bookmakers returned no active supported books")
        return tuple(active_names[:2])

    def _normalize_event_odds(
        self,
        *,
        sport: str,
        event_id: str,
        payload: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        league = self.league_names.get(sport)
        if league is None:
            raise ValueError(f"unsupported odds sport: {sport}")
        now = self.clock().astimezone(timezone.utc).isoformat()
        rows: list[dict[str, Any]] = []
        for event in payload:
            if not isinstance(event, dict):
                continue
            source_event_id = str(
                event.get("id")
                or event.get("eventId")
                or event.get("eventID")
                or event_id
            )
            start = str(
                event.get("commence_time")
                or event.get("start")
                or event.get("start_time")
                or event.get("date")
                or ""
            )
            home = str(event.get("home_team") or event.get("home") or "")
            away = str(event.get("away_team") or event.get("away") or "")
            if not all((source_event_id, start, home, away)):
                continue

            bookmakers = event.get("bookmakers")
            if isinstance(bookmakers, dict):
                for book_name, markets in bookmakers.items():
                    book_key = self._normalize_text(book_name).replace(" ", "")
                    if book_key not in self.supported_books:
                        self.last_stats["unsupported_bookmaker_rows"] += 1
                        continue
                    if not isinstance(markets, list):
                        continue
                    for market in markets:
                        if not isinstance(market, dict):
                            continue
                        market_name = str(market.get("name") or market.get("key") or "").strip()
                        if market_name and market_name not in {"ML", "Moneyline", "h2h"}:
                            continue
                        observed = str(
                            market.get("updatedAt") or market.get("last_update") or now
                        )
                        outcomes = market.get("odds") or market.get("outcomes") or []
                        for outcome in outcomes:
                            if not isinstance(outcome, dict):
                                continue

                            compact_prices = (
                                ("home", home, outcome.get("home")),
                                ("away", away, outcome.get("away")),
                            )
                            if any(price is not None for _, _, price in compact_prices):
                                for selection, team, price in compact_prices:
                                    if price is None:
                                        continue
                                    american_price = self._coerce_american_price(price)
                                    if american_price is None:
                                        continue
                                    rows.append(
                                        {
                                            "provider_event_id": source_event_id,
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
                                            "sportsbook": str(book_name),
                                            "market": "h2h",
                                            "period": "full_game",
                                            "selection": selection,
                                            "selection_team": team,
                                            "line": None,
                                            "american_price": american_price,
                                            "observed_at": observed,
                                            "source": self.name,
                                            "source_url": f"{self.base_url}/odds?eventId={source_event_id}",
                                            "data_mode": "live",
                                            "is_live": False,
                                        }
                                    )
                                continue

                            team = str(outcome.get("name") or outcome.get("team") or "")
                            selection = (
                                "home" if team == home else "away" if team == away else ""
                            )
                            price = outcome.get("price")
                            if price is None:
                                price = outcome.get("odds")
                            if not selection or price is None:
                                continue
                            american_price = self._coerce_american_price(price)
                            if american_price is None:
                                continue
                            rows.append(
                                {
                                    "provider_event_id": source_event_id,
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
                                    "sportsbook": str(book_name),
                                    "market": "h2h",
                                    "period": "full_game",
                                    "selection": selection,
                                    "selection_team": team,
                                    "line": None,
                                    "american_price": american_price,
                                    "observed_at": observed,
                                    "source": self.name,
                                    "source_url": f"{self.base_url}/odds?eventId={source_event_id}",
                                    "data_mode": "live",
                                    "is_live": False,
                                }
                            )
                continue

            for book in event.get("bookmakers") or []:
                if not isinstance(book, dict):
                    continue
                book_key = str(
                    book.get("key") or book.get("id") or book.get("name") or ""
                ).lower()
                if book_key not in self.supported_books:
                    self.last_stats["unsupported_bookmaker_rows"] += 1
                    continue
                for market in book.get("markets") or []:
                    if not isinstance(market, dict):
                        continue
                    market_key = str(market.get("key") or market.get("market") or "")
                    if market_key and market_key != "h2h":
                        continue
                    observed = str(market.get("last_update") or book.get("last_update") or now)
                    for outcome in market.get("outcomes") or []:
                        team = str(outcome.get("name") or outcome.get("team") or "")
                        selection = (
                            "home" if team == home else "away" if team == away else ""
                        )
                        price = outcome.get("price")
                        if price is None:
                            price = outcome.get("odds")
                        if not selection or price is None:
                            continue
                        american_price = self._coerce_american_price(price)
                        if american_price is None:
                            continue
                        rows.append(
                            {
                                "provider_event_id": source_event_id,
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
                                "sportsbook": str(book.get("title") or book_key),
                                "market": "h2h",
                                "period": "full_game",
                                "selection": selection,
                                "selection_team": team,
                                "line": None,
                                "american_price": american_price,
                                "observed_at": observed,
                                "source": self.name,
                                "source_url": f"{self.base_url}/odds?eventId={source_event_id}",
                                "data_mode": "live",
                                "is_live": False,
                            }
                        )
        return rows

    def _response_detail(self, response: Any) -> str:
        detail = "request failed"
        try:
            payload = response.json()
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            detail = str(payload.get("message") or payload.get("error") or detail)
        return self._sanitize(detail)

    def _sanitize(self, value: str) -> str:
        text = str(value)
        if self.api_key:
            return text.replace(self.api_key, "[REDACTED]")
        return text

    def _wrap_cache(self, payload: Any) -> dict[str, Any]:
        return {
            "stored_at": self.clock().astimezone(timezone.utc),
            "payload": payload,
        }

    def _read_cache(self, wrapped: dict[str, Any] | None) -> Any | None:
        if not wrapped:
            return None
        stored_at = wrapped.get("stored_at")
        if not isinstance(stored_at, datetime):
            return None
        age = (self.clock().astimezone(timezone.utc) - stored_at).total_seconds()
        if age > self.cache_ttl_seconds:
            return None
        return wrapped.get("payload")

    @staticmethod
    def _pick(item: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = item.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    @staticmethod
    def _normalize_text(value: str) -> str:
        return " ".join(str(value).strip().lower().replace("_", " ").split())

    @staticmethod
    def _coerce_american_price(value: Any) -> int | None:
        text = str(value).strip()
        if not text:
            return None
        try:
            if "." in text:
                decimal = float(text)
                if decimal <= 1:
                    return None
                if decimal >= 2:
                    return int(round((decimal - 1) * 100))
                return int(round(-100 / (decimal - 1)))
            return int(text.replace("+", ""))
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    def _quota(self, headers: Any) -> None:
        for label, header in (
            ("remaining", "x-requests-remaining"),
            ("used", "x-requests-used"),
            ("last", "x-requests-last"),
        ):
            value = headers.get(header)
            self.last_quota[label] = int(value) if value is not None else None
