"""Verified contribution accounting for MissionaryX Profit Loop v0.1.

Invariants:
- Costs may be recorded from explicit evidence-bearing observations.
- Customer revenue does not count until payment is VERIFIED.
- Unverified or indeterminate payments never reduce unrecovered loss.
- A verified payment cannot be counted twice.
- This module cannot initiate payments or spending.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Mapping

from revenue_bridge.capital import CapitalSnapshot
from revenue_bridge.events import _canonical_bytes, _require_text, _require_timestamp


def _require_nonnegative_money(value: float, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    if value < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return value


class ContributionKind(str, Enum):
    ACQUISITION_COST = "acquisition_cost"
    FULFILLMENT_COST = "fulfillment_cost"
    MODEL_API_COST = "model_api_cost"
    PAYMENT_PLATFORM_FEE = "payment_platform_fee"
    VERIFIED_CUSTOMER_REVENUE = "verified_customer_revenue"


class PaymentVerificationState(str, Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class PaymentObservation:
    payment_id: str
    opportunity_id: str
    amount_usd: float
    state: PaymentVerificationState
    evidence_ref: str
    observed_at: datetime
    provider_reference: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payment_id", _require_text(self.payment_id, "payment_id"))
        object.__setattr__(
            self, "opportunity_id",
            _require_text(self.opportunity_id, "opportunity_id"),
        )
        object.__setattr__(
            self, "amount_usd",
            _require_nonnegative_money(self.amount_usd, "amount_usd"),
        )
        if not isinstance(self.state, PaymentVerificationState):
            raise TypeError("state must be a PaymentVerificationState")
        object.__setattr__(
            self, "evidence_ref",
            _require_text(self.evidence_ref, "evidence_ref"),
        )
        object.__setattr__(
            self, "observed_at",
            _require_timestamp(self.observed_at, "observed_at"),
        )
        if self.provider_reference is not None:
            object.__setattr__(
                self,
                "provider_reference",
                _require_text(self.provider_reference, "provider_reference"),
            )

    @property
    def fingerprint(self) -> str:
        payload = {
            "payment_id": self.payment_id,
            "opportunity_id": self.opportunity_id,
            "amount_usd": self.amount_usd,
            "state": self.state.value,
            "evidence_ref": self.evidence_ref,
            "observed_at": self.observed_at.isoformat(),
            "provider_reference": self.provider_reference,
        }
        return hashlib.sha256(_canonical_bytes(payload)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "payment_id": self.payment_id,
            "opportunity_id": self.opportunity_id,
            "amount_usd": self.amount_usd,
            "state": self.state.value,
            "evidence_ref": self.evidence_ref,
            "observed_at": self.observed_at.isoformat(),
            "provider_reference": self.provider_reference,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PaymentObservation":
        observed_at = data["observed_at"]
        if isinstance(observed_at, str):
            observed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        return cls(
            payment_id=data["payment_id"],
            opportunity_id=data["opportunity_id"],
            amount_usd=float(data["amount_usd"]),
            state=PaymentVerificationState(data["state"]),
            evidence_ref=data["evidence_ref"],
            observed_at=observed_at,
            provider_reference=data.get("provider_reference"),
        )


@dataclass(frozen=True, slots=True)
class ContributionEntry:
    entry_id: str
    opportunity_id: str
    kind: ContributionKind
    amount_usd: float
    evidence_ref: str
    occurred_at: datetime
    source_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_id", _require_text(self.entry_id, "entry_id"))
        object.__setattr__(
            self, "opportunity_id",
            _require_text(self.opportunity_id, "opportunity_id"),
        )
        if not isinstance(self.kind, ContributionKind):
            raise TypeError("kind must be a ContributionKind")
        object.__setattr__(
            self, "amount_usd",
            _require_nonnegative_money(self.amount_usd, "amount_usd"),
        )
        object.__setattr__(
            self, "evidence_ref",
            _require_text(self.evidence_ref, "evidence_ref"),
        )
        object.__setattr__(
            self, "occurred_at",
            _require_timestamp(self.occurred_at, "occurred_at"),
        )
        if self.source_ref is not None:
            object.__setattr__(
                self, "source_ref",
                _require_text(self.source_ref, "source_ref"),
            )

    @property
    def fingerprint(self) -> str:
        payload = {
            "entry_id": self.entry_id,
            "opportunity_id": self.opportunity_id,
            "kind": self.kind.value,
            "amount_usd": self.amount_usd,
            "evidence_ref": self.evidence_ref,
            "occurred_at": self.occurred_at.isoformat(),
            "source_ref": self.source_ref,
        }
        return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


@dataclass
class ContributionLedger:
    _entries: list[ContributionEntry] = field(default_factory=list)
    _entry_ids: set[str] = field(default_factory=set)
    _counted_payment_ids: set[str] = field(default_factory=set)

    @property
    def entries(self) -> tuple[ContributionEntry, ...]:
        return tuple(self._entries)

    def _append(self, entry: ContributionEntry) -> ContributionEntry:
        if entry.entry_id in self._entry_ids:
            raise ValueError(f"duplicate contribution entry_id: {entry.entry_id}")
        self._entries.append(entry)
        self._entry_ids.add(entry.entry_id)
        return entry

    def record_cost(
        self,
        *,
        entry_id: str,
        opportunity_id: str,
        kind: ContributionKind,
        amount_usd: float,
        evidence_ref: str,
        occurred_at: datetime | None = None,
        source_ref: str | None = None,
    ) -> ContributionEntry:
        if kind == ContributionKind.VERIFIED_CUSTOMER_REVENUE:
            raise ValueError(
                "customer revenue must come from a verified payment observation"
            )
        return self._append(
            ContributionEntry(
                entry_id=entry_id,
                opportunity_id=opportunity_id,
                kind=kind,
                amount_usd=amount_usd,
                evidence_ref=evidence_ref,
                occurred_at=occurred_at or datetime.now(timezone.utc),
                source_ref=source_ref,
            )
        )

    def record_verified_payment(
        self,
        payment: PaymentObservation,
    ) -> ContributionEntry:
        if not isinstance(payment, PaymentObservation):
            raise TypeError("payment must be a PaymentObservation")
        if payment.state != PaymentVerificationState.VERIFIED:
            raise ValueError(
                "customer revenue requires VERIFIED payment evidence; "
                f"received {payment.state.value}"
            )
        if payment.payment_id in self._counted_payment_ids:
            raise ValueError(f"payment already counted: {payment.payment_id}")

        entry = ContributionEntry(
            entry_id=f"revenue_{payment.payment_id}",
            opportunity_id=payment.opportunity_id,
            kind=ContributionKind.VERIFIED_CUSTOMER_REVENUE,
            amount_usd=payment.amount_usd,
            evidence_ref=payment.evidence_ref,
            occurred_at=payment.observed_at,
            source_ref=payment.provider_reference,
        )
        self._append(entry)
        self._counted_payment_ids.add(payment.payment_id)
        return entry

    def capital_snapshot(self, *, as_of_date: date | None = None) -> CapitalSnapshot:
        day = as_of_date or datetime.now(timezone.utc).date()

        acquisition = fulfillment = model_api = platform_fees = verified_revenue = 0.0
        today_acquisition = 0.0

        for entry in self._entries:
            if entry.kind == ContributionKind.ACQUISITION_COST:
                acquisition += entry.amount_usd
                if entry.occurred_at.date() == day:
                    today_acquisition += entry.amount_usd
            elif entry.kind == ContributionKind.FULFILLMENT_COST:
                fulfillment += entry.amount_usd
            elif entry.kind == ContributionKind.MODEL_API_COST:
                model_api += entry.amount_usd
            elif entry.kind == ContributionKind.PAYMENT_PLATFORM_FEE:
                platform_fees += entry.amount_usd
            elif entry.kind == ContributionKind.VERIFIED_CUSTOMER_REVENUE:
                verified_revenue += entry.amount_usd

        return CapitalSnapshot(
            acquisition_spend_usd=round(acquisition, 2),
            fulfillment_cost_usd=round(fulfillment, 2),
            model_api_cost_usd=round(model_api, 2),
            payment_platform_fees_usd=round(platform_fees, 2),
            verified_customer_revenue_usd=round(verified_revenue, 2),
            today_acquisition_spend_usd=round(today_acquisition, 2),
        )
