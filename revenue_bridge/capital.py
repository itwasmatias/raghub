"""Capital governor for MissionaryX Profit Loop v0.1.

This module is deliberately inert:
- it cannot move money;
- it cannot approve its own spend;
- it only evaluates whether a proposed acquisition spend is economically
  eligible to be presented to the creator for explicit approval.

Frozen v0.1 boundaries:
- $60 maximum unrecovered experiment loss;
- caution mode begins at $40 unrecovered loss;
- $5 normal per-opportunity spend;
- $10 exception per-opportunity spend;
- $20 daily acquisition-spend ceiling;
- only verified customer revenue can reduce unrecovered experiment loss.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from revenue_bridge.events import _canonical_bytes, _require_text


def _require_money(value: float, field_name: str, *, allow_negative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    if not allow_negative and value < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return value


class CapitalMode(str, Enum):
    NORMAL = "normal"
    CAUTION = "caution"
    HARD_STOP = "hard_stop"


class CapitalDecision(str, Enum):
    REJECT = "reject"
    FREE_ONLY = "free_only"
    PROPOSE_SPEND = "propose_spend"


class SpendTier(str, Enum):
    NORMAL = "normal"
    EXCEPTION = "exception"


@dataclass(frozen=True, slots=True)
class CapitalPolicy:
    total_loss_ceiling_usd: float = 60.0
    caution_loss_threshold_usd: float = 40.0
    normal_per_opportunity_limit_usd: float = 5.0
    exception_per_opportunity_limit_usd: float = 10.0
    daily_acquisition_spend_limit_usd: float = 20.0
    caution_min_expected_contribution_multiple: float = 2.0

    def __post_init__(self) -> None:
        for field_name in (
            "total_loss_ceiling_usd",
            "caution_loss_threshold_usd",
            "normal_per_opportunity_limit_usd",
            "exception_per_opportunity_limit_usd",
            "daily_acquisition_spend_limit_usd",
            "caution_min_expected_contribution_multiple",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_money(getattr(self, field_name), field_name),
            )

        if self.total_loss_ceiling_usd <= 0:
            raise ValueError("total_loss_ceiling_usd must be > 0")
        if self.caution_loss_threshold_usd >= self.total_loss_ceiling_usd:
            raise ValueError("caution threshold must be below total loss ceiling")
        if self.normal_per_opportunity_limit_usd > self.exception_per_opportunity_limit_usd:
            raise ValueError("normal per-opportunity limit cannot exceed exception limit")
        if self.exception_per_opportunity_limit_usd > self.daily_acquisition_spend_limit_usd:
            raise ValueError("exception per-opportunity limit cannot exceed daily limit")
        if self.caution_min_expected_contribution_multiple <= 0:
            raise ValueError("caution contribution multiple must be > 0")


@dataclass(frozen=True, slots=True)
class CapitalSnapshot:
    """Observed Profit Loop economics.

    No savings/payroll field exists intentionally. External personal cash does
    not replenish the experiment. Only verified customer revenue offsets costs.
    """

    acquisition_spend_usd: float = 0.0
    fulfillment_cost_usd: float = 0.0
    model_api_cost_usd: float = 0.0
    payment_platform_fees_usd: float = 0.0
    verified_customer_revenue_usd: float = 0.0
    today_acquisition_spend_usd: float = 0.0

    def __post_init__(self) -> None:
        for field_name in (
            "acquisition_spend_usd",
            "fulfillment_cost_usd",
            "model_api_cost_usd",
            "payment_platform_fees_usd",
            "verified_customer_revenue_usd",
            "today_acquisition_spend_usd",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_money(getattr(self, field_name), field_name),
            )

    @property
    def total_cost_usd(self) -> float:
        return round(
            self.acquisition_spend_usd
            + self.fulfillment_cost_usd
            + self.model_api_cost_usd
            + self.payment_platform_fees_usd,
            2,
        )

    @property
    def net_contribution_usd(self) -> float:
        return round(self.verified_customer_revenue_usd - self.total_cost_usd, 2)

    @property
    def unrecovered_loss_usd(self) -> float:
        return round(max(0.0, self.total_cost_usd - self.verified_customer_revenue_usd), 2)

    def mode(self, policy: CapitalPolicy | None = None) -> CapitalMode:
        policy = policy or CapitalPolicy()
        if self.unrecovered_loss_usd >= policy.total_loss_ceiling_usd:
            return CapitalMode.HARD_STOP
        if self.unrecovered_loss_usd >= policy.caution_loss_threshold_usd:
            return CapitalMode.CAUTION
        return CapitalMode.NORMAL


@dataclass(frozen=True, slots=True)
class SpendProposal:
    """Inert request for creator authorization of one exact acquisition spend."""

    proposal_id: str
    opportunity_id: str
    requested_spend_usd: float
    spend_tier: SpendTier
    expected_contribution_usd: float
    acquisition_channel: str
    purpose: str
    requires_creator_approval: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "proposal_id", _require_text(self.proposal_id, "proposal_id"))
        object.__setattr__(
            self,
            "opportunity_id",
            _require_text(self.opportunity_id, "opportunity_id"),
        )
        object.__setattr__(
            self,
            "requested_spend_usd",
            _require_money(self.requested_spend_usd, "requested_spend_usd"),
        )
        object.__setattr__(
            self,
            "expected_contribution_usd",
            _require_money(
                self.expected_contribution_usd,
                "expected_contribution_usd",
                allow_negative=True,
            ),
        )
        object.__setattr__(
            self,
            "acquisition_channel",
            _require_text(self.acquisition_channel, "acquisition_channel"),
        )
        object.__setattr__(self, "purpose", _require_text(self.purpose, "purpose"))

        if not isinstance(self.spend_tier, SpendTier):
            raise TypeError("spend_tier must be a SpendTier")
        if self.requires_creator_approval is not True:
            raise ValueError("external spend MUST require creator approval")

    @property
    def fingerprint(self) -> str:
        payload = {
            "proposal_id": self.proposal_id,
            "opportunity_id": self.opportunity_id,
            "requested_spend_usd": self.requested_spend_usd,
            "spend_tier": self.spend_tier.value,
            "expected_contribution_usd": self.expected_contribution_usd,
            "acquisition_channel": self.acquisition_channel,
            "purpose": self.purpose,
            "requires_creator_approval": self.requires_creator_approval,
        }
        return hashlib.sha256(_canonical_bytes(payload)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "opportunity_id": self.opportunity_id,
            "requested_spend_usd": self.requested_spend_usd,
            "spend_tier": self.spend_tier.value,
            "expected_contribution_usd": self.expected_contribution_usd,
            "acquisition_channel": self.acquisition_channel,
            "purpose": self.purpose,
            "requires_creator_approval": self.requires_creator_approval,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SpendProposal":
        return cls(
            proposal_id=data["proposal_id"],
            opportunity_id=data["opportunity_id"],
            requested_spend_usd=float(data["requested_spend_usd"]),
            spend_tier=SpendTier(data["spend_tier"]),
            expected_contribution_usd=float(data["expected_contribution_usd"]),
            acquisition_channel=data["acquisition_channel"],
            purpose=data["purpose"],
            requires_creator_approval=bool(data.get("requires_creator_approval", True)),
        )


@dataclass(frozen=True, slots=True)
class CapitalEvaluation:
    decision: CapitalDecision
    mode: CapitalMode
    reason: str
    current_unrecovered_loss_usd: float
    projected_unrecovered_loss_usd: float

    def __post_init__(self) -> None:
        if not isinstance(self.decision, CapitalDecision):
            raise TypeError("decision must be a CapitalDecision")
        if not isinstance(self.mode, CapitalMode):
            raise TypeError("mode must be a CapitalMode")
        object.__setattr__(self, "reason", _require_text(self.reason, "reason"))


class CapitalGovernor:
    """Pure policy engine. Never executes or approves external spending."""

    def __init__(self, policy: CapitalPolicy | None = None) -> None:
        self.policy = policy or CapitalPolicy()

    def evaluate(
        self,
        snapshot: CapitalSnapshot,
        proposal: SpendProposal,
    ) -> CapitalEvaluation:
        if not isinstance(snapshot, CapitalSnapshot):
            raise TypeError("snapshot must be a CapitalSnapshot")
        if not isinstance(proposal, SpendProposal):
            raise TypeError("proposal must be a SpendProposal")

        mode = snapshot.mode(self.policy)
        current_loss = snapshot.unrecovered_loss_usd

        if mode == CapitalMode.HARD_STOP:
            return CapitalEvaluation(
                decision=CapitalDecision.REJECT,
                mode=mode,
                reason="Hard stop: unrecovered experiment loss has reached the $60 ceiling.",
                current_unrecovered_loss_usd=current_loss,
                projected_unrecovered_loss_usd=current_loss,
            )

        if proposal.requested_spend_usd == 0:
            return CapitalEvaluation(
                decision=CapitalDecision.FREE_ONLY,
                mode=mode,
                reason="Opportunity requires no acquisition spend.",
                current_unrecovered_loss_usd=current_loss,
                projected_unrecovered_loss_usd=current_loss,
            )

        per_opportunity_limit = (
            self.policy.normal_per_opportunity_limit_usd
            if proposal.spend_tier == SpendTier.NORMAL
            else self.policy.exception_per_opportunity_limit_usd
        )
        if proposal.requested_spend_usd > per_opportunity_limit:
            return CapitalEvaluation(
                decision=CapitalDecision.REJECT,
                mode=mode,
                reason=(
                    f"Requested spend exceeds the {proposal.spend_tier.value} "
                    f"per-opportunity limit of ${per_opportunity_limit:.2f}."
                ),
                current_unrecovered_loss_usd=current_loss,
                projected_unrecovered_loss_usd=current_loss,
            )

        if (
            snapshot.today_acquisition_spend_usd + proposal.requested_spend_usd
            > self.policy.daily_acquisition_spend_limit_usd
        ):
            return CapitalEvaluation(
                decision=CapitalDecision.REJECT,
                mode=mode,
                reason="Requested spend would exceed the daily acquisition-spend ceiling.",
                current_unrecovered_loss_usd=current_loss,
                projected_unrecovered_loss_usd=current_loss,
            )

        projected_loss = round(current_loss + proposal.requested_spend_usd, 2)
        if projected_loss > self.policy.total_loss_ceiling_usd:
            return CapitalEvaluation(
                decision=CapitalDecision.REJECT,
                mode=mode,
                reason="Requested spend would exceed the $60 unrecovered-loss ceiling.",
                current_unrecovered_loss_usd=current_loss,
                projected_unrecovered_loss_usd=projected_loss,
            )

        if proposal.expected_contribution_usd <= 0:
            return CapitalEvaluation(
                decision=CapitalDecision.REJECT,
                mode=mode,
                reason="Expected contribution must be positive before paid acquisition is proposed.",
                current_unrecovered_loss_usd=current_loss,
                projected_unrecovered_loss_usd=projected_loss,
            )

        if mode == CapitalMode.CAUTION:
            required = round(
                proposal.requested_spend_usd
                * self.policy.caution_min_expected_contribution_multiple,
                2,
            )
            if proposal.expected_contribution_usd < required:
                return CapitalEvaluation(
                    decision=CapitalDecision.REJECT,
                    mode=mode,
                    reason=(
                        "Caution mode requires expected contribution of at least "
                        f"{self.policy.caution_min_expected_contribution_multiple:.1f}x "
                        "the proposed acquisition spend."
                    ),
                    current_unrecovered_loss_usd=current_loss,
                    projected_unrecovered_loss_usd=projected_loss,
                )

        return CapitalEvaluation(
            decision=CapitalDecision.PROPOSE_SPEND,
            mode=mode,
            reason="Economically eligible for exact creator approval; no spending authority granted.",
            current_unrecovered_loss_usd=current_loss,
            projected_unrecovered_loss_usd=projected_loss,
        )
