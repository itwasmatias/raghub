"""Comprehensive tests for MissionaryX Agent Identity & Delegation v0.1 authority corrections.

Tests verify the four major corrections:
1. Authority evaluator pure evaluation logic
2. Immutable SQLite store enforcement
3. Atomic migration with internal metadata
4. Deterministic parent resolution in JSONL registry
"""

import concurrent.futures
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from federation.agent_authority_store import (
    AUTHORITY_SCHEMA_VERSION,
    AgentAuthorityStore,
    AuthorityStoreError,
)
from federation.agent_identity import AgentIdentityLifecycle, AuthoritativeAgentIdentity
from federation.authority_evaluator import AuthorityDecision, AuthorityDenialReason, evaluate_grant
from federation.control_domain import DomainLifecycle
from federation.control_domain_registry import AuthoritativeDomain
from federation.delegation_grant import (
    AuthoritativeDelegationGrant,
    DelegationCapabilities,
    DelegationGrantStatus,
    DelegationScope,
    grant_fingerprint,
)
from federation.delegation_grant_registry import (
    DelegationGrantCorruptionError,
    DelegationGrantNotFoundError,
)

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


# ============================================================================
# AUTHORITY EVALUATOR TESTS
# ============================================================================


def test_evaluate_grant_all_match_allows():
    """Capability + resource + mission match → ALLOW."""
    domain = AuthoritativeDomain(
        domain_id="domain-1",
        domain_fingerprint="df1",
        owner="owner",
        name="Domain 1",
        lifecycle=DomainLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    grant = AuthoritativeDelegationGrant(
        grant_id="grant-1",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        capabilities=DelegationCapabilities(["vehicle.registration.renew"]),
        resource_scope=DelegationScope(["vehicle:ABC123"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,
        grant_fingerprint="fp1",
    )

    decision = evaluate_grant(
        control_domain=domain,
        grant=grant,
        requested_capability="vehicle.registration.renew",
        requested_resource="vehicle:ABC123",
        requested_mission="mission-1",
        evaluation_time=NOW + timedelta(minutes=30),
    )

    assert decision.allowed is True
    assert decision.denial_code is None


def test_evaluate_grant_wrong_resource_denies():
    """Resource mismatch → DENY."""
    domain = AuthoritativeDomain(
        domain_id="domain-1",
        domain_fingerprint="df1",
        owner="owner",
        name="Domain 1",
        lifecycle=DomainLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    grant = AuthoritativeDelegationGrant(
        grant_id="grant-1",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        capabilities=DelegationCapabilities(["vehicle.registration.renew"]),
        resource_scope=DelegationScope(["vehicle:ABC123"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,
        grant_fingerprint="fp1",
    )

    decision = evaluate_grant(
        control_domain=domain,
        grant=grant,
        requested_capability="vehicle.registration.renew",
        requested_resource="vehicle:XYZ789",  # Wrong resource
        requested_mission="mission-1",
        evaluation_time=NOW + timedelta(minutes=30),
    )

    assert decision.allowed is False
    assert decision.denial_code == AuthorityDenialReason.RESOURCE_DENIED


def test_evaluate_grant_wrong_capability_denies():
    """Capability mismatch → DENY."""
    domain = AuthoritativeDomain(
        domain_id="domain-1",
        domain_fingerprint="df1",
        owner="owner",
        name="Domain 1",
        lifecycle=DomainLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    grant = AuthoritativeDelegationGrant(
        grant_id="grant-1",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        capabilities=DelegationCapabilities(["vehicle.registration.renew"]),
        resource_scope=DelegationScope(["vehicle:ABC123"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,
        grant_fingerprint="fp1",
    )

    decision = evaluate_grant(
        control_domain=domain,
        grant=grant,
        requested_capability="vehicle.registration.cancel",  # Wrong capability
        requested_resource="vehicle:ABC123",
        requested_mission="mission-1",
        evaluation_time=NOW + timedelta(minutes=30),
    )

    assert decision.allowed is False
    assert decision.denial_code == AuthorityDenialReason.CAPABILITY_DENIED


def test_evaluate_grant_wrong_mission_denies():
    """Mission mismatch → DENY."""
    domain = AuthoritativeDomain(
        domain_id="domain-1",
        domain_fingerprint="df1",
        owner="owner",
        name="Domain 1",
        lifecycle=DomainLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    grant = AuthoritativeDelegationGrant(
        grant_id="grant-1",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        capabilities=DelegationCapabilities(["vehicle.registration.renew"]),
        resource_scope=DelegationScope(["vehicle:ABC123"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,
        grant_fingerprint="fp1",
    )

    decision = evaluate_grant(
        control_domain=domain,
        grant=grant,
        requested_capability="vehicle.registration.renew",
        requested_resource="vehicle:ABC123",
        requested_mission="mission-2",  # Wrong mission
        evaluation_time=NOW + timedelta(minutes=30),
    )

    assert decision.allowed is False
    assert decision.denial_code == AuthorityDenialReason.MISSION_DENIED


def test_evaluate_grant_revoked_denies():
    """Revoked grant → DENY."""
    domain = AuthoritativeDomain(
        domain_id="domain-1",
        domain_fingerprint="df1",
        owner="owner",
        name="Domain 1",
        lifecycle=DomainLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    grant = AuthoritativeDelegationGrant(
        grant_id="grant-1",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        capabilities=DelegationCapabilities(["vehicle.registration.renew"]),
        resource_scope=DelegationScope(["vehicle:ABC123"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.REVOKED,  # Revoked
        grant_fingerprint="fp1",
        revoked_at=NOW + timedelta(minutes=15),
        revocation_reason="test revocation",
    )

    decision = evaluate_grant(
        control_domain=domain,
        grant=grant,
        requested_capability="vehicle.registration.renew",
        requested_resource="vehicle:ABC123",
        requested_mission="mission-1",
        evaluation_time=NOW + timedelta(minutes=30),
    )

    assert decision.allowed is False
    assert decision.denial_code == AuthorityDenialReason.GRANT_REVOKED


def test_evaluate_grant_expired_denies():
    """Expired grant → DENY."""
    domain = AuthoritativeDomain(
        domain_id="domain-1",
        domain_fingerprint="df1",
        owner="owner",
        name="Domain 1",
        lifecycle=DomainLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    grant = AuthoritativeDelegationGrant(
        grant_id="grant-1",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        capabilities=DelegationCapabilities(["vehicle.registration.renew"]),
        resource_scope=DelegationScope(["vehicle:ABC123"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,
        grant_fingerprint="fp1",
    )

    # Evaluate after expiration
    decision = evaluate_grant(
        control_domain=domain,
        grant=grant,
        requested_capability="vehicle.registration.renew",
        requested_resource="vehicle:ABC123",
        requested_mission="mission-1",
        evaluation_time=NOW + timedelta(hours=2),  # After expiry
    )

    assert decision.allowed is False
    assert decision.denial_code == AuthorityDenialReason.GRANT_EXPIRED


def test_evaluate_grant_revoked_parent_denies():
    """Revoked parent → DENY."""
    domain = AuthoritativeDomain(
        domain_id="domain-1",
        domain_fingerprint="df1",
        owner="owner",
        name="Domain 1",
        lifecycle=DomainLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    parent_grant = AuthoritativeDelegationGrant(
        grant_id="parent-1",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="agent-0",
        grantee_identity="agent-1",
        capabilities=DelegationCapabilities(["vehicle.registration.renew"]),
        resource_scope=DelegationScope(["vehicle:ABC123"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.REVOKED,  # Parent is revoked
        grant_fingerprint="fp_parent",
        revoked_at=NOW + timedelta(minutes=10),
        revocation_reason="parent revocation",
    )

    grant = AuthoritativeDelegationGrant(
        grant_id="grant-1",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        capabilities=DelegationCapabilities(["vehicle.registration.renew"]),
        resource_scope=DelegationScope(["vehicle:ABC123"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,
        grant_fingerprint="fp1",
        parent_grant_id="parent-1",
        parent_grant_fingerprint="fp_parent",
    )

    decision = evaluate_grant(
        control_domain=domain,
        grant=grant,
        requested_capability="vehicle.registration.renew",
        requested_resource="vehicle:ABC123",
        requested_mission="mission-1",
        evaluation_time=NOW + timedelta(minutes=30),
        parent_grant=parent_grant,
    )

    assert decision.allowed is False
    assert decision.denial_code == AuthorityDenialReason.PARENT_REVOKED


def test_evaluate_grant_expired_parent_denies():
    """Expired parent → DENY."""
    domain = AuthoritativeDomain(
        domain_id="domain-1",
        domain_fingerprint="df1",
        owner="owner",
        name="Domain 1",
        lifecycle=DomainLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    parent_grant = AuthoritativeDelegationGrant(
        grant_id="parent-1",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="agent-0",
        grantee_identity="agent-1",
        capabilities=DelegationCapabilities(["vehicle.registration.renew"]),
        resource_scope=DelegationScope(["vehicle:ABC123"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(minutes=30),  # Parent expires earlier
        status=DelegationGrantStatus.ACTIVE,
        grant_fingerprint="fp_parent",
    )

    grant = AuthoritativeDelegationGrant(
        grant_id="grant-1",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        capabilities=DelegationCapabilities(["vehicle.registration.renew"]),
        resource_scope=DelegationScope(["vehicle:ABC123"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,
        grant_fingerprint="fp1",
        parent_grant_id="parent-1",
        parent_grant_fingerprint="fp_parent",
    )

    # Evaluate after parent expiration
    decision = evaluate_grant(
        control_domain=domain,
        grant=grant,
        requested_capability="vehicle.registration.renew",
        requested_resource="vehicle:ABC123",
        requested_mission="mission-1",
        evaluation_time=NOW + timedelta(minutes=45),  # Parent has expired
        parent_grant=parent_grant,
    )

    assert decision.allowed is False
    assert decision.denial_code == AuthorityDenialReason.PARENT_EXPIRED


def test_evaluate_grant_unbound_mission_allows_any():
    """Unbound grant (mission_id=None) accepts any mission."""
    domain = AuthoritativeDomain(
        domain_id="domain-1",
        domain_fingerprint="df1",
        owner="owner",
        name="Domain 1",
        lifecycle=DomainLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    grant = AuthoritativeDelegationGrant(
        grant_id="grant-1",
        domain_id="domain-1",
        mission_id=None,  # Unbound
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        capabilities=DelegationCapabilities(["vehicle.registration.renew"]),
        resource_scope=DelegationScope(["vehicle:ABC123"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,
        grant_fingerprint="fp1",
    )

    # Try different missions - all should succeed
    for mission in ["mission-1", "mission-2", "mission-3"]:
        decision = evaluate_grant(
            control_domain=domain,
            grant=grant,
            requested_capability="vehicle.registration.renew",
            requested_resource="vehicle:ABC123",
            requested_mission=mission,
            evaluation_time=NOW + timedelta(minutes=30),
        )

        assert decision.allowed is True
        assert decision.denial_code is None


def test_evaluate_grant_correct_grantee_allows():
    """Grant issued to agent-a + supplied agent-a identity → ALLOW."""
    domain = AuthoritativeDomain(
        domain_id="domain-1",
        domain_fingerprint="df1",
        owner="owner",
        name="Domain 1",
        lifecycle=DomainLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    grant = AuthoritativeDelegationGrant(
        grant_id="grant-1",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="grantor",
        grantee_identity="agent-a",  # Grant issued to agent-a
        capabilities=DelegationCapabilities(["vehicle.registration.renew"]),
        resource_scope=DelegationScope(["vehicle:ABC123"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,
        grant_fingerprint="fp1",
    )

    agent_a = AuthoritativeAgentIdentity(
        agent_id="agent-a",
        domain_id="domain-1",
        domain_fingerprint="df1",
        name="Agent A",
        identity_fingerprint="if-a",
        lifecycle=AgentIdentityLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    decision = evaluate_grant(
        control_domain=domain,
        grant=grant,
        requested_capability="vehicle.registration.renew",
        requested_resource="vehicle:ABC123",
        requested_mission="mission-1",
        grantee_identity=agent_a,  # Correct grantee
        evaluation_time=NOW + timedelta(minutes=30),
    )

    assert decision.allowed is True
    assert decision.denial_code is None


def test_evaluate_grant_wrong_grantee_denies():
    """Grant issued to agent-a + supplied agent-b identity → DENY."""
    domain = AuthoritativeDomain(
        domain_id="domain-1",
        domain_fingerprint="df1",
        owner="owner",
        name="Domain 1",
        lifecycle=DomainLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    grant = AuthoritativeDelegationGrant(
        grant_id="grant-1",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="grantor",
        grantee_identity="agent-a",  # Grant issued to agent-a
        capabilities=DelegationCapabilities(["vehicle.registration.renew"]),
        resource_scope=DelegationScope(["vehicle:ABC123"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,
        grant_fingerprint="fp1",
    )

    agent_b = AuthoritativeAgentIdentity(
        agent_id="agent-b",  # Different agent!
        domain_id="domain-1",
        domain_fingerprint="df1",
        name="Agent B",
        identity_fingerprint="if-b",
        lifecycle=AgentIdentityLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    decision = evaluate_grant(
        control_domain=domain,
        grant=grant,
        requested_capability="vehicle.registration.renew",
        requested_resource="vehicle:ABC123",
        requested_mission="mission-1",
        grantee_identity=agent_b,  # Wrong grantee!
        evaluation_time=NOW + timedelta(minutes=30),
    )

    assert decision.allowed is False
    assert decision.denial_code == AuthorityDenialReason.GRANTEE_MISMATCH


def test_evaluate_grant_wrong_grantee_all_else_valid():
    """Grant issued to agent-a + agent-b with same domain/mission/capability/resource → DENY."""
    domain = AuthoritativeDomain(
        domain_id="domain-1",
        domain_fingerprint="df1",
        owner="owner",
        name="Domain 1",
        lifecycle=DomainLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    grant = AuthoritativeDelegationGrant(
        grant_id="grant-1",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="grantor",
        grantee_identity="agent-a",
        capabilities=DelegationCapabilities(["vehicle.registration.renew"]),
        resource_scope=DelegationScope(["vehicle:ABC123"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,
        grant_fingerprint="fp1",
    )

    # Agent B is ACTIVE and from same domain
    agent_b_active = AuthoritativeAgentIdentity(
        agent_id="agent-b",
        domain_id="domain-1",  # Same domain
        domain_fingerprint="df1",
        name="Agent B",
        identity_fingerprint="if-b",
        lifecycle=AgentIdentityLifecycle.ACTIVE,  # ACTIVE
        created_at=NOW,
        last_transition_at=NOW,
    )

    # All other dimensions match, only grantee differs
    decision = evaluate_grant(
        control_domain=domain,
        grant=grant,
        requested_capability="vehicle.registration.renew",  # Matches grant
        requested_resource="vehicle:ABC123",  # Matches grant
        requested_mission="mission-1",  # Matches grant
        grantee_identity=agent_b_active,  # Wrong grantee but ACTIVE
        evaluation_time=NOW + timedelta(minutes=30),
    )

    # Must DENY because grantee doesn't match, even though everything else is valid
    assert decision.allowed is False
    assert decision.denial_code == AuthorityDenialReason.GRANTEE_MISMATCH


def test_evaluate_grant_inactive_correct_grantee_denies():
    """Grant issued to agent-a + revoked agent-a identity → DENY (lifecycle check)."""
    domain = AuthoritativeDomain(
        domain_id="domain-1",
        domain_fingerprint="df1",
        owner="owner",
        name="Domain 1",
        lifecycle=DomainLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    grant = AuthoritativeDelegationGrant(
        grant_id="grant-1",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="grantor",
        grantee_identity="agent-a",
        capabilities=DelegationCapabilities(["vehicle.registration.renew"]),
        resource_scope=DelegationScope(["vehicle:ABC123"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,
        grant_fingerprint="fp1",
    )

    agent_a_revoked = AuthoritativeAgentIdentity(
        agent_id="agent-a",
        domain_id="domain-1",
        domain_fingerprint="df1",
        name="Agent A",
        identity_fingerprint="if-a",
        lifecycle=AgentIdentityLifecycle.REVOKED,  # Revoked
        created_at=NOW,
        last_transition_at=NOW,
        revoked_at=NOW + timedelta(minutes=15),
        revocation_reason="test revocation",
    )

    decision = evaluate_grant(
        control_domain=domain,
        grant=grant,
        requested_capability="vehicle.registration.renew",
        requested_resource="vehicle:ABC123",
        requested_mission="mission-1",
        grantee_identity=agent_a_revoked,
        evaluation_time=NOW + timedelta(minutes=30),
    )

    # Revoked grantee must be denied (lifecycle check before grantee binding)
    assert decision.allowed is False
    assert decision.denial_code == AuthorityDenialReason.IDENTITY_NOT_ACTIVE


def test_evaluate_grant_correct_grantee_wrong_capability_denies():
    """Correct grantee + wrong capability → DENY (capability check still enforced)."""
    domain = AuthoritativeDomain(
        domain_id="domain-1",
        domain_fingerprint="df1",
        owner="owner",
        name="Domain 1",
        lifecycle=DomainLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    grant = AuthoritativeDelegationGrant(
        grant_id="grant-1",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="grantor",
        grantee_identity="agent-a",
        capabilities=DelegationCapabilities(["vehicle.registration.renew"]),
        resource_scope=DelegationScope(["vehicle:ABC123"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,
        grant_fingerprint="fp1",
    )

    agent_a = AuthoritativeAgentIdentity(
        agent_id="agent-a",
        domain_id="domain-1",
        domain_fingerprint="df1",
        name="Agent A",
        identity_fingerprint="if-a",
        lifecycle=AgentIdentityLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    decision = evaluate_grant(
        control_domain=domain,
        grant=grant,
        requested_capability="vehicle.registration.cancel",  # Wrong capability
        requested_resource="vehicle:ABC123",
        requested_mission="mission-1",
        grantee_identity=agent_a,  # Correct grantee
        evaluation_time=NOW + timedelta(minutes=30),
    )

    assert decision.allowed is False
    assert decision.denial_code == AuthorityDenialReason.CAPABILITY_DENIED


def test_evaluate_grant_correct_grantee_wrong_resource_denies():
    """Correct grantee + wrong resource → DENY (resource check still enforced)."""
    domain = AuthoritativeDomain(
        domain_id="domain-1",
        domain_fingerprint="df1",
        owner="owner",
        name="Domain 1",
        lifecycle=DomainLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    grant = AuthoritativeDelegationGrant(
        grant_id="grant-1",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="grantor",
        grantee_identity="agent-a",
        capabilities=DelegationCapabilities(["vehicle.registration.renew"]),
        resource_scope=DelegationScope(["vehicle:ABC123"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,
        grant_fingerprint="fp1",
    )

    agent_a = AuthoritativeAgentIdentity(
        agent_id="agent-a",
        domain_id="domain-1",
        domain_fingerprint="df1",
        name="Agent A",
        identity_fingerprint="if-a",
        lifecycle=AgentIdentityLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    decision = evaluate_grant(
        control_domain=domain,
        grant=grant,
        requested_capability="vehicle.registration.renew",
        requested_resource="vehicle:XYZ789",  # Wrong resource
        requested_mission="mission-1",
        grantee_identity=agent_a,  # Correct grantee
        evaluation_time=NOW + timedelta(minutes=30),
    )

    assert decision.allowed is False
    assert decision.denial_code == AuthorityDenialReason.RESOURCE_DENIED


def test_evaluate_grant_correct_grantee_wrong_mission_denies():
    """Correct grantee + wrong mission → DENY (mission check still enforced)."""
    domain = AuthoritativeDomain(
        domain_id="domain-1",
        domain_fingerprint="df1",
        owner="owner",
        name="Domain 1",
        lifecycle=DomainLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    grant = AuthoritativeDelegationGrant(
        grant_id="grant-1",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="grantor",
        grantee_identity="agent-a",
        capabilities=DelegationCapabilities(["vehicle.registration.renew"]),
        resource_scope=DelegationScope(["vehicle:ABC123"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,
        grant_fingerprint="fp1",
    )

    agent_a = AuthoritativeAgentIdentity(
        agent_id="agent-a",
        domain_id="domain-1",
        domain_fingerprint="df1",
        name="Agent A",
        identity_fingerprint="if-a",
        lifecycle=AgentIdentityLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    decision = evaluate_grant(
        control_domain=domain,
        grant=grant,
        requested_capability="vehicle.registration.renew",
        requested_resource="vehicle:ABC123",
        requested_mission="mission-2",  # Wrong mission
        grantee_identity=agent_a,  # Correct grantee
        evaluation_time=NOW + timedelta(minutes=30),
    )

    assert decision.allowed is False
    assert decision.denial_code == AuthorityDenialReason.MISSION_DENIED


# ============================================================================
# IMMUTABLE SQLITE TESTS
# ============================================================================


def test_register_identity_conflict_fails(tmp_path):
    """Conflicting identity re-registration fails."""
    store = AgentAuthorityStore(tmp_path / "authority.db")

    identity1 = AuthoritativeAgentIdentity(
        domain_id="domain-1",
        agent_id="agent-1",
        domain_fingerprint="df1",
        name="Agent One",
        identity_fingerprint="if1",
        lifecycle=AgentIdentityLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    store.register_identity(identity1)

    # Try to register with different immutable field
    identity2 = AuthoritativeAgentIdentity(
        domain_id="domain-1",
        agent_id="agent-1",
        domain_fingerprint="df1",
        name="Different Name",  # Conflicting name
        identity_fingerprint="if1",
        lifecycle=AgentIdentityLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    with pytest.raises(AuthorityStoreError, match="conflicting name"):
        store.register_identity(identity2)


def test_register_identity_duplicate_idempotent(tmp_path):
    """Exact duplicate identity is no-op."""
    store = AgentAuthorityStore(tmp_path / "authority.db")

    identity = AuthoritativeAgentIdentity(
        domain_id="domain-1",
        agent_id="agent-1",
        domain_fingerprint="df1",
        name="Agent One",
        identity_fingerprint="if1",
        lifecycle=AgentIdentityLifecycle.ACTIVE,
        created_at=NOW,
        last_transition_at=NOW,
    )

    # First registration
    store.register_identity(identity)

    # Exact duplicate should succeed (no-op)
    store.register_identity(identity)

    # Verify only one identity
    retrieved = store.get_identity("agent-1", "domain-1")
    assert retrieved.name == "Agent One"


def test_register_grant_conflict_fails(tmp_path):
    """Conflicting grant re-registration fails."""
    store = AgentAuthorityStore(tmp_path / "authority.db")

    grant1 = AuthoritativeDelegationGrant(
        domain_id="domain-1",
        grant_id="grant-1",
        mission_id="mission-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        capabilities=DelegationCapabilities(["read"]),
        resource_scope=DelegationScope(["resource:1"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,
        grant_fingerprint="fp1",
    )

    store.register_grant(grant1)

    # Try to register with different capabilities
    grant2 = AuthoritativeDelegationGrant(
        domain_id="domain-1",
        grant_id="grant-1",
        mission_id="mission-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        capabilities=DelegationCapabilities(["write"]),  # Conflicting
        resource_scope=DelegationScope(["resource:1"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,
        grant_fingerprint="fp2",
    )

    with pytest.raises(AuthorityStoreError, match="conflicting capabilities"):
        store.register_grant(grant2)


def test_register_grant_duplicate_idempotent(tmp_path):
    """Exact duplicate grant is no-op."""
    store = AgentAuthorityStore(tmp_path / "authority.db")

    grant = AuthoritativeDelegationGrant(
        domain_id="domain-1",
        grant_id="grant-1",
        mission_id="mission-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        capabilities=DelegationCapabilities(["read"]),
        resource_scope=DelegationScope(["resource:1"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,
        grant_fingerprint="fp1",
    )

    # First registration
    store.register_grant(grant)

    # Exact duplicate should succeed (no-op)
    store.register_grant(grant)

    # Verify only one grant
    retrieved = store.get_grant("grant-1", "domain-1")
    assert retrieved.grant_fingerprint == "fp1"


def test_register_grant_resurrection_attack(tmp_path):
    """Revoked grant cannot be made ACTIVE through register_grant."""
    store = AgentAuthorityStore(tmp_path / "authority.db")

    # Register revoked grant
    revoked_grant = AuthoritativeDelegationGrant(
        domain_id="domain-1",
        grant_id="grant-1",
        mission_id="mission-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        capabilities=DelegationCapabilities(["read"]),
        resource_scope=DelegationScope(["resource:1"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.REVOKED,
        grant_fingerprint="fp1",
        revoked_at=NOW + timedelta(minutes=30),
        revocation_reason="security breach",
    )

    store.register_grant(revoked_grant)

    # Try to resurrect by registering as ACTIVE
    active_grant = AuthoritativeDelegationGrant(
        domain_id="domain-1",
        grant_id="grant-1",
        mission_id="mission-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        capabilities=DelegationCapabilities(["read"]),
        resource_scope=DelegationScope(["resource:1"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,  # Trying to resurrect
        grant_fingerprint="fp1",
    )

    with pytest.raises(AuthorityStoreError, match="revoked and cannot be resurrected"):
        store.register_grant(active_grant)


def test_register_grant_revocation_evidence_immutable(tmp_path):
    """Revocation reason cannot change."""
    store = AgentAuthorityStore(tmp_path / "authority.db")

    # Register revoked grant
    revoked_grant1 = AuthoritativeDelegationGrant(
        domain_id="domain-1",
        grant_id="grant-1",
        mission_id="mission-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        capabilities=DelegationCapabilities(["read"]),
        resource_scope=DelegationScope(["resource:1"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.REVOKED,
        grant_fingerprint="fp1",
        revoked_at=NOW + timedelta(minutes=30),
        revocation_reason="original reason",
    )

    store.register_grant(revoked_grant1)

    # Try to change revocation reason
    revoked_grant2 = AuthoritativeDelegationGrant(
        domain_id="domain-1",
        grant_id="grant-1",
        mission_id="mission-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        capabilities=DelegationCapabilities(["read"]),
        resource_scope=DelegationScope(["resource:1"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.REVOKED,
        grant_fingerprint="fp1",
        revoked_at=NOW + timedelta(minutes=30),
        revocation_reason="different reason",  # Different reason
    )

    with pytest.raises(AuthorityStoreError, match="conflicting revocation_reason"):
        store.register_grant(revoked_grant2)


# ============================================================================
# MIGRATION TESTS
# ============================================================================


def test_migration_creates_internal_metadata(tmp_path):
    """Migration stores metadata in DB."""
    jsonl_path = tmp_path / "grants.jsonl"
    sqlite_path = tmp_path / "authority.db"

    # Create minimal valid JSONL
    record = {
        "schema_version": 1,
        "sequence": 1,
        "payload": {
            "grant_id": "grant-1",
            "domain_id": "domain-1",
            "mission_id": "mission-1",
            "grantor_identity": "agent-1",
            "grantee_identity": "agent-2",
            "authority_scope": ["read", "write"],
            "parent_grant_id": None,
            "parent_grant_fingerprint": None,
            "created_at": NOW.isoformat(),
            "effective_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "status": "active",
            "grant_fingerprint": grant_fingerprint(
                grant_id="grant-1",
                domain_id="domain-1",
                mission_id="mission-1",
                grantor_identity="agent-1",
                grantee_identity="agent-2",
                authority_scope=("read", "write"),
                created_at=NOW,
                effective_at=NOW,
                expires_at=NOW + timedelta(hours=1),
                status=DelegationGrantStatus.ACTIVE,
            ),
            "revoked_at": None,
            "revocation_reason": None,
        },
        "predecessor_authentication_tag": "0" * 64,
        "authentication_tag": "a" * 64,
    }

    jsonl_path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")

    # Migrate
    store = AgentAuthorityStore.migrate_from_jsonl(jsonl_path, sqlite_path)

    # Verify metadata table exists
    conn = sqlite3.connect(str(sqlite_path))
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='migration_metadata'")
    assert cursor.fetchone() is not None

    # Verify metadata content
    cursor = conn.execute("SELECT source_path, grant_count, schema_version FROM migration_metadata WHERE migration_id=1")
    row = cursor.fetchone()
    conn.close()

    assert row is not None
    assert row[0] == str(jsonl_path)
    assert row[1] == 1  # One grant
    assert row[2] == AUTHORITY_SCHEMA_VERSION


def test_migration_idempotent(tmp_path):
    """Re-running migration succeeds (checks internal evidence)."""
    jsonl_path = tmp_path / "grants.jsonl"
    sqlite_path = tmp_path / "authority.db"

    # Create minimal valid JSONL
    record = {
        "schema_version": 1,
        "sequence": 1,
        "payload": {
            "grant_id": "grant-1",
            "domain_id": "domain-1",
            "mission_id": "mission-1",
            "grantor_identity": "agent-1",
            "grantee_identity": "agent-2",
            "authority_scope": ["read"],
            "parent_grant_id": None,
            "parent_grant_fingerprint": None,
            "created_at": NOW.isoformat(),
            "effective_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "status": "active",
            "grant_fingerprint": grant_fingerprint(
                grant_id="grant-1",
                domain_id="domain-1",
                mission_id="mission-1",
                grantor_identity="agent-1",
                grantee_identity="agent-2",
                authority_scope=("read",),
                created_at=NOW,
                effective_at=NOW,
                expires_at=NOW + timedelta(hours=1),
                status=DelegationGrantStatus.ACTIVE,
            ),
            "revoked_at": None,
            "revocation_reason": None,
        },
        "predecessor_authentication_tag": "0" * 64,
        "authentication_tag": "a" * 64,
    }

    jsonl_path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")

    # First migration
    store1 = AgentAuthorityStore.migrate_from_jsonl(jsonl_path, sqlite_path)
    assert sqlite_path.exists()

    # Second migration should succeed (detects internal metadata)
    store2 = AgentAuthorityStore.migrate_from_jsonl(jsonl_path, sqlite_path)
    assert isinstance(store2, AgentAuthorityStore)


def test_migration_atomic_failure(tmp_path):
    """Failed migration leaves no partial DB."""
    jsonl_path = tmp_path / "grants.jsonl"
    sqlite_path = tmp_path / "authority.db"

    # Create invalid JSONL (missing required field)
    invalid_record = {
        "schema_version": 1,
        "sequence": 1,
        "payload": {
            # Missing grant_id
            "domain_id": "domain-1",
            "mission_id": "mission-1",
            "grantor_identity": "agent-1",
            "grantee_identity": "agent-2",
            "authority_scope": ["read"],
        },
        "predecessor_authentication_tag": "0" * 64,
        "authentication_tag": "a" * 64,
    }

    jsonl_path.write_text(json.dumps(invalid_record, sort_keys=True, separators=(",", ":")) + "\n")

    # Migration should fail
    with pytest.raises(Exception):
        AgentAuthorityStore.migrate_from_jsonl(jsonl_path, sqlite_path)

    # Verify no DB was created
    assert not sqlite_path.exists()


def test_migration_does_not_widen_authority(tmp_path):
    """Authority_scope mapping is conservative (capabilities == scope)."""
    jsonl_path = tmp_path / "grants.jsonl"
    sqlite_path = tmp_path / "authority.db"

    # Create JSONL with authority_scope
    record = {
        "schema_version": 1,
        "sequence": 1,
        "payload": {
            "grant_id": "grant-1",
            "domain_id": "domain-1",
            "mission_id": "mission-1",
            "grantor_identity": "agent-1",
            "grantee_identity": "agent-2",
            "authority_scope": ["alpha", "beta", "gamma"],
            "parent_grant_id": None,
            "parent_grant_fingerprint": None,
            "created_at": NOW.isoformat(),
            "effective_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "status": "active",
            "grant_fingerprint": grant_fingerprint(
                grant_id="grant-1",
                domain_id="domain-1",
                mission_id="mission-1",
                grantor_identity="agent-1",
                grantee_identity="agent-2",
                authority_scope=("alpha", "beta", "gamma"),
                created_at=NOW,
                effective_at=NOW,
                expires_at=NOW + timedelta(hours=1),
                status=DelegationGrantStatus.ACTIVE,
            ),
            "revoked_at": None,
            "revocation_reason": None,
        },
        "predecessor_authentication_tag": "0" * 64,
        "authentication_tag": "a" * 64,
    }

    jsonl_path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")

    # Migrate
    store = AgentAuthorityStore.migrate_from_jsonl(jsonl_path, sqlite_path)

    # Verify capabilities and resource_scope are identical
    grant = store.get_grant("grant-1", "domain-1")
    assert grant.capabilities.to_sorted_list() == ["alpha", "beta", "gamma"]
    assert grant.resource_scope.to_sorted_list() == ["alpha", "beta", "gamma"]


# ============================================================================
# PARENT RESOLUTION TESTS
# ============================================================================


def test_parent_resolution_exact_fingerprint(tmp_path, grant_registry_fixture):
    """Exact fingerprint match succeeds."""
    from federation.delegation_grant import DelegationGrant
    from federation.delegation_grant_registry import _resolve_parent_deterministic

    registry = grant_registry_fixture

    # Register parent grant
    parent = registry.register(
        DelegationGrant(
            grant_id="parent-1",
            domain_id="domain-1",
            mission_id="mission-1",
            grantor_identity="agent-1",
            grantee_identity="agent-2",
            capabilities=["read"],
            resource_scope=["resource:1"],
            created_at=NOW,
        )
    )

    # Build current_by_key lookup
    current_by_key = {
        ("domain-1", "mission-1", "parent-1"): parent,
    }

    # Resolve with exact fingerprint
    resolved_key, resolved_grant = _resolve_parent_deterministic(
        domain_id="domain-1",
        parent_grant_id="parent-1",
        parent_grant_fingerprint=parent.grant_fingerprint,
        child_mission_id="mission-1",
        current_by_key=current_by_key,
    )

    assert resolved_grant.grant_id == "parent-1"
    assert resolved_grant.grant_fingerprint == parent.grant_fingerprint


def test_parent_resolution_wrong_fingerprint(tmp_path, grant_registry_fixture):
    """Wrong fingerprint fails."""
    from federation.delegation_grant import DelegationGrant
    from federation.delegation_grant_registry import _resolve_parent_deterministic

    registry = grant_registry_fixture

    # Register parent grant
    parent = registry.register(
        DelegationGrant(
            grant_id="parent-1",
            domain_id="domain-1",
            mission_id="mission-1",
            grantor_identity="agent-1",
            grantee_identity="agent-2",
            capabilities=["read"],
            resource_scope=["resource:1"],
            created_at=NOW,
        )
    )

    # Build current_by_key lookup
    current_by_key = {
        ("domain-1", "mission-1", "parent-1"): parent,
    }

    # Try to resolve with wrong fingerprint
    with pytest.raises(DelegationGrantCorruptionError, match="fingerprint mismatch"):
        _resolve_parent_deterministic(
            domain_id="domain-1",
            parent_grant_id="parent-1",
            parent_grant_fingerprint="wrong_fingerprint",
            child_mission_id="mission-1",
            current_by_key=current_by_key,
        )


def test_parent_resolution_ambiguous_fails(tmp_path, grant_registry_fixture):
    """Multiple parents with different missions fails."""
    from federation.delegation_grant import DelegationGrant
    from federation.delegation_grant_registry import _resolve_parent_deterministic

    registry = grant_registry_fixture

    # Register parent grant in mission-1
    parent1 = registry.register(
        DelegationGrant(
            grant_id="parent-1",
            domain_id="domain-1",
            mission_id="mission-1",
            grantor_identity="agent-1",
            grantee_identity="agent-2",
            capabilities=["read"],
            resource_scope=["resource:1"],
            created_at=NOW,
        )
    )

    # Register parent grant in mission-2 with same grant_id
    parent2 = registry.register(
        DelegationGrant(
            grant_id="parent-1",
            domain_id="domain-1",
            mission_id="mission-2",
            grantor_identity="agent-1",
            grantee_identity="agent-2",
            capabilities=["read"],
            resource_scope=["resource:1"],
            created_at=NOW,
        )
    )

    # Build current_by_key with both
    current_by_key = {
        ("domain-1", "mission-1", "parent-1"): parent1,
        ("domain-1", "mission-2", "parent-1"): parent2,
    }

    # Try to resolve from mission-3 (neither direct match) - should detect ambiguity
    with pytest.raises(DelegationGrantCorruptionError, match="ambiguous"):
        _resolve_parent_deterministic(
            domain_id="domain-1",
            parent_grant_id="parent-1",
            parent_grant_fingerprint=parent1.grant_fingerprint,
            child_mission_id="mission-3",
            current_by_key=current_by_key,
        )


# ============================================================================
# CONCURRENCY TESTS
# ============================================================================


def test_concurrent_identity_registration(tmp_path):
    """Concurrent identity registration handles conflicts correctly."""
    passes = 0

    # Run concurrently 10 independent times
    for iteration in range(10):
        store_path = tmp_path / f"authority_{iteration}.db"
        store = AgentAuthorityStore(store_path)

        identity1 = AuthoritativeAgentIdentity(
            domain_id="domain-1",
            agent_id="agent-concurrent",
            domain_fingerprint="df1",
            name="First Name",
            identity_fingerprint="if1",
            lifecycle=AgentIdentityLifecycle.ACTIVE,
            created_at=NOW,
            last_transition_at=NOW,
        )

        identity2 = AuthoritativeAgentIdentity(
            domain_id="domain-1",
            agent_id="agent-concurrent",
            domain_fingerprint="df1",
            name="Second Name",  # Conflicting
            identity_fingerprint="if1",
            lifecycle=AgentIdentityLifecycle.ACTIVE,
            created_at=NOW,
            last_transition_at=NOW,
        )

        def register_identity(identity):
            store = AgentAuthorityStore(store_path)
            try:
                store.register_identity(identity)
                return True
            except (AuthorityStoreError, sqlite3.IntegrityError):
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future1 = executor.submit(register_identity, identity1)
            future2 = executor.submit(register_identity, identity2)

            result1 = future1.result()
            result2 = future2.result()

            # Exactly one should succeed
            if result1 != result2:
                passes += 1

    # All 10 iterations must pass
    assert passes == 10


def test_concurrent_grant_registration(tmp_path):
    """Concurrent grant registration handles conflicts correctly."""
    passes = 0

    # Run concurrently 10 independent times
    for iteration in range(10):
        store_path = tmp_path / f"authority_{iteration}.db"
        store = AgentAuthorityStore(store_path)

        grant1 = AuthoritativeDelegationGrant(
            domain_id="domain-1",
            grant_id="grant-concurrent",
            mission_id="mission-1",
            grantor_identity="agent-1",
            grantee_identity="agent-2",
            capabilities=DelegationCapabilities(["read"]),
            resource_scope=DelegationScope(["resource:1"]),
            created_at=NOW,
            effective_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            status=DelegationGrantStatus.ACTIVE,
            grant_fingerprint="fp1",
        )

        grant2 = AuthoritativeDelegationGrant(
            domain_id="domain-1",
            grant_id="grant-concurrent",
            mission_id="mission-1",
            grantor_identity="agent-1",
            grantee_identity="agent-2",
            capabilities=DelegationCapabilities(["write"]),  # Conflicting
            resource_scope=DelegationScope(["resource:1"]),
            created_at=NOW,
            effective_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            status=DelegationGrantStatus.ACTIVE,
            grant_fingerprint="fp2",
        )

        def register_grant(grant):
            store = AgentAuthorityStore(store_path)
            try:
                store.register_grant(grant)
                return True
            except (AuthorityStoreError, sqlite3.IntegrityError):
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future1 = executor.submit(register_grant, grant1)
            future2 = executor.submit(register_grant, grant2)

            result1 = future1.result()
            result2 = future2.result()

            # Exactly one should succeed
            if result1 != result2:
                passes += 1

    # All 10 iterations must pass
    assert passes == 10


# ============================================================================
# TEST FIXTURES
# ============================================================================


@pytest.fixture
def grant_registry_fixture(tmp_path):
    """Create a grant registry with domain and identity registries."""
    from federation.agent_identity import AgentIdentity
    from federation.agent_identity_registry import DurableAgentIdentityRegistry
    from federation.control_domain import ControlDomain
    from federation.control_domain_registry import DurableControlDomainRegistry
    from federation.delegation_grant_registry import DelegationGrantRegistry

    # Create domain registry
    domain_registry = DurableControlDomainRegistry(
        tmp_path / "domains.jsonl",
        integrity_key=b"test-key-32-bytes-long-exactly!!",
    )
    domain_registry.register(
        ControlDomain(
            domain_id="domain-1",
            name="Test Domain",
            owner="test-owner",
            created_at=NOW,
        )
    )

    # Create identity registry
    identity_registry = DurableAgentIdentityRegistry(
        tmp_path / "identities.jsonl",
        domain_registry=domain_registry,
        integrity_key=b"test-key-32-bytes-long-exactly!!",
    )
    for agent_id in ["agent-1", "agent-2", "agent-3"]:
        identity_registry.register(
            AgentIdentity(
                agent_id=agent_id,
                domain_id="domain-1",
                name=f"Agent {agent_id}",
                created_at=NOW,
            )
        )

    # Create grant registry
    registry = DelegationGrantRegistry(
        tmp_path / "grants.jsonl",
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        integrity_key=b"test-key-32-bytes-long-exactly!!",
        clock=lambda: NOW,
    )

    return registry
