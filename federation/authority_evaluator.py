"""Pure authority evaluation for delegation grants.

This module provides fail-closed authority evaluation that determines whether
a delegation grant permits a requested operation without executing anything,
accessing credentials, or changing state.

The evaluator enforces three independent authority dimensions:
- capabilities: WHAT action is allowed
- resource_scope: WHERE that action is allowed
- mission_id: WHICH mission may exercise it
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from federation.agent_identity import AgentIdentityLifecycle, AuthoritativeAgentIdentity
from federation.control_domain_registry import AuthoritativeDomain
from federation.control_domain import DomainLifecycle
from federation.delegation_grant import AuthoritativeDelegationGrant, DelegationGrantStatus


class AuthorityDenialReason(str, Enum):
    """Structured reasons for authority denial."""

    GRANT_NOT_FOUND = "grant_not_found"
    GRANT_NOT_ACTIVE = "grant_not_active"
    GRANT_EXPIRED = "grant_expired"
    GRANT_NOT_EFFECTIVE = "grant_not_effective"
    GRANT_REVOKED = "grant_revoked"
    DOMAIN_MISMATCH = "domain_mismatch"
    DOMAIN_NOT_ACTIVE = "domain_not_active"
    IDENTITY_NOT_ACTIVE = "identity_not_active"
    GRANTEE_MISMATCH = "grantee_mismatch"
    CAPABILITY_DENIED = "capability_denied"
    RESOURCE_DENIED = "resource_denied"
    MISSION_DENIED = "mission_denied"
    PARENT_REVOKED = "parent_revoked"
    PARENT_EXPIRED = "parent_expired"


@dataclass(frozen=True, slots=True)
class AuthorityDecision:
    """Immutable authority evaluation result."""

    allowed: bool
    reason: str | None = None
    denial_code: AuthorityDenialReason | None = None

    @classmethod
    def allow(cls) -> AuthorityDecision:
        """Return a positive authority decision."""
        return cls(allowed=True, reason="authority granted", denial_code=None)

    @classmethod
    def deny(cls, reason: str, code: AuthorityDenialReason) -> AuthorityDecision:
        """Return a negative authority decision with structured reason."""
        return cls(allowed=False, reason=reason, denial_code=code)


def evaluate_grant(
    *,
    control_domain: AuthoritativeDomain,
    grant: AuthoritativeDelegationGrant,
    requested_capability: str,
    requested_resource: str,
    requested_mission: str | None,
    grantor_identity: AuthoritativeAgentIdentity | None = None,
    grantee_identity: AuthoritativeAgentIdentity | None = None,
    parent_grant: AuthoritativeDelegationGrant | None = None,
    evaluation_time: datetime | None = None,
) -> AuthorityDecision:
    """Evaluate whether a grant permits a requested operation.

    This is a pure evaluation function that:
    - Does NOT execute anything
    - Does NOT access credentials
    - Does NOT change Mission Runtime state
    - Does NOT make effect-status claims
    - Does NOT perform automatic retry/reconciliation

    Args:
        control_domain: The ControlDomain context for evaluation
        grant: The delegation grant to evaluate
        requested_capability: The capability being requested (e.g., "vehicle.registration.renew")
        requested_resource: The resource being accessed (e.g., "vehicle:ABC123")
        requested_mission: The mission context (must match grant.mission_id if bound)
        grantor_identity: Optional grantor identity for lifecycle validation
        grantee_identity: Optional grantee identity for lifecycle validation
        parent_grant: Optional parent grant for ancestor revocation checks
        evaluation_time: Time of evaluation (defaults to now)

    Returns:
        AuthorityDecision: Structured allow/deny result with reason

    Authority Rules:
        1. Grant must exist and be in storage
        2. ControlDomain must match and be ACTIVE
        3. Grant must not be directly revoked
        4. Grant must be within its validity window
        5. If grantee_identity provided, agent_id must match grant.grantee_identity
        6. Requested capability must be in grant.capabilities
        7. Requested resource must be in grant.resource_scope (exact match, no prefix)
        8. If grant.mission_id is not None, requested_mission must exactly equal it
        9. If parent provided, parent must not be revoked/expired
        10. If identities provided, they must be ACTIVE

    All checks fail closed - any missing/invalid input results in denial.
    """

    if not isinstance(control_domain, AuthoritativeDomain):
        raise TypeError("control_domain must be an AuthoritativeDomain")
    if not isinstance(grant, AuthoritativeDelegationGrant):
        raise TypeError("grant must be an AuthoritativeDelegationGrant")
    if not isinstance(requested_capability, str) or not requested_capability:
        raise ValueError("requested_capability must be a non-empty string")
    if not isinstance(requested_resource, str) or not requested_resource:
        raise ValueError("requested_resource must be a non-empty string")
    if requested_mission is not None and (not isinstance(requested_mission, str) or not requested_mission):
        raise ValueError("requested_mission must be None or a non-empty string")

    now = evaluation_time
    if now is None:
        now = datetime.now(timezone.utc)
    elif not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("evaluation_time must be a timezone-aware datetime")
    else:
        now = now.astimezone(timezone.utc)

    # Step 1: Verify ControlDomain matches and is active
    if grant.domain_id != control_domain.domain_id:
        return AuthorityDecision.deny(
            f"grant domain {grant.domain_id!r} does not match control domain {control_domain.domain_id!r}",
            AuthorityDenialReason.DOMAIN_MISMATCH,
        )

    if control_domain.lifecycle is not DomainLifecycle.ACTIVE:
        return AuthorityDecision.deny(
            f"control domain {control_domain.domain_id!r} is not active: {control_domain.lifecycle.value}",
            AuthorityDenialReason.DOMAIN_NOT_ACTIVE,
        )

    # Step 2: Verify grant status and validity window
    if grant.status is DelegationGrantStatus.REVOKED:
        return AuthorityDecision.deny(
            f"grant {grant.grant_id!r} is revoked",
            AuthorityDenialReason.GRANT_REVOKED,
        )

    if grant.status is DelegationGrantStatus.EXPIRED:
        return AuthorityDecision.deny(
            f"grant {grant.grant_id!r} is expired",
            AuthorityDenialReason.GRANT_EXPIRED,
        )

    if now < grant.effective_at:
        return AuthorityDecision.deny(
            f"grant {grant.grant_id!r} is not yet effective (effective at {grant.effective_at.isoformat()})",
            AuthorityDenialReason.GRANT_NOT_EFFECTIVE,
        )

    if now >= grant.expires_at:
        return AuthorityDecision.deny(
            f"grant {grant.grant_id!r} has expired (expired at {grant.expires_at.isoformat()})",
            AuthorityDenialReason.GRANT_EXPIRED,
        )

    # Step 3: Verify identity lifecycle if provided
    if grantor_identity is not None:
        if not isinstance(grantor_identity, AuthoritativeAgentIdentity):
            raise TypeError("grantor_identity must be an AuthoritativeAgentIdentity")
        if grantor_identity.lifecycle is not AgentIdentityLifecycle.ACTIVE:
            return AuthorityDecision.deny(
                f"grantor identity {grantor_identity.agent_id!r} is not active: {grantor_identity.lifecycle.value}",
                AuthorityDenialReason.IDENTITY_NOT_ACTIVE,
            )

    if grantee_identity is not None:
        if not isinstance(grantee_identity, AuthoritativeAgentIdentity):
            raise TypeError("grantee_identity must be an AuthoritativeAgentIdentity")
        if grantee_identity.lifecycle is not AgentIdentityLifecycle.ACTIVE:
            return AuthorityDecision.deny(
                f"grantee identity {grantee_identity.agent_id!r} is not active: {grantee_identity.lifecycle.value}",
                AuthorityDenialReason.IDENTITY_NOT_ACTIVE,
            )
        # Verify grantee identity matches the grant's grantee
        if grantee_identity.agent_id != grant.grantee_identity:
            return AuthorityDecision.deny(
                f"grantee identity {grantee_identity.agent_id!r} does not match grant grantee {grant.grantee_identity!r}",
                AuthorityDenialReason.GRANTEE_MISMATCH,
            )

    # Step 4: Check parent grant if provided
    if parent_grant is not None:
        if not isinstance(parent_grant, AuthoritativeDelegationGrant):
            raise TypeError("parent_grant must be an AuthoritativeDelegationGrant")

        if parent_grant.status is DelegationGrantStatus.REVOKED:
            return AuthorityDecision.deny(
                f"parent grant {parent_grant.grant_id!r} is revoked",
                AuthorityDenialReason.PARENT_REVOKED,
            )

        if parent_grant.status is DelegationGrantStatus.EXPIRED or now >= parent_grant.expires_at:
            return AuthorityDecision.deny(
                f"parent grant {parent_grant.grant_id!r} is expired",
                AuthorityDenialReason.PARENT_EXPIRED,
            )

    # Step 5: Enforce capability dimension (WHAT)
    # No prefix matching, no wildcards - exact membership only
    if requested_capability not in grant.capabilities.capabilities:
        return AuthorityDecision.deny(
            f"requested capability {requested_capability!r} not in grant capabilities {sorted(grant.capabilities.capabilities)}",
            AuthorityDenialReason.CAPABILITY_DENIED,
        )

    # Step 6: Enforce resource scope dimension (WHERE)
    # No prefix matching, no wildcards - exact membership only
    if requested_resource not in grant.resource_scope.scope:
        return AuthorityDecision.deny(
            f"requested resource {requested_resource!r} not in grant resource scope {sorted(grant.resource_scope.scope)}",
            AuthorityDenialReason.RESOURCE_DENIED,
        )

    # Step 7: Enforce mission binding dimension (WHICH)
    # If grant has a mission_id, requested_mission must exactly equal it
    # If grant has no mission_id (unbound), any mission is permitted
    if grant.mission_id is not None:
        if requested_mission != grant.mission_id:
            return AuthorityDecision.deny(
                f"requested mission {requested_mission!r} does not match grant mission {grant.mission_id!r}",
                AuthorityDenialReason.MISSION_DENIED,
            )

    # All checks passed
    return AuthorityDecision.allow()


__all__ = [
    "AuthorityDecision",
    "AuthorityDenialReason",
    "evaluate_grant",
]
