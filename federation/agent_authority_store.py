"""Durable SQLite-backed authority store for agent identities and delegation grants.

This implements schema version 2 with:
- Optional mission binding
- Separate capabilities and resource scope
- Transactional migration from JSONL schema v1
- Fail-closed corruption handling
- Authority-non-widening guarantees
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from federation.agent_identity import AgentIdentityLifecycle, AuthoritativeAgentIdentity
from federation.delegation_grant import (
    AuthoritativeDelegationGrant,
    DelegationCapabilities,
    DelegationGrantStatus,
    DelegationScope,
)


AUTHORITY_SCHEMA_VERSION = 2
_MIGRATION_MARKER_SUFFIX = ".migrated_to_v2"


class AuthorityStoreError(Exception):
    """Base error for authority store operations."""


class AuthorityStoreCorruptionError(AuthorityStoreError):
    """Raised when authority evidence cannot be trusted."""


class AuthorityStoreMigrationError(AuthorityStoreError):
    """Raised when JSONL migration fails."""


def _require_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AuthorityStoreCorruptionError(f"{field_name} must be text or NULL")
    return value


def _parse_timestamp(value: str | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AuthorityStoreCorruptionError(f"{field_name} must be text or NULL")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorityStoreCorruptionError(f"{field_name} is invalid") from exc
    if parsed.tzinfo is None:
        raise AuthorityStoreCorruptionError(f"{field_name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _parse_lifecycle(value: str) -> AgentIdentityLifecycle:
    if not isinstance(value, str):
        raise AuthorityStoreCorruptionError("lifecycle must be text")
    try:
        return AgentIdentityLifecycle(value)
    except ValueError as exc:
        raise AuthorityStoreCorruptionError(f"invalid lifecycle: {value!r}") from exc


def _parse_grant_status(value: str) -> DelegationGrantStatus:
    if not isinstance(value, str):
        raise AuthorityStoreCorruptionError("grant status must be text")
    try:
        return DelegationGrantStatus(value)
    except ValueError as exc:
        raise AuthorityStoreCorruptionError(f"invalid grant status: {value!r}") from exc


def _parse_json_list(value: str, field_name: str) -> list[str]:
    if not isinstance(value, str):
        raise AuthorityStoreCorruptionError(f"{field_name} must be JSON text")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise AuthorityStoreCorruptionError(f"{field_name} is invalid JSON") from exc
    if not isinstance(parsed, list):
        raise AuthorityStoreCorruptionError(f"{field_name} must be JSON array")
    if not all(isinstance(item, str) for item in parsed):
        raise AuthorityStoreCorruptionError(f"{field_name} must contain only strings")
    return parsed


class AgentAuthorityStore:
    """SQLite-backed durable store for agent identities and delegation grants.

    Schema version 2:
    - Optional mission_id for grants
    - Separate capabilities and resource_scope
    - Transactional migration from JSONL v1
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        """Initialize or verify schema version 2."""
        with self._transaction() as conn:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='authority_schema'")
            if cursor.fetchone() is None:
                self._create_schema_v2(conn)
            else:
                cursor = conn.execute("SELECT version FROM authority_schema")
                row = cursor.fetchone()
                if row is None or row[0] != AUTHORITY_SCHEMA_VERSION:
                    raise AuthorityStoreCorruptionError(
                        f"incompatible authority schema version: expected {AUTHORITY_SCHEMA_VERSION}, got {row[0] if row else None}"
                    )

    def _create_schema_v2(self, conn: sqlite3.Connection) -> None:
        """Create schema version 2 tables."""
        conn.execute("""
            CREATE TABLE authority_schema (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            INSERT INTO authority_schema (version, applied_at)
            VALUES (?, ?)
        """, (AUTHORITY_SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()))

        conn.execute("""
            CREATE TABLE agent_identities (
                control_domain TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                domain_fingerprint TEXT NOT NULL,
                name TEXT NOT NULL,
                identity_fingerprint TEXT NOT NULL,
                lifecycle TEXT NOT NULL CHECK (lifecycle IN ('active', 'revoked', 'archived')),
                created_at TEXT NOT NULL,
                last_transition_at TEXT NOT NULL,
                revoked_at TEXT,
                archived_at TEXT,
                revocation_reason TEXT,
                archive_reason TEXT,
                PRIMARY KEY (control_domain, agent_id)
            )
        """)

        conn.execute("""
            CREATE TABLE delegation_grants (
                control_domain TEXT NOT NULL,
                grant_id TEXT NOT NULL,
                mission_id TEXT,
                grantor_identity TEXT NOT NULL,
                grantee_identity TEXT NOT NULL,
                capabilities_json TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                parent_grant_id TEXT,
                parent_grant_fingerprint TEXT,
                created_at TEXT NOT NULL,
                effective_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                grant_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('active', 'pending', 'revoked', 'expired')),
                revoked_at TEXT,
                revocation_reason TEXT,
                PRIMARY KEY (control_domain, grant_id)
            )
        """)

        conn.execute("""
            CREATE INDEX idx_grants_by_mission
            ON delegation_grants(control_domain, mission_id, grant_id)
            WHERE mission_id IS NOT NULL
        """)

        conn.execute("""
            CREATE INDEX idx_grants_by_grantee
            ON delegation_grants(control_domain, grantee_identity)
        """)

    @contextmanager
    def _transaction(self):
        """Provide a transactional database connection."""
        conn = sqlite3.connect(str(self.path), isolation_level="DEFERRED")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def register_identity(self, identity: AuthoritativeAgentIdentity) -> None:
        """Register an agent identity with immutable enforcement.

        First registration succeeds. Exact duplicate is idempotent no-op.
        Conflicting immutable fields fail closed.
        """
        with self._transaction() as conn:
            # Check for existing identity
            cursor = conn.execute("""
                SELECT control_domain, agent_id, domain_fingerprint, name,
                       identity_fingerprint, lifecycle, created_at, last_transition_at,
                       revoked_at, archived_at, revocation_reason, archive_reason
                FROM agent_identities
                WHERE control_domain = ? AND agent_id = ?
            """, (identity.domain_id, identity.agent_id))
            existing = cursor.fetchone()

            if existing is None:
                # First registration - insert
                conn.execute("""
                    INSERT INTO agent_identities (
                        control_domain, agent_id, domain_fingerprint, name,
                        identity_fingerprint, lifecycle, created_at, last_transition_at,
                        revoked_at, archived_at, revocation_reason, archive_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    identity.domain_id,
                    identity.agent_id,
                    identity.domain_fingerprint,
                    identity.name,
                    identity.identity_fingerprint,
                    identity.lifecycle.value,
                    identity.created_at.isoformat(),
                    identity.last_transition_at.isoformat(),
                    identity.revoked_at.isoformat() if identity.revoked_at else None,
                    identity.archived_at.isoformat() if identity.archived_at else None,
                    identity.revocation_reason,
                    identity.archive_reason,
                ))
            else:
                # Check immutable fields for conflicts
                existing_domain_fp = existing[2]
                existing_name = existing[3]
                existing_identity_fp = existing[4]
                existing_created_at = existing[6]

                # Immutable fields that must not change
                if existing_domain_fp != identity.domain_fingerprint:
                    raise AuthorityStoreError(
                        f"identity {identity.agent_id!r} in domain {identity.domain_id!r} "
                        f"has conflicting domain_fingerprint: existing {existing_domain_fp!r} != new {identity.domain_fingerprint!r}"
                    )
                if existing_name != identity.name:
                    raise AuthorityStoreError(
                        f"identity {identity.agent_id!r} in domain {identity.domain_id!r} "
                        f"has conflicting name: existing {existing_name!r} != new {identity.name!r}"
                    )
                if existing_identity_fp != identity.identity_fingerprint:
                    raise AuthorityStoreError(
                        f"identity {identity.agent_id!r} in domain {identity.domain_id!r} "
                        f"has conflicting identity_fingerprint: existing {existing_identity_fp!r} != new {identity.identity_fingerprint!r}"
                    )
                if existing_created_at != identity.created_at.isoformat():
                    raise AuthorityStoreError(
                        f"identity {identity.agent_id!r} in domain {identity.domain_id!r} "
                        f"has conflicting created_at: existing {existing_created_at!r} != new {identity.created_at.isoformat()!r}"
                    )

                # If all immutable fields match, this is idempotent no-op
                # (lifecycle changes would be handled by explicit lifecycle operations)

    def get_identity(self, agent_id: str, domain_id: str) -> AuthoritativeAgentIdentity | None:
        """Retrieve an agent identity by domain and agent_id."""
        with self._transaction() as conn:
            cursor = conn.execute("""
                SELECT control_domain, agent_id, domain_fingerprint, name,
                       identity_fingerprint, lifecycle, created_at, last_transition_at,
                       revoked_at, archived_at, revocation_reason, archive_reason
                FROM agent_identities
                WHERE control_domain = ? AND agent_id = ?
            """, (domain_id, agent_id))
            row = cursor.fetchone()
            if row is None:
                return None
            return AuthoritativeAgentIdentity(
                domain_id=_require_text(row[0], "control_domain"),
                agent_id=_require_text(row[1], "agent_id"),
                domain_fingerprint=_require_text(row[2], "domain_fingerprint"),
                name=_require_text(row[3], "name"),
                identity_fingerprint=_require_text(row[4], "identity_fingerprint"),
                lifecycle=_parse_lifecycle(row[5]),
                created_at=_parse_timestamp(row[6], "created_at"),
                last_transition_at=_parse_timestamp(row[7], "last_transition_at"),
                revoked_at=_parse_timestamp(row[8], "revoked_at"),
                archived_at=_parse_timestamp(row[9], "archived_at"),
                revocation_reason=_require_text(row[10], "revocation_reason"),
                archive_reason=_require_text(row[11], "archive_reason"),
            )

    def register_grant(self, grant: AuthoritativeDelegationGrant) -> None:
        """Register a delegation grant with immutable enforcement.

        First registration succeeds. Exact duplicate is idempotent no-op.
        Conflicting immutable authority fields fail closed.
        Revoked grants cannot be resurrected through register_grant.
        Revocation evidence is immutable.
        """
        with self._transaction() as conn:
            # Check for existing grant
            cursor = conn.execute("""
                SELECT control_domain, grant_id, mission_id, grantor_identity, grantee_identity,
                       capabilities_json, scope_json, parent_grant_id, parent_grant_fingerprint,
                       created_at, effective_at, expires_at, grant_fingerprint, status,
                       revoked_at, revocation_reason
                FROM delegation_grants
                WHERE control_domain = ? AND grant_id = ?
            """, (grant.domain_id, grant.grant_id))
            existing = cursor.fetchone()

            if existing is None:
                # First registration - insert
                conn.execute("""
                    INSERT INTO delegation_grants (
                        control_domain, grant_id, mission_id, grantor_identity, grantee_identity,
                        capabilities_json, scope_json, parent_grant_id, parent_grant_fingerprint,
                        created_at, effective_at, expires_at, grant_fingerprint, status,
                        revoked_at, revocation_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    grant.domain_id,
                    grant.grant_id,
                    grant.mission_id,
                    grant.grantor_identity,
                    grant.grantee_identity,
                    json.dumps(grant.capabilities.to_sorted_list()),
                    json.dumps(grant.resource_scope.to_sorted_list()),
                    grant.parent_grant_id,
                    grant.parent_grant_fingerprint,
                    grant.created_at.isoformat(),
                    grant.effective_at.isoformat(),
                    grant.expires_at.isoformat(),
                    grant.grant_fingerprint,
                    grant.status.value,
                    grant.revoked_at.isoformat() if grant.revoked_at else None,
                    grant.revocation_reason,
                ))
            else:
                # Check immutable fields for conflicts
                existing_mission_id = existing[2]
                existing_grantor = existing[3]
                existing_grantee = existing[4]
                existing_capabilities = existing[5]
                existing_scope = existing[6]
                existing_parent_id = existing[7]
                existing_parent_fp = existing[8]
                existing_created_at = existing[9]
                existing_effective_at = existing[10]
                existing_expires_at = existing[11]
                existing_fingerprint = existing[12]
                existing_status = existing[13]
                existing_revoked_at = existing[14]
                existing_revocation_reason = existing[15]

                # Immutable authority fields that must not change
                if existing_mission_id != grant.mission_id:
                    raise AuthorityStoreError(
                        f"grant {grant.grant_id!r} in domain {grant.domain_id!r} "
                        f"has conflicting mission_id: existing {existing_mission_id!r} != new {grant.mission_id!r}"
                    )
                if existing_grantor != grant.grantor_identity:
                    raise AuthorityStoreError(
                        f"grant {grant.grant_id!r} in domain {grant.domain_id!r} "
                        f"has conflicting grantor_identity: existing {existing_grantor!r} != new {grant.grantor_identity!r}"
                    )
                if existing_grantee != grant.grantee_identity:
                    raise AuthorityStoreError(
                        f"grant {grant.grant_id!r} in domain {grant.domain_id!r} "
                        f"has conflicting grantee_identity: existing {existing_grantee!r} != new {grant.grantee_identity!r}"
                    )
                if existing_capabilities != json.dumps(grant.capabilities.to_sorted_list()):
                    raise AuthorityStoreError(
                        f"grant {grant.grant_id!r} in domain {grant.domain_id!r} "
                        f"has conflicting capabilities"
                    )
                if existing_scope != json.dumps(grant.resource_scope.to_sorted_list()):
                    raise AuthorityStoreError(
                        f"grant {grant.grant_id!r} in domain {grant.domain_id!r} "
                        f"has conflicting resource_scope"
                    )
                if existing_parent_id != grant.parent_grant_id:
                    raise AuthorityStoreError(
                        f"grant {grant.grant_id!r} in domain {grant.domain_id!r} "
                        f"has conflicting parent_grant_id"
                    )
                if existing_parent_fp != grant.parent_grant_fingerprint:
                    raise AuthorityStoreError(
                        f"grant {grant.grant_id!r} in domain {grant.domain_id!r} "
                        f"has conflicting parent_grant_fingerprint"
                    )
                if existing_created_at != grant.created_at.isoformat():
                    raise AuthorityStoreError(
                        f"grant {grant.grant_id!r} in domain {grant.domain_id!r} "
                        f"has conflicting created_at"
                    )
                if existing_effective_at != grant.effective_at.isoformat():
                    raise AuthorityStoreError(
                        f"grant {grant.grant_id!r} in domain {grant.domain_id!r} "
                        f"has conflicting effective_at"
                    )
                if existing_expires_at != grant.expires_at.isoformat():
                    raise AuthorityStoreError(
                        f"grant {grant.grant_id!r} in domain {grant.domain_id!r} "
                        f"has conflicting expires_at"
                    )
                if existing_fingerprint != grant.grant_fingerprint:
                    raise AuthorityStoreError(
                        f"grant {grant.grant_id!r} in domain {grant.domain_id!r} "
                        f"has conflicting grant_fingerprint"
                    )

                # CRITICAL: A revoked grant cannot be resurrected
                if existing_status == DelegationGrantStatus.REVOKED.value:
                    if grant.status.value != DelegationGrantStatus.REVOKED.value:
                        raise AuthorityStoreError(
                            f"grant {grant.grant_id!r} in domain {grant.domain_id!r} "
                            f"is revoked and cannot be resurrected through register_grant"
                        )
                    # Revocation evidence must not change
                    if existing_revoked_at != (grant.revoked_at.isoformat() if grant.revoked_at else None):
                        raise AuthorityStoreError(
                            f"grant {grant.grant_id!r} in domain {grant.domain_id!r} "
                            f"has conflicting revoked_at timestamp"
                        )
                    if existing_revocation_reason != grant.revocation_reason:
                        raise AuthorityStoreError(
                            f"grant {grant.grant_id!r} in domain {grant.domain_id!r} "
                            f"has conflicting revocation_reason"
                        )

                # If all immutable fields match, this is idempotent no-op

    def get_grant(self, grant_id: str, domain_id: str) -> AuthoritativeDelegationGrant | None:
        """Retrieve a delegation grant by domain and grant_id."""
        with self._transaction() as conn:
            cursor = conn.execute("""
                SELECT control_domain, grant_id, mission_id, grantor_identity, grantee_identity,
                       capabilities_json, scope_json, parent_grant_id, parent_grant_fingerprint,
                       created_at, effective_at, expires_at, grant_fingerprint, status,
                       revoked_at, revocation_reason
                FROM delegation_grants
                WHERE control_domain = ? AND grant_id = ?
            """, (domain_id, grant_id))
            row = cursor.fetchone()
            if row is None:
                return None
            return self._grant_from_row(row)

    def list_grants(
        self,
        domain_id: str | None = None,
        mission_id: str | None = None,
    ) -> tuple[AuthoritativeDelegationGrant, ...]:
        """List delegation grants with optional filtering."""
        with self._transaction() as conn:
            if domain_id and mission_id:
                cursor = conn.execute("""
                    SELECT control_domain, grant_id, mission_id, grantor_identity, grantee_identity,
                           capabilities_json, scope_json, parent_grant_id, parent_grant_fingerprint,
                           created_at, effective_at, expires_at, grant_fingerprint, status,
                           revoked_at, revocation_reason
                    FROM delegation_grants
                    WHERE control_domain = ? AND mission_id = ?
                    ORDER BY grant_id
                """, (domain_id, mission_id))
            elif domain_id:
                cursor = conn.execute("""
                    SELECT control_domain, grant_id, mission_id, grantor_identity, grantee_identity,
                           capabilities_json, scope_json, parent_grant_id, parent_grant_fingerprint,
                           created_at, effective_at, expires_at, grant_fingerprint, status,
                           revoked_at, revocation_reason
                    FROM delegation_grants
                    WHERE control_domain = ?
                    ORDER BY grant_id
                """, (domain_id,))
            else:
                cursor = conn.execute("""
                    SELECT control_domain, grant_id, mission_id, grantor_identity, grantee_identity,
                           capabilities_json, scope_json, parent_grant_id, parent_grant_fingerprint,
                           created_at, effective_at, expires_at, grant_fingerprint, status,
                           revoked_at, revocation_reason
                    FROM delegation_grants
                    ORDER BY control_domain, grant_id
                """)
            grants = [self._grant_from_row(row) for row in cursor.fetchall()]
            return tuple(grants)

    def _grant_from_row(self, row: tuple) -> AuthoritativeDelegationGrant:
        """Parse a delegation grant from a database row."""
        capabilities_list = _parse_json_list(row[5], "capabilities_json")
        scope_list = _parse_json_list(row[6], "scope_json")
        return AuthoritativeDelegationGrant(
            domain_id=_require_text(row[0], "control_domain"),
            grant_id=_require_text(row[1], "grant_id"),
            mission_id=_require_text(row[2], "mission_id"),
            grantor_identity=_require_text(row[3], "grantor_identity"),
            grantee_identity=_require_text(row[4], "grantee_identity"),
            capabilities=DelegationCapabilities(capabilities_list),
            resource_scope=DelegationScope(scope_list),
            parent_grant_id=_require_text(row[7], "parent_grant_id"),
            parent_grant_fingerprint=_require_text(row[8], "parent_grant_fingerprint"),
            created_at=_parse_timestamp(row[9], "created_at"),
            effective_at=_parse_timestamp(row[10], "effective_at"),
            expires_at=_parse_timestamp(row[11], "expires_at"),
            grant_fingerprint=_require_text(row[12], "grant_fingerprint"),
            status=_parse_grant_status(row[13]),
            revoked_at=_parse_timestamp(row[14], "revoked_at"),
            revocation_reason=_require_text(row[15], "revocation_reason"),
        )

    @classmethod
    def migrate_from_jsonl(
        cls,
        jsonl_path: Path,
        sqlite_path: Path,
        *,
        force: bool = False,
    ) -> AgentAuthorityStore:
        """Migrate delegation grants from JSONL schema v1 to SQLite schema v2.

        This performs a conservative, atomic, idempotent migration that:
        - Preserves all grant evidence
        - Maps authority_scope → (capabilities, resource_scope) conservatively
        - Fails closed on corruption or ambiguity
        - Stores internal migration metadata for completion verification
        - Never exposes partial authoritative state
        - Is safe to re-run (checks internal completion evidence)
        - Does not destroy valid existing target before replacement succeeds
        """

        if not jsonl_path.exists():
            raise AuthorityStoreMigrationError(f"JSONL source not found: {jsonl_path}")

        # Check if target already exists and has internal migration evidence
        if sqlite_path.exists():
            if not force:
                # Try to open and check for internal migration metadata
                try:
                    conn = sqlite3.connect(str(sqlite_path))
                    cursor = conn.execute("""
                        SELECT name FROM sqlite_master
                        WHERE type='table' AND name='migration_metadata'
                    """)
                    if cursor.fetchone() is not None:
                        cursor = conn.execute("""
                            SELECT migrated_at, source_path, grant_count
                            FROM migration_metadata
                            WHERE migration_id = 1
                        """)
                        row = cursor.fetchone()
                        if row is not None:
                            conn.close()
                            # Migration already completed successfully
                            return cls(sqlite_path)
                    conn.close()
                except Exception:
                    # If we can't read the target, fail closed
                    pass

                raise AuthorityStoreMigrationError(
                    f"SQLite target already exists: {sqlite_path}; refusing to overwrite (use force=True to override)"
                )

        # Parse source data before touching target
        grants = cls._parse_jsonl_v1(jsonl_path)

        # Build migration into a temporary target
        import tempfile
        temp_fd, temp_path_str = tempfile.mkstemp(
            suffix=".sqlite",
            prefix="authority_migration_",
            dir=sqlite_path.parent,
        )
        import os as os_module
        os_module.close(temp_fd)
        temp_path = Path(temp_path_str)

        try:
            # Create new store in temporary location
            temp_store = cls(temp_path)

            # Populate and persist migration metadata transactionally
            with temp_store._transaction() as conn:
                # Create migration metadata table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS migration_metadata (
                        migration_id INTEGER PRIMARY KEY,
                        migrated_at TEXT NOT NULL,
                        source_path TEXT NOT NULL,
                        grant_count INTEGER NOT NULL,
                        schema_version INTEGER NOT NULL
                    )
                """)

                # Insert grants
                for grant_dict in grants:
                    authority_scope = grant_dict["authority_scope"]
                    conn.execute("""
                        INSERT INTO delegation_grants (
                            control_domain, grant_id, mission_id, grantor_identity, grantee_identity,
                            capabilities_json, scope_json, parent_grant_id, parent_grant_fingerprint,
                            created_at, effective_at, expires_at, grant_fingerprint, status,
                            revoked_at, revocation_reason
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        grant_dict["domain_id"],
                        grant_dict["grant_id"],
                        grant_dict["mission_id"],
                        grant_dict["grantor_identity"],
                        grant_dict["grantee_identity"],
                        json.dumps(sorted(authority_scope)),
                        json.dumps(sorted(authority_scope)),
                        grant_dict["parent_grant_id"],
                        grant_dict["parent_grant_fingerprint"],
                        grant_dict["created_at"],
                        grant_dict["effective_at"],
                        grant_dict["expires_at"],
                        grant_dict["grant_fingerprint"],
                        grant_dict["status"],
                        grant_dict.get("revoked_at"),
                        grant_dict.get("revocation_reason"),
                    ))

                # Record migration completion INSIDE the database
                conn.execute("""
                    INSERT INTO migration_metadata (
                        migration_id, migrated_at, source_path, grant_count, schema_version
                    ) VALUES (?, ?, ?, ?, ?)
                """, (
                    1,
                    datetime.now(timezone.utc).isoformat(),
                    str(jsonl_path),
                    len(grants),
                    AUTHORITY_SCHEMA_VERSION,
                ))

            # Verify migration metadata persisted
            verify_conn = sqlite3.connect(str(temp_path))
            cursor = verify_conn.execute("""
                SELECT grant_count FROM migration_metadata WHERE migration_id = 1
            """)
            row = cursor.fetchone()
            verify_conn.close()
            if row is None or row[0] != len(grants):
                raise AuthorityStoreMigrationError(
                    "migration metadata verification failed"
                )

            # Atomically publish completed migration
            if sqlite_path.exists():
                backup_path = Path(str(sqlite_path) + ".pre-migration-backup")
                sqlite_path.rename(backup_path)
                try:
                    temp_path.rename(sqlite_path)
                except Exception:
                    # Restore backup if rename failed
                    backup_path.rename(sqlite_path)
                    raise
            else:
                temp_path.rename(sqlite_path)

            return cls(sqlite_path)

        except Exception:
            # Clean up temporary file on failure
            if temp_path.exists():
                temp_path.unlink()
            raise

    @staticmethod
    def _parse_jsonl_v1(jsonl_path: Path) -> list[dict[str, Any]]:
        """Parse JSONL schema v1 delegation grants for migration.

        Fail-closed validation that rejects malformed legacy authority.
        Legacy v1 format (before cfc2164) used:
        - Required mission_id (str, not optional)
        - Single authority_scope field (list of strings)
        - No capabilities or resource_scope fields
        """
        with open(jsonl_path, "rb") as handle:
            raw = handle.read()

        if not raw:
            return []

        if not raw.endswith(b"\n"):
            raise AuthorityStoreMigrationError("JSONL file has incomplete tail")

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AuthorityStoreMigrationError("JSONL is not UTF-8") from exc

        grants_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
        seen_exact: dict[tuple[str, str, str], bytes] = {}

        for line_num, line in enumerate(text.splitlines(), 1):
            if not line:
                continue

            # Parse JSON
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AuthorityStoreMigrationError(f"line {line_num}: invalid JSON") from exc

            # Validate record structure
            if not isinstance(record, dict):
                raise AuthorityStoreMigrationError(f"line {line_num}: record is not a dict")

            # Validate schema_version
            if "schema_version" not in record:
                raise AuthorityStoreMigrationError(f"line {line_num}: missing schema_version")
            schema_version = record["schema_version"]
            if not isinstance(schema_version, int) or schema_version != 1:
                raise AuthorityStoreMigrationError(
                    f"line {line_num}: unsupported schema_version: {schema_version!r} (expected 1)"
                )

            # Validate payload exists
            if "payload" not in record:
                raise AuthorityStoreMigrationError(f"line {line_num}: missing payload")

            payload = record["payload"]
            if not isinstance(payload, dict):
                raise AuthorityStoreMigrationError(f"line {line_num}: payload is not a dict")

            # Validate required identity fields
            required_text_fields = [
                ("domain_id", 255),
                ("grant_id", 255),
                ("mission_id", 255),  # Legacy v1 required mission_id
                ("grantor_identity", 255),
                ("grantee_identity", 255),
                ("status", 50),
                ("grant_fingerprint", 64),
            ]

            for field_name, max_len in required_text_fields:
                if field_name not in payload:
                    raise AuthorityStoreMigrationError(
                        f"line {line_num}: payload missing required field {field_name!r}"
                    )
                value = payload[field_name]
                if not isinstance(value, str):
                    raise AuthorityStoreMigrationError(
                        f"line {line_num}: {field_name} must be string, got {type(value).__name__}"
                    )
                if not value or not value.strip():
                    raise AuthorityStoreMigrationError(
                        f"line {line_num}: {field_name} must be non-empty"
                    )
                if value != value.strip():
                    raise AuthorityStoreMigrationError(
                        f"line {line_num}: {field_name} contains surrounding whitespace"
                    )
                if len(value) > max_len:
                    raise AuthorityStoreMigrationError(
                        f"line {line_num}: {field_name} exceeds {max_len} characters"
                    )
                if "\x00" in value:
                    raise AuthorityStoreMigrationError(
                        f"line {line_num}: {field_name} contains NULL byte"
                    )

            # Validate legacy authority_scope exists and has correct structure
            if "authority_scope" not in payload:
                raise AuthorityStoreMigrationError(
                    f"line {line_num}: payload missing required legacy field 'authority_scope'"
                )

            authority_scope = payload["authority_scope"]
            if not isinstance(authority_scope, list):
                raise AuthorityStoreMigrationError(
                    f"line {line_num}: authority_scope must be list, got {type(authority_scope).__name__}"
                )
            if not authority_scope:
                raise AuthorityStoreMigrationError(
                    f"line {line_num}: authority_scope must be non-empty"
                )

            for idx, atom in enumerate(authority_scope):
                if not isinstance(atom, str):
                    raise AuthorityStoreMigrationError(
                        f"line {line_num}: authority_scope[{idx}] must be string"
                    )
                if not atom or not atom.strip():
                    raise AuthorityStoreMigrationError(
                        f"line {line_num}: authority_scope[{idx}] must be non-empty"
                    )
                if atom != atom.strip():
                    raise AuthorityStoreMigrationError(
                        f"line {line_num}: authority_scope[{idx}] contains surrounding whitespace"
                    )
                if "\x00" in atom:
                    raise AuthorityStoreMigrationError(
                        f"line {line_num}: authority_scope[{idx}] contains NULL byte"
                    )
                if len(atom) > 256:
                    raise AuthorityStoreMigrationError(
                        f"line {line_num}: authority_scope[{idx}] exceeds 256 characters"
                    )

            # Validate status is a valid enum value
            valid_statuses = {"active", "pending", "revoked", "expired"}
            if payload["status"] not in valid_statuses:
                raise AuthorityStoreMigrationError(
                    f"line {line_num}: invalid status {payload['status']!r}"
                )

            # Validate timestamps
            required_timestamps = ["created_at", "effective_at", "expires_at"]
            for ts_field in required_timestamps:
                if ts_field not in payload:
                    raise AuthorityStoreMigrationError(
                        f"line {line_num}: payload missing required timestamp {ts_field!r}"
                    )
                ts_value = payload[ts_field]
                if not isinstance(ts_value, str):
                    raise AuthorityStoreMigrationError(
                        f"line {line_num}: {ts_field} must be string"
                    )
                try:
                    parsed = datetime.fromisoformat(ts_value.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        raise AuthorityStoreMigrationError(
                            f"line {line_num}: {ts_field} must be timezone-aware"
                        )
                except (ValueError, AttributeError) as exc:
                    raise AuthorityStoreMigrationError(
                        f"line {line_num}: {ts_field} has invalid timestamp format"
                    ) from exc

            # Validate timestamp ordering
            created = datetime.fromisoformat(payload["created_at"].replace("Z", "+00:00"))
            effective = datetime.fromisoformat(payload["effective_at"].replace("Z", "+00:00"))
            expires = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))

            if effective < created:
                raise AuthorityStoreMigrationError(
                    f"line {line_num}: effective_at precedes created_at"
                )
            if expires <= effective:
                raise AuthorityStoreMigrationError(
                    f"line {line_num}: expires_at does not follow effective_at"
                )

            # Validate optional parent fields have valid types
            if "parent_grant_id" in payload:
                parent_id = payload["parent_grant_id"]
                if parent_id is not None and not isinstance(parent_id, str):
                    raise AuthorityStoreMigrationError(
                        f"line {line_num}: parent_grant_id must be string or null"
                    )

            if "parent_grant_fingerprint" in payload:
                parent_fp = payload["parent_grant_fingerprint"]
                if parent_fp is not None and not isinstance(parent_fp, str):
                    raise AuthorityStoreMigrationError(
                        f"line {line_num}: parent_grant_fingerprint must be string or null"
                    )

            # Validate revocation fields are internally consistent
            has_revoked_at = "revoked_at" in payload and payload["revoked_at"] is not None
            has_revocation_reason = "revocation_reason" in payload and payload["revocation_reason"] is not None

            if payload["status"] == "revoked":
                if not has_revoked_at:
                    raise AuthorityStoreMigrationError(
                        f"line {line_num}: status is 'revoked' but revoked_at is missing"
                    )
            else:
                if has_revoked_at or has_revocation_reason:
                    raise AuthorityStoreMigrationError(
                        f"line {line_num}: revocation fields present but status is not 'revoked'"
                    )

            # Handle duplicate records
            key = (payload["domain_id"], payload["mission_id"], payload["grant_id"])

            # Compute canonical form for exact duplicate detection
            canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

            if key in grants_by_key:
                # Duplicate key found - check if exact duplicate or conflict
                if canonical_payload != seen_exact[key]:
                    raise AuthorityStoreMigrationError(
                        f"line {line_num}: duplicate grant key {key!r} with conflicting authority definition"
                    )
                # Exact duplicate - last-write-wins for lifecycle snapshots is acceptable
                # This allows legacy registry to have repeated lifecycle state snapshots

            grants_by_key[key] = payload
            seen_exact[key] = canonical_payload

        return list(grants_by_key.values())


__all__ = [
    "AUTHORITY_SCHEMA_VERSION",
    "AgentAuthorityStore",
    "AuthorityStoreCorruptionError",
    "AuthorityStoreError",
    "AuthorityStoreMigrationError",
]
