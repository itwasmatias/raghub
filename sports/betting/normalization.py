from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sports.betting.models import OddsQuote


@dataclass(frozen=True, slots=True)
class MarketNormalizationResult:
    rankable_quotes: list[OddsQuote]
    exclusions: list[str]
    normalized_count: int
    complete_book_count: int


class MarketNormalizer:
    MARKET_ALIASES = {
        "player points": "player_points",
        "player_points": "player_points",
        "points": "player_points",
    }
    OUTCOME_ALIASES = {"over": "over", "under": "under"}

    def __init__(
        self,
        *,
        team_aliases: dict[str, str],
        player_aliases: dict[str, str],
        sportsbook_aliases: dict[str, str],
        maximum_age_seconds: int = 180,
    ) -> None:
        self.team_aliases = self._normalized_mapping(team_aliases)
        self.player_aliases = self._normalized_mapping(player_aliases)
        self.sportsbook_aliases = self._normalized_mapping(sportsbook_aliases)
        self.maximum_age_seconds = maximum_age_seconds

    @classmethod
    def release_candidate_defaults(
        cls, *, players: dict[str, str]
    ) -> MarketNormalizer:
        return cls(
            team_aliases={
                "new york knicks": "nba:team:nyk",
                "ny knicks": "nba:team:nyk",
                "nyk": "nba:team:nyk",
                "chicago bulls": "nba:team:chi",
                "chi": "nba:team:chi",
            },
            player_aliases=players,
            sportsbook_aliases={
                "draftkings": "draftkings",
                "fanduel": "fanduel",
                "betmgm": "betmgm",
            },
        )

    def normalize(
        self,
        rows: list[dict[str, Any]],
        *,
        as_of: datetime | None = None,
    ) -> MarketNormalizationResult:
        current = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
        exclusions: list[str] = []
        candidates: list[OddsQuote] = []
        for row in rows:
            book_label = str(row.get("sportsbook") or "Unknown sportsbook")
            book = self.sportsbook_aliases.get(self._key(book_label))
            if book is None:
                exclusions.append(
                    f"{book_label}: sportsbook identity could not be resolved"
                )
                continue
            player_label = str(row.get("player") or "")
            player_id = self.player_aliases.get(self._key(player_label))
            if player_id is None:
                exclusions.append(
                    f"{book_label}: player identity could not be resolved for {player_label or 'missing player'}"
                )
                continue
            home = self.team_aliases.get(self._key(str(row.get("home_team") or "")))
            away = self.team_aliases.get(self._key(str(row.get("away_team") or "")))
            if home is None or away is None:
                exclusions.append(
                    f"{book_label}: team identity could not be resolved"
                )
                continue
            market = self.MARKET_ALIASES.get(
                self._key(str(row.get("market") or ""))
            )
            outcome = self.OUTCOME_ALIASES.get(
                self._key(str(row.get("outcome") or ""))
            )
            if market is None or outcome is None:
                exclusions.append(
                    f"{book_label}: market or outcome definition could not be resolved"
                )
                continue
            status = self._key(str(row.get("status") or "active"))
            if status != "active":
                exclusions.append(f"{book_label} quote is {status}")
                continue
            try:
                event_start = self._timestamp(row["event_start"])
                fetched_at = self._timestamp(row["fetched_at"])
                price = int(row["american_price"])
                line = float(row["line"])
            except (KeyError, TypeError, ValueError) as error:
                exclusions.append(f"{book_label}: malformed quote ({error})")
                continue
            age = (current - fetched_at).total_seconds()
            if age > self.maximum_age_seconds:
                exclusions.append(
                    f"{book_label} quote is stale ({int(age)} seconds old)"
                )
                continue
            event_id = self._event_id(event_start, away, home)
            try:
                candidates.append(
                    OddsQuote(
                        event_id=event_id,
                        market=market,
                        selection=outcome,
                        sportsbook=book,
                        line=line,
                        american_price=price,
                        event_start=event_start,
                        fetched_at=fetched_at,
                        source=str(row.get("source") or book_label),
                        source_url=str(row.get("source_url") or ""),
                        designation=str(row.get("designation") or "current"),
                        canonical_player_id=player_id,
                        outcome_definition=str(
                            row.get("outcome_definition") or "over_under"
                        ),
                        status=status,
                        liquidity=(
                            float(row["liquidity"])
                            if row.get("liquidity") is not None
                            else None
                        ),
                    )
                )
            except ValueError as error:
                exclusions.append(f"{book_label}: malformed quote ({error})")

        latest: dict[tuple[str, str, str, str, float | None, str], OddsQuote] = {}
        for quote in sorted(candidates, key=lambda item: item.fetched_at):
            key = (
                quote.event_id,
                quote.canonical_player_id,
                quote.market,
                quote.sportsbook,
                quote.line,
                quote.selection,
            )
            latest[key] = quote
        grouped: dict[
            tuple[str, str, str, str, float | None, str], list[OddsQuote]
        ] = {}
        for quote in latest.values():
            key = (
                quote.event_id,
                quote.canonical_player_id,
                quote.market,
                quote.sportsbook,
                quote.line,
                quote.outcome_definition,
            )
            grouped.setdefault(key, []).append(quote)

        rankable: list[OddsQuote] = []
        complete_books = 0
        for key, quotes in grouped.items():
            selections = {quote.selection for quote in quotes}
            if {"over", "under"} <= selections:
                rankable.extend(quotes)
                complete_books += 1
            else:
                missing = sorted({"over", "under"} - selections)
                exclusions.append(
                    f"{quotes[0].sportsbook} market missing the opposing outcome: {', '.join(missing)}"
                )
        if complete_books < 2:
            exclusions.append(
                f"Only {complete_books} complete sportsbook quote set(s); at least two are required"
            )
            rankable = []
        return MarketNormalizationResult(
            rankable_quotes=rankable,
            exclusions=exclusions,
            normalized_count=len(candidates),
            complete_book_count=complete_books,
        )

    @staticmethod
    def _normalized_mapping(values: dict[str, str]) -> dict[str, str]:
        return {MarketNormalizer._key(key): value for key, value in values.items()}

    @staticmethod
    def _key(value: str) -> str:
        return " ".join(value.strip().lower().replace("_", " ").split())

    @staticmethod
    def _timestamp(value: Any) -> datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _event_id(event_start: datetime, away: str, home: str) -> str:
        away_code = away.rsplit(":", maxsplit=1)[-1]
        home_code = home.rsplit(":", maxsplit=1)[-1]
        return (
            f"nba:game:{event_start.strftime('%Y%m%d')}:{away_code}:{home_code}"
        )
