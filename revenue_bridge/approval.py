"""Creator approval boundary for MissionaryX Revenue Bridge v0.1.

This module enforces the strict boundary between AI proposals and external effects:
- External effects CANNOT execute without affirmative creator approval.
- Approvals bind strictly to the proposal fingerprint (content, target, capability).
- Any material modification to the proposal invalidates prior approval.
- Approvals are recorded as immutable evidence.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from revenue_bridge.events import _canonical_bytes, _require_text, _require_timestamp
from revenue_bridge.proposals import ActionProposal, ActionProposalState


class ApprovalDecision(str, Enum):
    """Decision made by the human creator."""
    APPROVED = "approved"
    REJECTED = "rejected"


class CreatorApprovalError(Exception):
    """Base error for creator approval boundary violations."""


class ApprovalRequiredError(CreatorApprovalError):
    """Raised when an external effect is attempted without creator approval."""


class ApprovalInvalidatedError(CreatorApprovalError):
    """Raised when proposal content or parameters have changed after approval was granted."""


class ApprovalIdentityError(CreatorApprovalError):
    """Raised when creator identity is invalid or unauthorized."""


@dataclass(frozen=True, slots=True)
class CreatorApprovalRecord:
    """Immutable evidence record of a human creator's decision on a proposal."""
    approval_id: str
    proposal_id: str
    proposal_fingerprint: str
    creator_identity: str
    decision: ApprovalDecision
    approved_at: datetime
    scope_boundary: str
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "approval_id", _require_text(self.approval_id, "approval_id"))
        object.__setattr__(self, "proposal_id", _require_text(self.proposal_id, "proposal_id"))
        object.__setattr__(self, "proposal_fingerprint", _require_text(self.proposal_fingerprint, "proposal_fingerprint"))
        object.__setattr__(self, "creator_identity", _require_text(self.creator_identity, "creator_identity"))
        if not isinstance(self.decision, ApprovalDecision):
            raise TypeError("decision must be an ApprovalDecision enum")
        object.__setattr__(self, "approved_at", _require_timestamp(self.approved_at, "approved_at"))
        object.__setattr__(self, "scope_boundary", _require_text(self.scope_boundary, "scope_boundary"))

    @property
    def is_approved(self) -> bool:
        return self.decision == ApprovalDecision.APPROVED

    @property
    def record_fingerprint(self) -> str:
        payload = {
            "approval_id": self.approval_id,
            "proposal_id": self.proposal_id,
            "proposal_fingerprint": self.proposal_fingerprint,
            "creator_identity": self.creator_identity,
            "decision": self.decision.value,
            "approved_at": self.approved_at.isoformat(),
            "scope_boundary": self.scope_boundary,
            "reason": self.reason,
        }
        return hashlib.sha256(_canonical_bytes(payload)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "proposal_id": self.proposal_id,
            "proposal_fingerprint": self.proposal_fingerprint,
            "creator_identity": self.creator_identity,
            "decision": self.decision.value,
            "approved_at": self.approved_at.isoformat(),
            "scope_boundary": self.scope_boundary,
            "reason": self.reason,
            "record_fingerprint": self.record_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CreatorApprovalRecord:
        approved_at = data["approved_at"]
        if isinstance(approved_at, str):
            approved_at = datetime.fromisoformat(approved_at.replace("Z", "+00:00"))

        return cls(
            approval_id=data["approval_id"],
            proposal_id=data["proposal_id"],
            proposal_fingerprint=data["proposal_fingerprint"],
            creator_identity=data["creator_identity"],
            decision=ApprovalDecision(data["decision"]),
            approved_at=approved_at,
            scope_boundary=data["scope_boundary"],
            reason=data.get("reason", ""),
        )


class CreatorApprovalBoundary:
    """Enforces authorization boundary for action execution."""

    def __init__(self, authorized_creators: set[str] | None = None) -> None:
        self.authorized_creators = authorized_creators or {"creator:matias", "creator:operator", "creator:admin"}
        self._ledger: dict[str, CreatorApprovalRecord] = {}

    def grant_approval(
        self,
        proposal: ActionProposal,
        creator_identity: str,
        reason: str = "Approved for dispatch",
        approval_id: str | None = None,
        approved_at: datetime | None = None,
    ) -> CreatorApprovalRecord:
        """Issue affirmative creator approval for an exact proposal fingerprint."""
        if not isinstance(proposal, ActionProposal):
            raise TypeError("proposal must be an ActionProposal")

        creator_clean = _require_text(creator_identity, "creator_identity")
        if not creator_clean.startswith("creator:") and creator_clean not in self.authorized_creators:
            raise ApprovalIdentityError(f"Unauthorized creator identity: {creator_clean}")

        now = approved_at or datetime.now(timezone.utc)
        aid = approval_id or f"appr_{uuid.uuid4().hex[:12]}"
        scope = f"target={proposal.target_resource};cap={proposal.required_capability}"

        record = CreatorApprovalRecord(
            approval_id=aid,
            proposal_id=proposal.proposal_id,
            proposal_fingerprint=proposal.proposal_fingerprint,
            creator_identity=creator_clean,
            decision=ApprovalDecision.APPROVED,
            approved_at=now,
            scope_boundary=scope,
            reason=reason,
        )
        self._ledger[record.approval_id] = record
        return record

    def reject_proposal(
        self,
        proposal: ActionProposal,
        creator_identity: str,
        reason: str = "Rejected by creator",
        approval_id: str | None = None,
        approved_at: datetime | None = None,
    ) -> CreatorApprovalRecord:
        """Record explicit rejection of a proposal."""
        if not isinstance(proposal, ActionProposal):
            raise TypeError("proposal must be an ActionProposal")

        creator_clean = _require_text(creator_identity, "creator_identity")
        now = approved_at or datetime.now(timezone.utc)
        aid = approval_id or f"appr_{uuid.uuid4().hex[:12]}"
        scope = f"target={proposal.target_resource};cap={proposal.required_capability}"

        record = CreatorApprovalRecord(
            approval_id=aid,
            proposal_id=proposal.proposal_id,
            proposal_fingerprint=proposal.proposal_fingerprint,
            creator_identity=creator_clean,
            decision=ApprovalDecision.REJECTED,
            approved_at=now,
            scope_boundary=scope,
            reason=reason,
        )
        self._ledger[record.approval_id] = record
        return record

    def verify_approval(
        self,
        proposal: ActionProposal,
        approval: CreatorApprovalRecord | None,
    ) -> bool:
        """Verify that an approval record authentically authorizes the given proposal.

        Raises:
            ApprovalRequiredError: If approval record is missing or decision is not APPROVED.
            ApprovalInvalidatedError: If proposal fingerprint does not match approval record.
        """
        if approval is None:
            raise ApprovalRequiredError(
                f"Cannot execute action for proposal {proposal.proposal_id}: "
                "No creator approval record provided. External effects require explicit creator approval."
            )

        if not isinstance(approval, CreatorApprovalRecord):
            raise TypeError("approval must be a CreatorApprovalRecord")

        if approval.proposal_id != proposal.proposal_id:
            raise ApprovalInvalidatedError(
                f"Approval record {approval.approval_id} is for proposal {approval.proposal_id}, "
                f"not {proposal.proposal_id}"
            )

        if approval.decision != ApprovalDecision.APPROVED:
            raise ApprovalRequiredError(
                f"Proposal {proposal.proposal_id} was not approved (decision: {approval.decision.value})"
            )

        current_fp = proposal.proposal_fingerprint
        if approval.proposal_fingerprint != current_fp:
            raise ApprovalInvalidatedError(
                f"Approval {approval.approval_id} is INVALIDATED. The proposal content/target/capability "
                f"has changed since approval was given.\n"
                f"Approved fingerprint: {approval.proposal_fingerprint}\n"
                f"Current fingerprint:  {current_fp}"
            )

        return True

    def get_approval(self, approval_id: str) -> CreatorApprovalRecord | None:
        return self._ledger.get(approval_id)
