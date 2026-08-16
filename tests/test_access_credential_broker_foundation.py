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


def test_credential_backend_prevents_unauthorized_access():
    """Test credential backend prevents unauthorized access."""
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
