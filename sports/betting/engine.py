from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from sports.betting.models import (
    BettingPolicy,
    BookPrice,
    Decision,
    MarketAssessment,
    ModelPrediction,
    OddsQuote,
)
from sports.betting.no_vig import remove_vig


class BettingIntelligenceEngine:
    """Compare a calibrated SIP probability with fresh, no-vig market prices."""

    def __init__(self, policy: BettingPolicy | None = None) -> None:
        self.policy = policy or BettingPolicy()

    def assess(
        self,
        prediction: ModelPrediction,
        quotes: list[OddsQuote],
        *,
        as_of: datetime | None = None,
    ) -> MarketAssessment:
        now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
        matching = [
            quote
            for quote in quotes
            if quote.event_id == prediction.event_id
            and quote.market == prediction.market
            and quote.line == prediction.line
            and quote.status == "active"
            and (
                not prediction.canonical_player_id
                or quote.canonical_player_id == prediction.canonical_player_id
            )
            and (
                not prediction.outcome_definition
                or quote.outcome_definition == prediction.outcome_definition
            )
            and quote.fetched_at <= now
            and quote.event_start > now
        ]
        target_quotes = [q for q in matching if q.selection == prediction.selection]
        by_book: dict[str, list[OddsQuote]] = defaultdict(list)
        for quote in matching:
            by_book[quote.sportsbook].append(quote)

        fair_by_book: dict[str, float] = {}
        for sportsbook, book_quotes in by_book.items():
            latest_by_selection: dict[str, OddsQuote] = {}
            for quote in sorted(book_quotes, key=lambda item: item.fetched_at):
                latest_by_selection[quote.selection] = quote
            if prediction.selection not in latest_by_selection or len(latest_by_selection) < 2:
                continue
            ordered = list(latest_by_selection.values())
            fair = remove_vig(
                [quote.implied_probability for quote in ordered],
                self.policy.no_vig_method,
            )
            fair_by_book[sportsbook] = fair[
                next(i for i, quote in enumerate(ordered) if quote.selection == prediction.selection)
            ]

        prices: list[BookPrice] = []
        latest_targets: dict[str, OddsQuote] = {}
        for quote in sorted(target_quotes, key=lambda item: item.fetched_at):
            latest_targets[quote.sportsbook] = quote
        for sportsbook, quote in latest_targets.items():
            age = max(0.0, (now - quote.fetched_at).total_seconds())
            prices.append(
                BookPrice(
                    sportsbook=sportsbook,
                    american_price=quote.american_price,
                    decimal_price=quote.decimal_price,
                    fair_probability=fair_by_book.get(sportsbook, quote.implied_probability),
                    age_seconds=age,
                    stale=age > self.policy.maximum_quote_age_seconds,
                )
            )
        prices.sort(key=lambda item: item.decimal_price, reverse=True)

        fresh_books = {price.sportsbook for price in prices if not price.stale}
        fresh_fair = [prob for book, prob in fair_by_book.items() if book in fresh_books]
        fresh_quotes = [quote for book, quote in latest_targets.items() if book in fresh_books]
        best = max(fresh_quotes, key=lambda quote: quote.decimal_price, default=None)
        consensus = sum(fresh_fair) / len(fresh_fair) if fresh_fair else None
        edge = prediction.probability - consensus if consensus is not None else None
        expected_return = (
            prediction.probability * best.decimal_price - 1.0 if best is not None else None
        )
        confidence_adjusted_return = (
            max(0.0, prediction.probability - prediction.uncertainty)
            * best.decimal_price
            - 1.0
            if best is not None
            else None
        )
        agreement = sum(
            component > (consensus if consensus is not None else 0.5)
            for component in prediction.model_probabilities
        ) / len(prediction.model_probabilities)
        interval = (
            max(0.0, prediction.probability - prediction.uncertainty),
            min(1.0, prediction.probability + prediction.uncertainty),
        )

        rejected: list[str] = []
        if len(fresh_fair) < self.policy.minimum_books:
            rejected.append("insufficient fresh books with complete markets")
        if edge is None or edge < self.policy.minimum_edge:
            rejected.append("probability edge below policy threshold")
        if expected_return is None or expected_return < self.policy.minimum_expected_return:
            rejected.append("expected return below policy threshold")
        if (
            confidence_adjusted_return is None
            or confidence_adjusted_return
            < self.policy.minimum_confidence_adjusted_return
        ):
            rejected.append("confidence-adjusted return below policy threshold")
        if prediction.uncertainty > self.policy.maximum_uncertainty:
            rejected.append("prediction uncertainty too high")
        if agreement < self.policy.minimum_model_agreement:
            rejected.append("component models do not agree")
        if prediction.similar_bet_sample < self.policy.minimum_similar_sample:
            rejected.append("similar-bet validation sample too small")
        if (
            prediction.similar_bet_brier_score is None
            or prediction.similar_bet_brier_score > self.policy.maximum_similar_brier_score
        ):
            rejected.append("similar-bet calibration is inadequate or unknown")

        warnings = []
        stale = [price.sportsbook for price in prices if price.stale]
        if stale:
            warnings.append(f"stale quotes excluded: {', '.join(sorted(stale))}")
        if prediction.invalidators:
            warnings.append("reassess if: " + "; ".join(prediction.invalidators))

        return MarketAssessment(
            decision=Decision.NO_BET if rejected else Decision.QUALIFIED,
            prediction=prediction,
            best_quote=best,
            book_prices=tuple(prices),
            consensus_probability=consensus,
            probability_edge=edge,
            expected_return=expected_return,
            confidence_adjusted_return=confidence_adjusted_return,
            confidence_interval=interval,
            model_agreement=agreement,
            rejection_reasons=tuple(rejected),
            warnings=tuple(warnings),
        )
