from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sports.personal.models import (
    CanonicalEvent,
    CompleteBookMarket,
    MoneylineNormalizationResult,
    NormalizationRejection,
    NormalizedMoneylineQuote,
)


class MoneylineNormalizer:
    LEAGUES = {
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
        "new york yankees": "new york yankees",
        "la dodgers": "los angeles dodgers",
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
            book = self.BOOKS.get(self._key(book_label))
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
                start = self._time(row["event_start"])
                observed = self._time(row["observed_at"])
            except (KeyError, TypeError, ValueError) as error:
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
                        "FUTURE_OBSERVATION", "Observation timestamp is in the future", book
                    )
                )
                continue
            if bool(row.get("is_live")):
                rejections.append(
                    NormalizationRejection(
                        "LIVE_QUOTE_UNSUPPORTED", "Only pregame quotes are supported", book
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
            canonical_id = self._event_id(league, start, away_name, home_name)
            quote = NormalizedMoneylineQuote(
                canonical_event_id=canonical_id,
                provider_event_id=str(row.get("provider_event_id") or ""),
                league=league,
                season=str(row.get("season") or start.year),
                event_start=start.isoformat(),
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
                source=str(row.get("source") or ""),
                source_url=str(row.get("source_url") or ""),
                data_mode=str(row.get("data_mode") or "live"),
            )
            quotes.append(quote)
            event = event_parts.setdefault(
                canonical_id,
                {
                    "league": league,
                    "season": quote.season,
                    "start": start.isoformat(),
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
            latest[
                (quote.canonical_event_id, quote.sportsbook, quote.selection)
            ] = quote
        by_book: dict[
            tuple[str, str], dict[str, NormalizedMoneylineQuote]
        ] = defaultdict(dict)
        for quote in latest.values():
            by_book[(quote.canonical_event_id, quote.sportsbook)][
                quote.selection
            ] = quote
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
            quotes=tuple(latest.values()),
            complete_books=tuple(complete),
            rejections=tuple(rejections),
        )

    @staticmethod
    def _key(value: str) -> str:
        return " ".join(value.strip().lower().replace("_", " ").split())

    def _team(self, value: str) -> str:
        key = self._key(value)
        return self.TEAM_ALIASES.get(key, key)

    @staticmethod
    def _team_id(league: str, name: str) -> str:
        return f"{league.lower()}:{'-'.join(name.split())}"

    @staticmethod
    def _event_id(
        league: str, start: datetime, away: str, home: str
    ) -> str:
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
