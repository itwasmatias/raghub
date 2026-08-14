"""Governed Effect Gateway v0.1 - Mandatory path for credential-bearing effects.

The gateway enforces a durable authorization-to-dispatch path ensuring:
- Exact ControlDomain isolation
- Principal and agent identity binding
- Mission and task identity
- Valid delegation and capability authority
- Policy and effect-boundary approval
- Reserved authority binding
- Committed EffectIntent
- Operation digest and idempotency
- Provider and adapter identity
- Durable single-winner dispatch ownership
- Single-use cryptographic dispatch permit
- Credential scope enforcement
- handoff_started record before provider invocation
- Effect status derivation from boundary evidence
- Reconciliation obligation for indeterminate outcomes

This is an ENFORCEMENT path, not a convenience wrapper.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping

from federation.control_domain import validate_domain_id
from federation.effect_safety import (
    AuthorityDisposition,
    EffectState,
    ProviderReconcilability,
)


MAX_IDEMPOTENCY_KEY_LENGTH = 255
MAX_PROVIDER_ID_LENGTH = 255
MAX_ADAPTER_ID_LENGTH = 255
MAX_OPERATION_DIGEST_LENGTH = 64  # SHA-256 hex
MIN_PERMIT_ENTROPY_BYTES = 32  # 256 bits
MIN_PERMIT_TOKEN_LENGTH = 43  # token_urlsafe(32) minimum length


class GatewayError(Exception):
    """Base gateway error."""


class GatewayAuthorizationError(GatewayError):
    """Authorization validation failed."""


class GatewayClaimConflictError(GatewayError):
    """Dispatch claim conflict - another owner holds the claim."""


class GatewayPermitError(GatewayError):
    """Permit validation or consumption failed."""


class GatewayStateError(GatewayError):
    """Gateway state invariant violated."""


class EffectConsequence(str, Enum):
    """Classification of effect external consequence severity."""

    INFORMATIONAL = "informational"  # Read-only, no external mutation
    CREDENTIAL_ACQUISITION = "credential_acquisition"  # Obtains secret material
    PRIVILEGED_EXECUTION = "privileged_execution"  # System-level actions
    EXTERNAL_COMMUNICATION = "external_communication"  # Network/IPC
    FINANCIAL = "financial"  # Monetary consequence
    DATA_MUTATION = "data_mutation"  # Persistent state change


class GatewayClaimState(str, Enum):
    """Lifecycle state of a gateway dispatch claim."""

    PREPARED = "prepared"  # Claim reserved but not yet owner-locked
    CLAIMED = "claimed"  # Single winner claimed ownership
    HANDOFF_STARTED = "handoff_started"  # Provider invocation began
    RECEIPT_RECORDED = "receipt_recorded"  # Provider response recorded
    TERMINAL = "terminal"  # Authority disposition finalized
    INDETERMINATE = "indeterminate"  # Uncertain outcome requiring reconciliation


def _require_text(value: Any, name: str, *, max_length: int) -> str:
    """Validate non-empty trimmed text field."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{name} must not have surrounding whitespace")
    if len(value) > max_length:
        raise ValueError(f"{name} exceeds {max_length} characters")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NULL bytes")
    return value


def _normalize_timestamp(value: datetime, name: str) -> datetime:
    """Normalize timestamp to UTC."""
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _canonical_json(value: Any) -> bytes:
    """Canonical JSON serialization for hashing/signing."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _operation_digest(operation_params: Mapping[str, Any]) -> str:
    """Compute stable digest of operation parameters."""
    if not isinstance(operation_params, Mapping):
        raise TypeError("operation_params must be a mapping")
    canonical = _canonical_json(dict(operation_params))
    return hashlib.sha256(canonical).hexdigest()


def _generate_permit_token() -> str:
    """Generate cryptographically random high-entropy permit token."""
    return secrets.token_urlsafe(MIN_PERMIT_ENTROPY_BYTES)


def _permit_verifier(permit_token: str) -> str:
    """Derive storage verifier from permit token (one-way hash)."""
    if not isinstance(permit_token, str) or len(permit_token) < MIN_PERMIT_TOKEN_LENGTH:
        raise ValueError("permit_token must be high-entropy string")
    if len(set(permit_token)) < 2:
        raise ValueError("permit_token must be high-entropy string")
    return hashlib.sha256(permit_token.encode("utf-8")).hexdigest()


def _verify_permit_token(permit_token: str, stored_verifier: str) -> bool:
    """Constant-time permit verification."""
    if not isinstance(permit_token, str) or not isinstance(stored_verifier, str):
        return False
    try:
        computed = _permit_verifier(permit_token)
        return hmac.compare_digest(computed, stored_verifier)
    except Exception:
        return False


@dataclass(slots=True, frozen=True)
class GatewayEffectRequest:
    """Frozen gateway request for governed effect dispatch.

    Contains complete authorization context required before any
    credential-bearing or externally effectful operation.
    """

    # Identity and scope
    control_domain: str
    principal_identity: str
    agent_identity: str
    mission_id: str
    task_id: str
    attempt_id: str

    # Delegation and capability
    delegation_grant_id: str
    delegation_grant_fingerprint: str
    requested_capability: str

    # Effect chain
    effect_intent_id: str
    effect_dispatch_id: str
    authority_reservation_id: str

    # Operation identity
    operation_digest: str
    idempotency_key: str

    # Provider and adapter
    provider_id: str
    adapter_id: str
    effect_consequence: EffectConsequence
    provider_reconcilability: ProviderReconcilability

    # Temporal bounds
    request_timestamp: datetime
    request_expiry: datetime

    # Credential scope
    credential_scope: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """Validate gateway request invariants."""
        object.__setattr__(
            self,
            "control_domain",
            validate_domain_id(self.control_domain, "control_domain"),
        )

        for name in (
            "principal_identity",
            "mission_id",
            "task_id",
            "attempt_id",
            "delegation_grant_id",
            "requested_capability",
            "effect_intent_id",
            "effect_dispatch_id",
            "authority_reservation_id",
            "idempotency_key",
            "provider_id",
            "adapter_id",
        ):
            object.__setattr__(
                self,
                name,
                _require_text(getattr(self, name), name, max_length=255),
            )

        object.__setattr__(
            self,
            "agent_identity",
            _require_text(self.agent_identity, "agent_identity", max_length=255),
        )

        object.__setattr__(
            self,
            "delegation_grant_fingerprint",
            _require_text(
                self.delegation_grant_fingerprint,
                "delegation_grant_fingerprint",
                max_length=64,
            ),
        )

        # Validate operation_digest is hex SHA-256
        if (
            not isinstance(self.operation_digest, str)
            or len(self.operation_digest) != 64
            or not all(c in "0123456789abcdef" for c in self.operation_digest)
        ):
            raise ValueError("operation_digest must be lowercase SHA-256 hex")

        # Validate enums
        try:
            object.__setattr__(
                self, "effect_consequence", EffectConsequence(self.effect_consequence)
            )
            object.__setattr__(
                self,
                "provider_reconcilability",
                ProviderReconcilability(self.provider_reconcilability),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid effect_consequence or provider_reconcilability") from exc

        # Normalize timestamps
        object.__setattr__(
            self,
            "request_timestamp",
            _normalize_timestamp(self.request_timestamp, "request_timestamp"),
        )
        object.__setattr__(
            self,
            "request_expiry",
            _normalize_timestamp(self.request_expiry, "request_expiry"),
        )

        if self.request_expiry <= self.request_timestamp:
            raise ValueError("request_expiry must follow request_timestamp")

        # Normalize credential_scope
        if not isinstance(self.credential_scope, tuple):
            if isinstance(self.credential_scope, str):
                raise TypeError("credential_scope must be tuple of strings")
            object.__setattr__(self, "credential_scope", tuple(self.credential_scope))

        validated_scopes = []
        for scope in self.credential_scope:
            validated_scopes.append(_require_text(scope, "credential scope", max_length=255))
        if len(validated_scopes) != len(set(validated_scopes)):
            raise ValueError("credential_scope must not contain duplicates")
        object.__setattr__(self, "credential_scope", tuple(sorted(validated_scopes)))

    def request_fingerprint(self) -> str:
        """Compute stable fingerprint of gateway request."""
        payload = {
            "control_domain": self.control_domain,
            "principal_identity": self.principal_identity,
            "agent_identity": self.agent_identity,
            "mission_id": self.mission_id,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "delegation_grant_id": self.delegation_grant_id,
            "delegation_grant_fingerprint": self.delegation_grant_fingerprint,
            "requested_capability": self.requested_capability,
            "effect_intent_id": self.effect_intent_id,
            "effect_dispatch_id": self.effect_dispatch_id,
            "authority_reservation_id": self.authority_reservation_id,
            "operation_digest": self.operation_digest,
            "idempotency_key": self.idempotency_key,
            "provider_id": self.provider_id,
            "adapter_id": self.adapter_id,
            "effect_consequence": self.effect_consequence.value,
            "provider_reconcilability": self.provider_reconcilability.value,
            "request_timestamp": self.request_timestamp.isoformat(),
            "request_expiry": self.request_expiry.isoformat(),
            "credential_scope": list(self.credential_scope),
        }
        return hashlib.sha256(_canonical_json(payload)).hexdigest()


@dataclass(slots=True, frozen=True)
class GatewayDispatchPermit:
    """Single-use cryptographic permit for adapter invocation.

    The permit binds authorization to a specific dispatch attempt.
    Permits are NOT stored in logs or evidence - only the verifier hash.
    """

    permit_id: str
    control_domain: str
    request_fingerprint: str
    effect_intent_id: str
    effect_dispatch_id: str
    authority_reservation_id: str
    gateway_claim_id: str
    delegation_grant_id: str
    delegation_grant_fingerprint: str
    requested_capability: str
    operation_digest: str
    idempotency_key: str
    provider_id: str
    adapter_id: str
    credential_scope: tuple[str, ...]
    owner_identity: str
    issued_at: datetime
    expires_at: datetime
    permit_token: InitVar[str]
    permit_verifier: str = field(init=False, repr=False)

    def __post_init__(self, permit_token: str) -> None:
        """Validate permit invariants."""
        for name in (
            "permit_id",
            "request_fingerprint",
            "effect_intent_id",
            "effect_dispatch_id",
            "authority_reservation_id",
            "gateway_claim_id",
            "delegation_grant_id",
            "delegation_grant_fingerprint",
            "requested_capability",
            "idempotency_key",
            "provider_id",
            "adapter_id",
            "owner_identity",
        ):
            object.__setattr__(
                self,
                name,
                _require_text(getattr(self, name), name, max_length=255),
            )

        object.__setattr__(
            self,
            "control_domain",
            validate_domain_id(self.control_domain, "control_domain"),
        )

        object.__setattr__(self, "permit_verifier", _permit_verifier(permit_token))

        if (
            not isinstance(self.operation_digest, str)
            or len(self.operation_digest) != 64
            or not all(c in "0123456789abcdef" for c in self.operation_digest)
        ):
            raise ValueError("operation_digest must be lowercase SHA-256 hex")

        if not isinstance(self.credential_scope, tuple):
            if isinstance(self.credential_scope, str):
                raise TypeError("credential_scope must be tuple of strings")
            object.__setattr__(self, "credential_scope", tuple(self.credential_scope))
        validated_scopes = []
        for scope in self.credential_scope:
            validated_scopes.append(_require_text(scope, "credential scope", max_length=255))
        if len(validated_scopes) != len(set(validated_scopes)):
            raise ValueError("credential_scope must not contain duplicates")
        object.__setattr__(self, "credential_scope", tuple(sorted(validated_scopes)))

        object.__setattr__(
            self, "issued_at", _normalize_timestamp(self.issued_at, "issued_at")
        )
        object.__setattr__(
            self, "expires_at", _normalize_timestamp(self.expires_at, "expires_at")
        )

        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must follow issued_at")

    def verifier(self) -> str:
        """Derive one-way verifier for storage (NOT the permit itself)."""
        return self.permit_verifier

    def __repr__(self) -> str:
        """Safe representation excluding secret token."""
        return (
            f"GatewayDispatchPermit(permit_id={self.permit_id!r}, "
            f"control_domain={self.control_domain!r}, "
            f"gateway_claim_id={self.gateway_claim_id!r}, "
            f"permit_verifier={self.permit_verifier!r})"
        )


@dataclass(slots=True)
class GatewayEffectResult:
    """Gateway result separating task status, effect status, and posture."""

    # Task execution
    task_succeeded: bool
    task_error: str | None

    # Effect status (authoritative from gateway state)
    effect_status: EffectState  # nothing_landed, something_landed, indeterminate

    # Dispatch posture
    dispatch_attempted: bool
    handoff_started: bool
    receipt_recorded: bool

    # Authority posture
    authority_disposition: AuthorityDisposition | None

    # Reconciliation requirement
    reconciliation_required: bool
    reconciliation_obligation_id: str | None

    # Evidence references
    gateway_claim_id: str
    effect_intent_id: str
    effect_dispatch_id: str

    def __post_init__(self) -> None:
        """Validate result invariants."""
        if not isinstance(self.task_succeeded, bool):
            raise TypeError("task_succeeded must be bool")
        if self.task_error is not None:
            if not isinstance(self.task_error, str) or not self.task_error.strip():
                raise ValueError("task_error must be a non-empty string when present")
        if self.task_succeeded and self.task_error is not None:
            raise GatewayStateError("successful task cannot carry task_error")
        if not self.task_succeeded and self.task_error is None:
            raise GatewayStateError("failed task must carry task_error")
        if not isinstance(self.dispatch_attempted, bool):
            raise TypeError("dispatch_attempted must be bool")
        if not isinstance(self.handoff_started, bool):
            raise TypeError("handoff_started must be bool")
        if not isinstance(self.receipt_recorded, bool):
            raise TypeError("receipt_recorded must be bool")
        if not isinstance(self.reconciliation_required, bool):
            raise TypeError("reconciliation_required must be bool")

        if not isinstance(self.effect_status, EffectState):
            raise TypeError("effect_status must be EffectState")

        if self.authority_disposition is not None and not isinstance(
            self.authority_disposition, AuthorityDisposition
        ):
            raise TypeError("authority_disposition must be AuthorityDisposition or None")

        for name in ("gateway_claim_id", "effect_intent_id", "effect_dispatch_id"):
            object.__setattr__(
                self,
                name,
                _require_text(getattr(self, name), name, max_length=255),
            )

        if self.reconciliation_obligation_id is not None:
            object.__setattr__(
                self,
                "reconciliation_obligation_id",
                _require_text(
                    self.reconciliation_obligation_id,
                    "reconciliation_obligation_id",
                    max_length=255,
                ),
            )

        # Invariant: handoff_started implies dispatch_attempted
        if self.handoff_started and not self.dispatch_attempted:
            raise GatewayStateError("handoff_started requires dispatch_attempted")

        # Invariant: receipt_recorded implies handoff_started
        if self.receipt_recorded and not self.handoff_started:
            raise GatewayStateError("receipt_recorded requires handoff_started")

        # Effect status semantics
        if self.effect_status is EffectState.NOTHING_LANDED:
            if self.handoff_started and not self.receipt_recorded:
                raise GatewayStateError(
                    "nothing_landed after handoff requires receipt evidence"
                )
            if self.authority_disposition not in (None, AuthorityDisposition.RELEASED):
                raise GatewayStateError("nothing_landed releases authority when terminal")
            if self.reconciliation_required:
                raise GatewayStateError("nothing_landed cannot require reconciliation")
            if self.reconciliation_obligation_id is not None:
                raise GatewayStateError("nothing_landed cannot carry reconciliation_obligation_id")

        if self.effect_status is EffectState.SOMETHING_LANDED:
            if not self.handoff_started:
                raise GatewayStateError("something_landed requires handoff_started")
            if not self.receipt_recorded:
                raise GatewayStateError("something_landed requires receipt_recorded")
            if self.authority_disposition not in (
                AuthorityDisposition.CONSUMED,
                AuthorityDisposition.ASSUMED_CONSUMED_UNRECONCILED,
            ):
                raise GatewayStateError("something_landed requires consumed authority when terminal")
            if self.reconciliation_required:
                raise GatewayStateError("something_landed cannot require reconciliation")
            if self.reconciliation_obligation_id is not None:
                raise GatewayStateError("something_landed cannot carry reconciliation_obligation_id")

        if self.effect_status is EffectState.INDETERMINATE:
            if not self.handoff_started:
                raise GatewayStateError("indeterminate requires handoff_started")
            if self.receipt_recorded:
                raise GatewayStateError("indeterminate cannot record a verified receipt")
            if not self.reconciliation_required:
                raise GatewayStateError("indeterminate requires reconciliation_required")
            if self.reconciliation_obligation_id is None:
                raise GatewayStateError("indeterminate requires reconciliation_obligation_id")
            if self.authority_disposition is not AuthorityDisposition.RESERVED:
                raise GatewayStateError("indeterminate retains RESERVED authority")

        if self.effect_status is not EffectState.INDETERMINATE:
            if self.authority_disposition is AuthorityDisposition.RESERVED and self.reconciliation_required:
                raise GatewayStateError("resolved results cannot retain reserved authority")


__all__ = [
    "EffectConsequence",
    "GatewayAuthorizationError",
    "GatewayClaimConflictError",
    "GatewayClaimState",
    "GatewayDispatchPermit",
    "GatewayEffectRequest",
    "GatewayEffectResult",
    "GatewayError",
    "GatewayPermitError",
    "GatewayStateError",
]
