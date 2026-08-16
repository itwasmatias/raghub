"""Comprehensive tests for v0.1 authority-contract compliance."""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from federation.agent_authority_store import AgentAuthorityStore
from federation.delegation_grant import (
    AuthoritativeDelegationGrant,
    DelegationCapabilities,
    DelegationGrant,
    DelegationGrantStatus,
    DelegationScope,
    grant_fingerprint,
)
from federation.delegation_grant_registry import (
    DelegationGrantCorruptionError,
    DelegationGrantRegistry,
    DelegationGrantScopeError,
)

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


# ============================================================================
# DelegationCapabilities Tests
# ============================================================================


def test_capabilities_creation_from_list():
    caps = DelegationCapabilities(["read", "write"])
    assert "read" in caps.capabilities
    assert "write" in caps.capabilities
    assert len(caps.capabilities) == 2


def test_capabilities_creation_from_set():
    caps = DelegationCapabilities({"read", "write"})
    assert caps.capabilities == frozenset({"read", "write"})


def test_capabilities_creation_from_frozenset():
    frozen = frozenset({"read", "write"})
    caps = DelegationCapabilities(frozen)
    assert caps.capabilities == frozen


def test_capabilities_requires_non_empty():
    with pytest.raises(ValueError, match="capabilities must be non-empty"):
        DelegationCapabilities([])


def test_capabilities_validates_strings():
    with pytest.raises(ValueError, match="capability must be a non-empty string"):
        DelegationCapabilities(["read", "", "write"])


def test_capabilities_rejects_null_bytes():
    with pytest.raises(ValueError, match="capability must not contain NULL bytes"):
        DelegationCapabilities(["read\x00write"])


def test_capabilities_rejects_whitespace_padding():
    with pytest.raises(ValueError, match="capability must not contain surrounding whitespace"):
        DelegationCapabilities([" read "])


def test_capabilities_immutability():
    caps = DelegationCapabilities(["read", "write"])
    with pytest.raises(AttributeError):
        caps._capabilities = frozenset({"admin"})


def test_capabilities_subset_check():
    parent = DelegationCapabilities(["read", "write", "delete"])
    child = DelegationCapabilities(["read", "write"])
    assert child.is_subset_of(parent)
    assert not parent.is_subset_of(child)


def test_capabilities_sorted_serialization():
    caps = DelegationCapabilities(["write", "read", "delete"])
    assert caps.to_sorted_list() == ["delete", "read", "write"]


def test_capabilities_equality():
    caps1 = DelegationCapabilities(["write", "read"])
    caps2 = DelegationCapabilities(["read", "write"])
    assert caps1 == caps2


def test_capabilities_hash_stability():
    caps1 = DelegationCapabilities(["write", "read"])
    caps2 = DelegationCapabilities(["read", "write"])
    assert hash(caps1) == hash(caps2)


# ============================================================================
# DelegationScope Tests
# ============================================================================


def test_scope_creation_from_list():
    scope = DelegationScope(["resource:1", "resource:2"])
    assert "resource:1" in scope.scope
    assert "resource:2" in scope.scope
    assert len(scope.scope) == 2


def test_scope_creation_from_set():
    scope = DelegationScope({"resource:1", "resource:2"})
    assert scope.scope == frozenset({"resource:1", "resource:2"})


def test_scope_requires_non_empty():
    with pytest.raises(ValueError, match="scope must be non-empty"):
        DelegationScope([])


def test_scope_validates_strings():
    with pytest.raises(ValueError, match="scope resource must be a non-empty string"):
        DelegationScope(["resource:1", "", "resource:2"])


def test_scope_rejects_null_bytes():
    with pytest.raises(ValueError, match="scope resource must not contain NULL bytes"):
        DelegationScope(["resource\x00:1"])


def test_scope_rejects_whitespace_padding():
    with pytest.raises(ValueError, match="scope resource must not contain surrounding whitespace"):
        DelegationScope([" resource:1 "])


def test_scope_immutability():
    scope = DelegationScope(["resource:1", "resource:2"])
    with pytest.raises(AttributeError):
        scope._scope = frozenset({"admin"})


def test_scope_subset_check():
    parent = DelegationScope(["resource:1", "resource:2", "resource:3"])
    child = DelegationScope(["resource:1", "resource:2"])
    assert child.is_subset_of(parent)
    assert not parent.is_subset_of(child)


def test_scope_sorted_serialization():
    scope = DelegationScope(["resource:3", "resource:1", "resource:2"])
    assert scope.to_sorted_list() == ["resource:1", "resource:2", "resource:3"]


def test_scope_equality():
    scope1 = DelegationScope(["resource:2", "resource:1"])
    scope2 = DelegationScope(["resource:1", "resource:2"])
    assert scope1 == scope2


def test_scope_hash_stability():
    scope1 = DelegationScope(["resource:2", "resource:1"])
    scope2 = DelegationScope(["resource:1", "resource:2"])
    assert hash(scope1) == hash(scope2)


# ============================================================================
# Optional mission_id Tests
# ============================================================================


def test_unbound_grant_creation():
    grant = DelegationGrant(
        grant_id="grant-1",
        domain_id="domain-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        capabilities=["read"],
        resource_scope=["resource:1"],
        mission_id=None,
        created_at=NOW,
    )
    assert grant.mission_id is None


def test_mission_bound_grant_creation():
    grant = DelegationGrant(
        grant_id="grant-1",
        domain_id="domain-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        capabilities=["read"],
        resource_scope=["resource:1"],
        mission_id="mission-1",
        created_at=NOW,
    )
    assert grant.mission_id == "mission-1"


def test_fingerprint_includes_mission_id():
    fp_bound = grant_fingerprint(
        grant_id="grant-1",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        capabilities=["read"],
        resource_scope=["resource:1"],
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,
    )
    fp_unbound = grant_fingerprint(
        grant_id="grant-1",
        domain_id="domain-1",
        mission_id=None,
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        capabilities=["read"],
        resource_scope=["resource:1"],
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,
    )
    # Different mission_id → different fingerprint
    assert fp_bound != fp_unbound


def test_fingerprint_lifecycle_stability():
    """Lifecycle changes (status, revocation) don't change fingerprint."""
    common = {
        "grant_id": "grant-1",
        "domain_id": "domain-1",
        "mission_id": "mission-1",
        "grantor_identity": "agent-1",
        "grantee_identity": "agent-2",
        "capabilities": ["read"],
        "resource_scope": ["resource:1"],
        "created_at": NOW,
        "effective_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
    }

    fp_active = grant_fingerprint(**common, status=DelegationGrantStatus.ACTIVE)
    fp_revoked = grant_fingerprint(
        **common,
        status=DelegationGrantStatus.REVOKED,
        revoked_at=NOW + timedelta(minutes=5),
        revocation_reason="test",
    )

    assert fp_active == fp_revoked


def test_fingerprint_authority_sensitivity():
    """Authority changes (capabilities, scope) change fingerprint."""
    common = {
        "grant_id": "grant-1",
        "domain_id": "domain-1",
        "mission_id": "mission-1",
        "grantor_identity": "agent-1",
        "grantee_identity": "agent-2",
        "created_at": NOW,
        "effective_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
        "status": DelegationGrantStatus.ACTIVE,
    }

    fp_read = grant_fingerprint(**common, capabilities=["read"], resource_scope=["resource:1"])
    fp_write = grant_fingerprint(**common, capabilities=["write"], resource_scope=["resource:1"])

    assert fp_read != fp_write


# ============================================================================
# Mission Binding Attenuation Tests
# ============================================================================


def test_mission_attenuation_unbound_to_unbound(tmp_path, domain_registry, identity_registry):
    """Unbound parent → unbound child (valid)."""
    registry = DelegationGrantRegistry(
        tmp_path / "grants.jsonl",
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        integrity_key=b"test-key-32-bytes-long-exactly!!",
        clock=lambda: NOW,
    )

    parent = registry.register(
        DelegationGrant(
            grant_id="parent-1",
            domain_id="domain-1",
            mission_id=None,  # Unbound
            grantor_identity="agent-1",
            grantee_identity="agent-2",
            capabilities=["read", "write"],
            resource_scope=["resource:1"],
            created_at=NOW,
        )
    )

    # Child can also be unbound
    child = registry.register(
        DelegationGrant(
            grant_id="child-1",
            domain_id="domain-1",
            mission_id=None,  # Still unbound
            grantor_identity="agent-2",
            grantee_identity="agent-3",
            capabilities=["read"],
            resource_scope=["resource:1"],
            parent_grant_id="parent-1",
            created_at=NOW,
        )
    )

    assert child.mission_id is None
    assert child.parent_grant_id == "parent-1"


def test_mission_attenuation_unbound_to_bound(tmp_path, domain_registry, identity_registry):
    """Unbound parent → bound child (narrows scope - valid)."""
    registry = DelegationGrantRegistry(
        tmp_path / "grants.jsonl",
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        integrity_key=b"test-key-32-bytes-long-exactly!!",
        clock=lambda: NOW,
    )

    parent = registry.register(
        DelegationGrant(
            grant_id="parent-1",
            domain_id="domain-1",
            mission_id=None,  # Unbound
            grantor_identity="agent-1",
            grantee_identity="agent-2",
            capabilities=["read", "write"],
            resource_scope=["resource:1"],
            created_at=NOW,
        )
    )

    # Child can bind to a specific mission (narrowing)
    child = registry.register(
        DelegationGrant(
            grant_id="child-1",
            domain_id="domain-1",
            mission_id="mission-1",  # Binds to mission
            grantor_identity="agent-2",
            grantee_identity="agent-3",
            capabilities=["read"],
            resource_scope=["resource:1"],
            parent_grant_id="parent-1",
            created_at=NOW,
        )
    )

    assert child.mission_id == "mission-1"
    assert child.parent_grant_id == "parent-1"


def test_mission_attenuation_bound_to_same(tmp_path, domain_registry, identity_registry):
    """Bound parent → same mission child (valid)."""
    registry = DelegationGrantRegistry(
        tmp_path / "grants.jsonl",
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        integrity_key=b"test-key-32-bytes-long-exactly!!",
        clock=lambda: NOW,
    )

    parent = registry.register(
        DelegationGrant(
            grant_id="parent-1",
            domain_id="domain-1",
            mission_id="mission-1",  # Bound to mission-1
            grantor_identity="agent-1",
            grantee_identity="agent-2",
            capabilities=["read", "write"],
            resource_scope=["resource:1"],
            created_at=NOW,
        )
    )

    # Child must stay bound to same mission
    child = registry.register(
        DelegationGrant(
            grant_id="child-1",
            domain_id="domain-1",
            mission_id="mission-1",  # Same mission
            grantor_identity="agent-2",
            grantee_identity="agent-3",
            capabilities=["read"],
            resource_scope=["resource:1"],
            parent_grant_id="parent-1",
            created_at=NOW,
        )
    )

    assert child.mission_id == "mission-1"
    assert child.parent_grant_id == "parent-1"


def test_mission_attenuation_bound_to_different_rejects(tmp_path, domain_registry, identity_registry):
    """Bound parent → different mission child (REJECTS)."""
    registry = DelegationGrantRegistry(
        tmp_path / "grants.jsonl",
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        integrity_key=b"test-key-32-bytes-long-exactly!!",
        clock=lambda: NOW,
    )

    parent = registry.register(
        DelegationGrant(
            grant_id="parent-1",
            domain_id="domain-1",
            mission_id="mission-1",  # Bound to mission-1
            grantor_identity="agent-1",
            grantee_identity="agent-2",
            capabilities=["read", "write"],
            resource_scope=["resource:1"],
            created_at=NOW,
        )
    )

    # Child cannot bind to a different mission
    with pytest.raises(DelegationGrantScopeError, match="mission binding widens parent authority"):
        registry.register(
            DelegationGrant(
                grant_id="child-1",
                domain_id="domain-1",
                mission_id="mission-2",  # Different mission!
                grantor_identity="agent-2",
                grantee_identity="agent-3",
                capabilities=["read"],
                resource_scope=["resource:1"],
                parent_grant_id="parent-1",
                created_at=NOW,
            )
        )


def test_mission_attenuation_bound_to_unbound_rejects(tmp_path, domain_registry, identity_registry):
    """Bound parent → unbound child (REJECTS - would widen)."""
    registry = DelegationGrantRegistry(
        tmp_path / "grants.jsonl",
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        integrity_key=b"test-key-32-bytes-long-exactly!!",
        clock=lambda: NOW,
    )

    parent = registry.register(
        DelegationGrant(
            grant_id="parent-1",
            domain_id="domain-1",
            mission_id="mission-1",  # Bound to mission-1
            grantor_identity="agent-1",
            grantee_identity="agent-2",
            capabilities=["read", "write"],
            resource_scope=["resource:1"],
            created_at=NOW,
        )
    )

    # Child cannot become unbound (would widen authority)
    with pytest.raises(DelegationGrantScopeError, match="mission binding widens parent authority"):
        registry.register(
            DelegationGrant(
                grant_id="child-1",
                domain_id="domain-1",
                mission_id=None,  # Trying to unbind!
                grantor_identity="agent-2",
                grantee_identity="agent-3",
                capabilities=["read"],
                resource_scope=["resource:1"],
                parent_grant_id="parent-1",
                created_at=NOW,
            )
        )


# ============================================================================
# Capability Attenuation Tests
# ============================================================================


def test_capability_attenuation_equal(tmp_path, domain_registry, identity_registry):
    """Child with equal capabilities (valid)."""
    registry = DelegationGrantRegistry(
        tmp_path / "grants.jsonl",
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        integrity_key=b"test-key-32-bytes-long-exactly!!",
        clock=lambda: NOW,
    )

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

    child = registry.register(
        DelegationGrant(
            grant_id="child-1",
            domain_id="domain-1",
            mission_id="mission-1",
            grantor_identity="agent-2",
            grantee_identity="agent-3",
            capabilities=["read"],  # Equal
            resource_scope=["resource:1"],
            parent_grant_id="parent-1",
            created_at=NOW,
        )
    )

    assert child.capabilities == parent.capabilities


def test_capability_attenuation_narrower(tmp_path, domain_registry, identity_registry):
    """Child with narrower capabilities (valid)."""
    registry = DelegationGrantRegistry(
        tmp_path / "grants.jsonl",
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        integrity_key=b"test-key-32-bytes-long-exactly!!",
        clock=lambda: NOW,
    )

    parent = registry.register(
        DelegationGrant(
            grant_id="parent-1",
            domain_id="domain-1",
            mission_id="mission-1",
            grantor_identity="agent-1",
            grantee_identity="agent-2",
            capabilities=["read", "write", "delete"],
            resource_scope=["resource:1"],
            created_at=NOW,
        )
    )

    child = registry.register(
        DelegationGrant(
            grant_id="child-1",
            domain_id="domain-1",
            mission_id="mission-1",
            grantor_identity="agent-2",
            grantee_identity="agent-3",
            capabilities=["read"],  # Narrower
            resource_scope=["resource:1"],
            parent_grant_id="parent-1",
            created_at=NOW,
        )
    )

    assert child.capabilities.is_subset_of(parent.capabilities)


def test_capability_attenuation_wider_rejects(tmp_path, domain_registry, identity_registry):
    """Child with wider capabilities (REJECTS)."""
    registry = DelegationGrantRegistry(
        tmp_path / "grants.jsonl",
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        integrity_key=b"test-key-32-bytes-long-exactly!!",
        clock=lambda: NOW,
    )

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

    with pytest.raises(DelegationGrantScopeError, match="capabilities widens"):
        registry.register(
            DelegationGrant(
                grant_id="child-1",
                domain_id="domain-1",
                mission_id="mission-1",
                grantor_identity="agent-2",
                grantee_identity="agent-3",
                capabilities=["read", "write"],  # Wider!
                resource_scope=["resource:1"],
                parent_grant_id="parent-1",
                created_at=NOW,
            )
        )


# ============================================================================
# Resource Scope Attenuation Tests
# ============================================================================


def test_scope_attenuation_equal(tmp_path, domain_registry, identity_registry):
    """Child with equal scope (valid)."""
    registry = DelegationGrantRegistry(
        tmp_path / "grants.jsonl",
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        integrity_key=b"test-key-32-bytes-long-exactly!!",
        clock=lambda: NOW,
    )

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

    child = registry.register(
        DelegationGrant(
            grant_id="child-1",
            domain_id="domain-1",
            mission_id="mission-1",
            grantor_identity="agent-2",
            grantee_identity="agent-3",
            capabilities=["read"],
            resource_scope=["resource:1"],  # Equal
            parent_grant_id="parent-1",
            created_at=NOW,
        )
    )

    assert child.resource_scope == parent.resource_scope


def test_scope_attenuation_narrower(tmp_path, domain_registry, identity_registry):
    """Child with narrower scope (valid)."""
    registry = DelegationGrantRegistry(
        tmp_path / "grants.jsonl",
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        integrity_key=b"test-key-32-bytes-long-exactly!!",
        clock=lambda: NOW,
    )

    parent = registry.register(
        DelegationGrant(
            grant_id="parent-1",
            domain_id="domain-1",
            mission_id="mission-1",
            grantor_identity="agent-1",
            grantee_identity="agent-2",
            capabilities=["read"],
            resource_scope=["resource:1", "resource:2", "resource:3"],
            created_at=NOW,
        )
    )

    child = registry.register(
        DelegationGrant(
            grant_id="child-1",
            domain_id="domain-1",
            mission_id="mission-1",
            grantor_identity="agent-2",
            grantee_identity="agent-3",
            capabilities=["read"],
            resource_scope=["resource:1"],  # Narrower
            parent_grant_id="parent-1",
            created_at=NOW,
        )
    )

    assert child.resource_scope.is_subset_of(parent.resource_scope)


def test_scope_attenuation_wider_rejects(tmp_path, domain_registry, identity_registry):
    """Child with wider scope (REJECTS)."""
    registry = DelegationGrantRegistry(
        tmp_path / "grants.jsonl",
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        integrity_key=b"test-key-32-bytes-long-exactly!!",
        clock=lambda: NOW,
    )

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

    with pytest.raises(DelegationGrantScopeError, match="resource scope widens"):
        registry.register(
            DelegationGrant(
                grant_id="child-1",
                domain_id="domain-1",
                mission_id="mission-1",
                grantor_identity="agent-2",
                grantee_identity="agent-3",
                capabilities=["read"],
                resource_scope=["resource:1", "resource:2"],  # Wider!
                parent_grant_id="parent-1",
                created_at=NOW,
            )
        )


# ============================================================================
# Deep Immutability Tests
# ============================================================================


def test_capabilities_frozen():
    """DelegationCapabilities is frozen and cannot be mutated."""
    caps = DelegationCapabilities(["read", "write"])
    with pytest.raises(AttributeError):
        caps._capabilities = frozenset({"admin"})


def test_scope_frozen():
    """DelegationScope is frozen and cannot be mutated."""
    scope = DelegationScope(["resource:1", "resource:2"])
    with pytest.raises(AttributeError):
        scope._scope = frozenset({"admin"})


def test_authoritative_grant_frozen():
    """AuthoritativeDelegationGrant is frozen and cannot be mutated."""
    grant = AuthoritativeDelegationGrant(
        grant_id="grant-1",
        domain_id="domain-1",
        mission_id="mission-1",
        grantor_identity="agent-1",
        grantee_identity="agent-2",
        capabilities=DelegationCapabilities(["read"]),
        resource_scope=DelegationScope(["resource:1"]),
        created_at=NOW,
        effective_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=DelegationGrantStatus.ACTIVE,
        grant_fingerprint="f" * 64,
    )

    with pytest.raises(AttributeError):
        grant.status = DelegationGrantStatus.REVOKED


# ============================================================================
# Migration Tests
# ============================================================================


@pytest.mark.skip(reason="Migration test needs verification of legacy JSONL format")
def test_migration_jsonl_to_sqlite(tmp_path):
    """Verify JSONL v1 → SQLite v2 migration."""
    jsonl_path = tmp_path / "grants.jsonl"
    sqlite_path = tmp_path / "authority.db"

    # Create JSONL record with legacy authority_scope format
    legacy_record = {
        "schema_version": 1,
        "sequence": 1,
        "payload": {
            "grant_id": "grant-1",
            "domain_id": "domain-1",
            "mission_id": "mission-1",
            "grantor_identity": "agent-1",
            "grantee_identity": "agent-2",
            "capabilities": ["read", "write"],
            "resource_scope": ["read", "write"],
            "parent_grant_id": None,
            "parent_grant_fingerprint": None,
            "created_at": NOW.isoformat(),
            "effective_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "status": "active",
            "grant_fingerprint": "f" * 64,
            "revoked_at": None,
            "revocation_reason": None,
        },
        "predecessor_authentication_tag": "0" * 64,
        "authentication_tag": "a" * 64,
    }

    # Write JSONL file
    jsonl_path.write_text(json.dumps(legacy_record, sort_keys=True, separators=(",", ":")) + "\n")

    # Migrate to SQLite
    store = AgentAuthorityStore.migrate_from_jsonl(jsonl_path, sqlite_path)

    # Verify migration created SQLite file
    assert sqlite_path.exists()

    # Verify schema version
    assert store.schema_version == 2


def test_corruption_unsorted_capabilities_rejected(tmp_path, domain_registry, identity_registry):
    """Corruption: unsorted capabilities array is rejected."""
    jsonl_path = tmp_path / "grants.jsonl"

    # Create malformed record with unsorted capabilities
    corrupt_record = {
        "schema_version": 1,
        "sequence": 1,
        "payload": {
            "grant_id": "grant-1",
            "domain_id": "domain-1",
            "mission_id": "mission-1",
            "grantor_identity": "agent-1",
            "grantee_identity": "agent-2",
            "capabilities": ["write", "read"],  # UNSORTED!
            "resource_scope": ["read", "write"],
            "parent_grant_id": None,
            "parent_grant_fingerprint": None,
            "created_at": NOW.isoformat(),
            "effective_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "status": "active",
            "grant_fingerprint": "f" * 64,
            "revoked_at": None,
            "revocation_reason": None,
        },
        "predecessor_authentication_tag": "0" * 64,
    }

    # Add authentication tag
    from federation.integrity import authentication_tag

    key = b"test-key-32-bytes-long-exactly!!"
    unsigned = {k: v for k, v in corrupt_record.items() if k != "authentication_tag"}
    corrupt_record["authentication_tag"] = authentication_tag(
        key,
        b"raghub.delegation-grant.v1",
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )

    jsonl_path.write_text(json.dumps(corrupt_record, sort_keys=True, separators=(",", ":")) + "\n")

    # Attempt to load should fail
    from federation.delegation_grant_registry import DelegationGrantRegistry

    registry = DelegationGrantRegistry(
        jsonl_path,
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        integrity_key=key,
    )

    with pytest.raises(DelegationGrantCorruptionError, match="capabilities must be sorted"):
        registry._read()


def test_corruption_unsorted_scope_rejected(tmp_path, domain_registry, identity_registry):
    """Corruption: unsorted resource_scope array is rejected."""
    jsonl_path = tmp_path / "grants.jsonl"

    # Create malformed record with unsorted scope
    corrupt_record = {
        "schema_version": 1,
        "sequence": 1,
        "payload": {
            "grant_id": "grant-1",
            "domain_id": "domain-1",
            "mission_id": "mission-1",
            "grantor_identity": "agent-1",
            "grantee_identity": "agent-2",
            "capabilities": ["read", "write"],
            "resource_scope": ["write", "read"],  # UNSORTED!
            "parent_grant_id": None,
            "parent_grant_fingerprint": None,
            "created_at": NOW.isoformat(),
            "effective_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "status": "active",
            "grant_fingerprint": "f" * 64,
            "revoked_at": None,
            "revocation_reason": None,
        },
        "predecessor_authentication_tag": "0" * 64,
    }

    # Add authentication tag
    from federation.integrity import authentication_tag

    key = b"test-key-32-bytes-long-exactly!!"
    unsigned = {k: v for k, v in corrupt_record.items() if k != "authentication_tag"}
    corrupt_record["authentication_tag"] = authentication_tag(
        key,
        b"raghub.delegation-grant.v1",
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )

    jsonl_path.write_text(json.dumps(corrupt_record, sort_keys=True, separators=(",", ":")) + "\n")

    # Attempt to load should fail
    from federation.delegation_grant_registry import DelegationGrantRegistry

    registry = DelegationGrantRegistry(
        jsonl_path,
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        integrity_key=key,
    )

    with pytest.raises(DelegationGrantCorruptionError, match="resource_scope must be sorted"):
        registry._read()


# ============================================================================
# Test Fixtures
# ============================================================================


@pytest.fixture
def domain_registry(tmp_path):
    """Create a test domain registry with an active domain."""
    from federation.control_domain import ControlDomain, DomainLifecycle
    from federation.control_domain_registry import DurableControlDomainRegistry

    registry = DurableControlDomainRegistry(
        tmp_path / "domains.jsonl",
        integrity_key=b"test-key-32-bytes-long-exactly!!",
    )

    registry.register(
        ControlDomain(
            domain_id="domain-1",
            name="Test Domain",
            owner="test-owner",
            created_at=NOW,
        )
    )

    return registry


@pytest.fixture
def identity_registry(tmp_path, domain_registry):
    """Create a test identity registry with active agents."""
    from federation.agent_identity import AgentIdentity, AgentIdentityLifecycle
    from federation.agent_identity_registry import DurableAgentIdentityRegistry

    registry = DurableAgentIdentityRegistry(
        tmp_path / "identities.jsonl",
        domain_registry=domain_registry,
        integrity_key=b"test-key-32-bytes-long-exactly!!",
    )

    for agent_id in ["agent-1", "agent-2", "agent-3"]:
        registry.register(
            AgentIdentity(
                agent_id=agent_id,
                domain_id="domain-1",
                name=f"Agent {agent_id}",
                created_at=NOW,
            )
        )

    return registry
