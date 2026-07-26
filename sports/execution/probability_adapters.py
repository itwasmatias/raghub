from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from sports.execution.contracts import ProbabilitySnapshotV1
from sports.execution.statuses import (
    CalibrationApprovalStatusV1,
    CalibrationStatusV1,
    ModelApprovalStatusV1,
    ModelStatusV1,
    VersionApprovalStatusV1,
)


class LegacyProbabilityAdapterV1:
    """Explicit compatibility boundary for legacy probability payloads."""

    FORBIDDEN_FIELDS = frozenset(
        {
            "sip_adjusted_probability",
            "synthetic_implied_probability",
            "qualification_probability",
            "legacy_ev",
        }
    )

    @classmethod
    def snapshot_from_legacy_mapping(
        cls,
        payload: Mapping[str, object],
        *,
        compatibility_mode: bool,
    ) -> ProbabilitySnapshotV1:
        if not compatibility_mode:
            raise RuntimeError(
                "Legacy adapter is disabled; set compatibility_mode=True explicitly"
            )

        forbidden = cls.FORBIDDEN_FIELDS.intersection(payload.keys())
        if forbidden:
            joined = ", ".join(sorted(forbidden))
            raise ValueError(f"legacy payload contains forbidden fields: {joined}")

        return ProbabilitySnapshotV1(
            snapshot_id=str(payload["snapshot_id"]),
            league=str(payload["league"]),
            event_id=str(payload["event_id"]),
            market_id=str(payload["market_id"]),
            market_type=str(payload["market_type"]),
            period=str(payload["period"]),
            outcome_id=str(payload["outcome_id"]),
            selection_id=str(payload["selection_id"]),
            outcome_schema=str(payload["outcome_schema"]),
            event_start_time=str(payload["event_start_time"]),
            as_of=str(payload["as_of"]),
            raw_american_odds=int(payload["raw_american_odds"]),
            raw_decimal_odds=Decimal(str(payload["raw_decimal_odds"])),
            raw_implied_probability=Decimal(str(payload["raw_implied_probability"])),
            no_vig_probability=Decimal(str(payload["no_vig_probability"])),
            cross_book_consensus_probability=(
                Decimal(str(payload["cross_book_consensus_probability"]))
                if payload.get("cross_book_consensus_probability") is not None
                else None
            ),
            raw_sip_probability=Decimal(str(payload["raw_sip_probability"])),
            calibrated_sip_probability=Decimal(
                str(payload["calibrated_sip_probability"])
            ),
            reconciled_execution_probability=None,
            confidence_lower_bound=Decimal(str(payload["confidence_lower_bound"])),
            confidence_upper_bound=Decimal(str(payload["confidence_upper_bound"])),
            break_even_probability=Decimal(str(payload["break_even_probability"])),
            historical_prior_probability=Decimal(
                str(payload["historical_prior_probability"])
            )
            if payload.get("historical_prior_probability") is not None
            else None,
            historical_prior_source=str(payload.get("historical_prior_source", "")),
            historical_prior_version=str(payload.get("historical_prior_version", "")),
            historical_prior_timestamp=str(
                payload.get("historical_prior_timestamp", "")
            ),
            historical_prior_sample_scope=str(
                payload.get("historical_prior_sample_scope", "")
            ),
            historical_prior_missing=bool(
                payload.get("historical_prior_missing", False)
            ),
            execution_quote_id=str(payload["execution_quote_id"]),
            execution_sportsbook_id=str(payload["execution_sportsbook_id"]),
            execution_quote_timestamp=str(payload["execution_quote_timestamp"]),
            consensus_constituent_quote_ids=tuple(
                str(value)
                for value in payload.get("consensus_constituent_quote_ids", ())
            ),
            constituent_sportsbook_ids=tuple(
                str(value) for value in payload.get("constituent_sportsbook_ids", ())
            ),
            oldest_constituent_timestamp=str(payload["oldest_constituent_timestamp"]),
            consensus_calculated_at=str(payload["consensus_calculated_at"]),
            complete_fresh_sportsbook_count=int(
                payload["complete_fresh_sportsbook_count"]
            ),
            market_dispersion=Decimal(str(payload["market_dispersion"])),
            canonical_input_hash=str(payload.get("canonical_input_hash", "")),
            source_quote_set_id=str(payload["source_quote_set_id"]),
            quote_timestamp=str(payload["quote_timestamp"]),
            forecast_timestamp=str(payload["forecast_timestamp"]),
            model_version=str(payload["model_version"]),
            calibration_version=str(payload["calibration_version"]),
            reconciliation_version=str(payload["reconciliation_version"]),
            reconciliation_policy_version=str(payload["reconciliation_policy_version"]),
            reconciliation_method_version=str(payload["reconciliation_method_version"]),
            confidence_interval_method_version=str(
                payload["confidence_interval_method_version"]
            ),
            model_status=ModelStatusV1(str(payload.get("model_status", "approved"))),
            calibration_status=CalibrationStatusV1(
                str(payload.get("calibration_status", "approved"))
            ),
            model_approval_status=ModelApprovalStatusV1(
                str(payload.get("model_approval_status", "approved"))
            ),
            calibration_approval_status=CalibrationApprovalStatusV1(
                str(payload.get("calibration_approval_status", "approved"))
            ),
            policy_approval_status=VersionApprovalStatusV1(
                str(payload.get("policy_approval_status", "approved"))
            ),
            reconciliation_approval_status=VersionApprovalStatusV1(
                str(payload.get("reconciliation_approval_status", "approved"))
            ),
            data_quality_score=Decimal(str(payload["data_quality_score"])),
            sportsbook_coverage=int(payload["sportsbook_coverage"]),
            reconciliation_method=str(payload["reconciliation_method"]),
            reconciliation_weights={
                str(key): Decimal(str(value))
                for key, value in payload.get("reconciliation_weights", {}).items()
            },
            trust_factors={
                str(key): Decimal(str(value))
                for key, value in payload.get("trust_factors", {}).items()
            },
            source_quote_ids=tuple(
                str(value) for value in payload.get("source_quote_ids", ())
            ),
        )
