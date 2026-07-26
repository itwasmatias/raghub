from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sports.execution.models import (
    ProbabilityRecord,
    ProbabilityReconciliationPolicy,
    ReconciliationStatus,
)
from sports.execution.probability import ProbabilityReconciler


def _record(quote_timestamp: str) -> ProbabilityRecord:
    return ProbabilityRecord(
        market_id="market-1",
        outcome_id="outcome-1",
        raw_implied_probability=Decimal("0.44"),
        no_vig_probability=Decimal("0.46"),
        cross_book_consensus_probability=Decimal("0.52"),
        raw_sip_probability=Decimal("0.61"),
        calibrated_sip_probability=Decimal("0.64"),
        reconciled_execution_probability=Decimal("0.00"),
        confidence_interval=(Decimal("0.40"), Decimal("0.70")),
        break_even_probability=Decimal("0.50"),
        quote_timestamp=quote_timestamp,
        forecast_timestamp=quote_timestamp,
        model_version="model-v1",
        calibration_version="cal-v1",
        data_quality_score=Decimal("0.82"),
        sportsbook_coverage=4,
        reconciliation_method="weighted-shrink",
        reconciliation_weights={"calibrated_sip": Decimal("0.5")},
    )


def test_probability_reconciliation_preserves_raw_components_and_uses_calibrated_output():
    policy = ProbabilityReconciliationPolicy(
        policy_version="policy-v1",
        reconciliation_version="recon-v1",
        maximum_quote_age_minutes=30,
    )
    reconciler = ProbabilityReconciler(policy)
    now = datetime.now(timezone.utc)

    result = reconciler.reconcile(_record(now.isoformat()), as_of=now)

    assert result.status == ReconciliationStatus.RECONCILED
    assert result.raw_component_probabilities["calibrated_sip_probability"] == Decimal(
        "0.64"
    )
    assert result.raw_component_probabilities[
        "cross_book_consensus_probability"
    ] == Decimal("0.52")
    assert result.probability >= Decimal("0.52")
    assert result.probability <= Decimal("0.64")


def test_low_quality_data_shrinks_toward_consensus_and_stale_quotes_block():
    policy = ProbabilityReconciliationPolicy(
        policy_version="policy-v1",
        reconciliation_version="recon-v1",
        maximum_quote_age_minutes=10,
    )
    reconciler = ProbabilityReconciler(policy)
    now = datetime.now(timezone.utc)
    fresh = _record(now.isoformat())
    low_quality = _record(now.isoformat())
    object.__setattr__(low_quality, "data_quality_score", Decimal("0.10"))

    shrunk = reconciler.reconcile(low_quality, as_of=now)
    blocked = reconciler.reconcile(fresh, as_of=now + timedelta(minutes=11))

    assert shrunk.probability < low_quality.calibrated_sip_probability
    assert shrunk.probability > low_quality.cross_book_consensus_probability - Decimal(
        "0.05"
    )
    assert blocked.status.name == "UNAVAILABLE"
