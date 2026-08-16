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
        """Register or update an agent identity."""
        with self._transaction() as conn:
            conn.execute("""
                INSERT INTO agent_identities (
                    control_domain, agent_id, domain_fingerprint, name,
                    identity_fingerprint, lifecycle, created_at, last_transition_at,
                    revoked_at, archived_at, revocation_reason, archive_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(control_domain, agent_id) DO UPDATE SET
                    lifecycle = excluded.lifecycle,
                    last_transition_at = excluded.last_transition_at,
                    revoked_at = excluded.revoked_at,
                    archived_at = excluded.archived_at,
                    revocation_reason = excluded.revocation_reason,
                    archive_reason = excluded.archive_reason
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
        """Register or update a delegation grant."""
        with self._transaction() as conn:
            conn.execute("""
                INSERT INTO delegation_grants (
                    control_domain, grant_id, mission_id, grantor_identity, grantee_identity,
                    capabilities_json, scope_json, parent_grant_id, parent_grant_fingerprint,
                    created_at, effective_at, expires_at, grant_fingerprint, status,
                    revoked_at, revocation_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(control_domain, grant_id) DO UPDATE SET
                    status = excluded.status,
                    revoked_at = excluded.revoked_at,
                    revocation_reason = excluded.revocation_reason
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

        This performs a conservative, transactional migration that:
        - Preserves all grant evidence
        - Maps authority_scope → (capabilities, resource_scope) conservatively
        - Fails closed on corruption or ambiguity
        - Creates a migration marker to prevent re-migration
        - Is idempotent (can be safely re-run)
        """
        marker_path = Path(str(jsonl_path) + _MIGRATION_MARKER_SUFFIX)

        if marker_path.exists() and not force:
            raise AuthorityStoreMigrationError(
                f"migration marker exists: {marker_path}; migration already completed (use force=True to override)"
            )

        if not jsonl_path.exists():
            raise AuthorityStoreMigrationError(f"JSONL source not found: {jsonl_path}")

        if sqlite_path.exists() and not force:
            raise AuthorityStoreMigrationError(
                f"SQLite target already exists: {sqlite_path}; refusing to overwrite (use force=True to override)"
            )

        grants = cls._parse_jsonl_v1(jsonl_path)

        if sqlite_path.exists():
            sqlite_path.unlink()

        store = cls(sqlite_path)

        with store._transaction() as conn:
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

        marker_path.write_text(
            json.dumps({
                "migrated_at": datetime.now(timezone.utc).isoformat(),
                "source": str(jsonl_path),
                "target": str(sqlite_path),
                "grant_count": len(grants),
            })
        )

        return store

    @staticmethod
    def _parse_jsonl_v1(jsonl_path: Path) -> list[dict[str, Any]]:
        """Parse JSONL schema v1 delegation grants for migration."""
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

        for line in text.splitlines():
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AuthorityStoreMigrationError("JSONL contains invalid JSON") from exc

            if not isinstance(record, dict):
                raise AuthorityStoreMigrationError("JSONL record is not a dict")
            if "payload" not in record:
                raise AuthorityStoreMigrationError("JSONL record missing payload")

            payload = record["payload"]
            key = (payload["domain_id"], payload["mission_id"], payload["grant_id"])
            grants_by_key[key] = payload

        return list(grants_by_key.values())


__all__ = [
    "AUTHORITY_SCHEMA_VERSION",
    "AgentAuthorityStore",
    "AuthorityStoreCorruptionError",
    "AuthorityStoreError",
    "AuthorityStoreMigrationError",
]
