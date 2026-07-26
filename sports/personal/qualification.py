from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from sports.personal.models import (
    CanonicalEvent,
    CompleteBookMarket,
    FeedHealth,
    MoneylineForecast,
    QualificationResult,
)


class QualificationReason(StrEnum):
    ODDS_FEED_NOT_CONFIGURED = "ODDS_FEED_NOT_CONFIGURED"
    ODDS_FEED_UNAVAILABLE = "ODDS_FEED_UNAVAILABLE"
    ODDS_DATA_STALE = "ODDS_DATA_STALE"
    EVENT_NORMALIZATION_FAILED = "EVENT_NORMALIZATION_FAILED"
    EVENT_ALREADY_STARTED = "EVENT_ALREADY_STARTED"
    NO_COMPLETE_BOOKS = "NO_COMPLETE_BOOKS"
    INSUFFICIENT_COMPLETE_BOOKS = "INSUFFICIENT_COMPLETE_BOOKS"
    MARKET_PAIRING_FAILED = "MARKET_PAIRING_FAILED"
    FORECAST_MISSING = "FORECAST_MISSING"
    MODEL_NOT_TRAINED = "MODEL_NOT_TRAINED"
    FORECAST_NOT_CALIBRATED = "FORECAST_NOT_CALIBRATED"
    FORECAST_STALE = "FORECAST_STALE"
    FORECAST_MARKET_MISMATCH = "FORECAST_MARKET_MISMATCH"
    INSUFFICIENT_FEATURE_DATA = "INSUFFICIENT_FEATURE_DATA"
    EDGE_BELOW_THRESHOLD = "EDGE_BELOW_THRESHOLD"
    DATA_QUALITY_BELOW_THRESHOLD = "DATA_QUALITY_BELOW_THRESHOLD"


class MoneylineQualificationService:
    def __init__(
        self,
        *,
        minimum_complete_books: int = 2,
        minimum_edge: float = 0.03,
        minimum_data_quality: float = 0.75,
        forecast_max_age_seconds: int = 3600,
    ) -> None:
        self.minimum_complete_books = minimum_complete_books
        self.minimum_edge = minimum_edge
        self.minimum_data_quality = minimum_data_quality
        self.forecast_max_age_seconds = forecast_max_age_seconds

    def evaluate(
        self,
        event: CanonicalEvent,
        complete_books: tuple[CompleteBookMarket, ...] | list[CompleteBookMarket],
        forecasts: tuple[MoneylineForecast, ...] | list[MoneylineForecast],
        feed_health: FeedHealth,
        *,
        selection: str = "home",
        as_of: datetime | None = None,
    ) -> QualificationResult:
        now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
        reasons: list[QualificationReason] = []
        books = [
            item
            for item in complete_books
            if item.canonical_event_id == event.canonical_id
            and item.market == "moneyline"
            and item.period == "full_game"
        ]
        if not feed_health.configured:
            reasons.append(QualificationReason.ODDS_FEED_NOT_CONFIGURED)
        elif feed_health.status not in {"healthy", "cached"}:
            reasons.append(QualificationReason.ODDS_FEED_UNAVAILABLE)
        if feed_health.freshness != "fresh":
            reasons.append(QualificationReason.ODDS_DATA_STALE)
        try:
            event_start = self._time(event.start_time)
            if event_start <= now:
                reasons.append(QualificationReason.EVENT_ALREADY_STARTED)
        except ValueError:
            reasons.append(QualificationReason.EVENT_NORMALIZATION_FAILED)
        if not books:
            reasons.append(QualificationReason.NO_COMPLETE_BOOKS)
        elif len({item.sportsbook for item in books}) < self.minimum_complete_books:
            reasons.append(QualificationReason.INSUFFICIENT_COMPLETE_BOOKS)

        exact = [
            forecast
            for forecast in forecasts
            if forecast.canonical_event_id == event.canonical_id
            and forecast.market == "moneyline"
            and forecast.period == "full_game"
            and forecast.selection == selection
        ]
        forecast = max(exact, key=lambda item: item.forecast_timestamp, default=None)
        if forecast is None:
            same_event = [
                item
                for item in forecasts
                if item.canonical_event_id == event.canonical_id
            ]
            reasons.append(
                QualificationReason.FORECAST_MARKET_MISMATCH
                if same_event
                else QualificationReason.FORECAST_MISSING
            )
        else:
            if forecast.metadata.calibration_status == "not_trained":
                reasons.append(QualificationReason.MODEL_NOT_TRAINED)
            elif (
                forecast.calibration_status != "calibrated"
                or forecast.metadata.calibration_status != "calibrated"
                or forecast.metadata.brier_score is None
                or forecast.metadata.brier_score > 0.25
            ):
                reasons.append(QualificationReason.FORECAST_NOT_CALIBRATED)
            if (
                now - self._time(forecast.forecast_timestamp)
            ).total_seconds() > self.forecast_max_age_seconds:
                reasons.append(QualificationReason.FORECAST_STALE)
            if forecast.missing_feature_warnings:
                reasons.append(QualificationReason.INSUFFICIENT_FEATURE_DATA)

        target_quotes = [
            item.home_quote if selection == "home" else item.away_quote
            for item in books
        ]
        best = max(
            target_quotes,
            key=lambda item: item.decimal_price,
            default=None,
        )
        market_probability = (
            sum(item.fair_probability(selection) for item in books) / len(books)
            if books
            else None
        )
        model_probability = (
            forecast.calibrated_probability if forecast is not None else None
        )
        edge = (
            model_probability - market_probability
            if model_probability is not None and market_probability is not None
            else None
        )
        expected_value = (
            model_probability * best.decimal_price - 1
            if model_probability is not None and best is not None
            else None
        )
        if edge is None or edge < self.minimum_edge:
            reasons.append(QualificationReason.EDGE_BELOW_THRESHOLD)

        quality_checks = (
            feed_health.status in {"healthy", "cached"},
            feed_health.freshness == "fresh",
            len({item.sportsbook for item in books}) >= self.minimum_complete_books,
            forecast is not None,
            forecast is not None and forecast.calibration_status == "calibrated",
            not (forecast and forecast.missing_feature_warnings),
        )
        data_quality = sum(quality_checks) / len(quality_checks)
        if data_quality < self.minimum_data_quality:
            reasons.append(QualificationReason.DATA_QUALITY_BELOW_THRESHOLD)
        reasons = list(dict.fromkeys(reasons))
        next_actions = [self._next_action(reason) for reason in reasons]
        qualified = not reasons
        raw_implied = best.implied_probability if best else None
        consensus_price = (
            self._american(market_probability)
            if market_probability is not None
            else None
        )
        return QualificationResult(
            canonical_event_id=event.canonical_id,
            selection=selection,
            qualified=qualified,
            status="QUALIFIED" if qualified else "NO_BET",
            reason_codes=tuple(reasons),
            diagnostics={
                "odds_feed": {
                    "passed": feed_health.configured
                    and feed_health.status in {"healthy", "cached"}
                    and feed_health.freshness == "fresh",
                    "status": feed_health.status,
                },
                "complete_books": {
                    "passed": len({item.sportsbook for item in books})
                    >= self.minimum_complete_books,
                    "actual": len({item.sportsbook for item in books}),
                    "required": self.minimum_complete_books,
                },
                "event_pairing": {"passed": bool(event.canonical_id and books)},
                "forecast": {"passed": forecast is not None},
                "calibration": {
                    "passed": bool(
                        forecast and forecast.calibration_status == "calibrated"
                    )
                },
                "data_quality": {
                    "passed": data_quality >= self.minimum_data_quality,
                    "actual": data_quality,
                    "required": self.minimum_data_quality,
                },
                "edge": {
                    "passed": edge is not None and edge >= self.minimum_edge,
                    "actual": edge,
                    "required": self.minimum_edge,
                },
                "next_actions": next_actions,
            },
            best_sportsbook=best.sportsbook if best else None,
            best_price=best.american_price if best else None,
            consensus_price=consensus_price,
            raw_implied_probability=raw_implied,
            market_probability=market_probability,
            model_probability=model_probability,
            edge=edge,
            expected_value=expected_value,
            confidence=(
                max(0.0, 1 - forecast.metadata.brier_score)
                if forecast and forecast.metadata.brier_score is not None
                else None
            ),
            data_quality=data_quality,
            evaluated_at=now.isoformat(),
            evidence=tuple(
                f"{item.sportsbook}: home {item.home_quote.american_price:+d}, "
                f"away {item.away_quote.american_price:+d}, "
                f"observed {item.observed_at}"
                for item in books
            ),
            risks=tuple(
                str(value)
                for value in (forecast.missing_feature_warnings if forecast else ())
            ),
        )

    @staticmethod
    def _time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _american(probability: float) -> int:
        if probability >= 0.5:
            return round(-100 * probability / (1 - probability))
        return round(100 * (1 - probability) / probability)

    @staticmethod
    def _next_action(reason: QualificationReason) -> str:
        actions = {
            QualificationReason.ODDS_FEED_NOT_CONFIGURED: (
                "Configure ODDS_API_IO_API_KEY or SPORTSGAMEODDS_API_KEY and enable the odds feed."
            ),
            QualificationReason.ODDS_FEED_UNAVAILABLE: "Retry the provider refresh and inspect System Status.",
            QualificationReason.ODDS_DATA_STALE: "Refresh sportsbook odds before evaluating.",
            QualificationReason.EVENT_NORMALIZATION_FAILED: "Resolve league, teams, and start-time identities.",
            QualificationReason.EVENT_ALREADY_STARTED: "Wait for a future pregame event.",
            QualificationReason.NO_COMPLETE_BOOKS: "Retrieve paired home and away prices from sportsbooks.",
            QualificationReason.INSUFFICIENT_COMPLETE_BOOKS: "Wait for at least two distinct sportsbooks to post complete prices.",
            QualificationReason.MARKET_PAIRING_FAILED: "Correct event, market, period, or selection pairing.",
            QualificationReason.FORECAST_MISSING: "Generate a forecast for this exact event and selection.",
            QualificationReason.MODEL_NOT_TRAINED: "Run make train-model with sufficient chronological history.",
            QualificationReason.FORECAST_NOT_CALIBRATED: "Train and validate calibration before using this forecast.",
            QualificationReason.FORECAST_STALE: "Regenerate the event forecast.",
            QualificationReason.FORECAST_MARKET_MISMATCH: "Generate a full-game moneyline forecast for the matching event and selection.",
            QualificationReason.INSUFFICIENT_FEATURE_DATA: "Refresh the missing team, schedule, rest, or availability features.",
            QualificationReason.EDGE_BELOW_THRESHOLD: "Wait for a better price or a materially different calibrated forecast.",
            QualificationReason.DATA_QUALITY_BELOW_THRESHOLD: "Resolve failed evidence gates before reconsidering.",
        }
        return actions[reason]
