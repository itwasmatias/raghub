"""Authority-gated credential broker for external provider access.

This module provides credential release authorization that enforces all three
dimensions of MissionaryX delegation authority:
- Capabilities (WHAT action)
- Resource scope (WHERE/WHICH resource)
- Mission binding (WHICH mission)

Plus provider-specific scope validation.

Credential material is never released without full authority validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from federation.access_connection import AccessConnection
from federation.access_credential_store import AccessCredentialStore
from federation.agent_identity_registry import DurableAgentIdentityRegistry
from federation.authority_evaluator import AuthorityDecision, evaluate_grant
from federation.control_domain_registry import DurableControlDomainRegistry
from federation.credential_backend import CredentialBackend
from federation.delegation_grant_registry import DelegationGrantRegistry

_MAX_TEXT_LENGTH = 255


class AccessCredentialBrokerError(Exception):
    """Base error for access credential broker operations."""


class AccessCredentialAuthorityError(AccessCredentialBrokerError):
    """Raised when MissionaryX authority is denied."""


class AccessCredentialNotFoundError(AccessCredentialBrokerError):
    """Raised when connection or credential cannot be resolved."""


class AccessCredentialConnectionError(AccessCredentialBrokerError):
    """Raised when connection is not usable."""


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field_name} must not contain surrounding whitespace")
    if len(value) > _MAX_TEXT_LENGTH:
        raise ValueError(f"{field_name} exceeds {_MAX_TEXT_LENGTH} characters")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NULL bytes")
    return value


@dataclass(frozen=True, slots=True)
class AccessCredentialRequest:
    """Request for access credential authorization.

    Binds all three authority dimensions plus connection/provider constraints.
    """

    domain_id: str
    mission_id: str
    agent_id: str
    grant_id: str
    connection_id: str
    capability: str
    resource: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "domain_id", _require_text(self.domain_id, "domain_id"))
        object.__setattr__(self, "mission_id", _require_text(self.mission_id, "mission_id"))
        object.__setattr__(self, "agent_id", _require_text(self.agent_id, "agent_id"))
        object.__setattr__(self, "grant_id", _require_text(self.grant_id, "grant_id"))
        object.__setattr__(self, "connection_id", _require_text(self.connection_id, "connection_id"))
        object.__setattr__(self, "capability", _require_text(self.capability, "capability"))
        object.__setattr__(self, "resource", _require_text(self.resource, "resource"))


@dataclass(frozen=True, slots=True)
class AccessCredentialAuthorization:
    """Non-secret authorization evidence for credential use.

    This object represents MissionaryX authority to use a credential.
    It contains NO raw secret material and is safe to:
    - Serialize to JSON/dict
    - Log for audit
    - Store in databases
    - Include in repr/str output
    - Pass across trust boundaries

    Contains:
    - Full authority binding (domain, mission, agent, grant, capability, resource)
    - Connection and provider binding
    - Authority decision evidence
    - Time bounds

    This object does NOT imply:
    - Credential has been retrieved
    - Credential has been used
    - Effect has been dispatched
    - Operation has succeeded
    """

    authorization_id: str  # Unique identifier for this authorization instance
    request: AccessCredentialRequest
    connection_id: str
    provider: str
    grant_fingerprint: str
    decision: AuthorityDecision
    required_provider_scopes: tuple[str, ...]
    granted_provider_scopes: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "authorization_id", _require_text(self.authorization_id, "authorization_id"))
        if type(self.request) is not AccessCredentialRequest:
            raise TypeError("request must be an AccessCredentialRequest")
        object.__setattr__(self, "connection_id", _require_text(self.connection_id, "connection_id"))
        object.__setattr__(self, "provider", _require_text(self.provider, "provider"))
        object.__setattr__(self, "grant_fingerprint", _require_text(self.grant_fingerprint, "grant_fingerprint"))
        if type(self.decision) is not AuthorityDecision:
            raise TypeError("decision must be an AuthorityDecision")
        if not isinstance(self.required_provider_scopes, tuple):
            raise TypeError("required_provider_scopes must be a tuple")
        if not isinstance(self.granted_provider_scopes, tuple):
            raise TypeError("granted_provider_scopes must be a tuple")
        if not isinstance(self.issued_at, datetime) or self.issued_at.tzinfo is None:
            raise TypeError("issued_at must be a timezone-aware datetime")
        object.__setattr__(self, "issued_at", self.issued_at.astimezone(timezone.utc))
        if self.expires_at is not None:
            if not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None:
                raise TypeError("expires_at must be a timezone-aware datetime or None")
            object.__setattr__(self, "expires_at", self.expires_at.astimezone(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """Return safe serializable representation."""
        return {
            "authorization_id": self.authorization_id,
            "domain_id": self.request.domain_id,
            "mission_id": self.request.mission_id,
            "agent_id": self.request.agent_id,
            "grant_id": self.request.grant_id,
            "grant_fingerprint": self.grant_fingerprint,
            "connection_id": self.connection_id,
            "provider": self.provider,
            "capability": self.request.capability,
            "resource": self.request.resource,
            "required_provider_scopes": list(self.required_provider_scopes),
            "granted_provider_scopes": list(self.granted_provider_scopes),
            "issued_at": self.issued_at.isoformat(),
            "expires_at": None if self.expires_at is None else self.expires_at.isoformat(),
            "authority_granted": self.decision.allowed,
        }


class AccessCredentialBroker:
    """Authority-gated credential broker for external provider access.

    This broker enforces MissionaryX delegation authority using evaluate_grant
    before releasing any credential material.

    It validates:
    1. ControlDomain exists and is active
    2. Connection exists and is active
    3. Grant exists and is active
    4. Agent identity is active
    5. Capability is granted
    6. Resource is in scope
    7. Mission matches (if grant is mission-bound)
    8. Connection provider scopes satisfy requirements (future)

    All checks fail closed. Any missing/invalid input denies credential release.
    """

    def __init__(
        self,
        *,
        credential_store: AccessCredentialStore,
        credential_backend: CredentialBackend,
        domain_registry: DurableControlDomainRegistry,
        identity_registry: DurableAgentIdentityRegistry,
        grant_registry: DelegationGrantRegistry,
    ) -> None:
        if type(credential_store) is not AccessCredentialStore:
            raise TypeError("credential_store must be an AccessCredentialStore")
        # Duck-type check for CredentialBackend protocol
        if not (hasattr(credential_backend, 'resolve_credential') and callable(credential_backend.resolve_credential)):
            raise TypeError("credential_backend must implement CredentialBackend protocol (resolve_credential method)")
        if type(domain_registry) is not DurableControlDomainRegistry:
            raise TypeError("domain_registry must be a DurableControlDomainRegistry")
        if type(identity_registry) is not DurableAgentIdentityRegistry:
            raise TypeError("identity_registry must be a DurableAgentIdentityRegistry")
        if type(grant_registry) is not DelegationGrantRegistry:
            raise TypeError("grant_registry must be a DelegationGrantRegistry")

        self._credential_store = credential_store
        self._credential_backend = credential_backend
        self._domain_registry = domain_registry
        self._identity_registry = identity_registry
        self._grant_registry = grant_registry

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def authorize(
        self,
        request: AccessCredentialRequest,
        *,
        evaluation_time: datetime | None = None,
        required_provider_scopes: tuple[str, ...] = (),
    ) -> AccessCredentialAuthorization:
        """Authorize credential use for the requested access operation.

        This method validates MissionaryX delegation authority and provider scope
        requirements, then issues a non-secret authorization object.

        The authorization object contains NO raw credential material and is safe to
        serialize, log, and store.

        Args:
            request: The access credential request
            evaluation_time: Time for authority evaluation (defaults to now)
            required_provider_scopes: Provider-specific scopes required for this operation

        Returns:
            AccessCredentialAuthorization with authority evidence (NO secrets)

        Raises:
            AccessCredentialAuthorityError: If authority is denied or scopes insufficient
            AccessCredentialNotFoundError: If connection/grant/domain not found
            AccessCredentialConnectionError: If connection is not active/usable
        """
        if type(request) is not AccessCredentialRequest:
            raise TypeError("request must be an AccessCredentialRequest")
        if not isinstance(required_provider_scopes, tuple):
            raise TypeError("required_provider_scopes must be a tuple")

        now = evaluation_time if evaluation_time is not None else self._now()

        # Step 1: Resolve ControlDomain
        try:
            domain = self._domain_registry.get(request.domain_id)
        except Exception as exc:
            raise AccessCredentialNotFoundError(
                f"ControlDomain {request.domain_id!r} not found"
            ) from exc

        # Step 2: Resolve connection
        try:
            connection = self._credential_store.get_connection(
                connection_id=request.connection_id,
                domain_id=request.domain_id,
            )
        except Exception as exc:
            raise AccessCredentialNotFoundError(
                f"Connection {request.connection_id!r} not found in domain {request.domain_id!r}"
            ) from exc

        # Step 3: Validate connection is active
        if not connection.is_active():
            raise AccessCredentialConnectionError(
                f"Connection {request.connection_id!r} is not active: {connection.lifecycle.value}"
            )

        # Step 4: Enforce provider scope requirements
        # Required scopes must be subset of granted scopes
        required_scopes_set = set(required_provider_scopes)
        granted_scopes_set = set(connection.granted_scopes)

        if not required_scopes_set.issubset(granted_scopes_set):
            missing_scopes = required_scopes_set - granted_scopes_set
            raise AccessCredentialAuthorityError(
                f"Connection {request.connection_id!r} missing required provider scopes: {sorted(missing_scopes)}"
            )

        # Step 5: Resolve grant
        try:
            grant = self._grant_registry.get(
                request.grant_id,
                domain_id=request.domain_id,
                mission_id=request.mission_id,
            )
        except Exception as exc:
            raise AccessCredentialNotFoundError(
                f"Grant {request.grant_id!r} not found"
            ) from exc

        # Step 6: Resolve grantee identity
        try:
            grantee_identity = self._identity_registry.get(
                agent_id=request.agent_id,
                domain_id=request.domain_id,
            )
        except Exception as exc:
            raise AccessCredentialNotFoundError(
                f"Agent identity {request.agent_id!r} not found in domain {request.domain_id!r}"
            ) from exc

        # Step 7: Evaluate MissionaryX delegation authority
        decision = evaluate_grant(
            control_domain=domain,
            grant=grant,
            requested_capability=request.capability,
            requested_resource=request.resource,
            requested_mission=request.mission_id,
            grantee_identity=grantee_identity,
            evaluation_time=now,
        )

        # Step 8: Enforce authority decision
        if not decision.allowed:
            raise AccessCredentialAuthorityError(
                f"MissionaryX authority denied: {decision.reason} ({decision.denial_code.value if decision.denial_code else 'unknown'})"
            )

        # Step 9: Issue non-secret authorization
        # Raw credential resolution is NOT performed here - authorization is separate from credential use
        issued_at = self._now()
        expires_at = grant.expires_at if grant.expires_at is not None else None

        # Generate unique authorization ID
        import hashlib
        auth_id_input = f"{request.domain_id}:{request.mission_id}:{request.agent_id}:{request.grant_id}:{request.connection_id}:{request.capability}:{request.resource}:{issued_at.isoformat()}"
        authorization_id = "auth-" + hashlib.sha256(auth_id_input.encode()).hexdigest()[:32]

        return AccessCredentialAuthorization(
            authorization_id=authorization_id,
            request=request,
            connection_id=connection.connection_id,
            provider=connection.provider,
            grant_fingerprint=grant.grant_fingerprint,
            decision=decision,
            required_provider_scopes=required_provider_scopes,
            granted_provider_scopes=connection.granted_scopes,
            issued_at=issued_at,
            expires_at=expires_at,
        )


__all__ = [
    "AccessCredentialBroker",
    "AccessCredentialBrokerError",
    "AccessCredentialAuthorityError",
    "AccessCredentialAuthorization",
    "AccessCredentialConnectionError",
    "AccessCredentialNotFoundError",
    "AccessCredentialRequest",
]
