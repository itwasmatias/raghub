from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sports.execution.models import (
    ProbabilityRecord,
    ProbabilityReconciliationPolicy,
    ReconciledProbabilityResult,
    ReconciliationStatus,
)


def _clamp(value: Decimal) -> Decimal:
    if value < Decimal("0"):
        return Decimal("0")
    if value > Decimal("1"):
        return Decimal("1")
    return value


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize(weights: dict[str, Decimal]) -> dict[str, Decimal]:
    total = sum(weights.values(), Decimal("0"))
    if total <= 0:
        return {key: Decimal("0") for key in weights}
    return {key: value / total for key, value in weights.items()}


class ProbabilityReconciler:
    def __init__(self, policy: ProbabilityReconciliationPolicy) -> None:
        self.policy = policy

    def reconcile(
        self,
        record: ProbabilityRecord,
        *,
        historical_prior: Decimal | None = None,
        strategy_performance: Decimal = Decimal("1"),
        model_stability: Decimal = Decimal("1"),
        as_of: datetime | None = None,
    ) -> ReconciledProbabilityResult:
        as_of = as_of or datetime.now(timezone.utc)
        quote_timestamp = _parse_timestamp(record.quote_timestamp)
        quote_age_minutes = Decimal(
            str(max(0.0, (as_of - quote_timestamp).total_seconds() / 60.0))
        )

        warnings: list[str] = []
        adjustment_reasons: list[str] = []

        if quote_age_minutes > Decimal(str(self.policy.maximum_quote_age_minutes)):
            return ReconciledProbabilityResult(
                probability=record.cross_book_consensus_probability,
                lower_bound=record.cross_book_consensus_probability,
                upper_bound=record.cross_book_consensus_probability,
                component_weights={
                    "calibrated_sip": Decimal("0"),
                    "consensus": Decimal("1"),
                    "historical_prior": Decimal("0"),
                },
                adjustment_reasons=("quote stale beyond maximum age",),
                warnings=("stale quote",),
                model_version=record.model_version,
                reconciliation_version=self.policy.reconciliation_version,
                status=ReconciliationStatus.UNAVAILABLE,
                raw_component_probabilities={
                    "raw_implied_probability": record.raw_implied_probability,
                    "no_vig_probability": record.no_vig_probability,
                    "cross_book_consensus_probability": record.cross_book_consensus_probability,
                    "raw_sip_probability": record.raw_sip_probability,
                    "calibrated_sip_probability": record.calibrated_sip_probability,
                    "historical_prior": historical_prior or record.no_vig_probability,
                },
                explanation="stale quote blocks defensible reconciliation",
            )

        data_quality = _clamp(record.data_quality_score)
        freshness = _clamp(
            Decimal("1")
            - (
                quote_age_minutes
                / Decimal(str(max(1, self.policy.maximum_quote_age_minutes)))
            )
        )
        coverage_ratio = _clamp(
            Decimal(record.sportsbook_coverage)
            / Decimal(str(max(1, self.policy.minimum_sportsbook_coverage * 2)))
        )
        strategy_quality = _clamp(strategy_performance)
        stability = _clamp(model_stability)
        confidence = _clamp(
            (data_quality * Decimal("0.35"))
            + (freshness * Decimal("0.25"))
            + (coverage_ratio * Decimal("0.15"))
            + (strategy_quality * Decimal("0.125"))
            + (stability * Decimal("0.125"))
        )

        if data_quality < self.policy.minimum_data_quality:
            warnings.append("low data quality")
            adjustment_reasons.append(
                "shrank toward consensus because data quality is weak"
            )
        if record.sportsbook_coverage < self.policy.minimum_sportsbook_coverage:
            warnings.append("thin sportsbook coverage")
            adjustment_reasons.append(
                "used wider consensus anchor because coverage is thin"
            )
        if confidence < Decimal("0.50"):
            warnings.append("uncertain reconciliation")

        prior = (
            historical_prior
            if historical_prior is not None
            else record.no_vig_probability
        )
        prior = _clamp(prior)

        raw_weights = {
            "calibrated_sip": Decimal("0.40") * confidence + Decimal("0.10"),
            "consensus": Decimal("0.35")
            + (Decimal("1") - confidence) * Decimal("0.20"),
            "historical_prior": Decimal("0.15")
            + (Decimal("1") - confidence) * Decimal("0.10"),
        }
        component_weights = _normalize(raw_weights)
        blended = (
            component_weights["calibrated_sip"]
            * _clamp(record.calibrated_sip_probability)
            + component_weights["consensus"]
            * _clamp(record.cross_book_consensus_probability)
            + component_weights["historical_prior"] * prior
        )
        probability = _clamp(
            record.cross_book_consensus_probability
            + (blended - record.cross_book_consensus_probability) * confidence
        )
        uncertainty_width = Decimal("0.20") * (Decimal("1") - confidence)
        lower_bound = _clamp(probability - uncertainty_width / Decimal("2"))
        upper_bound = _clamp(probability + uncertainty_width / Decimal("2"))

        return ReconciledProbabilityResult(
            probability=probability,
            lower_bound=lower_bound,
            upper_bound=upper_bound,
            component_weights=component_weights,
            adjustment_reasons=tuple(adjustment_reasons),
            warnings=tuple(warnings),
            model_version=record.model_version,
            reconciliation_version=self.policy.reconciliation_version,
            status=ReconciliationStatus.RECONCILED,
            raw_component_probabilities={
                "raw_implied_probability": record.raw_implied_probability,
                "no_vig_probability": record.no_vig_probability,
                "cross_book_consensus_probability": record.cross_book_consensus_probability,
                "raw_sip_probability": record.raw_sip_probability,
                "calibrated_sip_probability": record.calibrated_sip_probability,
                "historical_prior": prior,
            },
            explanation="; ".join(adjustment_reasons)
            if adjustment_reasons
            else "reconciled with calibrated SIP output and market consensus",
        )
