"""Effect safety spine extending effect boundary with deeper state primitives.

M6 Execution Prompt Engine v0.1 — Effect Safety Spine

This module EXTENDS the existing effect_boundary.py (M4) with:
- Write-ahead intent commitment (before dispatch)
- Separate dispatch attempt tracking
- Effect states: nothing_landed, something_landed, indeterminate
- Authority reservation and terminal disposition
- Reconciliation obligations
- Provider capability contracts
- Mission posture and lifecycle folding over effect states

Design principle: EXTEND, not replace. Builds on effect_boundary abstractions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TYPE_CHECKING

from federation.effect_boundary import EffectRequest, EffectDecision

if TYPE_CHECKING:
    from research_mission.evidence_spine import EvidencePointer, EvidenceSpine


def _canonical(value: Any) -> bytes:
    """Canonical JSON serialization for fingerprinting."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fingerprint(value: Any) -> str:
    """SHA-256 fingerprint of canonical representation."""
    return hashlib.sha256(_canonical(value)).hexdigest()


def _require_text(value: Any, field: str) -> str:
    """Require non-empty trimmed string."""
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _require_timestamp(value: Any, field: str) -> datetime:
    """Require timezone-aware datetime."""
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _verify_terminal_decision_evidence(
    evidence_spine: "EvidenceSpine",
    evidence_pointer: "EvidencePointer",
    expected_effect_intent_id: str,
    expected_reservation_id: str | None = None,
    expected_obligation_id: str | None = None,
    expected_dispatch_id: str | None = None,
) -> "EvidenceRecord":
    """Verify terminal decision evidence from spine with semantic binding.

    Terminal unresolved authority disposition requires:
    - Verified evidence from authoritative EvidenceSpine
    - Semantic binding to exact effect, reservation, obligation, dispatch
    - Correct decision type and disposition

    Returns:
        Verified EvidenceRecord

    Raises:
        TypeError: If spine or pointer are not correct types
        ValueError: If semantic binding validation fails
        EvidenceSpineError: If evidence verification fails
    """
    from research_mission.evidence_spine import EvidencePointer, EvidenceSpine

    if type(evidence_spine) is not EvidenceSpine:
        raise TypeError("evidence_spine must be an EvidenceSpine")
    if type(evidence_pointer) is not EvidencePointer:
        raise TypeError("evidence_pointer must be an EvidencePointer")

    expected_effect_intent_id = _require_text(expected_effect_intent_id, "expected_effect_intent_id")

    # Verify evidence exists in spine and fingerprints match
    record = evidence_spine.verify_evidence(evidence_pointer)

    # Verify evidence source is a terminal effect decision
    if not record.key.source.startswith("terminal_effect_decision_"):
        raise ValueError(
            f"Evidence must be a terminal_effect_decision source, got {record.key.source!r}"
        )

    # Extract and validate decision type
    decision_type = record.metadata.get("decision_type")
    if decision_type != "assume_consumed_unreconciled":
        raise ValueError(
            f"Evidence decision_type must be 'assume_consumed_unreconciled', "
            f"got {decision_type!r}"
        )

    # Validate disposition
    disposition = record.metadata.get("disposition")
    if disposition != "assumed_consumed_unreconciled":
        raise ValueError(
            f"Evidence disposition must be 'assumed_consumed_unreconciled', "
            f"got {disposition!r}"
        )

    # Validate semantic binding to effect_intent_id
    recorded_effect_intent_id = record.metadata.get("effect_intent_id")
    if recorded_effect_intent_id != expected_effect_intent_id:
        raise ValueError(
            f"Evidence effect_intent_id mismatch: expected {expected_effect_intent_id!r}, "
            f"found {recorded_effect_intent_id!r}"
        )

    # Validate semantic binding to reservation_id if expected
    if expected_reservation_id is not None:
        expected_reservation_id = _require_text(expected_reservation_id, "expected_reservation_id")
        recorded_reservation_id = record.metadata.get("reservation_id")
        if recorded_reservation_id != expected_reservation_id:
            raise ValueError(
                f"Evidence reservation_id mismatch: expected {expected_reservation_id!r}, "
                f"found {recorded_reservation_id!r}"
            )

    # Validate semantic binding to obligation_id if expected
    if expected_obligation_id is not None:
        expected_obligation_id = _require_text(expected_obligation_id, "expected_obligation_id")
        recorded_obligation_id = record.metadata.get("obligation_id")
        if recorded_obligation_id != expected_obligation_id:
            raise ValueError(
                f"Evidence obligation_id mismatch: expected {expected_obligation_id!r}, "
                f"found {recorded_obligation_id!r}"
            )

    # Validate semantic binding to dispatch_id if expected
    if expected_dispatch_id is not None:
        expected_dispatch_id = _require_text(expected_dispatch_id, "expected_dispatch_id")
        recorded_dispatch_id = record.metadata.get("dispatch_id")
        if recorded_dispatch_id != expected_dispatch_id:
            raise ValueError(
                f"Evidence dispatch_id mismatch: expected {expected_dispatch_id!r}, "
                f"found {recorded_dispatch_id!r}"
            )

    return record


# ═══════════════════════════════════════════════════════════════════════════
# Effect State Enumerations
# ═══════════════════════════════════════════════════════════════════════════

class EffectState(str, Enum):
    """Posture of an external effect relative to the boundary.

    - NOTHING_LANDED: Affirmative proof no provider operation committed
    - SOMETHING_LANDED: External change committed (even if task later failed)
    - INDETERMINATE: Request may have escaped without reliable confirmation
    """
    NOTHING_LANDED = "nothing_landed"
    SOMETHING_LANDED = "something_landed"
    INDETERMINATE = "indeterminate"


class AuthorityDisposition(str, Enum):
    """Disposition of authority reservation.

    - RESERVED: Held, not yet consumed or released
    - CONSUMED: Charged against effect that landed
    - RELEASED: Freed after affirmative nothing_landed proof
    - ASSUMED_CONSUMED_UNRECONCILED: Terminal conservative disposition
    """
    RESERVED = "reserved"
    CONSUMED = "consumed"
    RELEASED = "released"
    ASSUMED_CONSUMED_UNRECONCILED = "assumed_consumed_unreconciled"


class ReconciliationState(str, Enum):
    """State of reconciliation obligation.

    - NOT_REQUIRED: Effect resolved without reconciliation
    - PENDING: Awaiting first probe
    - IN_PROGRESS: Active reconciliation
    - RESOLVED: Successfully reconciled
    - ESCALATED: Requires policy/operator decision
    """
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class ProviderReconcilability(str, Enum):
    """How provider supports effect reconciliation.

    - IDEMPOTENCY_KEY_LOOKUP: Can query by idempotency key
    - PROVIDER_OPERATION_LOOKUP: Can query by provider operation ID
    - BOUNDED_EXTERNAL_OBSERVATION: Limited external verification
    - NONE: Not reconcilable (requires bounded consequence policy)
    """
    IDEMPOTENCY_KEY_LOOKUP = "idempotency_key_lookup"
    PROVIDER_OPERATION_LOOKUP = "provider_operation_lookup"
    BOUNDED_EXTERNAL_OBSERVATION = "bounded_external_observation"
    NONE = "none"


class MissionPosture(str, Enum):
    """Mission state projected from effect safety fold.

    - CLEAN_ACTIVE: Running with all effects resolved
    - CLEAN_SUCCEEDED: All tasks succeeded with intended effects
    - CLEAN_FAILED: All failures with verified nothing_landed
    - PARKED_RECONCILIATION: Has indeterminate effects
    - DIRTY_COMPENSATION_REQUIRED: Uncompensated landed effects from failed tasks
    - COMPENSATING: Compensation in progress
    """
    CLEAN_ACTIVE = "clean_active"
    CLEAN_SUCCEEDED = "clean_succeeded"
    CLEAN_FAILED = "clean_failed"
    PARKED_RECONCILIATION = "parked_reconciliation"
    DIRTY_COMPENSATION_REQUIRED = "dirty_compensation_required"
    COMPENSATING = "compensating"


class MissionLifecycle(str, Enum):
    """Mission lifecycle independent of posture.

    - OPEN: Tasks may still execute
    - CLOSED: Task execution ended (effects may be unsettled)
    - SEALED: All effects settled, evidence committed, immutable
    """
    OPEN = "open"
    CLOSED = "closed"
    SEALED = "sealed"


# ═══════════════════════════════════════════════════════════════════════════
# Domain Models
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class EffectIntent:
    """Write-ahead intent commitment before dispatch.

    Committed before any request can escape the effect boundary.
    A committed intent with no dispatch-attempt record is not a provider
    reconciliation obligation.
    """
    effect_intent_id: str
    decision_id: str  # Links to EffectDecision
    mission_id: str
    task_id: str | None
    attempt_id: str
    operation_digest: str  # Canonical digest of operation + params
    idempotency_key: str
    provider_scope: str
    authority_reservation_id: str
    compensation_strategy: str | None
    evidence_reference: str
    state: str  # Initially "committed_not_dispatched"
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "effect_intent_id",
            "decision_id",
            "mission_id",
            "attempt_id",
            "operation_digest",
            "idempotency_key",
            "provider_scope",
            "authority_reservation_id",
            "evidence_reference",
            "state",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name),
            )
        if self.task_id is not None:
            object.__setattr__(self, "task_id", _require_text(self.task_id, "task_id"))
        if self.compensation_strategy is not None:
            object.__setattr__(
                self,
                "compensation_strategy",
                _require_text(self.compensation_strategy, "compensation_strategy"),
            )
        object.__setattr__(self, "created_at", _require_timestamp(self.created_at, "created_at"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect_intent_id": self.effect_intent_id,
            "decision_id": self.decision_id,
            "mission_id": self.mission_id,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "operation_digest": self.operation_digest,
            "idempotency_key": self.idempotency_key,
            "provider_scope": self.provider_scope,
            "authority_reservation_id": self.authority_reservation_id,
            "compensation_strategy": self.compensation_strategy,
            "evidence_reference": self.evidence_reference,
            "state": self.state,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class EffectDispatch:
    """Dispatch attempt record when transport is attempted.

    Created only when transport is actually attempted. Tracks whether the
    request crossed the boundary and what confirmation was received.
    """
    dispatch_id: str
    effect_intent_id: str
    attempt_id: str
    idempotency_key: str
    provider_adapter: str
    capability_profile_version: str
    transport_digest: str
    posture: str  # attempting, accepted_by_transport, rejected_before_send, transport_outcome_unknown
    provider_operation_id: str | None
    evidence_reference: str
    dispatched_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "dispatch_id",
            "effect_intent_id",
            "attempt_id",
            "idempotency_key",
            "provider_adapter",
            "capability_profile_version",
            "transport_digest",
            "posture",
            "evidence_reference",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name),
            )
        if self.provider_operation_id is not None:
            object.__setattr__(
                self,
                "provider_operation_id",
                _require_text(self.provider_operation_id, "provider_operation_id"),
            )
        object.__setattr__(
            self,
            "dispatched_at",
            _require_timestamp(self.dispatched_at, "dispatched_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "effect_intent_id": self.effect_intent_id,
            "attempt_id": self.attempt_id,
            "idempotency_key": self.idempotency_key,
            "provider_adapter": self.provider_adapter,
            "capability_profile_version": self.capability_profile_version,
            "transport_digest": self.transport_digest,
            "posture": self.posture,
            "provider_operation_id": self.provider_operation_id,
            "evidence_reference": self.evidence_reference,
            "dispatched_at": self.dispatched_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class AuthorityReservation:
    """Authority slice reserved for an effect.

    Tracks the lifecycle of authority from reservation through terminal
    disposition. Held while indeterminate, consumed on something_landed,
    released on nothing_landed.

    Terminal dispositions (ASSUMED_CONSUMED_UNRECONCILED) must be created
    through from_verified_terminal_decision() to ensure verified evidence.
    """
    reservation_id: str
    effect_intent_id: str
    capability_type: str
    amount: float
    disposition: AuthorityDisposition
    reserved_at: datetime
    disposition_at: datetime | None
    disposition_evidence: "EvidencePointer | None"
    _allow_terminal: bool = False  # Internal flag for verified factory

    def __post_init__(self) -> None:
        from research_mission.evidence_spine import EvidencePointer

        for field_name in ("reservation_id", "effect_intent_id", "capability_type"):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name),
            )
        if not isinstance(self.amount, (int, float)) or self.amount < 0:
            raise ValueError("amount must be non-negative number")
        if not isinstance(self.disposition, AuthorityDisposition):
            raise TypeError("disposition must be an AuthorityDisposition")
        object.__setattr__(
            self,
            "reserved_at",
            _require_timestamp(self.reserved_at, "reserved_at"),
        )
        if self.disposition_at is not None:
            object.__setattr__(
                self,
                "disposition_at",
                _require_timestamp(self.disposition_at, "disposition_at"),
            )

        # Terminal disposition must be created through verified factory
        if self.disposition == AuthorityDisposition.ASSUMED_CONSUMED_UNRECONCILED:
            if not self._allow_terminal:
                raise ValueError(
                    "ASSUMED_CONSUMED_UNRECONCILED disposition must be created through "
                    "AuthorityReservation.from_verified_terminal_decision() "
                    "to ensure verified evidence spine and semantic binding"
                )
            if self.disposition_evidence is None:
                raise ValueError(
                    "ASSUMED_CONSUMED_UNRECONCILED disposition requires verified "
                    "operator/policy decision evidence (cannot be None)"
                )
            if type(self.disposition_evidence) is not EvidencePointer:
                raise TypeError("disposition_evidence must be an EvidencePointer")
        elif self.disposition_evidence is not None:
            # For other dispositions, validate it's an EvidencePointer if present
            if type(self.disposition_evidence) is not EvidencePointer:
                raise TypeError("disposition_evidence must be an EvidencePointer or None")

        # Remove internal flag from instance
        object.__delattr__(self, "_allow_terminal")

    @classmethod
    def from_verified_terminal_decision(
        cls,
        reservation_id: str,
        effect_intent_id: str,
        capability_type: str,
        amount: float,
        reserved_at: datetime,
        disposition_at: datetime,
        evidence_spine: "EvidenceSpine",
        evidence_pointer: "EvidencePointer",
    ) -> "AuthorityReservation":
        """Create ASSUMED_CONSUMED_UNRECONCILED reservation from verified evidence.

        This is the ONLY valid path to create a terminal conservative disposition.
        Requires verified terminal decision evidence from authoritative spine.

        Args:
            reservation_id: Reservation ID
            effect_intent_id: Effect intent ID
            capability_type: Capability type
            amount: Authority amount
            reserved_at: When reservation was created
            disposition_at: When terminal disposition was decided
            evidence_spine: Authoritative evidence spine
            evidence_pointer: Pointer to verified terminal decision evidence

        Returns:
            AuthorityReservation with ASSUMED_CONSUMED_UNRECONCILED disposition

        Raises:
            TypeError: If spine or pointer are not correct types
            ValueError: If semantic binding validation fails
            EvidenceSpineError: If evidence verification fails
        """
        # Verify evidence through spine with semantic binding
        _verify_terminal_decision_evidence(
            evidence_spine=evidence_spine,
            evidence_pointer=evidence_pointer,
            expected_effect_intent_id=effect_intent_id,
            expected_reservation_id=reservation_id,
        )

        # Evidence verified - create terminal reservation
        return cls(
            reservation_id=reservation_id,
            effect_intent_id=effect_intent_id,
            capability_type=capability_type,
            amount=amount,
            disposition=AuthorityDisposition.ASSUMED_CONSUMED_UNRECONCILED,
            reserved_at=reserved_at,
            disposition_at=disposition_at,
            disposition_evidence=evidence_pointer,
            _allow_terminal=True,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "effect_intent_id": self.effect_intent_id,
            "capability_type": self.capability_type,
            "amount": self.amount,
            "disposition": self.disposition.value,
            "reserved_at": self.reserved_at.isoformat(),
            "disposition_at": None if self.disposition_at is None else self.disposition_at.isoformat(),
            "disposition_evidence": (
                None if self.disposition_evidence is None
                else self.disposition_evidence.to_dict()
            ),
        }


@dataclass(frozen=True, slots=True)
class ReconciliationObligation:
    """Durable background reconciliation obligation.

    Created when an effect becomes indeterminate. Tracks probe history
    and next probe time. Owned by control plane, not task execution.

    Terminal dispositions must be created through from_verified_terminal_decision()
    to ensure verified evidence. A reconciliation with terminal disposition
    must remain ESCALATED, not RESOLVED.
    """
    obligation_id: str
    effect_intent_id: str
    dispatch_id: str | None
    state: ReconciliationState
    provider_reconcilability: ProviderReconcilability
    next_probe_at: datetime | None
    probe_history: tuple[dict[str, Any], ...]
    terminal_disposition: "EvidencePointer | None"
    created_at: datetime
    _allow_terminal: bool = False  # Internal flag for verified factory

    def __post_init__(self) -> None:
        from research_mission.evidence_spine import EvidencePointer

        for field_name in ("obligation_id", "effect_intent_id"):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name),
            )
        if self.dispatch_id is not None:
            object.__setattr__(
                self,
                "dispatch_id",
                _require_text(self.dispatch_id, "dispatch_id"),
            )
        if not isinstance(self.state, ReconciliationState):
            raise TypeError("state must be a ReconciliationState")
        if not isinstance(self.provider_reconcilability, ProviderReconcilability):
            raise TypeError("provider_reconcilability must be a ProviderReconcilability")
        if self.next_probe_at is not None:
            object.__setattr__(
                self,
                "next_probe_at",
                _require_timestamp(self.next_probe_at, "next_probe_at"),
            )
        if not isinstance(self.probe_history, tuple):
            raise TypeError("probe_history must be a tuple")

        # Terminal disposition must be created through verified factory
        if self.terminal_disposition is not None:
            if not self._allow_terminal:
                raise ValueError(
                    "terminal_disposition must be created through "
                    "ReconciliationObligation.from_verified_terminal_decision() "
                    "to ensure verified evidence spine and semantic binding"
                )
            if type(self.terminal_disposition) is not EvidencePointer:
                raise TypeError("terminal_disposition must be an EvidencePointer")
            # Terminal disposition means ESCALATED, not RESOLVED
            if self.state != ReconciliationState.ESCALATED:
                raise ValueError(
                    f"ReconciliationObligation with terminal_disposition must be ESCALATED, "
                    f"got {self.state.value}"
                )

        # Remove internal flag from instance
        object.__delattr__(self, "_allow_terminal")

        object.__setattr__(
            self,
            "created_at",
            _require_timestamp(self.created_at, "created_at"),
        )

    @classmethod
    def from_verified_terminal_decision(
        cls,
        obligation_id: str,
        effect_intent_id: str,
        dispatch_id: str | None,
        provider_reconcilability: ProviderReconcilability,
        probe_history: tuple[dict[str, Any], ...],
        created_at: datetime,
        evidence_spine: "EvidenceSpine",
        evidence_pointer: "EvidencePointer",
    ) -> "ReconciliationObligation":
        """Create ESCALATED obligation with terminal disposition from verified evidence.

        This is the ONLY valid path to create a terminal disposition on a
        reconciliation obligation. The obligation must be ESCALATED and remain
        visibly unresolved.

        Args:
            obligation_id: Obligation ID
            effect_intent_id: Effect intent ID
            dispatch_id: Dispatch ID (if dispatch occurred)
            provider_reconcilability: Provider reconcilability
            probe_history: History of reconciliation probes
            created_at: When obligation was created
            evidence_spine: Authoritative evidence spine
            evidence_pointer: Pointer to verified terminal decision evidence

        Returns:
            ReconciliationObligation with terminal_disposition and ESCALATED state

        Raises:
            TypeError: If spine or pointer are not correct types
            ValueError: If semantic binding validation fails
            EvidenceSpineError: If evidence verification fails
        """
        # Verify evidence through spine with semantic binding
        _verify_terminal_decision_evidence(
            evidence_spine=evidence_spine,
            evidence_pointer=evidence_pointer,
            expected_effect_intent_id=effect_intent_id,
            expected_obligation_id=obligation_id,
            expected_dispatch_id=dispatch_id,
        )

        # Evidence verified - create terminal obligation
        return cls(
            obligation_id=obligation_id,
            effect_intent_id=effect_intent_id,
            dispatch_id=dispatch_id,
            state=ReconciliationState.ESCALATED,  # Must be ESCALATED
            provider_reconcilability=provider_reconcilability,
            next_probe_at=None,  # No more probes
            probe_history=probe_history,
            terminal_disposition=evidence_pointer,
            created_at=created_at,
            _allow_terminal=True,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "effect_intent_id": self.effect_intent_id,
            "dispatch_id": self.dispatch_id,
            "state": self.state.value,
            "provider_reconcilability": self.provider_reconcilability.value,
            "next_probe_at": None if self.next_probe_at is None else self.next_probe_at.isoformat(),
            "probe_history": list(self.probe_history),
            "terminal_disposition": (
                None if self.terminal_disposition is None
                else self.terminal_disposition.to_dict()
            ),
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    """Versioned provider effect capability profile.

    Routing eligibility constraint, not a provider benchmark.
    A provider with no reconcilability is permitted only for effects
    proven blind-retry-safe or bounded by explicit policy.
    """
    provider_id: str
    adapter_version: str
    operation_family: str
    idempotency_semantics: dict[str, Any]
    reconcilability: ProviderReconcilability
    confirmation_guarantees: dict[str, Any]
    blind_retry_safe: bool
    max_bounded_consequence: dict[str, Any] | None
    compensation_support: bool
    tested_at: datetime
    evidence: str

    def __post_init__(self) -> None:
        for field_name in (
            "provider_id",
            "adapter_version",
            "operation_family",
            "evidence",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name),
            )
        if not isinstance(self.idempotency_semantics, dict):
            raise TypeError("idempotency_semantics must be a dict")
        if not isinstance(self.reconcilability, ProviderReconcilability):
            raise TypeError("reconcilability must be a ProviderReconcilability")
        if not isinstance(self.confirmation_guarantees, dict):
            raise TypeError("confirmation_guarantees must be a dict")
        if not isinstance(self.blind_retry_safe, bool):
            raise TypeError("blind_retry_safe must be a bool")
        if self.max_bounded_consequence is not None and not isinstance(self.max_bounded_consequence, dict):
            raise TypeError("max_bounded_consequence must be a dict or None")
        if not isinstance(self.compensation_support, bool):
            raise TypeError("compensation_support must be a bool")
        object.__setattr__(
            self,
            "tested_at",
            _require_timestamp(self.tested_at, "tested_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "adapter_version": self.adapter_version,
            "operation_family": self.operation_family,
            "idempotency_semantics": dict(self.idempotency_semantics),
            "reconcilability": self.reconcilability.value,
            "confirmation_guarantees": dict(self.confirmation_guarantees),
            "blind_retry_safe": self.blind_retry_safe,
            "max_bounded_consequence": (
                None if self.max_bounded_consequence is None else dict(self.max_bounded_consequence)
            ),
            "compensation_support": self.compensation_support,
            "tested_at": self.tested_at.isoformat(),
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class MissionEffectFold:
    """Mission state projected from effect posture.

    Mission lifecycle is separate from posture. A mission can close when
    task execution ends even while effects are unsettled. A mission can
    seal only when no indeterminate effects remain and all required
    compensation is settled.
    """
    mission_id: str
    posture: MissionPosture
    lifecycle: MissionLifecycle
    indeterminate_effects: tuple[str, ...]  # effect_intent_ids
    uncompensated_landed_effects: tuple[str, ...]
    active_reconciliation_obligations: tuple[str, ...]
    folded_at: datetime
    seal_digest: str | None  # Evidence/ledger digest when sealed

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "mission_id",
            _require_text(self.mission_id, "mission_id"),
        )
        if not isinstance(self.posture, MissionPosture):
            raise TypeError("posture must be a MissionPosture")
        if not isinstance(self.lifecycle, MissionLifecycle):
            raise TypeError("lifecycle must be a MissionLifecycle")
        if not isinstance(self.indeterminate_effects, tuple):
            raise TypeError("indeterminate_effects must be a tuple")
        if not isinstance(self.uncompensated_landed_effects, tuple):
            raise TypeError("uncompensated_landed_effects must be a tuple")
        if not isinstance(self.active_reconciliation_obligations, tuple):
            raise TypeError("active_reconciliation_obligations must be a tuple")
        object.__setattr__(
            self,
            "folded_at",
            _require_timestamp(self.folded_at, "folded_at"),
        )
        if self.seal_digest is not None:
            object.__setattr__(
                self,
                "seal_digest",
                _require_text(self.seal_digest, "seal_digest"),
            )

        # FINDING 2 REMEDIATION: Enforce mission sealing constraints
        if self.lifecycle == MissionLifecycle.SEALED:
            # SEALED missions must have no indeterminate effects
            if len(self.indeterminate_effects) > 0:
                raise ValueError(
                    "Cannot seal mission with indeterminate effects: "
                    f"{len(self.indeterminate_effects)} indeterminate effect(s) remain"
                )
            # SEALED missions must have no uncompensated landed effects
            if len(self.uncompensated_landed_effects) > 0:
                raise ValueError(
                    "Cannot seal mission with uncompensated landed effects: "
                    f"{len(self.uncompensated_landed_effects)} uncompensated effect(s) remain"
                )
            # SEALED missions must have seal_digest
            if self.seal_digest is None:
                raise ValueError("Cannot seal mission without seal_digest")
        else:
            # Non-SEALED missions must not have seal_digest
            if self.seal_digest is not None:
                raise ValueError(
                    f"Mission in lifecycle {self.lifecycle.value} must not have seal_digest"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "posture": self.posture.value,
            "lifecycle": self.lifecycle.value,
            "indeterminate_effects": list(self.indeterminate_effects),
            "uncompensated_landed_effects": list(self.uncompensated_landed_effects),
            "active_reconciliation_obligations": list(self.active_reconciliation_obligations),
            "folded_at": self.folded_at.isoformat(),
            "seal_digest": self.seal_digest,
        }


# ═══════════════════════════════════════════════════════════════════════════
# State Derivation Logic
# ═══════════════════════════════════════════════════════════════════════════

def derive_effect_state(
    dispatch_posture: str,
    boundary_evidence: dict[str, Any],
) -> EffectState:
    """Derive effect state from dispatch posture and boundary evidence.

    - nothing_landed requires affirmative boundary evidence
    - something_landed when provider confirmed commit
    - indeterminate when request may have escaped without confirmation
    """
    if dispatch_posture == "rejected_before_send":
        if boundary_evidence.get("confirmed") == "no_provider_operation":
            return EffectState.NOTHING_LANDED
    if dispatch_posture == "accepted_by_transport":
        if boundary_evidence.get("confirmed") == "provider_committed":
            return EffectState.SOMETHING_LANDED
    if dispatch_posture == "transport_outcome_unknown":
        return EffectState.INDETERMINATE
    return EffectState.INDETERMINATE


def resolve_indeterminate_from_evidence(
    effect_state: EffectState,
    evidence_spine: "EvidenceSpine",
    evidence_pointer: "EvidencePointer",
    effect_intent_id: str,
    dispatch_id: str | None = None,
    idempotency_key: str | None = None,
) -> EffectState:
    """Resolve indeterminate state from verified provider boundary evidence.

    Only authoritative provider boundary reconciliation evidence verified from
    the evidence spine can resolve indeterminate. Fabricated evidence, timeout,
    task completion, or retry count cannot resolve indeterminate.

    Evidence must be bound to the exact effect scope: effect_intent_id,
    dispatch_id (if dispatch occurred), and idempotency_key (if available).

    Args:
        effect_state: Current effect state
        evidence_spine: Authoritative evidence spine
        evidence_pointer: Verified evidence pointer
        effect_intent_id: Effect intent ID to bind evidence to
        dispatch_id: Dispatch ID to bind evidence to (if dispatch occurred)
        idempotency_key: Idempotency key to validate (if available)

    Returns:
        Resolved effect state (NOTHING_LANDED, SOMETHING_LANDED, or INDETERMINATE)

    Raises:
        TypeError: If spine or pointer are not correct types
        ValueError: If evidence is not provider boundary reconciliation
        ValueError: If evidence is not for the correct effect intent
        ValueError: If evidence is for wrong dispatch_id or idempotency_key
        EvidenceSpineError: If evidence verification fails
    """
    from research_mission.evidence_spine import EvidencePointer, EvidenceSpine

    if effect_state != EffectState.INDETERMINATE:
        return effect_state

    if type(evidence_spine) is not EvidenceSpine:
        raise TypeError("evidence_spine must be an EvidenceSpine")
    if type(evidence_pointer) is not EvidencePointer:
        raise TypeError("evidence_pointer must be an EvidencePointer")

    effect_intent_id = _require_text(effect_intent_id, "effect_intent_id")
    if dispatch_id is not None:
        dispatch_id = _require_text(dispatch_id, "dispatch_id")
    if idempotency_key is not None:
        idempotency_key = _require_text(idempotency_key, "idempotency_key")

    # Verify evidence exists in spine and fingerprints match
    record = evidence_spine.verify_evidence(evidence_pointer)

    # Verify evidence is provider boundary reconciliation
    if record.key.source != "provider_boundary_reconciliation":
        raise ValueError(
            f"Evidence must be provider_boundary_reconciliation, "
            f"got {record.key.source!r}"
        )

    # Verify evidence is for this specific effect intent
    recorded_effect_intent_id = record.metadata.get("effect_intent_id")
    if recorded_effect_intent_id != effect_intent_id:
        raise ValueError(
            f"Evidence effect_intent_id mismatch: expected {effect_intent_id!r}, "
            f"found {recorded_effect_intent_id!r}"
        )

    # Verify evidence is for the correct dispatch if specified
    if dispatch_id is not None:
        recorded_dispatch_id = record.metadata.get("dispatch_id")
        if recorded_dispatch_id != dispatch_id:
            raise ValueError(
                f"Evidence dispatch_id mismatch: expected {dispatch_id!r}, "
                f"found {recorded_dispatch_id!r}"
            )

    # Verify idempotency key matches if specified
    if idempotency_key is not None:
        recorded_idempotency_key = record.payload.get("idempotency_key")
        if recorded_idempotency_key != idempotency_key:
            raise ValueError(
                f"Evidence idempotency_key mismatch: expected {idempotency_key!r}, "
                f"found {recorded_idempotency_key!r}"
            )

    # Extract reconciliation outcome from verified payload
    reconciliation_outcome = record.payload.get("reconciliation_outcome")
    if reconciliation_outcome == "no_operation_committed":
        return EffectState.NOTHING_LANDED
    if reconciliation_outcome == "operation_committed":
        return EffectState.SOMETHING_LANDED

    # Should not reach here if evidence model is correct, but fail closed
    return EffectState.INDETERMINATE


def project_mission_posture(
    task_states: dict[str, str],
    effect_states: dict[str, EffectState],
    compensation_states: dict[str, str],
) -> MissionPosture:
    """Project mission posture from effect fold.

    Mission posture is determined by effect states, not just task states.
    - Any indeterminate effect → parked_reconciliation
    - Failed task with uncompensated landed effect → dirty_compensation_required
    - All failed tasks with nothing_landed → clean_failed
    - Successful tasks with intended landed effects → clean_succeeded
    """
    # Any indeterminate effect parks the mission
    if any(state == EffectState.INDETERMINATE for state in effect_states.values()):
        return MissionPosture.PARKED_RECONCILIATION

    # Check for dirty state
    has_failed_tasks = any(state == "failed" for state in task_states.values())
    has_landed_effects = any(state == EffectState.SOMETHING_LANDED for state in effect_states.values())
    has_uncompensated = has_failed_tasks and has_landed_effects and not compensation_states

    if has_uncompensated:
        return MissionPosture.DIRTY_COMPENSATION_REQUIRED

    # Clean states
    if has_failed_tasks:
        # All failures with nothing_landed
        return MissionPosture.CLEAN_FAILED

    # Successful completion
    if any(state == "succeeded" for state in task_states.values()):
        return MissionPosture.CLEAN_SUCCEEDED

    return MissionPosture.CLEAN_ACTIVE


class EffectIntentRegistry:
    """Minimal registry for effect intent tracking.

    This is a placeholder for the scheduler integration. Real implementation
    would provide durable storage and prevent double-spending of reservations.

    Enforces:
    - Authority reservation cannot be reused across different effect intents
    - Idempotent retry with same effect_intent_id only if payload identical
    - No silent overwrites of existing committed intents
    - Tracks successful releases with verified evidence
    - Rejects releases of unknown reservations
    - Idempotent releases with identical evidence
    - Rejects conflicting releases with different evidence
    """

    def __init__(self) -> None:
        self._intents: dict[str, EffectIntent] = {}
        self._reservations: dict[str, str] = {}  # reservation_id -> effect_intent_id
        # Track successful releases: reservation_id -> (effect_intent_id, evidence_fingerprint)
        self._releases: dict[str, tuple[str, str]] = {}

    def commit_intent(self, intent: EffectIntent) -> None:
        """Commit a write-ahead intent.

        Raises:
            TypeError: If intent is not an EffectIntent
            ValueError: If authority reservation already used by different intent
            ValueError: If effect_intent_id exists with different payload
        """
        if type(intent) is not EffectIntent:
            raise TypeError("intent must be an EffectIntent")

        # Check for idempotent retry (same effect_intent_id)
        existing_intent = self._intents.get(intent.effect_intent_id)
        if existing_intent is not None:
            # Allow only if canonical payload is identical
            if intent.to_dict() != existing_intent.to_dict():
                raise ValueError(
                    f"effect_intent_id {intent.effect_intent_id} already committed with different payload"
                )
            # Idempotent retry - safe to return
            return

        # Check for authority reservation double-spend
        existing_intent_id = self._reservations.get(intent.authority_reservation_id)
        if existing_intent_id is not None:
            # Reservation already used by a different intent
            if existing_intent_id != intent.effect_intent_id:
                raise ValueError(
                    f"authority reservation {intent.authority_reservation_id} already committed "
                    f"to effect_intent_id {existing_intent_id}"
                )

        # Commit new intent
        self._intents[intent.effect_intent_id] = intent
        self._reservations[intent.authority_reservation_id] = intent.effect_intent_id

    def release_reservation(
        self,
        reservation_id: str,
        evidence_spine: "EvidenceSpine",
        evidence_pointer: "EvidencePointer",
        dispatch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        """Release an authority reservation after verified nothing_landed resolution.

        Requires verified provider boundary reconciliation evidence proving
        NOTHING_LANDED for the exact effect intent bound to this reservation.
        Cannot release on fabricated evidence, INDETERMINATE, or SOMETHING_LANDED.

        Tracks successful releases and enforces:
        - Error if reservation never existed
        - Idempotent if same evidence
        - Reject if conflicting evidence
        - Preserve state on rejection

        A released reservation can potentially be reused if policy permits.

        Args:
            reservation_id: Reservation to release
            evidence_spine: Authoritative evidence spine
            evidence_pointer: Verified evidence proving NOTHING_LANDED
            dispatch_id: Dispatch ID to bind evidence to (if dispatch occurred)
            idempotency_key: Idempotency key to validate (if available)

        Raises:
            TypeError: If spine or pointer are not correct types
            ValueError: If reservation never existed
            ValueError: If evidence does not prove NOTHING_LANDED
            ValueError: If evidence is for wrong effect intent, dispatch, or idempotency key
            ValueError: If conflicting release evidence
            EvidenceSpineError: If evidence verification fails
        """
        from research_mission.evidence_spine import EvidencePointer, EvidenceSpine

        if type(evidence_spine) is not EvidenceSpine:
            raise TypeError("evidence_spine must be an EvidenceSpine")
        if type(evidence_pointer) is not EvidencePointer:
            raise TypeError("evidence_pointer must be an EvidencePointer")

        reservation_id = _require_text(reservation_id, "reservation_id")
        if dispatch_id is not None:
            dispatch_id = _require_text(dispatch_id, "dispatch_id")
        if idempotency_key is not None:
            idempotency_key = _require_text(idempotency_key, "idempotency_key")

        # Check if already released
        existing_release = self._releases.get(reservation_id)
        if existing_release is not None:
            existing_effect_intent_id, existing_evidence_fingerprint = existing_release
            # Idempotent if same evidence
            if evidence_pointer.record_fingerprint == existing_evidence_fingerprint:
                return  # Idempotent success
            # Conflicting evidence - reject
            raise ValueError(
                f"Reservation {reservation_id} already released with different evidence: "
                f"existing fingerprint {existing_evidence_fingerprint}, "
                f"new fingerprint {evidence_pointer.record_fingerprint}"
            )

        # Check if reservation exists
        effect_intent_id = self._reservations.get(reservation_id)
        if effect_intent_id is None:
            # Not in active reservations and not in releases - never existed
            raise ValueError(
                f"Reservation {reservation_id} not found: reservation never existed or "
                f"was already released with different evidence"
            )

        # Verify evidence exists in spine
        record = evidence_spine.verify_evidence(evidence_pointer)

        # Verify evidence is provider boundary reconciliation
        if record.key.source != "provider_boundary_reconciliation":
            raise ValueError(
                f"Evidence must be provider_boundary_reconciliation, "
                f"got {record.key.source!r}"
            )

        # Verify evidence is for this specific effect intent
        recorded_effect_intent_id = record.metadata.get("effect_intent_id")
        if recorded_effect_intent_id != effect_intent_id:
            raise ValueError(
                f"Evidence effect_intent_id mismatch: expected {effect_intent_id!r}, "
                f"found {recorded_effect_intent_id!r}"
            )

        # Verify evidence is for the correct dispatch if specified
        if dispatch_id is not None:
            recorded_dispatch_id = record.metadata.get("dispatch_id")
            if recorded_dispatch_id != dispatch_id:
                raise ValueError(
                    f"Evidence dispatch_id mismatch: expected {dispatch_id!r}, "
                    f"found {recorded_dispatch_id!r}"
                )

        # Verify idempotency key matches if specified
        if idempotency_key is not None:
            recorded_idempotency_key = record.payload.get("idempotency_key")
            if recorded_idempotency_key != idempotency_key:
                raise ValueError(
                    f"Evidence idempotency_key mismatch: expected {idempotency_key!r}, "
                    f"found {recorded_idempotency_key!r}"
                )

        # Verify evidence proves NOTHING_LANDED
        reconciliation_outcome = record.payload.get("reconciliation_outcome")
        if reconciliation_outcome != "no_operation_committed":
            raise ValueError(
                f"Cannot release reservation: evidence shows {reconciliation_outcome!r}, "
                f"not 'no_operation_committed' (NOTHING_LANDED)"
            )

        # Verified NOTHING_LANDED - safe to release
        del self._reservations[reservation_id]
        # Track release with evidence fingerprint for idempotency
        self._releases[reservation_id] = (effect_intent_id, evidence_pointer.record_fingerprint)

    def get_intent(self, effect_intent_id: str) -> EffectIntent | None:
        """Retrieve committed intent by ID."""
        return self._intents.get(effect_intent_id)


__all__ = [
    "EffectState",
    "AuthorityDisposition",
    "ReconciliationState",
    "ProviderReconcilability",
    "MissionPosture",
    "MissionLifecycle",
    "EffectIntent",
    "EffectDispatch",
    "AuthorityReservation",
    "ReconciliationObligation",
    "ProviderCapability",
    "MissionEffectFold",
    "derive_effect_state",
    "resolve_indeterminate_from_evidence",
    "project_mission_posture",
    "EffectIntentRegistry",
]
