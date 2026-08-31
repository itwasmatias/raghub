"""Outbound effect boundary for MissionaryX Revenue Bridge v0.1.

This module defines the outbound effect dispatch and reconciliation architecture:
- Explicit separation from inbound BaseConnector (NO send/post methods on BaseConnector)
- Outbound AppEffectAdapter interface with capability declaration, dispatch, reconciliation
- Simulation adapters for safe testing and creator demos without live credentials
- Mandatory verification of creator approval before dispatch
- Full modeling of SOMETHING_LANDED, NOTHING_LANDED, and INDETERMINATE outcomes
- Rejection of blind retries on indeterminate effects until authoritative reconciliation

Invariants:
- Models propose. Tools execute. Evidence decides.
- Never blindly retry an indeterminate effect.
"""

from __future__ import annotations

import hashlib
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from revenue_bridge.approval import (
    ApprovalRequiredError,
    CreatorApprovalBoundary,
    CreatorApprovalRecord,
)
from revenue_bridge.events import _canonical_bytes, _require_text, _require_timestamp
from revenue_bridge.proposals import ActionProposal, ActionProposalState


class EffectState(str, Enum):
    """Outbound effect outcome state."""
    NOTHING_LANDED = "nothing_landed"
    SOMETHING_LANDED = "something_landed"
    INDETERMINATE = "indeterminate"


class EffectOutcomeMode(str, Enum):
    """Configured behavior for simulated adapters."""
    SUCCESS = "success"
    DEFINITE_REJECTION = "definite_rejection"
    CONNECTION_LOSS = "connection_loss"


class OutboundEffectError(Exception):
    """Base exception for outbound effect errors."""


class RefusedDispatchError(OutboundEffectError):
    """Raised when dispatch is refused due to safety, authority, or contract violation."""


class BlindRetryRefusedError(RefusedDispatchError):
    """Raised when attempting to blindly retry an effect that is in an indeterminate state."""


class CapabilityNotSupportedError(OutboundEffectError):
    """Raised when an adapter does not support the required capability."""


@dataclass(frozen=True, slots=True)
class ApprovedAppAction:
    """Action packaged with verified creator approval and write-ahead intent ID."""
    action_id: str
    proposal: ActionProposal
    approval: CreatorApprovalRecord
    idempotency_key: str
    effect_intent_id: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_id", _require_text(self.action_id, "action_id"))
        if not isinstance(self.proposal, ActionProposal):
            raise TypeError("proposal must be an ActionProposal")
        if not isinstance(self.approval, CreatorApprovalRecord):
            raise TypeError("approval must be a CreatorApprovalRecord")
        object.__setattr__(self, "idempotency_key", _require_text(self.idempotency_key, "idempotency_key"))
        object.__setattr__(self, "effect_intent_id", _require_text(self.effect_intent_id, "effect_intent_id"))
        object.__setattr__(self, "created_at", _require_timestamp(self.created_at, "created_at"))

    @property
    def action_fingerprint(self) -> str:
        payload = {
            "action_id": self.action_id,
            "proposal_fingerprint": self.proposal.proposal_fingerprint,
            "approval_fingerprint": self.approval.record_fingerprint,
            "idempotency_key": self.idempotency_key,
            "effect_intent_id": self.effect_intent_id,
        }
        return hashlib.sha256(_canonical_bytes(payload)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "proposal": self.proposal.to_dict(),
            "approval": self.approval.to_dict(),
            "idempotency_key": self.idempotency_key,
            "effect_intent_id": self.effect_intent_id,
            "created_at": self.created_at.isoformat(),
            "action_fingerprint": self.action_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class EffectExecutionResult:
    """Authoritative outcome of an outbound effect dispatch."""
    effect_intent_id: str
    action_id: str
    state: EffectState
    evidence_ref: str
    dispatched_at: datetime
    reconciliation_obligation_id: str | None = None
    details: tuple[tuple[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "effect_intent_id", _require_text(self.effect_intent_id, "effect_intent_id"))
        object.__setattr__(self, "action_id", _require_text(self.action_id, "action_id"))
        if not isinstance(self.state, EffectState):
            raise TypeError("state must be an EffectState enum")
        object.__setattr__(self, "evidence_ref", _require_text(self.evidence_ref, "evidence_ref"))
        object.__setattr__(self, "dispatched_at", _require_timestamp(self.dispatched_at, "dispatched_at"))
        if not isinstance(self.details, tuple):
            object.__setattr__(self, "details", tuple(self.details))

    @property
    def is_landed(self) -> bool:
        return self.state == EffectState.SOMETHING_LANDED

    @property
    def is_indeterminate(self) -> bool:
        return self.state == EffectState.INDETERMINATE

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect_intent_id": self.effect_intent_id,
            "action_id": self.action_id,
            "state": self.state.value,
            "evidence_ref": self.evidence_ref,
            "dispatched_at": self.dispatched_at.isoformat(),
            "reconciliation_obligation_id": self.reconciliation_obligation_id,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class EffectReconciliationResult:
    """Authoritative outcome of reconciling an indeterminate effect."""
    reconciliation_obligation_id: str
    effect_intent_id: str
    resolved_state: EffectState
    reconciled_at: datetime
    evidence_ref: str
    details: tuple[tuple[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "reconciliation_obligation_id", _require_text(self.reconciliation_obligation_id, "reconciliation_obligation_id"))
        object.__setattr__(self, "effect_intent_id", _require_text(self.effect_intent_id, "effect_intent_id"))
        if not isinstance(self.resolved_state, EffectState):
            raise TypeError("resolved_state must be an EffectState enum")
        if self.resolved_state == EffectState.INDETERMINATE:
            raise ValueError("Reconciliation result resolved_state cannot remain indeterminate")
        object.__setattr__(self, "evidence_ref", _require_text(self.evidence_ref, "evidence_ref"))
        object.__setattr__(self, "reconciled_at", _require_timestamp(self.reconciled_at, "reconciled_at"))
        if not isinstance(self.details, tuple):
            object.__setattr__(self, "details", tuple(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "reconciliation_obligation_id": self.reconciliation_obligation_id,
            "effect_intent_id": self.effect_intent_id,
            "resolved_state": self.resolved_state.value,
            "reconciled_at": self.reconciled_at.isoformat(),
            "evidence_ref": self.evidence_ref,
            "details": dict(self.details),
        }


class AppEffectAdapter(ABC):
    """Abstract interface for outbound application effect adapters.

    Distinct from inbound BaseConnector.
    """

    @abstractmethod
    def declared_capabilities(self) -> frozenset[str]:
        """Return the set of effect capabilities this adapter can handle."""
        pass

    @abstractmethod
    def dispatch(self, action: ApprovedAppAction) -> EffectExecutionResult:
        """Execute the approved action against the outbound effect boundary."""
        pass

    @abstractmethod
    def reconcile(self, effect_intent_id: str, idempotency_key: str) -> EffectReconciliationResult:
        """Query boundary to determine ground truth for an indeterminate intent."""
        pass


class SimulatedAppEffectAdapter(AppEffectAdapter):
    """Deterministic simulated adapter for tests, CI, and creator demo.

    Guarantees no live network calls or credential usage.
    """

    def __init__(
        self,
        adapter_name: str = "simulated_adapter",
        supported_capabilities: set[str] | None = None,
        default_mode: EffectOutcomeMode = EffectOutcomeMode.SUCCESS,
        approval_boundary: CreatorApprovalBoundary | None = None,
    ) -> None:
        self.adapter_name = adapter_name
        self._capabilities = frozenset(supported_capabilities or {
            "github.issue_comment.reply",
            "email.reply.send",
            "general.action",
        })
        self.mode = default_mode
        self.approval_boundary = approval_boundary or CreatorApprovalBoundary()

        # State stores
        self._dispatches: dict[str, EffectExecutionResult] = {}
        self._intents: dict[str, ApprovedAppAction] = {}
        self._indeterminate_obligations: dict[str, str] = {}  # intent_id -> obligation_id
        self._reconciled_outcomes: dict[str, EffectState] = {}  # intent_id -> resolved state

    def declared_capabilities(self) -> frozenset[str]:
        return self._capabilities

    def set_mode(self, mode: EffectOutcomeMode) -> None:
        self.mode = mode

    def configure_reconciliation_resolution(self, intent_id: str, state: EffectState) -> None:
        """Pre-configure ground truth that reconciliation probe will discover."""
        if state == EffectState.INDETERMINATE:
            raise ValueError("Reconciliation target cannot be indeterminate")
        self._reconciled_outcomes[intent_id] = state

    def dispatch(self, action: ApprovedAppAction) -> EffectExecutionResult:
        if not isinstance(action, ApprovedAppAction):
            raise TypeError("action must be an ApprovedAppAction")

        # 1. Check capability
        required_cap = action.proposal.required_capability
        if required_cap not in self._capabilities:
            raise CapabilityNotSupportedError(
                f"Adapter {self.adapter_name} does not support capability: {required_cap}"
            )

        # 2. Verify Creator Approval Boundary
        self.approval_boundary.verify_approval(action.proposal, action.approval)

        # 3. Check for Blind Retry on Indeterminate Intent
        intent_id = action.effect_intent_id
        if intent_id in self._indeterminate_obligations:
            # Obligation exists; check if it has been reconciled
            if intent_id not in self._reconciled_outcomes:
                raise BlindRetryRefusedError(
                    f"Refusing dispatch: Intent {intent_id} is in an INDETERMINATE state "
                    f"under reconciliation obligation {self._indeterminate_obligations[intent_id]}. "
                    f"MissionaryX strictly forbids blind retry on uncertain external effects. "
                    f"Reconcile boundary status first."
                )

        # 4. Check for Idempotent Dispatch
        if intent_id in self._dispatches and self._dispatches[intent_id].state == EffectState.SOMETHING_LANDED:
            # Already landed successfully, return existing result idempotently
            return self._dispatches[intent_id]

        now = datetime.now(timezone.utc)
        self._intents[intent_id] = action

        # 5. Execute according to configured outcome mode
        if self.mode == EffectOutcomeMode.SUCCESS:
            evidence_hash = hashlib.sha256(
                f"dispatch:{action.action_fingerprint}:{now.isoformat()}".encode("utf-8")
            ).hexdigest()
            result = EffectExecutionResult(
                effect_intent_id=intent_id,
                action_id=action.action_id,
                state=EffectState.SOMETHING_LANDED,
                evidence_ref=f"ev_landed_{evidence_hash[:16]}",
                dispatched_at=now,
                details=(
                    ("adapter", self.adapter_name),
                    ("status", "delivered_simulated"),
                    ("target", action.proposal.target_resource),
                ),
            )
        elif self.mode == EffectOutcomeMode.DEFINITE_REJECTION:
            evidence_hash = hashlib.sha256(
                f"rejection:{action.action_fingerprint}:{now.isoformat()}".encode("utf-8")
            ).hexdigest()
            result = EffectExecutionResult(
                effect_intent_id=intent_id,
                action_id=action.action_id,
                state=EffectState.NOTHING_LANDED,
                evidence_ref=f"ev_rejected_{evidence_hash[:16]}",
                dispatched_at=now,
                details=(
                    ("adapter", self.adapter_name),
                    ("status", "rejected_boundary_simulated"),
                    ("reason", "target_policy_refusal"),
                ),
            )
        elif self.mode == EffectOutcomeMode.CONNECTION_LOSS:
            obligation_id = f"ob_{uuid.uuid4().hex[:12]}"
            self._indeterminate_obligations[intent_id] = obligation_id
            evidence_hash = hashlib.sha256(
                f"indeterminate:{action.action_fingerprint}:{now.isoformat()}".encode("utf-8")
            ).hexdigest()
            result = EffectExecutionResult(
                effect_intent_id=intent_id,
                action_id=action.action_id,
                state=EffectState.INDETERMINATE,
                evidence_ref=f"ev_indet_{evidence_hash[:16]}",
                dispatched_at=now,
                reconciliation_obligation_id=obligation_id,
                details=(
                    ("adapter", self.adapter_name),
                    ("status", "connection_timeout_simulated"),
                    ("note", "effect may or may not have reached provider"),
                ),
            )
        else:
            raise ValueError(f"Unknown mode: {self.mode}")

        self._dispatches[intent_id] = result
        return result

    def reconcile(self, effect_intent_id: str, idempotency_key: str) -> EffectReconciliationResult:
        intent_id = _require_text(effect_intent_id, "effect_intent_id")
        key = _require_text(idempotency_key, "idempotency_key")

        obligation_id = self._indeterminate_obligations.get(intent_id)
        if not obligation_id:
            obligation_id = f"ob_reconciled_{intent_id}"

        # Determine resolved state (default to SOMETHING_LANDED if not explicitly configured)
        resolved_state = self._reconciled_outcomes.get(intent_id, EffectState.SOMETHING_LANDED)
        now = datetime.now(timezone.utc)

        evidence_hash = hashlib.sha256(
            f"reconcile:{intent_id}:{key}:{resolved_state.value}:{now.isoformat()}".encode("utf-8")
        ).hexdigest()

        recon_result = EffectReconciliationResult(
            reconciliation_obligation_id=obligation_id,
            effect_intent_id=intent_id,
            resolved_state=resolved_state,
            reconciled_at=now,
            evidence_ref=f"ev_recon_{evidence_hash[:16]}",
            details=(
                ("adapter", self.adapter_name),
                ("probe_method", "idempotency_key_lookup"),
                ("ground_truth_discovered", resolved_state.value),
            ),
        )

        # Clear active indeterminate obligation
        if intent_id in self._indeterminate_obligations:
            del self._indeterminate_obligations[intent_id]
        self._reconciled_outcomes[intent_id] = resolved_state

        # Update last dispatch state
        if intent_id in self._dispatches:
            orig = self._dispatches[intent_id]
            self._dispatches[intent_id] = EffectExecutionResult(
                effect_intent_id=orig.effect_intent_id,
                action_id=orig.action_id,
                state=resolved_state,
                evidence_ref=recon_result.evidence_ref,
                dispatched_at=orig.dispatched_at,
                reconciliation_obligation_id=obligation_id,
                details=orig.details + (("reconciled_to", resolved_state.value),),
            )

        return recon_result


class EffectAdapterRegistry:
    """Central registry of outbound effect adapters."""

    def __init__(self) -> None:
        self._adapters: dict[str, AppEffectAdapter] = {}

    def register(self, capability: str, adapter: AppEffectAdapter) -> None:
        if not isinstance(adapter, AppEffectAdapter):
            raise TypeError("adapter must inherit from AppEffectAdapter")
        self._adapters[capability] = adapter

    def get(self, capability: str) -> AppEffectAdapter:
        if capability not in self._adapters:
            raise CapabilityNotSupportedError(f"No outbound effect adapter registered for capability: {capability}")
        return self._adapters[capability]

    def has(self, capability: str) -> bool:
        return capability in self._adapters
