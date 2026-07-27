from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from sports.personal.models import (
    CanonicalEvent,
    CompleteBookMarket,
    MoneylineNormalizationResult,
    NormalizationRejection,
    NormalizedMoneylineQuote,
)


def canonical_game_identity_v1(
    *,
    league: str,
    start: datetime | None,
    away_team_name: str,
    home_team_name: str,
    official_game_number: int | None = None,
) -> str:
    away = "-".join(away_team_name.split())
    home = "-".join(home_team_name.split())
    league_key = league.lower()

    if league_key != "mlb":
        if start is None or start.tzinfo is None:
            raise ValueError("timezone-aware scheduled start is required")
        start_utc = start.astimezone(timezone.utc)
        return f"{league_key}:{start_utc.strftime('%Y%m%d')}:{away}:{home}"

    prefix = f"{league_key}:game:v1:away:{away}:home:{home}:instance:"

    if official_game_number is not None:
        if official_game_number < 1:
            raise ValueError("official game number must be a positive integer")
        return f"{prefix}official_game_number:{official_game_number}"

    if start is None or start.tzinfo is None:
        raise ValueError(
            "official game number or timezone-aware scheduled start is required"
        )
    start_utc = start.astimezone(timezone.utc)
    return f"{prefix}start:{start_utc.strftime('%Y%m%dT%H%M%SZ')}"


def canonical_moneyline_market_id_v1(canonical_game_id: str) -> str:
    return f"{canonical_game_id}:market:moneyline:full_game:v1"


def canonical_outcome_id_v1(canonical_market_id: str, selection: str) -> str:
    if selection not in {"home", "away"}:
        raise ValueError("selection must be home or away")
    return f"{canonical_market_id}:outcome:{selection}:v1"


def canonical_sportsbook_id_v1(label: str) -> str | None:
    return MoneylineNormalizer.BOOKS.get(MoneylineNormalizer._key(label))


def provider_quote_identity_v1(
    *,
    provider: str,
    provider_event_id: str,
    source_url: str,
    observed_at: str,
    canonical_sportsbook_id: str,
    canonical_outcome_id: str,
    american_price: int,
) -> str:
    payload = "|".join(
        [
            provider.strip().lower(),
            provider_event_id.strip().lower(),
            source_url.strip(),
            observed_at.strip(),
            canonical_sportsbook_id,
            canonical_outcome_id,
            str(american_price),
        ]
    )
    return f"quote:v1:{sha256(payload.encode('utf-8')).hexdigest()[:20]}"


class MoneylineNormalizer:
    LEAGUES = {
        "nba": "NBA",
        "basketball_nba": "NBA",
        "wnba": "WNBA",
        "basketball_wnba": "WNBA",
        "mlb": "MLB",
        "baseball_mlb": "MLB",
    }
    BOOKS = {
        "draftkings": "draftkings",
        "draft kings": "draftkings",
        "fanduel": "fanduel",
        "fan duel": "fanduel",
        "betmgm": "betmgm",
        "bet mgm": "betmgm",
    }
    TEAM_ALIASES = {
        "ny liberty": "new york liberty",
        "new york liberty": "new york liberty",
        "chicago sky": "chicago sky",
        "la sparks": "los angeles sparks",
        "los angeles sparks": "los angeles sparks",
        "ny yankees": "new york yankees",
        "n y yankees": "new york yankees",
        "new york yankees": "new york yankees",
        "new york yanks": "new york yankees",
        "la dodgers": "los angeles dodgers",
        "l a dodgers": "los angeles dodgers",
        "los angeles dodgers": "los angeles dodgers",
    }

    def __init__(self, *, maximum_age_seconds: int = 600) -> None:
        self.maximum_age_seconds = maximum_age_seconds

    def normalize(
        self,
        rows: list[dict[str, Any]],
        *,
        as_of: datetime | None = None,
    ) -> MoneylineNormalizationResult:
        now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
        quotes: list[NormalizedMoneylineQuote] = []
        rejections: list[NormalizationRejection] = []
        event_parts: dict[str, dict[str, Any]] = {}
        for row in rows:
            book_label = str(row.get("sportsbook") or "")
            book = canonical_sportsbook_id_v1(book_label)
            if book is None:
                rejections.append(
                    NormalizationRejection(
                        "UNKNOWN_SPORTSBOOK",
                        f"Sportsbook identity is not supported: {book_label or 'missing'}",
                        book_label or None,
                    )
                )
                continue
            try:
                league = self.LEAGUES[self._key(str(row["league"]))]
                observed = self._time(row["observed_at"])
            except (KeyError, TypeError, ValueError) as error:
                rejections.append(
                    NormalizationRejection(
                        "EVENT_NORMALIZATION_FAILED", str(error), book
                    )
                )
                continue
            try:
                start_value = row.get("event_start")
                start = self._time(start_value) if start_value is not None else None
                official_game_number_value = row.get("official_game_number")
                official_game_number = (
                    int(official_game_number_value)
                    if official_game_number_value is not None
                    else None
                )
            except (TypeError, ValueError) as error:
                rejections.append(
                    NormalizationRejection(
                        "EVENT_NORMALIZATION_FAILED", str(error), book
                    )
                )
                continue
            if now.timestamp() - observed.timestamp() > self.maximum_age_seconds:
                rejections.append(
                    NormalizationRejection(
                        "STALE_QUOTE",
                        f"Quote is older than {self.maximum_age_seconds} seconds",
                        book,
                    )
                )
                continue
            if observed > now:
                rejections.append(
                    NormalizationRejection(
                        "FUTURE_OBSERVATION",
                        "Observation timestamp is in the future",
                        book,
                    )
                )
                continue
            if bool(row.get("is_live")):
                rejections.append(
                    NormalizationRejection(
                        "LIVE_QUOTE_UNSUPPORTED",
                        "Only pregame quotes are supported",
                        book,
                    )
                )
                continue
            market = self._key(str(row.get("market") or ""))
            if market not in {"h2h", "moneyline"}:
                rejections.append(
                    NormalizationRejection(
                        "UNSUPPORTED_MARKET", f"Unsupported market: {market}", book
                    )
                )
                continue
            period = self._key(str(row.get("period") or "full_game"))
            if period not in {"full game", "full_game"}:
                rejections.append(
                    NormalizationRejection(
                        "UNSUPPORTED_PERIOD", f"Unsupported period: {period}", book
                    )
                )
                continue
            try:
                price = int(row["american_price"])
                if price == 0 or -100 < price < 100:
                    raise ValueError("American price must be <= -100 or >= +100")
            except (KeyError, TypeError, ValueError) as error:
                rejections.append(
                    NormalizationRejection("INVALID_PRICE", str(error), book)
                )
                continue
            home_name = self._team(str(row.get("home_team") or ""))
            away_name = self._team(str(row.get("away_team") or ""))
            if not home_name or not away_name or home_name == away_name:
                rejections.append(
                    NormalizationRejection(
                        "EVENT_NORMALIZATION_FAILED",
                        "Home and away teams must be known and distinct",
                        book,
                    )
                )
                continue
            selection = self._key(str(row.get("selection") or ""))
            if selection not in {"home", "away"}:
                rejections.append(
                    NormalizationRejection(
                        "INVALID_SELECTION", "Selection must be home or away", book
                    )
                )
                continue
            selected_team = self._team(str(row.get("selection_team") or ""))
            expected_team = home_name if selection == "home" else away_name
            if selected_team != expected_team:
                rejections.append(
                    NormalizationRejection(
                        "SELECTION_TEAM_MISMATCH",
                        f"{selection} selection does not match {expected_team}",
                        book,
                    )
                )
                continue
            try:
                canonical_id = canonical_game_identity_v1(
                    league=league,
                    start=start,
                    away_team_name=away_name,
                    home_team_name=home_name,
                    official_game_number=official_game_number,
                )
            except ValueError as error:
                rejections.append(
                    NormalizationRejection(
                        "EVENT_NORMALIZATION_FAILED", str(error), book
                    )
                )
                continue
            market_id = canonical_moneyline_market_id_v1(canonical_id)
            outcome_id = canonical_outcome_id_v1(market_id, selection)
            source = str(row.get("source") or "")
            source_url = str(row.get("source_url") or "")
            provider_event_id = str(row.get("provider_event_id") or "")
            quote = NormalizedMoneylineQuote(
                canonical_event_id=canonical_id,
                provider_event_id=provider_event_id,
                league=league,
                season=str(row.get("season") or (start.year if start else "")),
                event_start=start.isoformat() if start else "",
                home_team_id=self._team_id(league, home_name),
                away_team_id=self._team_id(league, away_name),
                sportsbook=book,
                market="moneyline",
                period="full_game",
                selection=selection,
                selection_team_id=self._team_id(league, selected_team),
                line=None,
                american_price=price,
                observed_at=observed.isoformat(),
                source=source,
                source_url=source_url,
                data_mode=str(row.get("data_mode") or "live"),
                canonical_sportsbook_id=book,
                canonical_market_id=market_id,
                canonical_outcome_id=outcome_id,
                provider_quote_id=provider_quote_identity_v1(
                    provider=source,
                    provider_event_id=provider_event_id,
                    source_url=source_url,
                    observed_at=observed.isoformat(),
                    canonical_sportsbook_id=book,
                    canonical_outcome_id=outcome_id,
                    american_price=price,
                ),
            )
            quotes.append(quote)
            event = event_parts.setdefault(
                canonical_id,
                {
                    "league": league,
                    "season": quote.season,
                    "start": start.isoformat() if start else "",
                    "home_name": home_name.title(),
                    "away_name": away_name.title(),
                    "home_id": quote.home_team_id,
                    "away_id": quote.away_team_id,
                    "venue": row.get("venue"),
                    "provider_ids": set(),
                    "urls": set(),
                },
            )
            if quote.provider_event_id:
                event["provider_ids"].add(quote.provider_event_id)
            if quote.source_url:
                event["urls"].add(quote.source_url)

        latest: dict[tuple[str, str, str], NormalizedMoneylineQuote] = {}
        for quote in sorted(quotes, key=lambda item: item.observed_at):
            latest[(quote.canonical_event_id, quote.sportsbook, quote.selection)] = (
                quote
            )
        by_book: dict[tuple[str, str], dict[str, NormalizedMoneylineQuote]] = (
            defaultdict(dict)
        )
        for quote in latest.values():
            by_book[(quote.canonical_event_id, quote.sportsbook)][quote.selection] = (
                quote
            )
        complete = []
        for (event_id, book), selections in by_book.items():
            if {"home", "away"} <= set(selections):
                home, away = selections["home"], selections["away"]
                if home.observed_at != away.observed_at:
                    rejections.append(
                        NormalizationRejection(
                            "MARKET_PAIRING_FAILED",
                            "Home and away observations are not from the same snapshot",
                            book,
                        )
                    )
                    continue
                complete.append(
                    CompleteBookMarket(
                        event_id,
                        book,
                        "moneyline",
                        "full_game",
                        home.observed_at,
                        home,
                        away,
                    )
                )
            else:
                rejections.append(
                    NormalizationRejection(
                        "NO_COMPLETE_BOOK",
                        f"{book} is missing home or away moneyline",
                        book,
                    )
                )
        events = tuple(
            CanonicalEvent(
                canonical_id=event_id,
                league=str(item["league"]),
                season=str(item["season"]),
                start_time=str(item["start"]),
                home_team_id=str(item["home_id"]),
                home_team_name=str(item["home_name"]),
                away_team_id=str(item["away_id"]),
                away_team_name=str(item["away_name"]),
                venue=str(item["venue"]) if item["venue"] else None,
                status="pregame",
                provider_event_ids=tuple(sorted(item["provider_ids"])),
                source_urls=tuple(sorted(item["urls"])),
            )
            for event_id, item in sorted(event_parts.items())
        )
        return MoneylineNormalizationResult(
            events=events,
            quotes=tuple(quotes),
            complete_books=tuple(complete),
            rejections=tuple(rejections),
        )

    @staticmethod
    def _key(value: str) -> str:
        return " ".join(value.strip().lower().replace("_", " ").split())

    def _team(self, value: str) -> str:
        key = self._team_key(value)
        return self.TEAM_ALIASES.get(key, key)

    @staticmethod
    def _team_key(value: str) -> str:
        lowered = value.strip().lower().replace("_", " ")
        for char in (".", "'", "-"):
            lowered = lowered.replace(char, " ")
        return " ".join(lowered.split())

    @staticmethod
    def _team_id(league: str, name: str) -> str:
        return f"{league.lower()}:{'-'.join(name.split())}"

    @staticmethod
    def _event_id(league: str, start: datetime, away: str, home: str) -> str:
        return (
            f"{league.lower()}:{start.strftime('%Y%m%d')}:"
            f"{'-'.join(away.split())}:{'-'.join(home.split())}"
        )

    @staticmethod
    def _time(value: Any) -> datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return parsed.astimezone(timezone.utc)
