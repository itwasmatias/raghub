"""Opportunity economics for MissionaryX Profit Loop v0.1.

Records explicit estimates only. This module cannot authorize or execute spending.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Mapping

from revenue_bridge.events import _canonical_bytes, _require_text


def _require_nonnegative_money(value: float, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    if value < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return value


def _require_probability(value: float, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    if not 0 <= value <= 1:
        raise ValueError(f"{field_name} must be between 0 and 1")
    return value


@dataclass(frozen=True, slots=True)
class OpportunityEconomics:
    opportunity_id: str
    sale_probability: float
    offer_price_usd: float
    proposed_acquisition_cost_usd: float
    estimated_fulfillment_cost_usd: float
    estimated_model_api_cost_usd: float
    estimated_payment_platform_fees_usd: float = 0.0
    evidence_refs: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "opportunity_id",
            _require_text(self.opportunity_id, "opportunity_id"),
        )
        object.__setattr__(
            self,
            "sale_probability",
            _require_probability(self.sale_probability, "sale_probability"),
        )

        for field_name in (
            "offer_price_usd",
            "proposed_acquisition_cost_usd",
            "estimated_fulfillment_cost_usd",
            "estimated_model_api_cost_usd",
            "estimated_payment_platform_fees_usd",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_nonnegative_money(getattr(self, field_name), field_name),
            )

        if not isinstance(self.evidence_refs, tuple):
            object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        if not isinstance(self.assumptions, tuple):
            object.__setattr__(self, "assumptions", tuple(self.assumptions))

        for value in self.evidence_refs:
            _require_text(value, "evidence_ref")
        for value in self.assumptions:
            _require_text(value, "assumption")

    @property
    def estimated_margin_if_sold_usd(self) -> float:
        return round(
            self.offer_price_usd
            - self.estimated_fulfillment_cost_usd
            - self.estimated_model_api_cost_usd
            - self.estimated_payment_platform_fees_usd,
            2,
        )

    @property
    def expected_sale_revenue_usd(self) -> float:
        return round(self.sale_probability * self.offer_price_usd, 2)

    @property
    def expected_contribution_usd(self) -> float:
        return round(
            self.sale_probability * self.estimated_margin_if_sold_usd
            - self.proposed_acquisition_cost_usd,
            2,
        )

    @property
    def break_even_sale_probability(self) -> float | None:
        margin = self.estimated_margin_if_sold_usd
        if margin <= 0:
            return None
        return round(self.proposed_acquisition_cost_usd / margin, 4)

    @property
    def is_expected_positive(self) -> bool:
        return self.expected_contribution_usd > 0

    @property
    def fingerprint(self) -> str:
        payload = {
            "opportunity_id": self.opportunity_id,
            "sale_probability": self.sale_probability,
            "offer_price_usd": self.offer_price_usd,
            "proposed_acquisition_cost_usd": self.proposed_acquisition_cost_usd,
            "estimated_fulfillment_cost_usd": self.estimated_fulfillment_cost_usd,
            "estimated_model_api_cost_usd": self.estimated_model_api_cost_usd,
            "estimated_payment_platform_fees_usd": self.estimated_payment_platform_fees_usd,
            "evidence_refs": sorted(self.evidence_refs),
            "assumptions": sorted(self.assumptions),
        }
        return hashlib.sha256(_canonical_bytes(payload)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "sale_probability": self.sale_probability,
            "offer_price_usd": self.offer_price_usd,
            "proposed_acquisition_cost_usd": self.proposed_acquisition_cost_usd,
            "estimated_fulfillment_cost_usd": self.estimated_fulfillment_cost_usd,
            "estimated_model_api_cost_usd": self.estimated_model_api_cost_usd,
            "estimated_payment_platform_fees_usd": self.estimated_payment_platform_fees_usd,
            "evidence_refs": list(self.evidence_refs),
            "assumptions": list(self.assumptions),
            "expected_sale_revenue_usd": self.expected_sale_revenue_usd,
            "expected_contribution_usd": self.expected_contribution_usd,
            "break_even_sale_probability": self.break_even_sale_probability,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OpportunityEconomics":
        return cls(
            opportunity_id=data["opportunity_id"],
            sale_probability=float(data["sale_probability"]),
            offer_price_usd=float(data["offer_price_usd"]),
            proposed_acquisition_cost_usd=float(data["proposed_acquisition_cost_usd"]),
            estimated_fulfillment_cost_usd=float(data["estimated_fulfillment_cost_usd"]),
            estimated_model_api_cost_usd=float(data["estimated_model_api_cost_usd"]),
            estimated_payment_platform_fees_usd=float(
                data.get("estimated_payment_platform_fees_usd", 0.0)
            ),
            evidence_refs=tuple(data.get("evidence_refs", ())),
            assumptions=tuple(data.get("assumptions", ())),
        )
