"""Payment evidence boundary for MissionaryX Profit Loop v0.1.

This module observes payment state only.
It cannot create invoices, charge customers, transfer funds, or spend money.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum

from revenue_bridge.contribution import (
    PaymentObservation,
    PaymentVerificationState,
)
from revenue_bridge.events import _canonical_bytes, _require_text


class PaymentObservationMode(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    INDETERMINATE = "indeterminate"


class PaymentEvidenceAdapter(ABC):
    """Read-only boundary for obtaining payment evidence."""

    @abstractmethod
    def observe(
        self,
        *,
        payment_id: str,
        opportunity_id: str,
    ) -> PaymentObservation:
        """Observe payment state without initiating any financial effect."""
        raise NotImplementedError


class SimulatedPaymentEvidenceAdapter(PaymentEvidenceAdapter):
    """Deterministic no-network payment observer for tests and demos."""

    def __init__(
        self,
        mode: PaymentObservationMode = PaymentObservationMode.VERIFIED,
        provider_name: str = "simulated_payment_provider",
        observed_amount_usd: float = 50.0,
    ) -> None:
        if not isinstance(mode, PaymentObservationMode):
            raise TypeError("mode must be a PaymentObservationMode")
        self.mode = mode
        self.provider_name = _require_text(provider_name, "provider_name")
        self.observed_amount_usd = observed_amount_usd

    def observe(
        self,
        *,
        payment_id: str,
        opportunity_id: str,
    ) -> PaymentObservation:
        payment_id = _require_text(payment_id, "payment_id")
        opportunity_id = _require_text(opportunity_id, "opportunity_id")

        now = datetime.now(timezone.utc)

        state = PaymentVerificationState(self.mode.value)
        evidence_payload = {
            "provider": self.provider_name,
            "payment_id": payment_id,
            "opportunity_id": opportunity_id,
            "amount_usd": float(self.observed_amount_usd),
            "state": state.value,
            "observed_at": now.isoformat(),
        }
        evidence_hash = hashlib.sha256(
            _canonical_bytes(evidence_payload)
        ).hexdigest()

        return PaymentObservation(
            payment_id=payment_id,
            opportunity_id=opportunity_id,
            amount_usd=self.observed_amount_usd,
            state=state,
            evidence_ref=f"payev_{evidence_hash[:16]}",
            observed_at=now,
            provider_reference=f"{self.provider_name}:{payment_id}",
        )
