"""Durable access and credential broker store with SQLite persistence."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from federation.access_connection import AccessConnection, ConnectionLifecycle
from federation.access_requirement import AccessRequirement
from federation.authentication_session import AuthenticationSession, AuthenticationState

_SCHEMA_VERSION = 1


class AccessCredentialError(Exception):
    """Base error for access credential store operations."""


class ConnectionNotFoundError(AccessCredentialError):
    """Raised when a connection cannot be found."""


class ConnectionConflictError(AccessCredentialError):
    """Raised when connection registration conflicts with existing data."""


class ConnectionLifecycleError(AccessCredentialError):
    """Raised when a lifecycle transition is invalid."""


class AuthSessionNotFoundError(AccessCredentialError):
    """Raised when an authentication session cannot be found."""


class AuthSessionConflictError(AccessCredentialError):
    """Raised when authentication session operation conflicts."""


class AccessCredentialCorruptionError(AccessCredentialError):
    """Raised when durable data cannot be trusted."""


def _require_text(value: Any, field_name: str) -> str:
    """Validate required text field."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


class AccessCredentialStore:
    """Durable store for access connections and authentication sessions.

    Provides:
    - Connection registration with conflict detection
    - Connection matching against requirements
    - Lifecycle management (active/revoked/reauth_required)
    - Authentication session tracking
    - Control Domain isolation
    - Restart durability

    Storage model:
    - SQLite for structured queries and transactions
    - NO raw credential material (only opaque backend references)
    - Atomic operations with IMMEDIATE transactions
    """

    def __init__(self, db_path: Path, *, clock=None) -> None:
        """Initialize store with SQLite database path."""
        self.db_path = Path(db_path)
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._init_schema()

    def _now(self) -> datetime:
        """Get current time from clock."""
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _init_schema(self) -> None:
        """Initialize database schema if needed."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")

            # Schema version tracking
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_metadata (
                    key TEXT PRIMARY KEY NOT NULL,
                    value TEXT NOT NULL
                )
            """)

            # Check/set schema version
            cursor = conn.execute("SELECT value FROM schema_metadata WHERE key = 'version'")
            row = cursor.fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_metadata (key, value) VALUES ('version', ?)",
                    (str(_SCHEMA_VERSION),),
                )
            elif int(row[0]) != _SCHEMA_VERSION:
                raise AccessCredentialCorruptionError(
                    f"Schema version mismatch: expected {_SCHEMA_VERSION}, found {row[0]}"
                )

            # Access connections table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS access_connections (
                    connection_id TEXT NOT NULL,
                    domain_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    granted_scopes_json TEXT NOT NULL,
                    credential_backend_ref TEXT NOT NULL,
                    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('active', 'revoked', 'reauth_required')),
                    created_at TEXT NOT NULL,
                    credential_generation INTEGER NOT NULL CHECK (credential_generation >= 1),
                    revoked_at TEXT,
                    revocation_reason TEXT,
                    reauth_required_at TEXT,
                    reauth_required_reason TEXT,
                    PRIMARY KEY (domain_id, connection_id),
                    CHECK (
                        (lifecycle = 'revoked' AND revoked_at IS NOT NULL AND revocation_reason IS NOT NULL) OR
                        (lifecycle != 'revoked' AND revoked_at IS NULL AND revocation_reason IS NULL)
                    ),
                    CHECK (
                        (lifecycle = 'reauth_required' AND reauth_required_at IS NOT NULL AND reauth_required_reason IS NOT NULL) OR
                        (lifecycle != 'reauth_required' AND reauth_required_at IS NULL AND reauth_required_reason IS NULL)
                    )
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_connections_domain_provider
                ON access_connections(domain_id, provider)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_connections_domain_lifecycle
                ON access_connections(domain_id, lifecycle)
            """)

            # Authentication sessions table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS authentication_sessions (
                    session_id TEXT NOT NULL,
                    domain_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    mission_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN (
                        'initiated', 'challenge_created', 'completed', 'failed', 'cancelled', 'expired'
                    )),
                    initiated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    challenge_data TEXT,
                    completed_at TEXT,
                    failed_at TEXT,
                    failure_reason TEXT,
                    cancelled_at TEXT,
                    cancellation_reason TEXT,
                    PRIMARY KEY (domain_id, session_id),
                    CHECK (expires_at > initiated_at),
                    CHECK (
                        (state = 'completed' AND completed_at IS NOT NULL) OR
                        (state != 'completed' AND completed_at IS NULL)
                    ),
                    CHECK (
                        (state = 'failed' AND failed_at IS NOT NULL AND failure_reason IS NOT NULL) OR
                        (state != 'failed' AND (failed_at IS NULL AND failure_reason IS NULL))
                    ),
                    CHECK (
                        (state = 'cancelled' AND cancelled_at IS NOT NULL AND cancellation_reason IS NOT NULL) OR
                        (state != 'cancelled' AND (cancelled_at IS NULL AND cancellation_reason IS NULL))
                    )
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sessions_domain_mission
                ON authentication_sessions(domain_id, mission_id)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sessions_domain_provider
                ON authentication_sessions(domain_id, provider)
            """)

            conn.commit()

    def register_connection(
        self,
        *,
        connection_id: str,
        domain_id: str,
        provider: str,
        account_id: str,
        granted_scopes: tuple[str, ...],
        credential_backend_ref: str,
    ) -> AccessConnection:
        """Register a new connection or return exact duplicate.

        Returns:
            Registered connection (new or existing exact duplicate)

        Raises:
            ConnectionConflictError: If connection exists with different immutable data
        """
        connection_id = _require_text(connection_id, "connection_id")
        domain_id = _require_text(domain_id, "domain_id")
        provider = _require_text(provider, "provider")
        account_id = _require_text(account_id, "account_id")
        credential_backend_ref = _require_text(credential_backend_ref, "credential_backend_ref")

        if not isinstance(granted_scopes, tuple) or not granted_scopes:
            raise ValueError("granted_scopes must be a non-empty tuple")

        scopes_json = json.dumps(list(granted_scopes), sort_keys=True)
        now = self._now()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Check for existing connection
                cursor = conn.execute(
                    """
                    SELECT connection_id, domain_id, provider, account_id, granted_scopes_json,
                           credential_backend_ref, lifecycle, created_at, credential_generation,
                           revoked_at, revocation_reason, reauth_required_at, reauth_required_reason
                    FROM access_connections
                    WHERE domain_id = ? AND connection_id = ?
                    """,
                    (domain_id, connection_id),
                )
                row = cursor.fetchone()

                if row is not None:
                    # Connection exists - check for exact duplicate vs conflict
                    existing_scopes_json = row[4]
                    existing_provider = row[2]
                    existing_account_id = row[3]
                    existing_backend_ref = row[5]

                    if (
                        existing_provider == provider
                        and existing_account_id == account_id
                        and existing_scopes_json == scopes_json
                        and existing_backend_ref == credential_backend_ref
                    ):
                        # Exact duplicate - return existing
                        return self._connection_from_row(row)

                    # Conflict: same connection_id but different immutable data
                    raise ConnectionConflictError(
                        f"Connection {connection_id!r} already exists in domain {domain_id!r} "
                        f"with conflicting data"
                    )

                # New connection - insert
                conn.execute(
                    """
                    INSERT INTO access_connections (
                        connection_id, domain_id, provider, account_id, granted_scopes_json,
                        credential_backend_ref, lifecycle, created_at, credential_generation,
                        revoked_at, revocation_reason, reauth_required_at, reauth_required_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        connection_id,
                        domain_id,
                        provider,
                        account_id,
                        scopes_json,
                        credential_backend_ref,
                        ConnectionLifecycle.ACTIVE.value,
                        now.isoformat(),
                        1,  # initial generation
                        None,
                        None,
                        None,
                        None,
                    ),
                )
                conn.commit()

                return AccessConnection(
                    connection_id=connection_id,
                    domain_id=domain_id,
                    provider=provider,
                    account_id=account_id,
                    granted_scopes=granted_scopes,
                    credential_backend_ref=credential_backend_ref,
                    lifecycle=ConnectionLifecycle.ACTIVE,
                    created_at=now,
                    credential_generation=1,
                )
            except Exception:
                conn.rollback()
                raise

    def revoke_connection(
        self,
        *,
        connection_id: str,
        domain_id: str,
        reason: str,
    ) -> AccessConnection:
        """Revoke a connection.

        Revocation is permanent and survives restart.

        Raises:
            ConnectionNotFoundError: If connection doesn't exist
            ConnectionLifecycleError: If already revoked with different reason
        """
        connection_id = _require_text(connection_id, "connection_id")
        domain_id = _require_text(domain_id, "domain_id")
        reason = _require_text(reason, "reason")
        now = self._now()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    """
                    SELECT connection_id, domain_id, provider, account_id, granted_scopes_json,
                           credential_backend_ref, lifecycle, created_at, credential_generation,
                           revoked_at, revocation_reason, reauth_required_at, reauth_required_reason
                    FROM access_connections
                    WHERE domain_id = ? AND connection_id = ?
                    """,
                    (domain_id, connection_id),
                )
                row = cursor.fetchone()

                if row is None:
                    raise ConnectionNotFoundError(
                        f"Connection {connection_id!r} not found in domain {domain_id!r}"
                    )

                existing_lifecycle = ConnectionLifecycle(row[6])

                if existing_lifecycle is ConnectionLifecycle.REVOKED:
                    # Already revoked - check reason matches
                    existing_reason = row[10]
                    if existing_reason == reason:
                        # Idempotent
                        return self._connection_from_row(row)
                    else:
                        raise ConnectionLifecycleError(
                            f"Connection {connection_id!r} already revoked with different reason"
                        )

                # Revoke it
                conn.execute(
                    """
                    UPDATE access_connections
                    SET lifecycle = ?, revoked_at = ?, revocation_reason = ?
                    WHERE domain_id = ? AND connection_id = ?
                    """,
                    (
                        ConnectionLifecycle.REVOKED.value,
                        now.isoformat(),
                        reason,
                        domain_id,
                        connection_id,
                    ),
                )
                conn.commit()

                # Fetch updated row
                cursor = conn.execute(
                    """
                    SELECT connection_id, domain_id, provider, account_id, granted_scopes_json,
                           credential_backend_ref, lifecycle, created_at, credential_generation,
                           revoked_at, revocation_reason, reauth_required_at, reauth_required_reason
                    FROM access_connections
                    WHERE domain_id = ? AND connection_id = ?
                    """,
                    (domain_id, connection_id),
                )
                row = cursor.fetchone()
                return self._connection_from_row(row)
            except Exception:
                conn.rollback()
                raise

    def get_connection(self, *, connection_id: str, domain_id: str) -> AccessConnection:
        """Get a specific connection by ID and domain.

        Raises:
            ConnectionNotFoundError: If connection doesn't exist
        """
        connection_id = _require_text(connection_id, "connection_id")
        domain_id = _require_text(domain_id, "domain_id")

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT connection_id, domain_id, provider, account_id, granted_scopes_json,
                       credential_backend_ref, lifecycle, created_at, credential_generation,
                       revoked_at, revocation_reason, reauth_required_at, reauth_required_reason
                FROM access_connections
                WHERE domain_id = ? AND connection_id = ?
                """,
                (domain_id, connection_id),
            )
            row = cursor.fetchone()

            if row is None:
                raise ConnectionNotFoundError(
                    f"Connection {connection_id!r} not found in domain {domain_id!r}"
                )

            return self._connection_from_row(row)

    def match_connection(self, requirement: AccessRequirement) -> AccessConnection | None:
        """Find an active connection that satisfies an access requirement.

        Matching rules:
        - Same ControlDomain
        - Same provider
        - Lifecycle is ACTIVE
        - Required scopes are subset of granted scopes
        - Account constraint matches if specified

        Returns:
            Matching connection or None

        Raises:
            ConnectionConflictError: If multiple connections match ambiguously
        """
        if not isinstance(requirement, AccessRequirement):
            raise TypeError("requirement must be an AccessRequirement")

        with sqlite3.connect(self.db_path) as conn:
            # Find all active connections for this domain + provider
            cursor = conn.execute(
                """
                SELECT connection_id, domain_id, provider, account_id, granted_scopes_json,
                       credential_backend_ref, lifecycle, created_at, credential_generation,
                       revoked_at, revocation_reason, reauth_required_at, reauth_required_reason
                FROM access_connections
                WHERE domain_id = ? AND provider = ? AND lifecycle = ?
                """,
                (requirement.domain_id, requirement.provider, ConnectionLifecycle.ACTIVE.value),
            )

            matches: list[AccessConnection] = []

            for row in cursor.fetchall():
                conn_account_id = row[3]
                granted_scopes_json = row[4]

                # Check account constraint if specified
                if requirement.account_constraint is not None:
                    if conn_account_id != requirement.account_constraint:
                        continue

                # Check scope matching: required must be subset of granted
                granted_scopes = set(json.loads(granted_scopes_json))
                required_scopes = set(requirement.required_scopes)

                if not required_scopes.issubset(granted_scopes):
                    continue

                # This connection matches
                matches.append(self._connection_from_row(row))

            if len(matches) == 0:
                return None

            if len(matches) == 1:
                return matches[0]

            # Multiple matches - fail closed with clear error
            connection_ids = sorted(m.connection_id for m in matches)
            raise ConnectionConflictError(
                f"Multiple connections match requirement in domain {requirement.domain_id!r} "
                f"for provider {requirement.provider!r}: {connection_ids}. "
                f"Ambiguous match - explicit connection selection required."
            )

    def create_auth_session(
        self,
        *,
        session_id: str,
        domain_id: str,
        provider: str,
        mission_id: str,
        expires_at: datetime,
    ) -> AuthenticationSession:
        """Create a new authentication session.

        Raises:
            AuthSessionConflictError: If session already exists
        """
        session_id = _require_text(session_id, "session_id")
        domain_id = _require_text(domain_id, "domain_id")
        provider = _require_text(provider, "provider")
        mission_id = _require_text(mission_id, "mission_id")
        now = self._now()

        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
            raise ValueError("expires_at must be a timezone-aware datetime")

        if expires_at <= now:
            raise ValueError("expires_at must be in the future")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Check if session already exists
                cursor = conn.execute(
                    "SELECT 1 FROM authentication_sessions WHERE domain_id = ? AND session_id = ?",
                    (domain_id, session_id),
                )

                if cursor.fetchone() is not None:
                    raise AuthSessionConflictError(
                        f"Session {session_id!r} already exists in domain {domain_id!r}"
                    )

                # Insert new session
                conn.execute(
                    """
                    INSERT INTO authentication_sessions (
                        session_id, domain_id, provider, mission_id, state,
                        initiated_at, expires_at, challenge_data,
                        completed_at, failed_at, failure_reason,
                        cancelled_at, cancellation_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        domain_id,
                        provider,
                        mission_id,
                        AuthenticationState.INITIATED.value,
                        now.isoformat(),
                        expires_at.isoformat(),
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                    ),
                )
                conn.commit()

                return AuthenticationSession(
                    session_id=session_id,
                    domain_id=domain_id,
                    provider=provider,
                    mission_id=mission_id,
                    state=AuthenticationState.INITIATED,
                    initiated_at=now,
                    expires_at=expires_at,
                )
            except Exception:
                conn.rollback()
                raise

    def complete_auth_session(
        self,
        *,
        session_id: str,
        domain_id: str,
        provider: str,
        mission_id: str,
    ) -> AuthenticationSession:
        """Complete an authentication session.

        Validates all security-critical bindings to prevent confused deputy attacks.

        Raises:
            AuthSessionNotFoundError: If session doesn't exist
            AuthSessionConflictError: If domain/provider/mission mismatch or already completed
        """
        session_id = _require_text(session_id, "session_id")
        domain_id = _require_text(domain_id, "domain_id")
        provider = _require_text(provider, "provider")
        mission_id = _require_text(mission_id, "mission_id")
        now = self._now()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    """
                    SELECT session_id, domain_id, provider, mission_id, state, initiated_at,
                           expires_at, challenge_data, completed_at, failed_at, failure_reason,
                           cancelled_at, cancellation_reason
                    FROM authentication_sessions
                    WHERE domain_id = ? AND session_id = ?
                    """,
                    (domain_id, session_id),
                )
                row = cursor.fetchone()

                if row is None:
                    raise AuthSessionNotFoundError(
                        f"Session {session_id!r} not found in domain {domain_id!r}"
                    )

                # Validate bindings
                stored_provider = row[2]
                stored_mission_id = row[3]

                if stored_provider != provider:
                    raise AuthSessionConflictError(
                        f"Session {session_id!r} provider mismatch: expected {stored_provider!r}, got {provider!r}"
                    )

                if stored_mission_id != mission_id:
                    raise AuthSessionConflictError(
                        f"Session {session_id!r} mission_id mismatch: expected {stored_mission_id!r}, got {mission_id!r}"
                    )

                state = AuthenticationState(row[4])
                expires_at = datetime.fromisoformat(row[6])

                # Check not already terminal
                if state in (
                    AuthenticationState.COMPLETED,
                    AuthenticationState.FAILED,
                    AuthenticationState.CANCELLED,
                    AuthenticationState.EXPIRED,
                ):
                    raise AuthSessionConflictError(
                        f"Session {session_id!r} is already in terminal state: {state.value}"
                    )

                # Check not expired
                if now >= expires_at:
                    raise AuthSessionConflictError(
                        f"Session {session_id!r} expired at {expires_at.isoformat()}"
                    )

                # Complete it
                conn.execute(
                    """
                    UPDATE authentication_sessions
                    SET state = ?, completed_at = ?
                    WHERE domain_id = ? AND session_id = ?
                    """,
                    (
                        AuthenticationState.COMPLETED.value,
                        now.isoformat(),
                        domain_id,
                        session_id,
                    ),
                )
                conn.commit()

                # Fetch updated
                cursor = conn.execute(
                    """
                    SELECT session_id, domain_id, provider, mission_id, state, initiated_at,
                           expires_at, challenge_data, completed_at, failed_at, failure_reason,
                           cancelled_at, cancellation_reason
                    FROM authentication_sessions
                    WHERE domain_id = ? AND session_id = ?
                    """,
                    (domain_id, session_id),
                )
                row = cursor.fetchone()
                return self._session_from_row(row)
            except Exception:
                conn.rollback()
                raise

    def _connection_from_row(self, row: tuple) -> AccessConnection:
        """Reconstruct AccessConnection from database row."""
        granted_scopes_json = row[4]
        granted_scopes = tuple(json.loads(granted_scopes_json))

        return AccessConnection(
            connection_id=row[0],
            domain_id=row[1],
            provider=row[2],
            account_id=row[3],
            granted_scopes=granted_scopes,
            credential_backend_ref=row[5],
            lifecycle=ConnectionLifecycle(row[6]),
            created_at=datetime.fromisoformat(row[7]),
            credential_generation=row[8],
            revoked_at=None if row[9] is None else datetime.fromisoformat(row[9]),
            revocation_reason=row[10],
            reauth_required_at=None if row[11] is None else datetime.fromisoformat(row[11]),
            reauth_required_reason=row[12],
        )

    def _session_from_row(self, row: tuple) -> AuthenticationSession:
        """Reconstruct AuthenticationSession from database row."""
        return AuthenticationSession(
            session_id=row[0],
            domain_id=row[1],
            provider=row[2],
            mission_id=row[3],
            state=AuthenticationState(row[4]),
            initiated_at=datetime.fromisoformat(row[5]),
            expires_at=datetime.fromisoformat(row[6]),
            challenge_data=row[7],
            completed_at=None if row[8] is None else datetime.fromisoformat(row[8]),
            failed_at=None if row[9] is None else datetime.fromisoformat(row[9]),
            failure_reason=row[10],
            cancelled_at=None if row[11] is None else datetime.fromisoformat(row[11]),
            cancellation_reason=row[12],
        )


__all__ = [
    "AccessCredentialError",
    "AccessCredentialStore",
    "AuthSessionConflictError",
    "AuthSessionNotFoundError",
    "ConnectionConflictError",
    "ConnectionLifecycleError",
    "ConnectionNotFoundError",
]
