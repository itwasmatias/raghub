from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sports.execution.contracts import PROBABILITY_PRECISION, ProbabilitySnapshotV1


def parse_utc_timestamp(name: str, value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware UTC")
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{name} must be UTC (+00:00)")
    return parsed.astimezone(timezone.utc)


def ensure_decimal_probability(name: str, value: Decimal) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    quantized = value.quantize(PROBABILITY_PRECISION, rounding=ROUND_HALF_UP)
    if quantized < Decimal("0") or quantized > Decimal("1"):
        raise ValueError(f"{name} must be in [0,1]")
    return quantized


def clamp_probability(value: Decimal) -> Decimal:
    if value < Decimal("0"):
        return Decimal("0")
    if value > Decimal("1"):
        return Decimal("1")
    return value


def validate_snapshot_for_reconciliation(snapshot: ProbabilitySnapshotV1) -> None:
    for name in (
        "raw_implied_probability",
        "no_vig_probability",
        "raw_sip_probability",
        "calibrated_sip_probability",
        "confidence_lower_bound",
        "confidence_upper_bound",
        "break_even_probability",
        "data_quality_score",
        "market_dispersion",
    ):
        ensure_decimal_probability(name, getattr(snapshot, name))

    if snapshot.cross_book_consensus_probability is not None:
        ensure_decimal_probability(
            "cross_book_consensus_probability",
            snapshot.cross_book_consensus_probability,
        )

    if snapshot.historical_prior_probability is not None:
        ensure_decimal_probability(
            "historical_prior_probability", snapshot.historical_prior_probability
        )

    if snapshot.raw_decimal_odds <= Decimal("1"):
        raise ValueError("raw_decimal_odds must be greater than one")

    implied_from_decimal = (Decimal("1") / snapshot.raw_decimal_odds).quantize(
        PROBABILITY_PRECISION,
        rounding=ROUND_HALF_UP,
    )
    tolerance = PROBABILITY_PRECISION
    if abs(implied_from_decimal - snapshot.raw_implied_probability) > tolerance:
        raise ValueError("raw_implied_probability inconsistent with decimal odds")
    if abs(implied_from_decimal - snapshot.break_even_probability) > tolerance:
        raise ValueError("break_even_probability inconsistent with decimal odds")

    if snapshot.confidence_lower_bound > snapshot.confidence_upper_bound:
        raise ValueError("invalid confidence bounds")
