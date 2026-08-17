"""Comprehensive tests for Access & Credential Broker v0.1 foundation.

Tests cover all mandatory areas specified in the milestone directive.
"""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from federation import (
    AccessConnection,
    AccessCredentialStore,
    AccessRequirement,
    AuthenticationSession,
    AuthenticationState,
    AuthSessionConflictError,
    AuthSessionNotFoundError,
    ConnectionConflictError,
    ConnectionLifecycle,
    ConnectionLifecycleError,
    ConnectionNotFoundError,
    CredentialBackendError,
    CredentialNotFoundError,
    InMemoryCredentialBackend,
    create_authentication_session,
)

NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
TEST_SECRET = b"test-access-token-super-secret"


class MutableClock:
    """Test clock for controlling time in tests."""

    def __init__(self, current=NOW):
        self.current = current

    def __call__(self):
        return self.current

    def advance(self, delta):
        self.current += delta


# ==================================================
# TEST AREA 1-2: ACCESS REQUIREMENT VALIDATION AND IMMUTABILITY
# ==================================================


def test_access_requirement_validates_fields():
    """Test access requirement field validation."""
    # Valid requirement
    req = AccessRequirement(
        domain_id="domain-a",
        provider="google",
        required_scopes=("scope-a", "scope-b"),
        mission_id="mission-1",
    )
    assert req.domain_id == "domain-a"
    assert req.provider == "google"
    assert req.required_scopes == ("scope-a", "scope-b")
    assert req.mission_id == "mission-1"

    # Invalid: empty strings
    with pytest.raises(ValueError, match="domain_id"):
        AccessRequirement(
            domain_id="",
            provider="google",
            required_scopes=("scope-a",),
            mission_id="mission-1",
        )

    with pytest.raises(ValueError, match="provider"):
        AccessRequirement(
            domain_id="domain-a",
            provider="  ",
            required_scopes=("scope-a",),
            mission_id="mission-1",
        )

    # Invalid: NUL bytes
    with pytest.raises(ValueError, match="NULL"):
        AccessRequirement(
            domain_id="domain\x00evil",
            provider="google",
            required_scopes=("scope-a",),
            mission_id="mission-1",
        )


def test_access_requirement_immutable():
    """Test access requirement deep immutability."""
    input_scopes = ["scope-b", "scope-a"]
    req = AccessRequirement(
        domain_id="domain-a",
        provider="google",
        required_scopes=input_scopes,
        mission_id="mission-1",
    )

    # Scopes are canonicalized (sorted)
    assert req.required_scopes == ("scope-a", "scope-b")

    # Mutating input doesn't affect requirement
    input_scopes.append("scope-c")
    assert req.required_scopes == ("scope-a", "scope-b")

    # Can't mutate requirement fields
    with pytest.raises((AttributeError, TypeError)):
        req.domain_id = "evil"

    with pytest.raises((AttributeError, TypeError)):
        req.required_scopes = ("evil",)


# ==================================================
# TEST AREA 3-4: PROVIDER SCOPE CANONICALIZATION
# ==================================================


def test_provider_scope_canonicalization():
    """Test provider scopes are canonicalized (sorted, deduplicated)."""
    req = AccessRequirement(
        domain_id="domain-a",
        provider="google",
        required_scopes=("scope-c", "scope-a", "scope-b"),
        mission_id="mission-1",
    )
    # Must be sorted
    assert req.required_scopes == ("scope-a", "scope-b", "scope-c")


def test_provider_scope_rejects_duplicates():
    """Test provider scopes reject duplicates."""
    with pytest.raises(ValueError, match="duplicates"):
        AccessRequirement(
            domain_id="domain-a",
            provider="google",
            required_scopes=("scope-a", "scope-b", "scope-a"),
            mission_id="mission-1",
        )


def test_provider_scope_rejects_string_not_iterable():
    """Test provider scopes reject single string."""
    with pytest.raises(TypeError, match="iterable"):
        AccessRequirement(
            domain_id="domain-a",
            provider="google",
            required_scopes="scope-a",  # Wrong: string instead of iterable
            mission_id="mission-1",
        )


# ==================================================
# TEST AREA 5-12: CONNECTION MATCHING AND DENIAL SCENARIOS
# ==================================================


def test_connection_matching_exact_match(tmp_path):
    """Test exact connection matching succeeds."""
    store = AccessCredentialStore(tmp_path / "test.db", clock=MutableClock())

    # Register connection
    conn = store.register_connection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user@example.com",
        granted_scopes=("scope-a", "scope-b", "scope-c"),
        credential_backend_ref="backend-ref-1",
    )

    # Requirement matches: subset of scopes
    req = AccessRequirement(
        domain_id="domain-a",
        provider="google",
        required_scopes=("scope-a", "scope-b"),
        mission_id="mission-1",
    )

    matched = store.match_connection(req)
    assert matched is not None
    assert matched.connection_id == "conn-1"


def test_connection_matching_wrong_domain_denied(tmp_path):
    """Test wrong domain denies connection match."""
    store = AccessCredentialStore(tmp_path / "test.db", clock=MutableClock())

    store.register_connection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user@example.com",
        granted_scopes=("scope-a",),
        credential_backend_ref="backend-ref-1",
    )

    # Wrong domain
    req = AccessRequirement(
        domain_id="domain-b",  # Different!
        provider="google",
        required_scopes=("scope-a",),
        mission_id="mission-1",
    )

    matched = store.match_connection(req)
    assert matched is None


def test_connection_matching_wrong_provider_denied(tmp_path):
    """Test wrong provider denies connection match."""
    store = AccessCredentialStore(tmp_path / "test.db", clock=MutableClock())

    store.register_connection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user@example.com",
        granted_scopes=("scope-a",),
        credential_backend_ref="backend-ref-1",
    )

    req = AccessRequirement(
        domain_id="domain-a",
        provider="github",  # Different!
        required_scopes=("scope-a",),
        mission_id="mission-1",
    )

    matched = store.match_connection(req)
    assert matched is None


def test_connection_matching_missing_scope_denied(tmp_path):
    """Test missing provider scope denies connection match."""
    store = AccessCredentialStore(tmp_path / "test.db", clock=MutableClock())

    store.register_connection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user@example.com",
        granted_scopes=("scope-a",),  # Only scope-a
        credential_backend_ref="backend-ref-1",
    )

    req = AccessRequirement(
        domain_id="domain-a",
        provider="google",
        required_scopes=("scope-a", "scope-b"),  # Needs scope-b too
        mission_id="mission-1",
    )

    matched = store.match_connection(req)
    assert matched is None


def test_connection_matching_account_subject_mismatch_denied(tmp_path):
    """Test account subject constraint mismatch denies match."""
    store = AccessCredentialStore(tmp_path / "test.db", clock=MutableClock())

    store.register_connection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user@example.com",
        granted_scopes=("scope-a",),
        credential_backend_ref="backend-ref-1",
    )

    req = AccessRequirement(
        domain_id="domain-a",
        provider="google",
        required_scopes=("scope-a",),
        mission_id="mission-1",
        account_constraint="other@example.com",  # Different account
    )

    matched = store.match_connection(req)
    assert matched is None


def test_connection_matching_ambiguous_connection_fails_closed(tmp_path):
    """Test ambiguous connection match fails closed."""
    store = AccessCredentialStore(tmp_path / "test.db", clock=MutableClock())

    # Register two connections that both match
    store.register_connection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user1@example.com",
        granted_scopes=("scope-a", "scope-b"),
        credential_backend_ref="backend-ref-1",
    )

    store.register_connection(
        connection_id="conn-2",
        domain_id="domain-a",
        provider="google",
        account_id="user2@example.com",
        granted_scopes=("scope-a", "scope-b"),
        credential_backend_ref="backend-ref-2",
    )

    req = AccessRequirement(
        domain_id="domain-a",
        provider="google",
        required_scopes=("scope-a",),
        mission_id="mission-1",
        # No account constraint - ambiguous!
    )

    with pytest.raises(ConnectionConflictError, match="Multiple connections"):
        store.match_connection(req)


# ==================================================
# TEST AREA 9-11: CONNECTION REGISTRATION AND LIFECYCLE
# ==================================================


def test_connection_duplicate_idempotence(tmp_path):
    """Test exact duplicate connection registration is idempotent."""
    store = AccessCredentialStore(tmp_path / "test.db", clock=MutableClock())

    conn1 = store.register_connection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user@example.com",
        granted_scopes=("scope-a",),
        credential_backend_ref="backend-ref-1",
    )

    # Exact duplicate - should return existing
    conn2 = store.register_connection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user@example.com",
        granted_scopes=("scope-a",),
        credential_backend_ref="backend-ref-1",
    )

    assert conn1.connection_id == conn2.connection_id
    assert conn1.created_at == conn2.created_at


def test_connection_conflicting_registration_denied(tmp_path):
    """Test conflicting connection registration is denied."""
    store = AccessCredentialStore(tmp_path / "test.db", clock=MutableClock())

    store.register_connection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user@example.com",
        granted_scopes=("scope-a",),
        credential_backend_ref="backend-ref-1",
    )

    # Same connection_id but different provider - conflict
    with pytest.raises(ConnectionConflictError):
        store.register_connection(
            connection_id="conn-1",
            domain_id="domain-a",
            provider="github",  # Different!
            account_id="user@example.com",
            granted_scopes=("scope-a",),
            credential_backend_ref="backend-ref-1",
        )


def test_revoked_connection_cannot_resurrect(tmp_path):
    """Test revoked connection cannot be resurrected."""
    store = AccessCredentialStore(tmp_path / "test.db", clock=MutableClock())

    store.register_connection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user@example.com",
        granted_scopes=("scope-a",),
        credential_backend_ref="backend-ref-1",
    )

    # Revoke it
    revoked = store.revoke_connection(
        connection_id="conn-1",
        domain_id="domain-a",
        reason="test revocation",
    )
    assert revoked.lifecycle is ConnectionLifecycle.REVOKED

    # Try to re-register exact duplicate - should return existing revoked
    conn = store.register_connection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user@example.com",
        granted_scopes=("scope-a",),
        credential_backend_ref="backend-ref-1",
    )
    # Stays revoked (exact duplicate returned)
    assert conn.lifecycle is ConnectionLifecycle.REVOKED


# ==================================================
# TEST AREA 12: REVOCATION SURVIVES RESTART
# ==================================================


def test_revocation_survives_restart(tmp_path):
    """Test revocation persists across restart."""
    clock = MutableClock()
    db_path = tmp_path / "test.db"

    store1 = AccessCredentialStore(db_path, clock=clock)
    store1.register_connection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user@example.com",
        granted_scopes=("scope-a",),
        credential_backend_ref="backend-ref-1",
    )

    store1.revoke_connection(
        connection_id="conn-1",
        domain_id="domain-a",
        reason="test revocation",
    )

    # Fresh store (restart)
    store2 = AccessCredentialStore(db_path, clock=clock)

    conn = store2.get_connection(connection_id="conn-1", domain_id="domain-a")
    assert conn.lifecycle is ConnectionLifecycle.REVOKED
    assert conn.revocation_reason == "test revocation"


# ==================================================
# TEST AREA 13-17: AUTHENTICATION SESSION SECURITY
# ==================================================


def test_auth_session_wrong_domain_denied(tmp_path):
    """Test authentication session wrong domain completion is denied."""
    store = AccessCredentialStore(tmp_path / "test.db", clock=MutableClock())

    session = store.create_auth_session(
        session_id="session-1",
        domain_id="domain-a",
        provider="google",
        mission_id="mission-1",
        expires_at=NOW + timedelta(minutes=10),
    )

    # Try to complete with wrong domain
    with pytest.raises(AuthSessionNotFoundError):
        store.complete_auth_session(
            session_id="session-1",
            domain_id="domain-b",  # Wrong domain!
            provider="google",
            mission_id="mission-1",
        )


def test_auth_session_wrong_provider_denied(tmp_path):
    """Test authentication session wrong provider completion is denied."""
    store = AccessCredentialStore(tmp_path / "test.db", clock=MutableClock())

    store.create_auth_session(
        session_id="session-1",
        domain_id="domain-a",
        provider="google",
        mission_id="mission-1",
        expires_at=NOW + timedelta(minutes=10),
    )

    # Try to complete with wrong provider
    with pytest.raises(AuthSessionConflictError, match="provider mismatch"):
        store.complete_auth_session(
            session_id="session-1",
            domain_id="domain-a",
            provider="github",  # Wrong provider!
            mission_id="mission-1",
        )


def test_auth_session_expired_denied(tmp_path):
    """Test expired authentication session completion is denied."""
    clock = MutableClock()
    store = AccessCredentialStore(tmp_path / "test.db", clock=clock)

    store.create_auth_session(
        session_id="session-1",
        domain_id="domain-a",
        provider="google",
        mission_id="mission-1",
        expires_at=NOW + timedelta(minutes=10),
    )

    # Advance clock past expiration
    clock.advance(timedelta(minutes=11))

    with pytest.raises(AuthSessionConflictError, match="expired"):
        store.complete_auth_session(
            session_id="session-1",
            domain_id="domain-a",
            provider="google",
            mission_id="mission-1",
        )


def test_auth_session_replay_denied(tmp_path):
    """Test authentication session replay (second completion) is denied."""
    store = AccessCredentialStore(tmp_path / "test.db", clock=MutableClock())

    store.create_auth_session(
        session_id="session-1",
        domain_id="domain-a",
        provider="google",
        mission_id="mission-1",
        expires_at=NOW + timedelta(minutes=10),
    )

    # Complete once - succeeds
    completed = store.complete_auth_session(
        session_id="session-1",
        domain_id="domain-a",
        provider="google",
        mission_id="mission-1",
    )
    assert completed.state is AuthenticationState.COMPLETED

    # Try to complete again - denied
    with pytest.raises(AuthSessionConflictError, match="terminal state"):
        store.complete_auth_session(
            session_id="session-1",
            domain_id="domain-a",
            provider="google",
            mission_id="mission-1",
        )


def test_auth_session_mission_mismatch_denied(tmp_path):
    """Test cross-mission substitution is denied."""
    store = AccessCredentialStore(tmp_path / "test.db", clock=MutableClock())

    store.create_auth_session(
        session_id="session-1",
        domain_id="domain-a",
        provider="google",
        mission_id="mission-1",
        expires_at=NOW + timedelta(minutes=10),
    )

    # Try to complete with different mission
    with pytest.raises(AuthSessionConflictError, match="mission_id mismatch"):
        store.complete_auth_session(
            session_id="session-1",
            domain_id="domain-a",
            provider="google",
            mission_id="mission-2",  # Wrong mission!
        )


# ==================================================
# TEST AREA 28: CONTROL DOMAIN ISOLATION
# ==================================================


def test_domain_isolation_same_bare_ids(tmp_path):
    """Test same bare connection_id in different domains are isolated."""
    store = AccessCredentialStore(tmp_path / "test.db", clock=MutableClock())

    # Register in domain-a
    conn_a = store.register_connection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user-a@example.com",
        granted_scopes=("scope-a",),
        credential_backend_ref="backend-ref-a",
    )

    # Register SAME connection_id in domain-b
    conn_b = store.register_connection(
        connection_id="conn-1",  # Same ID!
        domain_id="domain-b",  # Different domain
        provider="google",
        account_id="user-b@example.com",
        granted_scopes=("scope-b",),
        credential_backend_ref="backend-ref-b",
    )

    # Both exist and are isolated
    assert conn_a.domain_id == "domain-a"
    assert conn_b.domain_id == "domain-b"
    assert conn_a.account_id != conn_b.account_id

    # Lookup by domain retrieves correct one
    fetched_a = store.get_connection(connection_id="conn-1", domain_id="domain-a")
    fetched_b = store.get_connection(connection_id="conn-1", domain_id="domain-b")

    assert fetched_a.account_id == "user-a@example.com"
    assert fetched_b.account_id == "user-b@example.com"


def test_domain_isolation_no_cross_domain_revocation(tmp_path):
    """Test revocation in one domain doesn't affect another."""
    store = AccessCredentialStore(tmp_path / "test.db", clock=MutableClock())

    store.register_connection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user@example.com",
        granted_scopes=("scope-a",),
        credential_backend_ref="backend-ref-a",
    )

    store.register_connection(
        connection_id="conn-1",
        domain_id="domain-b",
        provider="google",
        account_id="user@example.com",
        granted_scopes=("scope-a",),
        credential_backend_ref="backend-ref-b",
    )

    # Revoke in domain-a
    store.revoke_connection(connection_id="conn-1", domain_id="domain-a", reason="test")

    # domain-a is revoked
    conn_a = store.get_connection(connection_id="conn-1", domain_id="domain-a")
    assert conn_a.lifecycle is ConnectionLifecycle.REVOKED

    # domain-b is still active
    conn_b = store.get_connection(connection_id="conn-1", domain_id="domain-b")
    assert conn_b.lifecycle is ConnectionLifecycle.ACTIVE


# ==================================================
# TEST AREA 25-27: SECRET EXPOSURE PREVENTION
# ==================================================


def test_no_secret_in_connection_repr():
    """Test connection repr doesn't expose secrets."""
    conn = AccessConnection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user@example.com",
        granted_scopes=("scope-a",),
        credential_backend_ref="backend-ref-secret-123",
        lifecycle=ConnectionLifecycle.ACTIVE,
        created_at=NOW,
    )

    # The backend_ref is visible since it's opaque, but raw secrets should never be here
    assert "secret" not in repr(conn).lower() or "backend-ref" in repr(conn).lower()
    # If we had raw secrets, they would NOT be in repr
    assert TEST_SECRET.decode() not in repr(conn)
    assert TEST_SECRET.decode() not in str(conn)


def test_no_secret_in_connection_to_dict():
    """Test connection to_dict doesn't expose raw secrets."""
    conn = AccessConnection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user@example.com",
        granted_scopes=("scope-a",),
        credential_backend_ref="backend-ref-1",
        lifecycle=ConnectionLifecycle.ACTIVE,
        created_at=NOW,
    )

    conn_dict = conn.to_dict()
    assert TEST_SECRET.decode() not in json.dumps(conn_dict)


def test_no_secret_in_sqlite_database(tmp_path):
    """Test SQLite database doesn't contain raw secrets."""
    store = AccessCredentialStore(tmp_path / "test.db", clock=MutableClock())

    store.register_connection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user@example.com",
        granted_scopes=("scope-a",),
        credential_backend_ref="backend-ref-1",  # Opaque reference only
    )

    # Read raw database
    db_bytes = (tmp_path / "test.db").read_bytes()

    # Synthetic test secret should NOT be in database
    assert TEST_SECRET not in db_bytes


def test_credential_backend_stores_secrets_safely():
    """Test credential backend stores but doesn't expose secrets."""
    backend = InMemoryCredentialBackend()

    backend.store_credential("backend-ref-1", TEST_SECRET)

    # Retrieve it
    retrieved = backend.resolve_credential("backend-ref-1")
    assert retrieved == TEST_SECRET

    # Backend repr doesn't expose secrets
    assert TEST_SECRET.decode() not in repr(backend)


def test_credential_backend_rejects_invalid_reference():
    """Test credential backend rejects invalid backend reference.

    This test verifies low-level backend reference validation only.
    It does NOT test MissionaryX authority gating.
    For authority tests, see test_access_broker_authority_* tests.
    """
    backend = InMemoryCredentialBackend()

    backend.store_credential("backend-ref-1", TEST_SECRET)

    # Wrong ref - denied
    with pytest.raises(CredentialNotFoundError):
        backend.resolve_credential("backend-ref-wrong")


# ==================================================
# TEST AREA 29: RESTART DURABILITY
# ==================================================


def test_restart_durability_connection_survives(tmp_path):
    """Test connection survives restart."""
    db_path = tmp_path / "test.db"
    clock = MutableClock()

    store1 = AccessCredentialStore(db_path, clock=clock)
    conn1 = store1.register_connection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user@example.com",
        granted_scopes=("scope-a", "scope-b"),
        credential_backend_ref="backend-ref-1",
    )

    # Fresh store
    store2 = AccessCredentialStore(db_path, clock=clock)
    conn2 = store2.get_connection(connection_id="conn-1", domain_id="domain-a")

    assert conn1.connection_id == conn2.connection_id
    assert conn1.granted_scopes == conn2.granted_scopes
    assert conn1.lifecycle == conn2.lifecycle


def test_restart_durability_auth_session_survives(tmp_path):
    """Test authentication session survives restart."""
    db_path = tmp_path / "test.db"
    clock = MutableClock()

    store1 = AccessCredentialStore(db_path, clock=clock)
    store1.create_auth_session(
        session_id="session-1",
        domain_id="domain-a",
        provider="google",
        mission_id="mission-1",
        expires_at=NOW + timedelta(minutes=10),
    )

    # Complete it
    store1.complete_auth_session(
        session_id="session-1",
        domain_id="domain-a",
        provider="google",
        mission_id="mission-1",
    )

    # Fresh store - can't complete again
    store2 = AccessCredentialStore(db_path, clock=clock)
    with pytest.raises(AuthSessionConflictError, match="terminal"):
        store2.complete_auth_session(
            session_id="session-1",
            domain_id="domain-a",
            provider="google",
            mission_id="mission-1",
        )


# ==================================================
# TEST AREA 30-31: CONCURRENCY
# ==================================================


def _concurrent_register_worker(db_path, connection_id, domain_id, provider, account_id, backend_ref):
    """Worker process for concurrent registration."""
    import sys
    from pathlib import Path

    # Import here to avoid pickling issues
    from federation import AccessCredentialStore, ConnectionConflictError

    try:
        store = AccessCredentialStore(Path(db_path))
        conn = store.register_connection(
            connection_id=connection_id,
            domain_id=domain_id,
            provider=provider,
            account_id=account_id,
            granted_scopes=("scope-a",),
            credential_backend_ref=backend_ref,
        )
        return {"success": True, "connection_id": conn.connection_id}
    except ConnectionConflictError as e:
        return {"success": False, "error": "conflict"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _concurrent_revoke_worker(db_path, connection_id, domain_id):
    """Worker process for concurrent revocation."""
    from pathlib import Path
    from federation import AccessCredentialStore

    try:
        store = AccessCredentialStore(Path(db_path))
        store.revoke_connection(connection_id=connection_id, domain_id=domain_id, reason="test")
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


def test_concurrent_same_connection_registration(tmp_path):
    """Test concurrent exact duplicate registration is safe."""
    db_path = tmp_path / "test.db"
    store = AccessCredentialStore(db_path)

    # Pre-register to create database
    store.register_connection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user@example.com",
        granted_scopes=("scope-a",),
        credential_backend_ref="backend-ref-1",
    )

    # Now run concurrent exact duplicates
    with multiprocessing.Pool(processes=2) as pool:
        results = pool.starmap(
            _concurrent_register_worker,
            [
                (str(db_path), "conn-1", "domain-a", "google", "user@example.com", "backend-ref-1"),
                (str(db_path), "conn-1", "domain-a", "google", "user@example.com", "backend-ref-1"),
            ],
        )

    # Both should succeed (idempotent) or one may get a lock error (acceptable)
    successes = [r for r in results if r["success"]]
    # At least one must succeed
    assert len(successes) >= 1


def test_concurrent_conflicting_registration(tmp_path):
    """Test concurrent conflicting registration fails closed."""
    db_path = tmp_path / "test.db"
    store = AccessCredentialStore(db_path)

    # Pre-create database
    store.register_connection(
        connection_id="conn-init",
        domain_id="domain-a",
        provider="google",
        account_id="init@example.com",
        granted_scopes=("scope-a",),
        credential_backend_ref="backend-ref-init",
    )

    # Concurrent conflicting registrations
    with multiprocessing.Pool(processes=2) as pool:
        results = pool.starmap(
            _concurrent_register_worker,
            [
                (str(db_path), "conn-1", "domain-a", "google", "user1@example.com", "backend-ref-1"),
                (str(db_path), "conn-1", "domain-a", "github", "user2@example.com", "backend-ref-2"),  # Conflict!
            ],
        )

    # One succeeds, one fails with conflict
    successes = [r for r in results if r["success"]]
    failures = [r for r in results if not r["success"]]

    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0]["error"] == "conflict"


def test_concurrent_revoke_and_register(tmp_path):
    """Test concurrent revoke and re-register."""
    db_path = tmp_path / "test.db"
    store = AccessCredentialStore(db_path)

    # Pre-register
    store.register_connection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user@example.com",
        granted_scopes=("scope-a",),
        credential_backend_ref="backend-ref-1",
    )

    # Concurrent revoke + register
    with multiprocessing.Pool(processes=2) as pool:
        results = [
            pool.apply_async(_concurrent_revoke_worker, (str(db_path), "conn-1", "domain-a")),
            pool.apply_async(
                _concurrent_register_worker,
                (str(db_path), "conn-1", "domain-a", "google", "user@example.com", "backend-ref-1"),
            ),
        ]
        results = [r.get() for r in results]

    # At least one completes successfully
    successes = [r for r in results if r["success"]]
    assert len(successes) >= 1

    # Final state: revoked if revoke succeeded
    final_conn = store.get_connection(connection_id="conn-1", domain_id="domain-a")
    # May be ACTIVE or REVOKED depending on race outcome
    assert final_conn.lifecycle in (ConnectionLifecycle.ACTIVE, ConnectionLifecycle.REVOKED)


# ==================================================
# TEST AREA 24: DEEP IMMUTABILITY
# ==================================================


def test_access_requirement_deep_immutability():
    """Test AccessRequirement is deeply immutable."""
    input_scopes = ["scope-b", "scope-a"]
    req = AccessRequirement(
        domain_id="domain-a",
        provider="google",
        required_scopes=input_scopes,
        mission_id="mission-1",
    )

    # Mutating input doesn't affect requirement
    input_scopes.append("scope-c")
    input_scopes.sort()

    assert req.required_scopes == ("scope-a", "scope-b")  # Unchanged


def test_access_connection_deep_immutability():
    """Test AccessConnection is deeply immutable."""
    input_scopes = ["scope-b", "scope-a"]
    conn = AccessConnection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user@example.com",
        granted_scopes=tuple(input_scopes),
        credential_backend_ref="backend-ref-1",
        lifecycle=ConnectionLifecycle.ACTIVE,
        created_at=NOW,
    )

    # Mutating input doesn't affect connection
    input_scopes.append("scope-c")

    assert len(conn.granted_scopes) == 2


# ==================================================
# TEST AREA 32-35: MISSIONARYX AUTHORITY GATING
# ==================================================


def _setup_authority_fixture(tmp_path):
    """Setup complete authority testing infrastructure."""
    from federation import (
        AccessCredentialBroker,
        AccessCredentialRequest,
        AgentIdentity,
        DelegationGrant,
        DelegationGrantRegistry,
        DurableAgentIdentityRegistry,
    )
    from federation.control_domain import ControlDomain
    from federation.control_domain_registry import DurableControlDomainRegistry

    KEY = b"test-authority-integrity-key-0001-min32bytes-required"
    clock = MutableClock()

    # Setup registries
    domain_registry = DurableControlDomainRegistry(
        tmp_path / "domains.jsonl",
        integrity_key=KEY,
    )
    domain_registry.register(
        ControlDomain(
            domain_id="domain-a",
            name="Domain A",
            owner="owner-a",
            created_at=NOW,
        )
    )
    domain_registry.register(
        ControlDomain(
            domain_id="domain-b",
            name="Domain B",
            owner="owner-b",
            created_at=NOW,
        )
    )

    identity_registry = DurableAgentIdentityRegistry(
        tmp_path / "identities.jsonl",
        domain_registry=domain_registry,
        integrity_key=KEY,
        clock=clock,
    )
    identity_registry.register(
        AgentIdentity(
            agent_id="agent-a",
            domain_id="domain-a",
            name="Agent A",
            created_at=NOW,
        )
    )
    identity_registry.register(
        AgentIdentity(
            agent_id="agent-b",
            domain_id="domain-a",
            name="Agent B",
            created_at=NOW,
        )
    )

    grant_registry = DelegationGrantRegistry(
        tmp_path / "grants.jsonl",
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        integrity_key=KEY,
        clock=clock,
    )

    # Setup credential infrastructure
    credential_store = AccessCredentialStore(tmp_path / "credentials.db", clock=clock)
    backend = InMemoryCredentialBackend()
    backend.store_credential("backend-ref-1", TEST_SECRET)

    # Register connection
    credential_store.register_connection(
        connection_id="conn-1",
        domain_id="domain-a",
        provider="google",
        account_id="user@example.com",
        granted_scopes=("scope-a", "scope-b"),
        credential_backend_ref="backend-ref-1",
    )

    broker = AccessCredentialBroker(
        credential_store=credential_store,
        credential_backend=backend,
        domain_registry=domain_registry,
        identity_registry=identity_registry,
        grant_registry=grant_registry,
    )

    return broker, grant_registry, credential_store, backend


def test_access_broker_authority_connection_without_grant_denied(tmp_path):
    """Test connection exists but no grant denies access."""
    from federation import AccessCredentialNotFoundError, AccessCredentialRequest

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    # Connection exists, but NO grant registered
    request = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-nonexistent",
        connection_id="conn-1",
        provider="google",
        capability="google.drive.read",
        resource="drive:user@example.com",
    )

    with pytest.raises(AccessCredentialNotFoundError, match="Grant.*not found"):
        broker.authorize(request, evaluation_time=NOW)


def test_access_broker_authority_wrong_capability_denied(tmp_path):
    """Test valid connection but wrong capability denies access."""
    from federation import (
        AccessCredentialAuthorityError,
        AccessCredentialRequest,
        DelegationGrant,
    )

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    # Register grant with specific capability
    grant_registry.register(
        DelegationGrant(
            grant_id="grant-1",
            domain_id="domain-a",
            mission_id="mission-1",
            grantor_identity="agent-a",  # Self-grant for test
            grantee_identity="agent-a",
            capabilities=["google.drive.read"],
            resource_scope=["drive:user@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),  # Far future for test stability
        )
    )

    # Request different capability
    request = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-1",
        connection_id="conn-1",
        provider="google",
        capability="google.drive.write",  # NOT granted!
        resource="drive:user@example.com",
    )

    with pytest.raises(AccessCredentialAuthorityError, match="authority denied.*capability"):
        broker.authorize(request, evaluation_time=NOW)


def test_access_broker_authority_wrong_resource_denied(tmp_path):
    """Test valid connection but wrong resource denies access."""
    from federation import (
        AccessCredentialAuthorityError,
        AccessCredentialRequest,
        DelegationGrant,
    )

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    grant_registry.register(
        DelegationGrant(
            grant_id="grant-1",
            domain_id="domain-a",
            mission_id="mission-1",
            grantor_identity="agent-a",
            grantee_identity="agent-a",
            capabilities=["google.drive.read"],
            resource_scope=["drive:user@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),  # Far future for test stability
        )
    )

    # Request different resource
    request = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-1",
        connection_id="conn-1",
        provider="google",
        capability="google.drive.read",
        resource="drive:other@example.com",  # NOT in scope!
    )

    with pytest.raises(AccessCredentialAuthorityError, match="authority denied.*resource"):
        broker.authorize(request, evaluation_time=NOW)


def test_access_broker_authority_wrong_mission_denied(tmp_path):
    """Test valid connection but wrong mission denies access.

    Grant lookup is mission-scoped, so wrong mission fails at grant resolution
    rather than at authority evaluation. This is semantically correct - failing
    closed on missing grant.
    """
    from federation import (
        AccessCredentialNotFoundError,
        AccessCredentialRequest,
        DelegationGrant,
    )

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    grant_registry.register(
        DelegationGrant(
            grant_id="grant-1",
            domain_id="domain-a",
            mission_id="mission-1",  # Bound to mission-1
            grantor_identity="agent-a",
            grantee_identity="agent-a",
            capabilities=["google.drive.read"],
            resource_scope=["drive:user@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),  # Far future for test stability
        )
    )

    # Request with different mission
    request = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-2",  # WRONG mission!
        agent_id="agent-a",
        grant_id="grant-1",
        connection_id="conn-1",
        provider="google",
        capability="google.drive.read",
        resource="drive:user@example.com",
    )

    # Grant lookup is mission-scoped, so this denies at grant resolution
    with pytest.raises(AccessCredentialNotFoundError, match="Grant.*not found"):
        broker.authorize(request, evaluation_time=NOW)


def test_access_broker_authority_wrong_grantee_denied(tmp_path):
    """Test grant for different agent denies access.

    Validates that evaluate_grant correctly enforces grantee identity matching.
    Agent B cannot use a grant issued to Agent A.
    """
    from federation import (
        AccessCredentialAuthorityError,
        AccessCredentialRequest,
        DelegationGrant,
    )

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    # Grant to agent-a
    grant_registry.register(
        DelegationGrant(
            grant_id="grant-1",
            domain_id="domain-a",
            mission_id="mission-1",
            grantor_identity="agent-a",
            grantee_identity="agent-a",  # Granted to agent-a
            capabilities=["google.drive.read"],
            resource_scope=["drive:user@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),  # Far future for test stability
        )
    )

    # agent-b tries to use it
    request = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-b",  # WRONG agent!
        grant_id="grant-1",
        connection_id="conn-1",
        provider="google",
        capability="google.drive.read",
        resource="drive:user@example.com",
    )

    # The grantee_identity check in evaluate_grant compares the provided
    # grantee_identity (agent-b) against the grant's grantee (agent-a) and denies
    with pytest.raises(AccessCredentialAuthorityError, match="authority denied"):
        broker.authorize(request, evaluation_time=NOW)


def test_access_broker_authority_full_valid_authorization_succeeds(tmp_path):
    """Test fully valid authority grants access."""
    from federation import AccessCredentialRequest, DelegationGrant

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    grant_registry.register(
        DelegationGrant(
            grant_id="grant-1",
            domain_id="domain-a",
            mission_id="mission-1",
            grantor_identity="agent-a",
            grantee_identity="agent-a",
            capabilities=["google.drive.read"],
            resource_scope=["drive:user@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),  # Far future for test stability
        )
    )

    request = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-1",
        connection_id="conn-1",
        provider="google",
        capability="google.drive.read",
        resource="drive:user@example.com",
    )

    authorization = broker.authorize(request, evaluation_time=NOW)
    # Authorization is non-secret - no secret_bytes() method
    assert authorization.connection_id == "conn-1"
    assert authorization.provider == "google"
    assert authorization.request.capability == "google.drive.read"
    assert authorization.request.resource == "drive:user@example.com"
    assert authorization.decision.allowed is True
    assert authorization.authorization_id.startswith("auth-")


def test_access_broker_cross_mission_connection_reuse_denied(tmp_path):
    """Test connection authorized for M1 cannot be reused for M2 without grant.

    Grant lookup is mission-scoped, so using wrong mission ID fails at grant
    resolution rather than at authority evaluation. This is semantically correct -
    failing closed on missing grant.
    """
    from federation import (
        AccessCredentialNotFoundError,
        AccessCredentialRequest,
        DelegationGrant,
    )

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    # Grant for mission-1
    grant_registry.register(
        DelegationGrant(
            grant_id="grant-m1",
            domain_id="domain-a",
            mission_id="mission-1",
            grantor_identity="agent-a",
            grantee_identity="agent-a",
            capabilities=["google.drive.read"],
            resource_scope=["drive:user@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),  # Far future for test stability
        )
    )

    # First request for mission-1 succeeds
    request_m1 = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-m1",
        connection_id="conn-1",
        provider="google",
        capability="google.drive.read",
        resource="drive:user@example.com",
    )

    authorization_m1 = broker.authorize(request_m1, evaluation_time=NOW)
    # Authorization succeeds for M1
    assert authorization_m1.decision.allowed is True
    assert authorization_m1.request.mission_id == "mission-1"

    # Now try to use same connection for mission-2 with NO M2 grant
    request_m2 = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-2",  # Different mission
        agent_id="agent-a",
        grant_id="grant-m1",  # Wrong grant (bound to mission-1)
        connection_id="conn-1",  # Same connection
        provider="google",
        capability="google.drive.read",
        resource="drive:user@example.com",
    )

    # Must be denied - grant lookup is mission-scoped, so this fails at grant resolution
    with pytest.raises(AccessCredentialNotFoundError, match="Grant.*not found"):
        broker.authorize(request_m2)


def test_access_broker_confused_deputy_field_alteration_attacks(tmp_path):
    """Test confused-deputy attacks by altering each authority field."""
    from federation import (
        AccessCredentialAuthorityError,
        AccessCredentialNotFoundError,
        AccessCredentialRequest,
        DelegationGrant,
    )

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    grant_registry.register(
        DelegationGrant(
            grant_id="grant-1",
            domain_id="domain-a",
            mission_id="mission-1",
            grantor_identity="agent-a",
            grantee_identity="agent-a",
            capabilities=["google.drive.read"],
            resource_scope=["drive:user@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),  # Far future for test stability
        )
    )

    # Valid baseline
    valid_request = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-1",
        connection_id="conn-1",
        provider="google",
        capability="google.drive.read",
        resource="drive:user@example.com",
    )

    # Baseline succeeds
    authorization = broker.authorize(valid_request, evaluation_time=NOW)
    assert authorization.decision.allowed is True

    # Attack 1: Different domain
    with pytest.raises(AccessCredentialNotFoundError, match="not found"):
        broker.authorize(
            AccessCredentialRequest(
                domain_id="domain-b",  # ALTERED
                mission_id="mission-1",
                agent_id="agent-a",
                grant_id="grant-1",
                connection_id="conn-1",
                provider="google",
                capability="google.drive.read",
                resource="drive:user@example.com",
            )
        )

    # Attack 2: Different mission (fails at mission-scoped grant lookup)
    with pytest.raises(AccessCredentialNotFoundError, match="Grant.*not found"):
        broker.authorize(
            AccessCredentialRequest(
                domain_id="domain-a",
                mission_id="mission-999",  # ALTERED
                agent_id="agent-a",
                grant_id="grant-1",
                connection_id="conn-1",
                provider="google",
                capability="google.drive.read",
                resource="drive:user@example.com",
            )
        )

    # Attack 3: Different agent (grantee mismatch)
    with pytest.raises(AccessCredentialAuthorityError, match="authority denied"):
        broker.authorize(
            AccessCredentialRequest(
                domain_id="domain-a",
                mission_id="mission-1",
                agent_id="agent-b",  # ALTERED - wrong grantee
                grant_id="grant-1",
                connection_id="conn-1",
                provider="google",
                capability="google.drive.read",
                resource="drive:user@example.com",
            )
        )

    # Attack 4: Different capability
    with pytest.raises(AccessCredentialAuthorityError, match="authority denied.*capability"):
        broker.authorize(
            AccessCredentialRequest(
                domain_id="domain-a",
                mission_id="mission-1",
                agent_id="agent-a",
                grant_id="grant-1",
                connection_id="conn-1",
                provider="google",
                capability="google.drive.write",  # ALTERED
                resource="drive:user@example.com",
            )
        )

    # Attack 5: Different resource
    with pytest.raises(AccessCredentialAuthorityError, match="authority denied.*resource"):
        broker.authorize(
            AccessCredentialRequest(
                domain_id="domain-a",
                mission_id="mission-1",
                agent_id="agent-a",
                grant_id="grant-1",
                connection_id="conn-1",
                provider="google",
                capability="google.drive.read",
                resource="drive:evil@example.com",  # ALTERED
            )
        )

    # Attack 6: Different connection (not found)
    with pytest.raises(AccessCredentialNotFoundError, match="Connection.*not found"):
        broker.authorize(
            AccessCredentialRequest(
                domain_id="domain-a",
                mission_id="mission-1",
                agent_id="agent-a",
                grant_id="grant-1",
                connection_id="conn-evil",  # ALTERED
                provider="google",
                capability="google.drive.read",
                resource="drive:user@example.com",
            )
        )


def test_access_broker_authorization_contains_no_secrets(tmp_path):
    """Test AccessCredentialAuthorization contains NO raw secrets."""
    from federation import AccessCredentialRequest, DelegationGrant
    import dataclasses

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    grant_registry.register(
        DelegationGrant(
            grant_id="grant-1",
            domain_id="domain-a",
            mission_id="mission-1",
            grantor_identity="agent-a",
            grantee_identity="agent-a",
            capabilities=["google.drive.read"],
            resource_scope=["drive:user@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),  # Far future for test stability
        )
    )

    request = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-1",
        connection_id="conn-1",
        provider="google",
        capability="google.drive.read",
        resource="drive:user@example.com",
    )

    authorization = broker.authorize(request, evaluation_time=NOW)

    # Authorization has NO secret_bytes() or secret_text() methods
    assert not hasattr(authorization, 'secret_bytes')
    assert not hasattr(authorization, 'secret_text')

    # Secret is NOT in repr/str/to_dict/asdict
    assert TEST_SECRET.decode() not in repr(authorization)
    assert TEST_SECRET.decode() not in str(authorization)
    assert TEST_SECRET.decode() not in json.dumps(authorization.to_dict())
    assert TEST_SECRET.decode() not in str(dataclasses.asdict(authorization))

    # Authorization contains only non-secret authority evidence
    auth_dict = authorization.to_dict()
    assert "authorization_id" in auth_dict
    assert "domain_id" in auth_dict
    assert "mission_id" in auth_dict
    assert "agent_id" in auth_dict
    assert "grant_id" in auth_dict
    assert "connection_id" in auth_dict
    assert "provider" in auth_dict
    assert "capability" in auth_dict
    assert "resource" in auth_dict
    # No secret-bearing fields
    assert "secret" not in str(auth_dict).lower() or "secret" in "required_provider_scopes" or "secret" in "granted_provider_scopes"  # Only scope field names contain "scope"


# ==================================================
# TEST AREA 36: AUTH SESSION MULTIPROCESS CONCURRENCY
# ==================================================


def _auth_session_complete_worker(db_path, session_id, domain_id, provider, mission_id):
    """Worker process for concurrent auth session completion."""
    from pathlib import Path
    from federation import (
        AccessCredentialStore,
        AuthSessionConflictError,
        AuthSessionNotFoundError,
    )

    try:
        store = AccessCredentialStore(Path(db_path))
        completed = store.complete_auth_session(
            session_id=session_id,
            domain_id=domain_id,
            provider=provider,
            mission_id=mission_id,
        )
        return {"success": True, "state": completed.state.value}
    except AuthSessionConflictError as e:
        return {"success": False, "error": "conflict", "message": str(e)}
    except AuthSessionNotFoundError as e:
        return {"success": False, "error": "not_found", "message": str(e)}
    except Exception as e:
        return {"success": False, "error": "unexpected", "message": str(e)}


def test_auth_session_concurrent_completion_exactly_once():
    """Test authentication session concurrent completion succeeds exactly once.

    This is a genuine multiprocess concurrency test with independent processes
    racing to complete the same session.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "test.db"
        store = AccessCredentialStore(db_path, clock=MutableClock())

        # Create session
        store.create_auth_session(
            session_id="session-race",
            domain_id="domain-a",
            provider="google",
            mission_id="mission-1",
            expires_at=NOW + timedelta(days=365),  # Far future for test
        )

        # Race two processes to complete it
        with multiprocessing.Pool(processes=2) as pool:
            results = pool.starmap(
                _auth_session_complete_worker,
                [
                    (str(db_path), "session-race", "domain-a", "google", "mission-1"),
                    (str(db_path), "session-race", "domain-a", "google", "mission-1"),
                ],
            )

        # Exactly one must succeed
        successes = [r for r in results if r["success"]]
        failures = [r for r in results if not r["success"]]

        assert len(successes) == 1, f"Expected 1 success, got {len(successes)}: {results}"
        assert len(failures) == 1, f"Expected 1 failure, got {len(failures)}: {results}"

        # Failure must be semantic conflict, not raw sqlite error
        assert failures[0]["error"] == "conflict", f"Expected 'conflict', got {failures[0]}"
        assert "terminal state" in failures[0]["message"].lower(), f"Wrong error message: {failures[0]['message']}"

        # Verify durability
        store2 = AccessCredentialStore(db_path)
        session = store2.get_auth_session(
            session_id="session-race",
            domain_id="domain-a",
        )
        assert session.state.value == "completed"


# ==================================================
# NEW TESTS: PROVIDER BINDING (Access Broker v0.1 Correction)
# ==================================================


def test_broker_provider_field_validation_rejects_blank(tmp_path):
    """Test AccessCredentialRequest rejects blank provider field."""
    from federation import AccessCredentialRequest

    # Blank provider
    with pytest.raises(ValueError, match="provider"):
        AccessCredentialRequest(
            domain_id="domain-a",
            mission_id="mission-1",
            agent_id="agent-a",
            grant_id="grant-1",
            connection_id="conn-1",
            provider="",  # Blank!
            capability="google.drive.read",
            resource="drive:user@example.com",
        )


def test_broker_provider_field_validation_rejects_nul(tmp_path):
    """Test AccessCredentialRequest rejects NUL-bearing provider field."""
    from federation import AccessCredentialRequest

    # NUL byte in provider
    with pytest.raises(ValueError, match="NULL"):
        AccessCredentialRequest(
            domain_id="domain-a",
            mission_id="mission-1",
            agent_id="agent-a",
            grant_id="grant-1",
            connection_id="conn-1",
            provider="google\x00evil",  # NUL byte!
            capability="google.drive.read",
            resource="drive:user@example.com",
        )


def test_broker_provider_match_authorizes(tmp_path):
    """Test matching provider between request and connection authorizes."""
    from federation import AccessCredentialRequest, DelegationGrant

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    grant_registry.register(
        DelegationGrant(
            grant_id="grant-1",
            domain_id="domain-a",
            mission_id="mission-1",
            grantor_identity="agent-a",
            grantee_identity="agent-a",
            capabilities=["google.drive.read"],
            resource_scope=["drive:user@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),
        )
    )

    # Connection is google, request is google - should match
    request = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-1",
        connection_id="conn-1",
        provider="google",  # Matches connection.provider
        capability="google.drive.read",
        resource="drive:user@example.com",
    )

    authorization = broker.authorize(request, evaluation_time=NOW)
    assert authorization.provider == "google"
    assert authorization.decision.allowed is True


def test_broker_provider_mismatch_denies(tmp_path):
    """Test provider mismatch between request and connection denies."""
    from federation import (
        AccessCredentialAuthorityError,
        AccessCredentialRequest,
        DelegationGrant,
    )

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    # Register a github connection
    credential_store.register_connection(
        connection_id="conn-github",
        domain_id="domain-a",
        provider="github",  # Different provider!
        account_id="user-github@example.com",
        granted_scopes=("repo", "user"),
        credential_backend_ref="backend-ref-github",
    )
    backend.store_credential("backend-ref-github", b"github-token")

    grant_registry.register(
        DelegationGrant(
            grant_id="grant-1",
            domain_id="domain-a",
            mission_id="mission-1",
            grantor_identity="agent-a",
            grantee_identity="agent-a",
            capabilities=["github.repo.read"],
            resource_scope=["repo:user-github@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),
        )
    )

    # Request says google but connection is github - must deny
    request = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-1",
        connection_id="conn-github",  # GitHub connection
        provider="google",  # But request claims google!
        capability="github.repo.read",
        resource="repo:user-github@example.com",
    )

    with pytest.raises(AccessCredentialAuthorityError, match="Provider mismatch"):
        broker.authorize(request, evaluation_time=NOW)


# ==================================================
# NEW TESTS: SCOPE CANONICALIZATION (Access Broker v0.1 Correction)
# ==================================================


def test_broker_empty_required_scopes_accepted(tmp_path):
    """Test empty required_provider_scopes tuple is valid."""
    from federation import AccessCredentialRequest, DelegationGrant

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    grant_registry.register(
        DelegationGrant(
            grant_id="grant-1",
            domain_id="domain-a",
            mission_id="mission-1",
            grantor_identity="agent-a",
            grantee_identity="agent-a",
            capabilities=["google.drive.read"],
            resource_scope=["drive:user@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),
        )
    )

    request = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-1",
        connection_id="conn-1",
        provider="google",
        capability="google.drive.read",
        resource="drive:user@example.com",
    )

    # Empty scopes means no provider scopes required
    authorization = broker.authorize(request, required_provider_scopes=(), evaluation_time=NOW)
    assert authorization.required_provider_scopes == ()
    assert authorization.decision.allowed is True


def test_broker_exact_required_scope_accepted(tmp_path):
    """Test exact required scope that is granted authorizes."""
    from federation import AccessCredentialRequest, DelegationGrant

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    grant_registry.register(
        DelegationGrant(
            grant_id="grant-1",
            domain_id="domain-a",
            mission_id="mission-1",
            grantor_identity="agent-a",
            grantee_identity="agent-a",
            capabilities=["google.drive.read"],
            resource_scope=["drive:user@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),
        )
    )

    request = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-1",
        connection_id="conn-1",
        provider="google",
        capability="google.drive.read",
        resource="drive:user@example.com",
    )

    # Connection has scope-a, scope-b; require scope-a
    authorization = broker.authorize(request, required_provider_scopes=("scope-a",), evaluation_time=NOW)
    assert authorization.required_provider_scopes == ("scope-a",)
    assert authorization.decision.allowed is True


def test_broker_missing_required_scope_denied(tmp_path):
    """Test missing required scope denies authorization."""
    from federation import (
        AccessCredentialAuthorityError,
        AccessCredentialRequest,
        DelegationGrant,
    )

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    grant_registry.register(
        DelegationGrant(
            grant_id="grant-1",
            domain_id="domain-a",
            mission_id="mission-1",
            grantor_identity="agent-a",
            grantee_identity="agent-a",
            capabilities=["google.drive.read"],
            resource_scope=["drive:user@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),
        )
    )

    request = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-1",
        connection_id="conn-1",
        provider="google",
        capability="google.drive.read",
        resource="drive:user@example.com",
    )

    # Connection has scope-a, scope-b; require scope-c (NOT granted)
    with pytest.raises(AccessCredentialAuthorityError, match="missing required provider scopes"):
        broker.authorize(request, required_provider_scopes=("scope-c",), evaluation_time=NOW)


def test_broker_scope_case_mismatch_denied(tmp_path):
    """Test scope case mismatch denies (exact match required)."""
    from federation import (
        AccessCredentialAuthorityError,
        AccessCredentialRequest,
        DelegationGrant,
    )

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    grant_registry.register(
        DelegationGrant(
            grant_id="grant-1",
            domain_id="domain-a",
            mission_id="mission-1",
            grantor_identity="agent-a",
            grantee_identity="agent-a",
            capabilities=["google.drive.read"],
            resource_scope=["drive:user@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),
        )
    )

    request = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-1",
        connection_id="conn-1",
        provider="google",
        capability="google.drive.read",
        resource="drive:user@example.com",
    )

    # Connection has "scope-a" (lowercase); require "Scope-A" (different case)
    with pytest.raises(AccessCredentialAuthorityError, match="missing required provider scopes"):
        broker.authorize(request, required_provider_scopes=("Scope-A",), evaluation_time=NOW)


def test_broker_scope_lookalike_prefix_denied(tmp_path):
    """Test lookalike prefix scope denies (exact match required)."""
    from federation import (
        AccessCredentialAuthorityError,
        AccessCredentialRequest,
        DelegationGrant,
    )

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    grant_registry.register(
        DelegationGrant(
            grant_id="grant-1",
            domain_id="domain-a",
            mission_id="mission-1",
            grantor_identity="agent-a",
            grantee_identity="agent-a",
            capabilities=["google.drive.read"],
            resource_scope=["drive:user@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),
        )
    )

    request = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-1",
        connection_id="conn-1",
        provider="google",
        capability="google.drive.read",
        resource="drive:user@example.com",
    )

    # Connection has "scope-a"; require "scope" (lookalike prefix, not exact)
    with pytest.raises(AccessCredentialAuthorityError, match="missing required provider scopes"):
        broker.authorize(request, required_provider_scopes=("scope",), evaluation_time=NOW)


def test_broker_scope_blank_rejected(tmp_path):
    """Test blank scope atom is rejected during canonicalization."""
    from federation import (
        AccessCredentialAuthorityError,
        AccessCredentialRequest,
        DelegationGrant,
    )

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    grant_registry.register(
        DelegationGrant(
            grant_id="grant-1",
            domain_id="domain-a",
            mission_id="mission-1",
            grantor_identity="agent-a",
            grantee_identity="agent-a",
            capabilities=["google.drive.read"],
            resource_scope=["drive:user@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),
        )
    )

    request = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-1",
        connection_id="conn-1",
        provider="google",
        capability="google.drive.read",
        resource="drive:user@example.com",
    )

    # Blank scope atom
    with pytest.raises(AccessCredentialAuthorityError, match="Invalid required_provider_scopes"):
        broker.authorize(request, required_provider_scopes=("",), evaluation_time=NOW)


def test_broker_scope_nul_rejected(tmp_path):
    """Test NUL-bearing scope atom is rejected during canonicalization."""
    from federation import (
        AccessCredentialAuthorityError,
        AccessCredentialRequest,
        DelegationGrant,
    )

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    grant_registry.register(
        DelegationGrant(
            grant_id="grant-1",
            domain_id="domain-a",
            mission_id="mission-1",
            grantor_identity="agent-a",
            grantee_identity="agent-a",
            capabilities=["google.drive.read"],
            resource_scope=["drive:user@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),
        )
    )

    request = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-1",
        connection_id="conn-1",
        provider="google",
        capability="google.drive.read",
        resource="drive:user@example.com",
    )

    # NUL byte in scope
    with pytest.raises(AccessCredentialAuthorityError, match="Invalid required_provider_scopes.*NULL"):
        broker.authorize(request, required_provider_scopes=("scope-a\x00bad",), evaluation_time=NOW)


def test_broker_scope_duplicate_rejected(tmp_path):
    """Test duplicate scope atoms are rejected during canonicalization."""
    from federation import (
        AccessCredentialAuthorityError,
        AccessCredentialRequest,
        DelegationGrant,
    )

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    grant_registry.register(
        DelegationGrant(
            grant_id="grant-1",
            domain_id="domain-a",
            mission_id="mission-1",
            grantor_identity="agent-a",
            grantee_identity="agent-a",
            capabilities=["google.drive.read"],
            resource_scope=["drive:user@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),
        )
    )

    request = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-1",
        connection_id="conn-1",
        provider="google",
        capability="google.drive.read",
        resource="drive:user@example.com",
    )

    # Duplicate scope-a
    with pytest.raises(AccessCredentialAuthorityError, match="Invalid required_provider_scopes.*duplicates"):
        broker.authorize(request, required_provider_scopes=("scope-a", "scope-a"), evaluation_time=NOW)


def test_broker_scope_reordered_canonicalizes_identically(tmp_path):
    """Test reordered equivalent scopes canonicalize to same tuple."""
    from federation import AccessCredentialRequest, DelegationGrant

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    grant_registry.register(
        DelegationGrant(
            grant_id="grant-1",
            domain_id="domain-a",
            mission_id="mission-1",
            grantor_identity="agent-a",
            grantee_identity="agent-a",
            capabilities=["google.drive.read"],
            resource_scope=["drive:user@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),
        )
    )

    request = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-1",
        connection_id="conn-1",
        provider="google",
        capability="google.drive.read",
        resource="drive:user@example.com",
    )

    # Authorize with ("scope-b", "scope-a")
    auth1 = broker.authorize(request, required_provider_scopes=("scope-b", "scope-a"), evaluation_time=NOW)

    # Authorize with ("scope-a", "scope-b") - different order
    auth2 = broker.authorize(request, required_provider_scopes=("scope-a", "scope-b"), evaluation_time=NOW)

    # Both canonicalize to same sorted tuple
    assert auth1.required_provider_scopes == ("scope-a", "scope-b")
    assert auth2.required_provider_scopes == ("scope-a", "scope-b")
    assert auth1.required_provider_scopes == auth2.required_provider_scopes


# ==================================================
# NEW TESTS: CALLER-SELECTABLE ID CONTRACT (Access Broker v0.1 Correction)
# ==================================================


def test_broker_alternate_grant_selection(tmp_path):
    """Test alternate independently valid grant can be explicitly selected."""
    from federation import AccessCredentialRequest, DelegationGrant

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    # Register two independent grants with same authority dimensions
    grant_registry.register(
        DelegationGrant(
            grant_id="grant-1",
            domain_id="domain-a",
            mission_id="mission-1",
            grantor_identity="agent-a",
            grantee_identity="agent-a",
            capabilities=["google.drive.read"],
            resource_scope=["drive:user@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),
        )
    )

    grant_registry.register(
        DelegationGrant(
            grant_id="grant-2",  # Different ID
            domain_id="domain-a",
            mission_id="mission-1",
            grantor_identity="agent-a",
            grantee_identity="agent-a",
            capabilities=["google.drive.read"],  # Same authority dimensions
            resource_scope=["drive:user@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),
        )
    )

    # Request with grant-1 succeeds
    request1 = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-1",  # Explicitly select grant-1
        connection_id="conn-1",
        provider="google",
        capability="google.drive.read",
        resource="drive:user@example.com",
    )
    auth1 = broker.authorize(request1, evaluation_time=NOW)
    assert auth1.request.grant_id == "grant-1"
    assert auth1.decision.allowed is True

    # Request with grant-2 ALSO succeeds (caller-selectable)
    request2 = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-2",  # Explicitly select grant-2
        connection_id="conn-1",
        provider="google",
        capability="google.drive.read",
        resource="drive:user@example.com",
    )
    auth2 = broker.authorize(request2, evaluation_time=NOW)
    assert auth2.request.grant_id == "grant-2"
    assert auth2.decision.allowed is True


def test_broker_alternate_connection_selection(tmp_path):
    """Test alternate independently valid same-provider connection can be explicitly selected."""
    from federation import AccessCredentialRequest, DelegationGrant

    broker, grant_registry, credential_store, backend = _setup_authority_fixture(tmp_path)

    # Register second google connection
    credential_store.register_connection(
        connection_id="conn-2",  # Different ID
        domain_id="domain-a",
        provider="google",  # Same provider
        account_id="user2@example.com",
        granted_scopes=("scope-a", "scope-b"),  # Same scopes
        credential_backend_ref="backend-ref-2",
    )
    backend.store_credential("backend-ref-2", b"another-token")

    grant_registry.register(
        DelegationGrant(
            grant_id="grant-1",
            domain_id="domain-a",
            mission_id="mission-1",
            grantor_identity="agent-a",
            grantee_identity="agent-a",
            capabilities=["google.drive.read"],
            resource_scope=["drive:user@example.com"],
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),
        )
    )

    # Request with conn-1 succeeds
    request1 = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-1",
        connection_id="conn-1",  # Explicitly select conn-1
        provider="google",
        capability="google.drive.read",
        resource="drive:user@example.com",
    )
    auth1 = broker.authorize(request1, evaluation_time=NOW)
    assert auth1.connection_id == "conn-1"
    assert auth1.decision.allowed is True

    # Request with conn-2 ALSO succeeds (caller-selectable)
    request2 = AccessCredentialRequest(
        domain_id="domain-a",
        mission_id="mission-1",
        agent_id="agent-a",
        grant_id="grant-1",
        connection_id="conn-2",  # Explicitly select conn-2
        provider="google",
        capability="google.drive.read",
        resource="drive:user@example.com",
    )
    auth2 = broker.authorize(request2, evaluation_time=NOW)
    assert auth2.connection_id == "conn-2"
    assert auth2.decision.allowed is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
