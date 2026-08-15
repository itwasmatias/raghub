"""Mission state models for MissionaryX Mission Runtime v0.1.

This module defines immutable mission specifications and lifecycle state primitives
for durable long-running mission orchestration.

Critical architectural boundaries:
- Mission Runtime owns mission lifecycle truth
- Governed Effect Gateway owns effect outcome truth
- Mission failure ≠ Effect failure (NEVER conflate these concepts)

All mission identity and state is ControlDomain-scoped.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


MAX_MISSION_ID_LENGTH = 255
MAX_OBJECTIVE_LENGTH = 4096
MAX_AGENT_ID_LENGTH = 255
MAX_DOMAIN_ID_LENGTH = 255
MAX_METADATA_SIZE = 65536  # Bounded metadata to prevent unbounded growth


class MissionLifecycle(str, Enum):
    """Lifecycle state of a durable mission.

    State machine semantics:
    - CREATED: Mission specification committed, not yet started
    - RUNNING: Mission actively executing
    - PAUSED: Mission suspended, can be resumed
    - COMPLETED: Terminal success state
    - FAILED: Terminal failure state
    - CANCELLED: Terminal cancellation state

    Terminal states (COMPLETED, FAILED, CANCELLED) are irreversible.
    """

    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _canonical_json(value: Any) -> bytes:
    """Canonical JSON serialization for deterministic hashing."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _require_text(value: Any, field_name: str, *, max_length: int) -> str:
    """Validate non-empty trimmed text field."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field_name} must not have surrounding whitespace")
    if len(value) > max_length:
        raise ValueError(f"{field_name} exceeds {max_length} characters")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NULL bytes")
    return value


def _normalize_timestamp(value: datetime, field_name: str) -> datetime:
    """Normalize timestamp to UTC."""
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Validate metadata is bounded and serializable."""
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise TypeError("metadata must be a dictionary")

    # Test serializability and size
    try:
        serialized = _canonical_json(metadata)
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata must be JSON-serializable") from exc

    if len(serialized) > MAX_METADATA_SIZE:
        raise ValueError(f"metadata exceeds {MAX_METADATA_SIZE} bytes")

    return metadata


@dataclass(frozen=True, slots=True)
class MissionSpecification:
    """Immutable mission specification defining mission identity and intent.

    The specification is the authoritative definition of what the mission is.
    It must NOT change after mission creation - lifecycle state changes separately.

    Mission identity is scoped to ControlDomain - same mission_id can exist in
    different domains without collision.
    """

    mission_id: str
    control_domain: str
    objective: str
    owner_identity: str
    agent_identity: str | None = None
    success_criteria: str | None = None
    constraints: str | None = None
    deadline: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Validate mission specification invariants."""
        from federation.control_domain import validate_domain_id

        # Validate IDs
        object.__setattr__(
            self,
            "mission_id",
            _require_text(self.mission_id, "mission_id", max_length=MAX_MISSION_ID_LENGTH),
        )
        object.__setattr__(
            self,
            "control_domain",
            validate_domain_id(self.control_domain, "control_domain"),
        )
        object.__setattr__(
            self,
            "owner_identity",
            _require_text(self.owner_identity, "owner_identity", max_length=MAX_AGENT_ID_LENGTH),
        )

        # Validate objective (required, bounded)
        object.__setattr__(
            self,
            "objective",
            _require_text(self.objective, "objective", max_length=MAX_OBJECTIVE_LENGTH),
        )

        # Validate optional agent_identity
        if self.agent_identity is not None:
            object.__setattr__(
                self,
                "agent_identity",
                _require_text(self.agent_identity, "agent_identity", max_length=MAX_AGENT_ID_LENGTH),
            )

        # Validate optional text fields
        if self.success_criteria is not None:
            object.__setattr__(
                self,
                "success_criteria",
                _require_text(self.success_criteria, "success_criteria", max_length=MAX_OBJECTIVE_LENGTH),
            )

        if self.constraints is not None:
            object.__setattr__(
                self,
                "constraints",
                _require_text(self.constraints, "constraints", max_length=MAX_OBJECTIVE_LENGTH),
            )

        # Validate deadline
        if self.deadline is not None:
            object.__setattr__(
                self,
                "deadline",
                _normalize_timestamp(self.deadline, "deadline"),
            )

        # Validate and normalize timestamps
        object.__setattr__(
            self,
            "created_at",
            _normalize_timestamp(self.created_at, "created_at"),
        )

        # Validate metadata
        object.__setattr__(
            self,
            "metadata",
            _validate_metadata(self.metadata),
        )

    def specification_fingerprint(self) -> str:
        """Compute stable fingerprint of mission specification.

        This fingerprint represents the immutable identity of the mission.
        It should NOT change when lifecycle state changes.
        """
        payload = {
            "mission_id": self.mission_id,
            "control_domain": self.control_domain,
            "objective": self.objective,
            "owner_identity": self.owner_identity,
            "agent_identity": self.agent_identity,
            "success_criteria": self.success_criteria,
            "constraints": self.constraints,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
        }
        return hashlib.sha256(_canonical_json(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class EffectReference:
    """Immutable reference to a governed effect without duplicating effect truth.

    Mission Runtime MUST NOT infer effect outcome from mission state.
    Effect references preserve the binding to gateway authority without
    claiming to know whether the effect actually landed.
    """

    effect_intent_id: str
    effect_dispatch_id: str | None = None
    gateway_claim_id: str | None = None
    referenced_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Validate effect reference."""
        object.__setattr__(
            self,
            "effect_intent_id",
            _require_text(self.effect_intent_id, "effect_intent_id", max_length=255),
        )

        if self.effect_dispatch_id is not None:
            object.__setattr__(
                self,
                "effect_dispatch_id",
                _require_text(self.effect_dispatch_id, "effect_dispatch_id", max_length=255),
            )

        if self.gateway_claim_id is not None:
            object.__setattr__(
                self,
                "gateway_claim_id",
                _require_text(self.gateway_claim_id, "gateway_claim_id", max_length=255),
            )

        object.__setattr__(
            self,
            "referenced_at",
            _normalize_timestamp(self.referenced_at, "referenced_at"),
        )


@dataclass(frozen=True, slots=True)
class MissionCheckpoint:
    """Immutable mission progress checkpoint.

    Checkpoints are append-only evidence of mission progress.
    They do NOT mutate - each checkpoint is a new immutable record.
    """

    checkpoint_id: str
    mission_id: str
    control_domain: str
    sequence: int
    mission_state: MissionLifecycle
    progress_data: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Validate checkpoint invariants."""
        from federation.control_domain import validate_domain_id

        object.__setattr__(
            self,
            "checkpoint_id",
            _require_text(self.checkpoint_id, "checkpoint_id", max_length=255),
        )
        object.__setattr__(
            self,
            "mission_id",
            _require_text(self.mission_id, "mission_id", max_length=MAX_MISSION_ID_LENGTH),
        )
        object.__setattr__(
            self,
            "control_domain",
            validate_domain_id(self.control_domain, "control_domain"),
        )

        if not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("sequence must be a non-negative integer")

        if not isinstance(self.mission_state, MissionLifecycle):
            if isinstance(self.mission_state, str):
                try:
                    object.__setattr__(self, "mission_state", MissionLifecycle(self.mission_state))
                except ValueError as exc:
                    raise ValueError(f"Invalid mission_state: {self.mission_state!r}") from exc
            else:
                raise TypeError("mission_state must be a MissionLifecycle")

        # Validate progress_data is bounded and serializable
        object.__setattr__(
            self,
            "progress_data",
            _validate_metadata(self.progress_data),
        )

        if self.reason is not None:
            object.__setattr__(
                self,
                "reason",
                _require_text(self.reason, "reason", max_length=4096),
            )

        object.__setattr__(
            self,
            "created_at",
            _normalize_timestamp(self.created_at, "created_at"),
        )


@dataclass(frozen=True, slots=True)
class MissionTransition:
    """Immutable record of a mission lifecycle state transition.

    Every successful state change creates a durable transition record.
    This provides append-only audit trail of mission lifecycle history.
    """

    transition_id: str
    mission_id: str
    control_domain: str
    from_state: MissionLifecycle
    to_state: MissionLifecycle
    revision: int
    reason: str | None = None
    checkpoint_id: str | None = None
    transitioned_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Validate transition invariants."""
        from federation.control_domain import validate_domain_id

        object.__setattr__(
            self,
            "transition_id",
            _require_text(self.transition_id, "transition_id", max_length=255),
        )
        object.__setattr__(
            self,
            "mission_id",
            _require_text(self.mission_id, "mission_id", max_length=MAX_MISSION_ID_LENGTH),
        )
        object.__setattr__(
            self,
            "control_domain",
            validate_domain_id(self.control_domain, "control_domain"),
        )

        # Validate states
        for field_name, state_value in [("from_state", self.from_state), ("to_state", self.to_state)]:
            if not isinstance(state_value, MissionLifecycle):
                if isinstance(state_value, str):
                    try:
                        object.__setattr__(self, field_name, MissionLifecycle(state_value))
                    except ValueError as exc:
                        raise ValueError(f"Invalid {field_name}: {state_value!r}") from exc
                else:
                    raise TypeError(f"{field_name} must be a MissionLifecycle")

        if not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError("revision must be a positive integer")

        if self.reason is not None:
            object.__setattr__(
                self,
                "reason",
                _require_text(self.reason, "reason", max_length=4096),
            )

        if self.checkpoint_id is not None:
            object.__setattr__(
                self,
                "checkpoint_id",
                _require_text(self.checkpoint_id, "checkpoint_id", max_length=255),
            )

        object.__setattr__(
            self,
            "transitioned_at",
            _normalize_timestamp(self.transitioned_at, "transitioned_at"),
        )


__all__ = [
    "MissionLifecycle",
    "MissionSpecification",
    "EffectReference",
    "MissionCheckpoint",
    "MissionTransition",
]
