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


class DenialReason(str, Enum):
    """Closed taxonomy of gateway authorization denial reasons."""

    # Request validation
    REQUEST_EXPIRED = "request_expired"

    # Claim conflicts
    CLAIM_CONFLICT = "claim_conflict"

    # Authority and chain validation
    RESERVATION_MISSING = "reservation_missing"
    RESERVATION_INELIGIBLE = "reservation_ineligible"
    INTENT_MISMATCH = "intent_mismatch"
    DISPATCH_MISMATCH = "dispatch_mismatch"

    # Request binding validation
    IDEMPOTENCY_MISMATCH = "idempotency_mismatch"
    REQUEST_FINGERPRINT_MISMATCH = "request_fingerprint_mismatch"
    DELEGATION_MISMATCH = "delegation_mismatch"
    CAPABILITY_MISMATCH = "capability_mismatch"
    PROVIDER_MISMATCH = "provider_mismatch"
    ADAPTER_MISMATCH = "adapter_mismatch"
    CREDENTIAL_SCOPE_MISMATCH = "credential_scope_mismatch"
    OWNER_MISMATCH = "owner_mismatch"
    OPERATION_DIGEST_MISMATCH = "operation_digest_mismatch"

    # Domain isolation
    CONTROL_DOMAIN_MISMATCH = "control_domain_mismatch"

    # Permit validation
    PERMIT_INVALID = "permit_invalid"
    PERMIT_EXPIRED = "permit_expired"
    PERMIT_REVOKED = "permit_revoked"
    PERMIT_ALREADY_CONSUMED = "permit_already_consumed"
    PERMIT_ALREADY_ISSUED = "permit_already_issued"

    # State machine
    ILLEGAL_STATE_TRANSITION = "illegal_state_transition"
    CLAIM_NOT_FOUND = "claim_not_found"


@dataclass(slots=True)
class GatewayDenied(GatewayError):
    """Gateway authorization denial with semantic reason."""

    reason: DenialReason
    context: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reason, DenialReason):
            raise TypeError("reason must be a DenialReason")
        if not isinstance(self.context, str):
            raise TypeError("context must be a string")

    def __str__(self) -> str:
        if self.context:
            return f"Gateway denied: {self.reason.value} ({self.context})"
        return f"Gateway denied: {self.reason.value}"


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


class GovernedEffectGateway:
    """Canonical enforcement path for credential-bearing effect dispatch.

    This gateway provides the mandatory authorization-to-dispatch flow enforcing:
    - Single-winner dispatch ownership
    - Single-use cryptographic permits
    - Atomic permit consumption + handoff authorization
    - Durable state transitions
    - Control domain isolation
    """

    def __init__(self, store: Any, *, clock: Any = None) -> None:
        """Initialize gateway with durable store.

        Args:
            store: DurableEffectStore instance
            clock: Optional clock for testing (defaults to datetime.now(timezone.utc))
        """
        self._store = store
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._lock = threading.Lock()

    def claim_dispatch(
        self, request: GatewayEffectRequest, owner_identity: str
    ) -> tuple[str, str]:
        """Claim dispatch ownership (single-winner).

        Args:
            request: Gateway effect request
            owner_identity: Identity claiming ownership

        Returns:
            (gateway_claim_id, request_fingerprint)

        Raises:
            GatewayDenied: If authorization denied with specific reason
        """
        # Check expiry
        now = self._clock()
        if now >= request.request_expiry:
            raise GatewayDenied(DenialReason.REQUEST_EXPIRED, f"expired at {request.request_expiry}")

        request_fingerprint = request.request_fingerprint()

        # Check for idempotent retry by looking for existing claim with this request fingerprint
        from federation.durable_effect_store import ConcurrencyConflictError

        try:
            # First try to find existing claim
            conn = self._store._connect()
            try:
                existing_row = conn.execute(
                    """
                    SELECT gateway_claim_id, owner_identity
                    FROM effect_gateway_claims
                    WHERE control_domain = ? AND request_fingerprint = ?
                    """,
                    (request.control_domain, request_fingerprint),
                ).fetchone()
            finally:
                if self._store._memory_connection is None:
                    conn.close()

            if existing_row is not None:
                # Idempotent retry - return existing claim if same owner
                if existing_row[1] == owner_identity:
                    return (existing_row[0], request_fingerprint)
                # Different owner means conflict
                raise GatewayDenied(
                    DenialReason.CLAIM_CONFLICT,
                    f"Request already claimed by different owner"
                )
        except Exception as e:
            if isinstance(e, GatewayDenied):
                raise
            # Continue to new claim if any error checking for existing

        # Generate new claim ID
        gateway_claim_id = f"gateway-claim-{secrets.token_urlsafe(16)}"

        # Claim duration: from now to request expiry
        claimed_at = now
        expires_at = request.request_expiry

        try:
            self._store.claim_gateway_dispatch(
                gateway_claim_id=gateway_claim_id,
                request_fingerprint=request_fingerprint,
                effect_intent_id=request.effect_intent_id,
                effect_dispatch_id=request.effect_dispatch_id,
                authority_reservation_id=request.authority_reservation_id,
                delegation_grant_id=request.delegation_grant_id,
                delegation_grant_fingerprint=request.delegation_grant_fingerprint,
                requested_capability=request.requested_capability,
                idempotency_key=request.idempotency_key,
                operation_digest=request.operation_digest,
                provider_id=request.provider_id,
                adapter_id=request.adapter_id,
                owner_identity=owner_identity,
                claimed_at=claimed_at,
                expires_at=expires_at,
                control_domain=request.control_domain,
            )
            return (gateway_claim_id, request_fingerprint)
        except ValueError as e:
            # Map ValueError to specific DenialReason
            msg = str(e).lower()
            if "not found" in msg:
                if "intent" in msg:
                    raise GatewayDenied(DenialReason.INTENT_MISMATCH, str(e)) from e
                if "dispatch" in msg:
                    raise GatewayDenied(DenialReason.DISPATCH_MISMATCH, str(e)) from e
                if "reservation" in msg:
                    raise GatewayDenied(DenialReason.RESERVATION_MISSING, str(e)) from e
            if "mismatch" in msg:
                if "idempotency" in msg:
                    raise GatewayDenied(DenialReason.IDEMPOTENCY_MISMATCH, str(e)) from e
                if "operation_digest" in msg:
                    raise GatewayDenied(DenialReason.OPERATION_DIGEST_MISMATCH, str(e)) from e
                if "reservation" in msg:
                    raise GatewayDenied(DenialReason.RESERVATION_INELIGIBLE, str(e)) from e
            if "disposition" in msg:
                raise GatewayDenied(DenialReason.RESERVATION_INELIGIBLE, str(e)) from e
            raise GatewayDenied(DenialReason.RESERVATION_INELIGIBLE, str(e)) from e
        except Exception as e:
            if isinstance(e, ConcurrencyConflictError):
                raise GatewayDenied(DenialReason.CLAIM_CONFLICT, str(e)) from e
            raise

    def issue_dispatch_permit(self, request: GatewayEffectRequest, gateway_claim_id: str) -> tuple[str, str]:
        """Issue single-use permit for winning claim.

        Args:
            request: Gateway effect request
            gateway_claim_id: Gateway claim ID

        Returns:
            (permit_id, permit_token) - token is NEVER stored, only verifier

        Raises:
            GatewayDenied: If authorization denied
        """
        # Generate permit
        permit_id = f"gateway-permit-{secrets.token_urlsafe(16)}"
        permit_token = _generate_permit_token()
        permit_verifier = _permit_verifier(permit_token)

        now = self._clock()
        issued_at = now
        # Permit inherits request expiry
        expires_at = request.request_expiry

        from federation.durable_effect_store import PermitAlreadyIssuedError

        try:
            self._store.issue_gateway_permit(
                permit_id=permit_id,
                permit_verifier=permit_verifier,
                request_fingerprint=request.request_fingerprint(),
                effect_intent_id=request.effect_intent_id,
                effect_dispatch_id=request.effect_dispatch_id,
                authority_reservation_id=request.authority_reservation_id,
                gateway_claim_id=gateway_claim_id,
                delegation_grant_id=request.delegation_grant_id,
                delegation_grant_fingerprint=request.delegation_grant_fingerprint,
                requested_capability=request.requested_capability,
                operation_digest=request.operation_digest,
                idempotency_key=request.idempotency_key,
                provider_id=request.provider_id,
                adapter_id=request.adapter_id,
                credential_scope=request.credential_scope,
                owner_identity=request.principal_identity,
                issued_at=issued_at,
                expires_at=expires_at,
                control_domain=request.control_domain,
            )
            return (permit_id, permit_token)
        except PermitAlreadyIssuedError as e:
            raise GatewayDenied(DenialReason.PERMIT_ALREADY_ISSUED, str(e)) from e
        except ValueError as e:
            raise GatewayDenied(DenialReason.CLAIM_NOT_FOUND, str(e)) from e

    def verify_and_consume_permit(
        self,
        permit_token: str,
        control_domain: str,
    ) -> str:
        """Atomically verify and consume permit, authorize provider handoff.

        This is the CRITICAL HANDOFF AUTHORIZATION boundary.

        Args:
            permit_token: Permit token to consume
            control_domain: Control domain

        Returns:
            gateway_claim_id of authorized claim

        Raises:
            GatewayDenied: If permit invalid/expired/revoked/consumed
        """
        permit_verifier = _permit_verifier(permit_token)
        now = self._clock()

        try:
            return self._store.verify_and_consume_permit(
                permit_verifier=permit_verifier,
                control_domain=control_domain,
                now=now,
            )
        except ValueError as e:
            msg = str(e).lower()
            if "expired" in msg:
                raise GatewayDenied(DenialReason.PERMIT_EXPIRED, str(e)) from e
            if "revoked" in msg:
                raise GatewayDenied(DenialReason.PERMIT_REVOKED, str(e)) from e
            if "consumed" in msg:
                raise GatewayDenied(DenialReason.PERMIT_ALREADY_CONSUMED, str(e)) from e
            if "not found" in msg:
                raise GatewayDenied(DenialReason.PERMIT_INVALID, str(e)) from e
            raise GatewayDenied(DenialReason.PERMIT_INVALID, str(e)) from e

    def record_receipt(
        self,
        gateway_claim_id: str,
        control_domain: str,
    ) -> None:
        """Record receipt from provider, transition to RECEIPT_RECORDED.

        Args:
            gateway_claim_id: Claim ID
            control_domain: Control domain

        Raises:
            GatewayStateError: If claim not in HANDOFF_STARTED state
        """
        now = self._clock()
        try:
            self._store.record_gateway_receipt(
                gateway_claim_id=gateway_claim_id,
                control_domain=control_domain,
                now=now,
            )
        except ValueError as e:
            raise GatewayStateError(str(e)) from e

    def record_effect_result(
        self,
        gateway_claim_id: str,
        control_domain: str,
        result: GatewayEffectResult,
    ) -> None:
        """Record final effect result, transition to TERMINAL or INDETERMINATE.

        Args:
            gateway_claim_id: Claim ID
            control_domain: Control domain
            result: Validated gateway effect result

        Raises:
            GatewayStateError: If invalid state transition or result contract violation
        """
        if not isinstance(result, GatewayEffectResult):
            raise TypeError("result must be GatewayEffectResult")

        if result.effect_status == EffectState.INDETERMINATE:
            raise GatewayStateError(
                "INDETERMINATE requires record_indeterminate_with_obligation"
            )

        if result.effect_status == EffectState.NOTHING_LANDED:
            effect_status = "nothing_landed"
        elif result.effect_status == EffectState.SOMETHING_LANDED:
            effect_status = "something_landed"
        else:
            raise GatewayStateError(f"Unknown effect_status: {result.effect_status}")

        now = self._clock()
        try:
            self._store.record_gateway_result(
                gateway_claim_id=gateway_claim_id,
                control_domain=control_domain,
                effect_status=effect_status,
                now=now,
            )
        except ValueError as e:
            raise GatewayStateError(str(e)) from e

    def record_indeterminate_with_obligation(
        self,
        request: GatewayEffectRequest,
        result: GatewayEffectResult,
        obligation: Any,
    ) -> None:
        """Validate and atomically record an indeterminate effect."""
        from federation.effect_safety import ReconciliationObligation, ReconciliationState

        if not isinstance(request, GatewayEffectRequest):
            raise TypeError("request must be GatewayEffectRequest")
        if not isinstance(result, GatewayEffectResult):
            raise TypeError("result must be GatewayEffectResult")
        if not isinstance(obligation, ReconciliationObligation):
            raise TypeError("obligation must be ReconciliationObligation")
        if result.effect_status is not EffectState.INDETERMINATE:
            raise GatewayStateError("atomic obligation operation requires INDETERMINATE")
        claim = self._store.get_gateway_claim(result.gateway_claim_id, request.control_domain)
        if claim is None:
            raise GatewayStateError("gateway claim not found")
        checks = {
            "control_domain": claim and request.control_domain == obligation.control_domain,
            "intent": result.effect_intent_id == request.effect_intent_id == claim["effect_intent_id"] == obligation.effect_intent_id,
            "dispatch": result.effect_dispatch_id == request.effect_dispatch_id == claim["effect_dispatch_id"] == obligation.dispatch_id,
            "authority_reservation": request.authority_reservation_id == claim["authority_reservation_id"],
            "fingerprint": claim["request_fingerprint"] == request.request_fingerprint(),
            "obligation_id": result.reconciliation_obligation_id == obligation.obligation_id,
            "reconcilability": obligation.provider_reconcilability is request.provider_reconcilability,
            "reserved": result.authority_disposition is AuthorityDisposition.RESERVED,
            "required": result.dispatch_attempted and result.reconciliation_required and result.handoff_started and not result.receipt_recorded,
        }
        if not all(checks.values()) or obligation.state is not ReconciliationState.PENDING or obligation.terminal_disposition is not None:
            raise GatewayStateError("invalid indeterminate reconciliation binding")
        try:
            self._store.record_indeterminate_with_obligation(
                gateway_claim_id=result.gateway_claim_id,
                control_domain=request.control_domain,
                obligation=obligation,
                now=self._clock(),
            )
        except (ValueError, TypeError) as e:
            raise GatewayStateError(str(e)) from e

    def reconcile_indeterminate(
        self, request: GatewayEffectRequest, gateway_claim_id: str, obligation_id: str,
        evidence_spine: Any, evidence_pointer: Any,
    ) -> None:
        """Resolve an indeterminate claim using exact verified provider evidence."""
        try:
            self._store.reconcile_indeterminate(
                gateway_claim_id=gateway_claim_id,
                control_domain=request.control_domain,
                obligation_id=obligation_id,
                evidence_spine=evidence_spine,
                evidence_pointer=evidence_pointer,
                now=self._clock(),
            )
        except (ValueError, TypeError) as e:
            raise GatewayStateError(str(e)) from e


__all__ = [
    "DenialReason",
    "EffectConsequence",
    "GatewayAuthorizationError",
    "GatewayClaimConflictError",
    "GatewayClaimState",
    "GatewayDenied",
    "GatewayDispatchPermit",
    "GatewayEffectRequest",
    "GatewayEffectResult",
    "GatewayError",
    "GatewayPermitError",
    "GatewayStateError",
    "GovernedEffectGateway",
]
