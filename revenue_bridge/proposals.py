"""Action proposal contract for MissionaryX Revenue Bridge v0.1.

This module defines proposals for external actions (replies, emails, follow-ups).

Key Invariants:
- AI/model output is always a PROPOSAL, never authority.
- Every external action proposal strictly requires creator approval.
- Proposal fingerprinting binds the exact proposed action, target, content, and capability.
- Changing material proposal content alters the fingerprint, invalidating any prior approval.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from revenue_bridge.events import AppEvent, _canonical_bytes, _require_text, _require_timestamp
from revenue_bridge.qualifier import RevenueQualification


class ActionProposalState(str, Enum):
    """Lifecycle state of an action proposal."""
    DRAFT = "draft"
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    INVALIDATED = "invalidated"
    DISPATCHED = "dispatched"


class ActionType(str, Enum):
    """Supported action types."""
    REPLY = "reply"
    EMAIL_RESPONSE = "email_response"
    FOLLOW_UP = "follow_up"


@dataclass(frozen=True, slots=True)
class ActionProposal:
    """Immutable proposal for an external action."""
    proposal_id: str
    event_id: str
    action_type: ActionType
    target_resource: str
    proposed_content: str
    reason: str
    required_capability: str
    evidence_refs: tuple[str, ...]
    requires_creator_approval: bool = True
    state: ActionProposalState = ActionProposalState.PROPOSED
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        object.__setattr__(self, "proposal_id", _require_text(self.proposal_id, "proposal_id"))
        object.__setattr__(self, "event_id", _require_text(self.event_id, "event_id"))
        if not isinstance(self.action_type, ActionType):
            raise TypeError("action_type must be an ActionType enum")
        object.__setattr__(self, "target_resource", _require_text(self.target_resource, "target_resource"))
        object.__setattr__(self, "proposed_content", _require_text(self.proposed_content, "proposed_content"))
        object.__setattr__(self, "reason", _require_text(self.reason, "reason"))
        object.__setattr__(self, "required_capability", _require_text(self.required_capability, "required_capability"))
        if not isinstance(self.evidence_refs, tuple):
            object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        if not isinstance(self.state, ActionProposalState):
            raise TypeError("state must be an ActionProposalState enum")
        object.__setattr__(self, "created_at", _require_timestamp(self.created_at, "created_at"))

        # Enforce hard safety invariant: external actions ALWAYS require creator approval
        if not self.requires_creator_approval:
            raise ValueError("Action proposals for external effects MUST require creator approval")

    @property
    def proposal_fingerprint(self) -> str:
        """Deterministic SHA-256 fingerprint binding the exact proposed action parameters."""
        payload = {
            "proposal_id": self.proposal_id,
            "event_id": self.event_id,
            "action_type": self.action_type.value,
            "target_resource": self.target_resource,
            "proposed_content": self.proposed_content,
            "reason": self.reason,
            "required_capability": self.required_capability,
            "evidence_refs": sorted(self.evidence_refs),
            "requires_creator_approval": self.requires_creator_approval,
        }
        return hashlib.sha256(_canonical_bytes(payload)).hexdigest()

    def with_state(self, new_state: ActionProposalState) -> ActionProposal:
        """Return a new ActionProposal with updated state (immutable transition)."""
        return ActionProposal(
            proposal_id=self.proposal_id,
            event_id=self.event_id,
            action_type=self.action_type,
            target_resource=self.target_resource,
            proposed_content=self.proposed_content,
            reason=self.reason,
            required_capability=self.required_capability,
            evidence_refs=self.evidence_refs,
            requires_creator_approval=self.requires_creator_approval,
            state=new_state,
            created_at=self.created_at,
        )

    def with_content(self, new_content: str) -> ActionProposal:
        """Return a new ActionProposal with modified content (changes fingerprint)."""
        return ActionProposal(
            proposal_id=self.proposal_id,
            event_id=self.event_id,
            action_type=self.action_type,
            target_resource=self.target_resource,
            proposed_content=new_content,
            reason=self.reason,
            required_capability=self.required_capability,
            evidence_refs=self.evidence_refs,
            requires_creator_approval=self.requires_creator_approval,
            state=ActionProposalState.PROPOSED,
            created_at=self.created_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "event_id": self.event_id,
            "action_type": self.action_type.value,
            "target_resource": self.target_resource,
            "proposed_content": self.proposed_content,
            "reason": self.reason,
            "required_capability": self.required_capability,
            "evidence_refs": list(self.evidence_refs),
            "requires_creator_approval": self.requires_creator_approval,
            "state": self.state.value,
            "created_at": self.created_at.isoformat(),
            "proposal_fingerprint": self.proposal_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ActionProposal:
        created_at = data["created_at"]
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

        return cls(
            proposal_id=data["proposal_id"],
            event_id=data["event_id"],
            action_type=ActionType(data["action_type"]),
            target_resource=data["target_resource"],
            proposed_content=data["proposed_content"],
            reason=data["reason"],
            required_capability=data["required_capability"],
            evidence_refs=tuple(data.get("evidence_refs", ())),
            requires_creator_approval=data.get("requires_creator_approval", True),
            state=ActionProposalState(data.get("state", ActionProposalState.PROPOSED)),
            created_at=created_at,
        )


def compile_action_proposal(
    event: AppEvent,
    qualification: RevenueQualification,
    proposal_id: str | None = None,
    custom_content: str | None = None,
) -> ActionProposal:
    """Compile a truthful, bounded action proposal for a qualified opportunity."""
    if not isinstance(event, AppEvent):
        raise TypeError("event must be an AppEvent")
    if not isinstance(qualification, RevenueQualification):
        raise TypeError("qualification must be a RevenueQualification")
    if qualification.event_id != event.event_id:
        raise ValueError(f"Qualification event_id {qualification.event_id} does not match event {event.event_id}")

    pid = proposal_id or f"prop_{event.event_id}"

    # Determine action type and capability based on source app
    if event.source_app == "github":
        action_type = ActionType.REPLY
        target_resource = event.source_url or f"github:thread/{event.thread_id}"
        required_capability = "github.issue_comment.reply"
    elif event.source_app == "email":
        action_type = ActionType.EMAIL_RESPONSE
        target_resource = f"email:{event.actor}:{event.thread_id}"
        required_capability = "email.reply.send"
    else:
        action_type = ActionType.FOLLOW_UP
        target_resource = f"{event.source_app}:{event.thread_id}"
        required_capability = f"{event.source_app}.action"

    offer = qualification.proposed_offer
    offer_name = offer.name if offer else "MissionaryX Reliability Check"
    offer_price = f"${int(offer.price_usd)}" if offer else "$50"
    offer_scope = offer.scope if offer else "one clearly bounded problem"

    if custom_content:
        proposed_content = custom_content.strip()
    else:
        # Construct truthful, respectful outreach referencing verified pain
        pain_summary = ", ".join(qualification.pain_categories) if qualification.pain_categories else "observed reliability friction"
        proposed_content = (
            f"Hello @{event.actor},\n\n"
            f"I noticed the issue you ran into regarding {pain_summary}.\n\n"
            f"We offer the {offer_name} ({offer_price} for {offer_scope}) where we analyze "
            f"the root cause and provide a deterministic safety fence with verified evidence.\n\n"
            f"If you would like us to take a look, let me know and we can coordinate."
        )

    evidence_refs = [
        f"event:{event.event_id}",
        f"raw:{event.raw_evidence_ref}",
        f"qualification:{qualification.fingerprint}",
    ]

    return ActionProposal(
        proposal_id=pid,
        event_id=event.event_id,
        action_type=action_type,
        target_resource=target_resource,
        proposed_content=proposed_content,
        reason=f"Engage qualified revenue lead ({event.actor}) with bounded {offer_name} offer",
        required_capability=required_capability,
        evidence_refs=tuple(evidence_refs),
        requires_creator_approval=True,
        state=ActionProposalState.PROPOSED,
    )
