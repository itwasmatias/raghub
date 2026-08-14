"""Durable, concurrency-safe effect registry storage.

MissionaryX v0.1 — Durable Effect Store

Replaces process-local authoritative effect state with SQLite-backed persistence
whose decisions survive restarts and remain correct under simultaneous registry
instances, threads, and processes.

This module persists all authoritative effect safety state:
- Effect intents (write-ahead commitments)
- Effect dispatches (transport attempts)
- Authority reservations (capability allocation and disposition)
- Reconciliation obligations (indeterminate effect tracking)
- Idempotency bindings (duplicate detection)
- Release records (evidence-backed authority recovery)

Architecture guarantees:
- All authorization-relevant mutations are atomic database transactions
- ControlDomain scoping enforced at schema level (composite keys)
- Concurrent identical commits produce one canonical record
- Concurrent conflicting payloads under same domain+key fail explicitly
- Cross-domain operations are isolated (no bare-ID lookups)
- Terminal decisions are monotonic and evidence-backed
- Unknown schema versions fail closed
- Corrupt or invalid stored data fails closed
- Lock contention is bounded (30s timeout)

Limitations:
- Single-host SQLite (no distributed consensus)
- Storage integrity ≠ evidence authenticity (Evidence Spine remains authoritative)
- Migration from in-memory state requires explicit data export/import
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from federation.control_domain import validate_domain_id

if TYPE_CHECKING:
    from research_mission.evidence_spine import EvidencePointer, EvidenceSpine

from federation.effect_safety import (
    AuthorityDisposition,
    AuthorityReservation,
    EffectDispatch,
    EffectIntent,
    ReconciliationObligation,
    ReconciliationState,
    ProviderReconcilability,
)


# Schema version for fail-closed compatibility checking
SCHEMA_VERSION = 2


class DurableEffectStoreError(Exception):
    """Base error for durable effect store operations."""


class SchemaVersionError(DurableEffectStoreError):
    """Raised when database schema version is incompatible."""


class StorageIntegrityError(DurableEffectStoreError):
    """Raised when stored data fails validation or reconstruction."""


class ConcurrencyConflictError(DurableEffectStoreError):
    """Raised when concurrent operations conflict."""


_FILE_JOURNAL_MODE = "wal"
_MAX_BUSY_TIMEOUT_MS = 30000

_EXPECTED_SCHEMA_TABLES: dict[str, tuple[str, ...]] = {
    "effect_store_schema": ("version", "applied_at"),
    "effect_intents": (
        "control_domain",
        "effect_intent_id",
        "decision_id",
        "mission_id",
        "task_id",
        "attempt_id",
        "operation_digest",
        "idempotency_key",
        "provider_scope",
        "authority_reservation_id",
        "compensation_strategy",
        "evidence_reference",
        "state",
        "created_at",
    ),
    "effect_dispatches": (
        "control_domain",
        "dispatch_id",
        "effect_intent_id",
        "attempt_id",
        "idempotency_key",
        "provider_adapter",
        "capability_profile_version",
        "transport_digest",
        "posture",
        "provider_operation_id",
        "evidence_reference",
        "dispatched_at",
    ),
    "authority_reservations": (
        "control_domain",
        "reservation_id",
        "effect_intent_id",
        "capability_type",
        "amount",
        "disposition",
        "reserved_at",
        "disposition_at",
        "disposition_evidence_json",
    ),
    "reconciliation_obligations": (
        "control_domain",
        "obligation_id",
        "effect_intent_id",
        "dispatch_id",
        "state",
        "provider_reconcilability",
        "next_probe_at",
        "probe_history_json",
        "terminal_disposition_json",
        "created_at",
    ),
    "reservation_bindings": (
        "control_domain",
        "authority_reservation_id",
        "effect_intent_id",
        "bound_at",
    ),
    "reservation_releases": (
        "control_domain",
        "reservation_id",
        "effect_intent_id",
        "evidence_fingerprint",
        "released_at",
    ),
    "effect_gateway_claims": (
        "control_domain",
        "gateway_claim_id",
        "request_fingerprint",
        "effect_intent_id",
        "effect_dispatch_id",
        "authority_reservation_id",
        "delegation_grant_id",
        "delegation_grant_fingerprint",
        "requested_capability",
        "idempotency_key",
        "operation_digest",
        "provider_id",
        "adapter_id",
        "owner_identity",
        "state",
        "permit_verifier",
        "claimed_at",
        "expires_at",
        "handoff_started_at",
        "receipt_recorded_at",
        "terminal_at",
    ),
    "effect_gateway_permits": (
        "control_domain",
        "permit_id",
        "permit_verifier",
        "request_fingerprint",
        "effect_intent_id",
        "effect_dispatch_id",
        "authority_reservation_id",
        "gateway_claim_id",
        "delegation_grant_id",
        "delegation_grant_fingerprint",
        "requested_capability",
        "operation_digest",
        "idempotency_key",
        "provider_id",
        "adapter_id",
        "credential_scope_json",
        "owner_identity",
        "issued_at",
        "expires_at",
        "consumed_at",
        "revoked_at",
    ),
}

_EXPECTED_INDEXES: dict[str, tuple[str, tuple[str, ...]]] = {
    "idx_intents_mission": ("effect_intents", ("control_domain", "mission_id", "created_at")),
    "idx_dispatches_intent": ("effect_dispatches", ("control_domain", "effect_intent_id")),
    "idx_reservations_intent": ("authority_reservations", ("control_domain", "effect_intent_id")),
    "idx_reservations_disposition": ("authority_reservations", ("control_domain", "disposition")),
    "idx_obligations_state": ("reconciliation_obligations", ("control_domain", "state", "next_probe_at")),
    "idx_gateway_claims_intent": ("effect_gateway_claims", ("control_domain", "effect_intent_id")),
    "idx_gateway_claims_idempotency": ("effect_gateway_claims", ("control_domain", "idempotency_key")),
    "idx_gateway_claims_request": ("effect_gateway_claims", ("control_domain", "request_fingerprint")),
    "idx_gateway_permits_claim": ("effect_gateway_permits", ("control_domain", "gateway_claim_id")),
    "idx_gateway_permits_verifier": ("effect_gateway_permits", ("control_domain", "permit_verifier")),
}

_EXPECTED_SCHEMA_TABLES_V1: dict[str, tuple[str, ...]] = {
    name: columns
    for name, columns in _EXPECTED_SCHEMA_TABLES.items()
    if name not in {"effect_gateway_claims", "effect_gateway_permits"}
}

_EXPECTED_INDEXES_V1: dict[str, tuple[str, tuple[str, ...]]] = {
    name: spec
    for name, spec in _EXPECTED_INDEXES.items()
    if not name.startswith("idx_gateway_")
}


def _is_lower_hex_64(value: object) -> bool:
    """Return True when value is exactly 64 lowercase hex characters."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _serialize_timestamp(dt: datetime) -> str:
    """Serialize datetime to ISO format UTC string."""
    return dt.astimezone(timezone.utc).isoformat()


def _deserialize_timestamp(value: str | None) -> datetime | None:
    """Deserialize ISO format UTC string to datetime."""
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _serialize_evidence_pointer(pointer: EvidencePointer | None) -> str | None:
    """Serialize EvidencePointer to JSON."""
    if pointer is None:
        return None
    return json.dumps(pointer.to_dict(), ensure_ascii=False, sort_keys=True)


def _deserialize_evidence_pointer(value: str | None) -> EvidencePointer | None:
    """Deserialize JSON to EvidencePointer."""
    if value is None:
        return None
    from research_mission.evidence_spine import EvidencePointer, EvidenceCorrelationKey
    data = json.loads(value)
    key_data = data["key"]
    key = EvidenceCorrelationKey(
        source=key_data["source"],
        record_id=key_data["record_id"],
        mission_id=key_data.get("mission_id"),
        task_id=key_data.get("task_id"),
        domain_id=key_data.get("domain_id"),
    )
    return EvidencePointer(
        key=key,
        reference_fingerprint=data["reference_fingerprint"],
        record_fingerprint=data["record_fingerprint"],
    )


def _serialize_probe_history(history: tuple[dict[str, Any], ...]) -> str:
    """Serialize probe history to JSON."""
    return json.dumps(list(history), ensure_ascii=False, sort_keys=True)


def _deserialize_probe_history(value: str) -> tuple[dict[str, Any], ...]:
    """Deserialize JSON to probe history tuple."""
    return tuple(json.loads(value))


def _execute_sql_script(connection: sqlite3.Connection, sql: str) -> None:
    """Execute a migration script statement-by-statement within the caller transaction."""
    for statement in sql.split(";"):
        statement = statement.strip()
        if statement:
            connection.execute(statement)


class DurableEffectStore:
    """Durable, concurrency-safe storage for authoritative effect state.

    Provides transactional persistence for:
    - Effect intents and dispatches
    - Authority reservations and dispositions
    - Reconciliation obligations
    - Idempotency and reservation bindings
    - Evidence-backed releases

    All operations are domain-scoped and transaction-protected.
    Concurrent access from multiple processes/threads is safe.
    """

    # Schema migrations: (version, sql)
    MIGRATIONS = (
        (
            1,
            """
            -- Schema version tracking
            CREATE TABLE IF NOT EXISTS effect_store_schema (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            -- Effect intents (write-ahead commitments)
            CREATE TABLE IF NOT EXISTS effect_intents (
                control_domain TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                decision_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                task_id TEXT,
                attempt_id TEXT NOT NULL,
                operation_digest TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                provider_scope TEXT NOT NULL,
                authority_reservation_id TEXT NOT NULL,
                compensation_strategy TEXT,
                evidence_reference TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, effect_intent_id)
            );

            -- Effect dispatches (transport attempts)
            CREATE TABLE IF NOT EXISTS effect_dispatches (
                control_domain TEXT NOT NULL,
                dispatch_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                provider_adapter TEXT NOT NULL,
                capability_profile_version TEXT NOT NULL,
                transport_digest TEXT NOT NULL,
                posture TEXT NOT NULL,
                provider_operation_id TEXT,
                evidence_reference TEXT NOT NULL,
                dispatched_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, dispatch_id),
                FOREIGN KEY (control_domain, effect_intent_id)
                    REFERENCES effect_intents(control_domain, effect_intent_id)
            );

            -- Authority reservations
            CREATE TABLE IF NOT EXISTS authority_reservations (
                control_domain TEXT NOT NULL,
                reservation_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                capability_type TEXT NOT NULL,
                amount REAL NOT NULL CHECK (amount >= 0),
                disposition TEXT NOT NULL,
                reserved_at TEXT NOT NULL,
                disposition_at TEXT,
                disposition_evidence_json TEXT,
                PRIMARY KEY (control_domain, reservation_id),
                FOREIGN KEY (control_domain, effect_intent_id)
                    REFERENCES effect_intents(control_domain, effect_intent_id)
            );

            -- Reconciliation obligations
            CREATE TABLE IF NOT EXISTS reconciliation_obligations (
                control_domain TEXT NOT NULL,
                obligation_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                dispatch_id TEXT,
                state TEXT NOT NULL,
                provider_reconcilability TEXT NOT NULL,
                next_probe_at TEXT,
                probe_history_json TEXT NOT NULL,
                terminal_disposition_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, obligation_id),
                FOREIGN KEY (control_domain, effect_intent_id)
                    REFERENCES effect_intents(control_domain, effect_intent_id),
                FOREIGN KEY (control_domain, dispatch_id)
                    REFERENCES effect_dispatches(control_domain, dispatch_id)
            );

            -- Idempotency bindings: track reservation usage by intent
            CREATE TABLE IF NOT EXISTS reservation_bindings (
                control_domain TEXT NOT NULL,
                authority_reservation_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                bound_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (control_domain, authority_reservation_id)
            );

            -- Release tracking: evidence-backed authority releases
            CREATE TABLE IF NOT EXISTS reservation_releases (
                control_domain TEXT NOT NULL,
                reservation_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                evidence_fingerprint TEXT NOT NULL,
                released_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (control_domain, reservation_id)
            );

            -- Indexes for common queries
            CREATE INDEX IF NOT EXISTS idx_intents_mission
                ON effect_intents(control_domain, mission_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_dispatches_intent
                ON effect_dispatches(control_domain, effect_intent_id);
            CREATE INDEX IF NOT EXISTS idx_reservations_intent
                ON authority_reservations(control_domain, effect_intent_id);
            CREATE INDEX IF NOT EXISTS idx_reservations_disposition
                ON authority_reservations(control_domain, disposition);
            CREATE INDEX IF NOT EXISTS idx_obligations_state
                ON reconciliation_obligations(control_domain, state, next_probe_at);
            """,
        ),
        (
            2,
            """
            -- Gateway v0.1: Durable single-winner dispatch claims and single-use permits

            -- Gateway dispatch claims: enforce single-winner dispatch ownership
            CREATE TABLE IF NOT EXISTS effect_gateway_claims (
                control_domain TEXT NOT NULL,
                gateway_claim_id TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT NOT NULL,
                authority_reservation_id TEXT NOT NULL,
                delegation_grant_id TEXT NOT NULL,
                delegation_grant_fingerprint TEXT NOT NULL,
                requested_capability TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                operation_digest TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                adapter_id TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN (
                        'prepared',
                        'claimed',
                        'handoff_started',
                        'receipt_recorded',
                        'terminal',
                        'indeterminate'
                    )
                ),
                permit_verifier TEXT,
                claimed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL CHECK (expires_at > claimed_at),
                handoff_started_at TEXT,
                receipt_recorded_at TEXT,
                terminal_at TEXT,
                PRIMARY KEY (control_domain, gateway_claim_id),
                FOREIGN KEY (control_domain, effect_intent_id)
                    REFERENCES effect_intents(control_domain, effect_intent_id),
                FOREIGN KEY (control_domain, effect_dispatch_id)
                    REFERENCES effect_dispatches(control_domain, dispatch_id),
                FOREIGN KEY (control_domain, authority_reservation_id)
                    REFERENCES authority_reservations(control_domain, reservation_id),
                UNIQUE (control_domain, effect_intent_id),
                UNIQUE (control_domain, effect_dispatch_id),
                UNIQUE (control_domain, authority_reservation_id),
                UNIQUE (control_domain, idempotency_key),
                UNIQUE (control_domain, request_fingerprint),
                CHECK (
                    permit_verifier IS NULL
                    OR (
                        typeof(permit_verifier) = 'text'
                        AND length(permit_verifier) = 64
                        AND permit_verifier NOT GLOB '*[^0-9a-f]*'
                    )
                )
            );

            -- Gateway dispatch permits: cryptographic single-use adapter authorization
            CREATE TABLE IF NOT EXISTS effect_gateway_permits (
                control_domain TEXT NOT NULL,
                permit_id TEXT NOT NULL,
                permit_verifier TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT NOT NULL,
                authority_reservation_id TEXT NOT NULL,
                gateway_claim_id TEXT NOT NULL,
                delegation_grant_id TEXT NOT NULL,
                delegation_grant_fingerprint TEXT NOT NULL,
                requested_capability TEXT NOT NULL,
                operation_digest TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                adapter_id TEXT NOT NULL,
                credential_scope_json TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                expires_at TEXT NOT NULL CHECK (expires_at > issued_at),
                consumed_at TEXT,
                revoked_at TEXT,
                PRIMARY KEY (control_domain, permit_id),
                FOREIGN KEY (control_domain, gateway_claim_id)
                    REFERENCES effect_gateway_claims(control_domain, gateway_claim_id),
                FOREIGN KEY (control_domain, effect_intent_id)
                    REFERENCES effect_intents(control_domain, effect_intent_id),
                FOREIGN KEY (control_domain, effect_dispatch_id)
                    REFERENCES effect_dispatches(control_domain, dispatch_id),
                FOREIGN KEY (control_domain, authority_reservation_id)
                    REFERENCES authority_reservations(control_domain, reservation_id),
                CHECK (
                    typeof(permit_verifier) = 'text'
                    AND length(permit_verifier) = 64
                    AND permit_verifier NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (NOT (consumed_at IS NOT NULL AND revoked_at IS NOT NULL))
            );

            -- Indexes for gateway operations
            CREATE INDEX IF NOT EXISTS idx_gateway_claims_intent
                ON effect_gateway_claims(control_domain, effect_intent_id);
            CREATE INDEX IF NOT EXISTS idx_gateway_claims_idempotency
                ON effect_gateway_claims(control_domain, idempotency_key);
            CREATE INDEX IF NOT EXISTS idx_gateway_claims_request
                ON effect_gateway_claims(control_domain, request_fingerprint);
            CREATE INDEX IF NOT EXISTS idx_gateway_permits_claim
                ON effect_gateway_permits(control_domain, gateway_claim_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_gateway_permits_verifier
                ON effect_gateway_permits(control_domain, permit_verifier);
            """,
        ),
    )

    def __init__(self, database_path: str | Path, *, busy_timeout_ms: int = _MAX_BUSY_TIMEOUT_MS) -> None:
        """Initialize durable effect store.

        Args:
            database_path: Path to SQLite database file

        Raises:
            SchemaVersionError: If database schema version is incompatible
        """
        self.database_path = str(database_path)
        self._closed = False
        self._busy_timeout_ms = self._validate_busy_timeout_ms(busy_timeout_ms)

        # For in-memory databases, keep a persistent connection
        # Otherwise each _connect() creates a new empty database
        if self.database_path == ":memory:":
            self._memory_connection = self._create_connection(self.database_path)
        else:
            self._memory_connection = None
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)

        self._ensure_schema()

    def _create_connection(self, database_path: str, *, require_wal: bool = True) -> sqlite3.Connection:
        """Create a new database connection with proper configuration.

        Args:
            database_path: Path to database

        Returns:
            Configured SQLite connection

        Configuration:
        - WAL mode for concurrent readers/writers (file-based only)
        - 30s busy timeout for lock contention
        - Foreign keys enabled
        """
        connection = sqlite3.connect(database_path, timeout=self._busy_timeout_ms / 1000.0)
        # WAL mode only works for file-based databases
        if database_path != ":memory:" and require_wal:
            self._ensure_wal_mode(connection, database_path)
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _validate_busy_timeout_ms(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("busy_timeout_ms must be an int")
        if value <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        if value > _MAX_BUSY_TIMEOUT_MS:
            raise ValueError(f"busy_timeout_ms exceeds {_MAX_BUSY_TIMEOUT_MS} ms")
        return value

    def _ensure_wal_mode(self, connection: sqlite3.Connection, database_path: str) -> None:
        """Require WAL journal mode for file-backed stores."""
        attempts = 3
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(attempts):
            try:
                row = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise StorageIntegrityError(
                        f"Failed to establish WAL journal mode for {database_path}: {exc}"
                    ) from exc
                last_error = exc
                if attempt < attempts - 1:
                    time.sleep(0.01 * (attempt + 1))
                continue

            mode = row[0] if row else None
            if isinstance(mode, str) and mode.lower() == _FILE_JOURNAL_MODE:
                actual_row = connection.execute("PRAGMA journal_mode").fetchone()
                actual_mode = actual_row[0] if actual_row else None
                if isinstance(actual_mode, str) and actual_mode.lower() == _FILE_JOURNAL_MODE:
                    return
                raise StorageIntegrityError(
                    f"Database journal mode {actual_mode!r} does not satisfy required WAL mode"
                )
            raise StorageIntegrityError(
                f"Database journal mode {mode!r} does not satisfy required WAL mode"
            )

        raise StorageIntegrityError(
            f"Failed to establish WAL journal mode for {database_path} after {attempts} attempts"
        ) from last_error

    def _ensure_open(self) -> None:
        """Fail closed if the store has been closed."""
        if self._closed:
            raise DurableEffectStoreError("DurableEffectStore has been closed")

    def _connect(self) -> sqlite3.Connection:
        """Get database connection.

        Returns:
            Configured SQLite connection

        For in-memory databases, returns the persistent connection.
        For file-based databases, creates a new connection.
        """
        self._ensure_open()
        if self._memory_connection is not None:
            return self._memory_connection
        return self._create_connection(self.database_path)

    @contextmanager
    def _connection(self, *, require_wal: bool = True) -> Iterator[sqlite3.Connection]:
        """Context manager for database connections.

        For in-memory databases, yields the persistent connection without closing.
        For file-based databases, creates and closes a new connection.

        Yields:
            Database connection
        """
        self._ensure_open()
        if self._memory_connection is not None:
            # In-memory: use persistent connection, don't close
            yield self._memory_connection
        else:
            # File-based: create new connection, close when done
            with closing(self._create_connection(self.database_path, require_wal=require_wal)) as conn:
                yield conn

    def _ensure_schema(self) -> None:
        """Ensure database schema is initialized and compatible.

        Raises:
            SchemaVersionError: If schema version is unknown or incompatible
        """
        self._ensure_open()
        with self._connection(require_wal=False) as connection:
            with connection:
                for attempt in range(5):
                    try:
                        existing_objects = {
                            row[0]
                            for row in connection.execute(
                                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
                            )
                        }
                    except sqlite3.OperationalError as exc:
                        if "locked" in str(exc).lower():
                            if attempt < 4:
                                time.sleep(0.01 * (attempt + 1))
                                continue
                            raise ConcurrencyConflictError(
                                "Database lock timeout while initializing schema (locked)"
                            ) from exc
                        raise

                    has_schema_table = "effect_store_schema" in existing_objects
                    if has_schema_table:
                        existing_versions = [
                            int(row[0])
                            for row in connection.execute(
                                "SELECT version FROM effect_store_schema ORDER BY version"
                            )
                        ]

                        if existing_versions:
                            distinct_versions = sorted(set(existing_versions))
                            if len(distinct_versions) != 1:
                                raise SchemaVersionError(
                                    f"Database schema metadata contains contradictory versions: "
                                    f"{distinct_versions}"
                                )

                            current_version = distinct_versions[0]
                            if current_version < 1:
                                raise SchemaVersionError(
                                    f"Database schema version {current_version} is older than "
                                    f"current version {SCHEMA_VERSION} and is not compatible with "
                                    "automatic migration"
                                )
                            if current_version > SCHEMA_VERSION:
                                raise SchemaVersionError(
                                    f"Database schema version {current_version} is newer than "
                                    f"supported version {SCHEMA_VERSION}. Upgrade required."
                                )
                            if current_version == 1:
                                self._validate_schema_contract(
                                    connection,
                                    expected_tables=_EXPECTED_SCHEMA_TABLES_V1,
                                    expected_indexes=_EXPECTED_INDEXES_V1,
                                    expected_version=1,
                                )
                                try:
                                    connection.execute("BEGIN IMMEDIATE")
                                except sqlite3.OperationalError as exc:
                                    if "locked" in str(exc).lower() and attempt < 4:
                                        time.sleep(0.01 * (attempt + 1))
                                        continue
                                    if "locked" in str(exc).lower():
                                        raise ConcurrencyConflictError(
                                            "Database lock timeout while initializing schema (locked)"
                                        ) from exc
                                    raise
                                refreshed_versions = [
                                    int(row[0])
                                    for row in connection.execute(
                                        "SELECT version FROM effect_store_schema ORDER BY version"
                                    )
                                ]
                                if refreshed_versions != [1]:
                                    connection.rollback()
                                    if refreshed_versions == [SCHEMA_VERSION]:
                                        self._validate_schema_contract(connection)
                                        self._validate_stored_rows(connection)
                                        connection.commit()
                                        if self.database_path != ":memory:":
                                            with closing(
                                                self._create_connection(
                                                    self.database_path,
                                                    require_wal=False,
                                                )
                                            ) as wal_connection:
                                                self._ensure_wal_mode(wal_connection, self.database_path)
                                        return
                                    continue
                                _execute_sql_script(connection, self.MIGRATIONS[1][1])
                                connection.execute("DELETE FROM effect_store_schema")
                                connection.execute(
                                    "INSERT INTO effect_store_schema(version) VALUES (?)",
                                    (SCHEMA_VERSION,),
                                )
                            self._validate_schema_contract(connection)
                            self._validate_stored_rows(connection)
                            connection.commit()
                            if self.database_path != ":memory:":
                                with closing(
                                    self._create_connection(
                                        self.database_path,
                                        require_wal=False,
                                    )
                                ) as wal_connection:
                                    self._ensure_wal_mode(wal_connection, self.database_path)
                            return

                    if existing_objects:
                        if attempt < 4:
                            time.sleep(0.01 * (attempt + 1))
                            continue
                        raise SchemaVersionError(
                            "Database contains schema objects but no authoritative current-version metadata"
                        )

                    # New database - apply migrations
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                    except sqlite3.OperationalError as exc:
                        if "locked" in str(exc).lower() and attempt < 4:
                            time.sleep(0.01 * (attempt + 1))
                            continue
                        if "locked" in str(exc).lower():
                            raise ConcurrencyConflictError(
                                "Database lock timeout while initializing schema (locked)"
                            ) from exc
                        raise
                    refreshed_objects = {
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
                        )
                    }
                    if "effect_store_schema" in refreshed_objects:
                        connection.rollback()
                        continue
                    for version, sql in self.MIGRATIONS:
                        if version > SCHEMA_VERSION:
                            # Skip future migrations
                            continue
                        _execute_sql_script(connection, sql)
                    connection.execute("DELETE FROM effect_store_schema")
                    connection.execute(
                        "INSERT INTO effect_store_schema(version) VALUES (?)",
                        (SCHEMA_VERSION,),
                    )
                    self._validate_schema_contract(connection)
                    self._validate_stored_rows(connection)
                    connection.commit()
                    if self.database_path != ":memory:":
                        with closing(
                            self._create_connection(
                                self.database_path,
                                require_wal=False,
                            )
                        ) as wal_connection:
                            self._ensure_wal_mode(wal_connection, self.database_path)
                    return

    def _validate_schema_contract(
        self,
        connection: sqlite3.Connection,
        *,
        expected_tables: dict[str, tuple[str, ...]] = _EXPECTED_SCHEMA_TABLES,
        expected_indexes: dict[str, tuple[str, tuple[str, ...]]] = _EXPECTED_INDEXES,
        expected_version: int = SCHEMA_VERSION,
    ) -> None:
        """Fail closed if required schema objects are absent or malformed."""
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        expected_table_names = set(expected_tables)
        missing_tables = expected_table_names - tables
        unexpected_tables = tables - expected_table_names
        if missing_tables or unexpected_tables:
            raise SchemaVersionError(
                "Database schema is incomplete; "
                f"missing tables: {sorted(missing_tables)}; "
                f"unexpected tables: {sorted(unexpected_tables)}"
            )

        for table_name, expected_columns in expected_tables.items():
            columns = tuple(
                row[1]
                for row in connection.execute(f"PRAGMA table_info({table_name})")
            )
            if columns != expected_columns:
                raise SchemaVersionError(
                    f"Database schema table {table_name} has incompatible columns: {columns!r}"
                )

        for index_name, (table_name, expected_columns) in expected_indexes.items():
            index_row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name = ?",
                (index_name,),
            ).fetchone()
            if index_row is None:
                raise SchemaVersionError(f"Database schema is incomplete; missing index {index_name}")
            indexed_columns = tuple(
                row[2]
                for row in connection.execute(f"PRAGMA index_info({index_name})")
            )
            if indexed_columns != expected_columns:
                raise SchemaVersionError(
                    f"Database index {index_name} on {table_name} has incompatible columns: "
                    f"{indexed_columns!r}"
                )

        version_rows = [
            int(row[0])
            for row in connection.execute("SELECT version FROM effect_store_schema ORDER BY version")
        ]
        if not version_rows:
            raise SchemaVersionError("Database schema version metadata is empty")
        if version_rows != [expected_version]:
            raise SchemaVersionError(
                f"Database schema version metadata must contain exactly [{expected_version}], "
                f"found {version_rows!r}"
            )

    def _validate_stored_rows(self, connection: sqlite3.Connection) -> None:
        """Fail closed if authoritative stored rows contain invalid domain bindings."""
        for table_name in (
            "effect_intents",
            "effect_dispatches",
            "authority_reservations",
            "reconciliation_obligations",
            "reservation_bindings",
            "reservation_releases",
            "effect_gateway_claims",
            "effect_gateway_permits",
        ):
            # Check if table exists (for v1 databases before migration to v2)
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            ).fetchone()
            if not table_exists:
                continue

            for (stored_domain,) in connection.execute(
                f"SELECT control_domain FROM {table_name}"
            ):
                try:
                    validate_domain_id(stored_domain, "control_domain")
                except ValueError as exc:
                    raise StorageIntegrityError(
                        f"Stored control_domain in {table_name} failed validation: {stored_domain!r}"
                    ) from exc

        for (
            stored_domain,
            reservation_id,
            disposition,
            disposition_evidence_json,
        ) in connection.execute(
            """
            SELECT control_domain, reservation_id, disposition, disposition_evidence_json
            FROM authority_reservations
            """
        ):
            if disposition_evidence_json is None:
                continue
            try:
                pointer = _deserialize_evidence_pointer(disposition_evidence_json)
            except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                raise StorageIntegrityError(
                    f"Stored reservation {reservation_id} failed evidence pointer validation"
                ) from exc
            if pointer is not None and pointer.key.domain_id is not None and pointer.key.domain_id != stored_domain:
                raise StorageIntegrityError(
                    f"Stored reservation {reservation_id} has mismatched evidence control_domain"
                )

            if disposition not in {
                AuthorityDisposition.RESERVED.value,
                AuthorityDisposition.CONSUMED.value,
                AuthorityDisposition.RELEASED.value,
                AuthorityDisposition.ASSUMED_CONSUMED_UNRECONCILED.value,
            }:
                raise StorageIntegrityError(
                    f"Stored reservation {reservation_id} has invalid disposition {disposition!r}"
                )

        for (
            stored_domain,
            gateway_claim_id,
            request_fingerprint,
            effect_intent_id,
            effect_dispatch_id,
            authority_reservation_id,
            delegation_grant_id,
            delegation_grant_fingerprint,
            requested_capability,
            idempotency_key,
            operation_digest,
            provider_id,
            adapter_id,
            owner_identity,
            state,
            permit_verifier,
            claimed_at,
            expires_at,
        ) in connection.execute(
            """
            SELECT control_domain, gateway_claim_id, request_fingerprint, effect_intent_id,
                   effect_dispatch_id, authority_reservation_id, delegation_grant_id,
                   delegation_grant_fingerprint, requested_capability, idempotency_key,
                   operation_digest, provider_id, adapter_id, owner_identity, state,
                   permit_verifier, claimed_at, expires_at
            FROM effect_gateway_claims
            """
        ):
            if state not in {
                "prepared",
                "claimed",
                "handoff_started",
                "receipt_recorded",
                "terminal",
                "indeterminate",
            }:
                raise StorageIntegrityError(
                    f"Stored gateway claim {gateway_claim_id} has invalid state {state!r}"
                )
            if permit_verifier is not None and not _is_lower_hex_64(permit_verifier):
                raise StorageIntegrityError(
                    f"Stored gateway claim {gateway_claim_id} has invalid permit_verifier"
                )
            claimed_at_dt = _deserialize_timestamp(claimed_at)
            expires_at_dt = _deserialize_timestamp(expires_at)
            if (
                claimed_at_dt is None
                or expires_at_dt is None
                or expires_at_dt <= claimed_at_dt
            ):
                raise StorageIntegrityError(
                    f"Stored gateway claim {gateway_claim_id} has invalid timestamps"
                )
            if not all(
                isinstance(value, str) and value.strip()
                for value in (
                    request_fingerprint,
                    effect_intent_id,
                    effect_dispatch_id,
                    authority_reservation_id,
                    delegation_grant_id,
                    delegation_grant_fingerprint,
                    requested_capability,
                    idempotency_key,
                    operation_digest,
                    provider_id,
                    adapter_id,
                    owner_identity,
                )
            ):
                raise StorageIntegrityError(
                    f"Stored gateway claim {gateway_claim_id} has invalid binding fields"
                )
            if state == "indeterminate" and permit_verifier is None:
                raise StorageIntegrityError(
                    f"Stored gateway claim {gateway_claim_id} is missing permit verifier"
                )

        for (
            stored_domain,
            permit_id,
            permit_verifier,
            request_fingerprint,
            effect_intent_id,
            effect_dispatch_id,
            authority_reservation_id,
            gateway_claim_id,
            delegation_grant_id,
            delegation_grant_fingerprint,
            requested_capability,
            operation_digest,
            idempotency_key,
            provider_id,
            adapter_id,
            credential_scope_json,
            owner_identity,
            issued_at,
            expires_at,
            consumed_at,
            revoked_at,
        ) in connection.execute(
            """
            SELECT control_domain, permit_id, permit_verifier, request_fingerprint,
                   effect_intent_id, effect_dispatch_id, authority_reservation_id,
                   gateway_claim_id, delegation_grant_id, delegation_grant_fingerprint,
                   requested_capability, operation_digest, idempotency_key, provider_id,
                   adapter_id, credential_scope_json, owner_identity, issued_at, expires_at,
                   consumed_at, revoked_at
            FROM effect_gateway_permits
            """
        ):
            if not _is_lower_hex_64(permit_verifier):
                raise StorageIntegrityError(
                    f"Stored permit {permit_id} has invalid verifier"
                )
            if not all(
                isinstance(value, str) and value.strip()
                for value in (
                    request_fingerprint,
                    effect_intent_id,
                    effect_dispatch_id,
                    authority_reservation_id,
                    gateway_claim_id,
                    delegation_grant_id,
                    delegation_grant_fingerprint,
                    requested_capability,
                    operation_digest,
                    idempotency_key,
                    provider_id,
                    adapter_id,
                    credential_scope_json,
                    owner_identity,
                )
            ):
                raise StorageIntegrityError(
                    f"Stored permit {permit_id} has invalid binding fields"
                )
            issued_at_dt = _deserialize_timestamp(issued_at)
            expires_at_dt = _deserialize_timestamp(expires_at)
            if issued_at_dt is None or expires_at_dt is None or expires_at_dt <= issued_at_dt:
                raise StorageIntegrityError(
                    f"Stored permit {permit_id} has invalid timestamps"
                )
            if consumed_at is not None and revoked_at is not None:
                raise StorageIntegrityError(
                    f"Stored permit {permit_id} has contradictory lifecycle state"
                )
            try:
                scopes = json.loads(credential_scope_json)
            except json.JSONDecodeError as exc:
                raise StorageIntegrityError(
                    f"Stored permit {permit_id} has invalid credential scope JSON"
                ) from exc
            if not isinstance(scopes, list) or any(
                not isinstance(scope, str) or not scope.strip() for scope in scopes
            ):
                raise StorageIntegrityError(
                    f"Stored permit {permit_id} has invalid credential scope JSON"
                )

    def commit_intent(self, intent: EffectIntent) -> None:
        """Commit a write-ahead intent atomically.

        Domain-scoped: intents and reservations are isolated per control_domain.

        Args:
            intent: Effect intent to commit

        Raises:
            TypeError: If intent is not an EffectIntent
            ValueError: If authority reservation already used by different intent
            ValueError: If effect_intent_id exists with different payload
            ConcurrencyConflictError: If database lock cannot be acquired
            StorageIntegrityError: If transaction fails
        """
        if type(intent) is not EffectIntent:
            raise TypeError("intent must be an EffectIntent")

        domain = validate_domain_id(intent.control_domain, "control_domain")
        self._ensure_open()

        try:
            with self._connection() as connection:
                with connection:
                    # Check for idempotent retry (same intent_id within domain)
                    existing = connection.execute(
                        """
                        SELECT operation_digest, idempotency_key, provider_scope,
                               authority_reservation_id, compensation_strategy,
                               evidence_reference, state
                        FROM effect_intents
                        WHERE control_domain = ? AND effect_intent_id = ?
                        """,
                        (domain, intent.effect_intent_id),
                    ).fetchone()

                    if existing is not None:
                        # Verify payload is identical for idempotent retry
                        if (
                            existing[0] != intent.operation_digest
                            or existing[1] != intent.idempotency_key
                            or existing[2] != intent.provider_scope
                            or existing[3] != intent.authority_reservation_id
                            or existing[4] != intent.compensation_strategy
                            or existing[5] != intent.evidence_reference
                            or existing[6] != intent.state
                        ):
                            raise ValueError(
                                f"effect_intent_id {intent.effect_intent_id} already committed "
                                f"with different payload in domain {domain}"
                            )
                        # Idempotent retry - safe to return
                        return

                    # Check for authority reservation double-spend (within domain)
                    existing_intent_id = connection.execute(
                        """
                        SELECT effect_intent_id FROM reservation_bindings
                        WHERE control_domain = ? AND authority_reservation_id = ?
                        """,
                        (domain, intent.authority_reservation_id),
                    ).fetchone()

                    if existing_intent_id is not None:
                        if existing_intent_id[0] != intent.effect_intent_id:
                            raise ValueError(
                                f"authority reservation {intent.authority_reservation_id} "
                                f"already committed to effect_intent_id {existing_intent_id[0]} "
                                f"in domain {domain}"
                            )

                    # Insert new intent (handle race condition with concurrent insert)
                    try:
                        connection.execute(
                            """
                            INSERT INTO effect_intents (
                                control_domain, effect_intent_id, decision_id, mission_id,
                                task_id, attempt_id, operation_digest, idempotency_key,
                                provider_scope, authority_reservation_id, compensation_strategy,
                                evidence_reference, state, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                domain,
                                intent.effect_intent_id,
                                intent.decision_id,
                                intent.mission_id,
                                intent.task_id,
                                intent.attempt_id,
                                intent.operation_digest,
                                intent.idempotency_key,
                                intent.provider_scope,
                                intent.authority_reservation_id,
                                intent.compensation_strategy,
                                intent.evidence_reference,
                                intent.state,
                                _serialize_timestamp(intent.created_at),
                            ),
                        )

                        # Bind reservation to intent
                        connection.execute(
                            """
                            INSERT INTO reservation_bindings (
                                control_domain, authority_reservation_id, effect_intent_id
                            ) VALUES (?, ?, ?)
                            """,
                            (domain, intent.authority_reservation_id, intent.effect_intent_id),
                        )
                    except sqlite3.IntegrityError as integrity_error:
                        # Concurrent insert detected - re-check for idempotent retry
                        # Roll back the failed transaction
                        connection.rollback()

                        # Re-query to check if it's an idempotent retry or a conflict
                        existing = connection.execute(
                            """
                            SELECT operation_digest, idempotency_key, provider_scope,
                                   authority_reservation_id, compensation_strategy,
                                   evidence_reference, state
                            FROM effect_intents
                            WHERE control_domain = ? AND effect_intent_id = ?
                            """,
                            (domain, intent.effect_intent_id),
                        ).fetchone()

                        if existing is not None:
                            # Verify payload is identical for idempotent retry
                            if (
                                existing[0] == intent.operation_digest
                                and existing[1] == intent.idempotency_key
                                and existing[2] == intent.provider_scope
                                and existing[3] == intent.authority_reservation_id
                                and existing[4] == intent.compensation_strategy
                                and existing[5] == intent.evidence_reference
                                and existing[6] == intent.state
                            ):
                                # Idempotent concurrent commit - safe to return
                                return
                            else:
                                # Conflicting payload
                                raise ValueError(
                                    f"effect_intent_id {intent.effect_intent_id} already committed "
                                    f"with different payload in domain {domain}"
                                ) from integrity_error
                        else:
                            # Check if reservation binding failed
                            existing_binding = connection.execute(
                                """
                                SELECT effect_intent_id FROM reservation_bindings
                                WHERE control_domain = ? AND authority_reservation_id = ?
                                """,
                                (domain, intent.authority_reservation_id),
                            ).fetchone()

                            if existing_binding is not None:
                                raise ValueError(
                                    f"authority reservation {intent.authority_reservation_id} "
                                    f"already committed to effect_intent_id {existing_binding[0]} "
                                    f"in domain {domain}"
                                ) from integrity_error
                            else:
                                # Unknown integrity error
                                raise StorageIntegrityError(
                                    f"Integrity constraint violation: {integrity_error}"
                                ) from integrity_error

        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                raise ConcurrencyConflictError(
                    f"Database lock timeout while committing intent {intent.effect_intent_id}"
                ) from e
            raise StorageIntegrityError(f"Failed to commit intent: {e}") from e

    def commit_dispatch(self, dispatch: EffectDispatch) -> None:
        """Register an authoritative dispatch against its committed intent.

        Domain-scoped: dispatch must match intent's domain.

        Args:
            dispatch: Effect dispatch to commit

        Raises:
            TypeError: If dispatch is not an EffectDispatch
            ValueError: If intent not found or domain mismatch
            ConcurrencyConflictError: If database lock cannot be acquired
            StorageIntegrityError: If transaction fails
        """
        if type(dispatch) is not EffectDispatch:
            raise TypeError("dispatch must be an EffectDispatch")

        domain = validate_domain_id(dispatch.control_domain, "control_domain")
        self._ensure_open()

        try:
            with self._connection() as connection:
                with connection:
                    # Look up intent in the same domain
                    intent = connection.execute(
                        """
                        SELECT attempt_id, idempotency_key
                        FROM effect_intents
                        WHERE control_domain = ? AND effect_intent_id = ?
                        """,
                        (domain, dispatch.effect_intent_id),
                    ).fetchone()

                    if intent is None:
                        raise ValueError(
                            f"Cannot register dispatch {dispatch.dispatch_id}: committed "
                            f"effect intent {dispatch.effect_intent_id!r} not found in domain {domain}"
                        )

                    # Verify attempt_id and idempotency_key match
                    if intent[0] != dispatch.attempt_id:
                        raise ValueError(
                            f"Dispatch attempt_id mismatch: expected {intent[0]!r}, "
                            f"found {dispatch.attempt_id!r}"
                        )
                    if intent[1] != dispatch.idempotency_key:
                        raise ValueError(
                            f"Dispatch idempotency_key mismatch: expected {intent[1]!r}, "
                            f"found {dispatch.idempotency_key!r}"
                        )

                    # Check for idempotent retry
                    existing = connection.execute(
                        """
                        SELECT provider_adapter, transport_digest, posture,
                               provider_operation_id, evidence_reference
                        FROM effect_dispatches
                        WHERE control_domain = ? AND dispatch_id = ?
                        """,
                        (domain, dispatch.dispatch_id),
                    ).fetchone()

                    if existing is not None:
                        # Verify payload is identical
                        if (
                            existing[0] != dispatch.provider_adapter
                            or existing[1] != dispatch.transport_digest
                            or existing[2] != dispatch.posture
                            or existing[3] != dispatch.provider_operation_id
                            or existing[4] != dispatch.evidence_reference
                        ):
                            raise ValueError(
                                f"dispatch_id {dispatch.dispatch_id} already registered "
                                f"with different payload in domain {domain}"
                            )
                        # Idempotent retry
                        return

                    # Insert dispatch (handle race condition with concurrent insert)
                    try:
                        connection.execute(
                            """
                            INSERT INTO effect_dispatches (
                                control_domain, dispatch_id, effect_intent_id, attempt_id,
                                idempotency_key, provider_adapter, capability_profile_version,
                                transport_digest, posture, provider_operation_id,
                                evidence_reference, dispatched_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                domain,
                                dispatch.dispatch_id,
                                dispatch.effect_intent_id,
                                dispatch.attempt_id,
                                dispatch.idempotency_key,
                                dispatch.provider_adapter,
                                dispatch.capability_profile_version,
                                dispatch.transport_digest,
                                dispatch.posture,
                                dispatch.provider_operation_id,
                                dispatch.evidence_reference,
                                _serialize_timestamp(dispatch.dispatched_at),
                            ),
                        )
                    except sqlite3.IntegrityError as integrity_error:
                        # Concurrent insert detected - re-check for idempotent retry
                        connection.rollback()

                        existing = connection.execute(
                            """
                            SELECT provider_adapter, transport_digest, posture,
                                   provider_operation_id, evidence_reference
                            FROM effect_dispatches
                            WHERE control_domain = ? AND dispatch_id = ?
                            """,
                            (domain, dispatch.dispatch_id),
                        ).fetchone()

                        if existing is not None:
                            # Verify payload is identical
                            if (
                                existing[0] == dispatch.provider_adapter
                                and existing[1] == dispatch.transport_digest
                                and existing[2] == dispatch.posture
                                and existing[3] == dispatch.provider_operation_id
                                and existing[4] == dispatch.evidence_reference
                            ):
                                # Idempotent concurrent commit
                                return
                            else:
                                # Conflicting payload
                                raise ValueError(
                                    f"dispatch_id {dispatch.dispatch_id} already registered "
                                    f"with different payload in domain {domain}"
                                ) from integrity_error
                        else:
                            # Unknown integrity error
                            raise StorageIntegrityError(
                                f"Integrity constraint violation: {integrity_error}"
                            ) from integrity_error

        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                raise ConcurrencyConflictError(
                    f"Database lock timeout while committing dispatch {dispatch.dispatch_id}"
                ) from e
            raise StorageIntegrityError(f"Failed to commit dispatch: {e}") from e

    def release_reservation(
        self,
        reservation_id: str,
        evidence_spine: EvidenceSpine,
        evidence_pointer: EvidencePointer,
        control_domain: str,
        dispatch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        """Release an authority reservation after verified nothing_landed resolution.

        Domain-scoped: reservation must exist in the specified domain.

        Requires verified provider boundary reconciliation evidence proving
        NOTHING_LANDED for the exact effect intent bound to this reservation.

        Args:
            reservation_id: Reservation to release
            evidence_spine: Authoritative evidence spine
            evidence_pointer: Verified evidence proving NOTHING_LANDED
            control_domain: Control domain for isolation
            dispatch_id: Dispatch ID to bind evidence to (if dispatch occurred)
            idempotency_key: Idempotency key to validate (if available)

        Raises:
            TypeError: If spine or pointer are not correct types
            ValueError: If reservation never existed
            ValueError: If evidence does not prove NOTHING_LANDED
            ValueError: If conflicting release evidence
            ConcurrencyConflictError: If database lock cannot be acquired
            StorageIntegrityError: If transaction fails
        """
        from research_mission.evidence_spine import EvidencePointer, EvidenceSpine

        if type(evidence_spine) is not EvidenceSpine:
            raise TypeError("evidence_spine must be an EvidenceSpine")
        if type(evidence_pointer) is not EvidencePointer:
            raise TypeError("evidence_pointer must be an EvidencePointer")

        domain = validate_domain_id(control_domain, "control_domain")
        self._ensure_open()

        try:
            with self._connection() as connection:
                with connection:
                    # Check if already released
                    existing_release = connection.execute(
                        """
                        SELECT effect_intent_id, evidence_fingerprint
                        FROM reservation_releases
                        WHERE control_domain = ? AND reservation_id = ?
                        """,
                        (domain, reservation_id),
                    ).fetchone()

                    if existing_release is not None:
                        existing_effect_intent_id, existing_evidence_fingerprint = existing_release

                        # Re-verify evidence
                        record = evidence_spine.verify_evidence(evidence_pointer)
                        if record.key.source != "provider_boundary_reconciliation":
                            raise ValueError(
                                f"Evidence must be provider_boundary_reconciliation, "
                                f"got {record.key.source!r}"
                            )
                        if record.key.domain_id != domain:
                            raise ValueError(
                                f"Evidence control_domain mismatch: expected {domain!r}, "
                                f"found {record.key.domain_id!r}"
                            )

                        recorded_effect_intent_id = record.metadata.get("effect_intent_id")
                        if recorded_effect_intent_id != existing_effect_intent_id:
                            raise ValueError(
                                f"Evidence effect_intent_id mismatch: expected "
                                f"{existing_effect_intent_id!r}, found {recorded_effect_intent_id!r}"
                            )

                        if record.payload.get("reconciliation_outcome") != "no_operation_committed":
                            raise ValueError("Duplicate release evidence must prove no_operation_committed")

                        # Idempotent if same evidence
                        if evidence_pointer.record_fingerprint == existing_evidence_fingerprint:
                            return

                        # Conflicting evidence
                        raise ValueError(
                            f"Reservation {reservation_id} already released with different evidence: "
                            f"existing fingerprint {existing_evidence_fingerprint}, "
                            f"new fingerprint {evidence_pointer.record_fingerprint}"
                        )

                    # Check if reservation exists
                    effect_intent_id = connection.execute(
                        """
                        SELECT effect_intent_id FROM reservation_bindings
                        WHERE control_domain = ? AND authority_reservation_id = ?
                        """,
                        (domain, reservation_id),
                    ).fetchone()

                    if effect_intent_id is None:
                        raise ValueError(
                            f"Reservation {reservation_id} not found: reservation never existed or "
                            f"was already released with different evidence in domain {domain}"
                        )

                    effect_intent_id = effect_intent_id[0]

                    # Verify evidence
                    record = evidence_spine.verify_evidence(evidence_pointer)

                    if record.key.source != "provider_boundary_reconciliation":
                        raise ValueError(
                            f"Evidence must be provider_boundary_reconciliation, "
                            f"got {record.key.source!r}"
                        )
                    if record.key.domain_id != domain:
                        raise ValueError(
                            f"Evidence control_domain mismatch: expected {domain!r}, "
                            f"found {record.key.domain_id!r}"
                        )

                    recorded_effect_intent_id = record.metadata.get("effect_intent_id")
                    if recorded_effect_intent_id != effect_intent_id:
                        raise ValueError(
                            f"Evidence effect_intent_id mismatch: expected {effect_intent_id!r}, "
                            f"found {recorded_effect_intent_id!r}"
                        )

                    if dispatch_id is not None:
                        recorded_dispatch_id = record.metadata.get("dispatch_id")
                        if recorded_dispatch_id != dispatch_id:
                            raise ValueError(
                                f"Evidence dispatch_id mismatch: expected {dispatch_id!r}, "
                                f"found {recorded_dispatch_id!r}"
                            )

                    if idempotency_key is not None:
                        recorded_idempotency_key = record.payload.get("idempotency_key")
                        if recorded_idempotency_key != idempotency_key:
                            raise ValueError(
                                f"Evidence idempotency_key mismatch: expected {idempotency_key!r}, "
                                f"found {recorded_idempotency_key!r}"
                            )

                    reconciliation_outcome = record.payload.get("reconciliation_outcome")
                    if reconciliation_outcome != "no_operation_committed":
                        raise ValueError(
                            f"Cannot release reservation: evidence shows {reconciliation_outcome!r}, "
                            f"not 'no_operation_committed' (NOTHING_LANDED)"
                        )

                    current_row = connection.execute(
                        """
                        SELECT disposition, disposition_evidence_json
                        FROM authority_reservations
                        WHERE control_domain = ? AND reservation_id = ?
                        """,
                        (domain, reservation_id),
                    ).fetchone()
                    serialized_evidence = _serialize_evidence_pointer(evidence_pointer)
                    if current_row is not None:
                        if current_row[0] == AuthorityDisposition.RELEASED.value:
                            if current_row[1] == serialized_evidence:
                                return
                            raise ValueError(
                                f"Reservation {reservation_id} already released with different evidence"
                            )
                        if current_row[0] != AuthorityDisposition.RESERVED.value:
                            raise ValueError(
                                f"Reservation {reservation_id} in domain {domain} cannot regress from "
                                f"{current_row[0]!r} to {AuthorityDisposition.RELEASED.value!r}"
                            )

                        updated = connection.execute(
                            """
                            UPDATE authority_reservations
                               SET disposition = ?,
                                   disposition_at = ?,
                                   disposition_evidence_json = ?
                             WHERE control_domain = ?
                               AND reservation_id = ?
                               AND disposition = ?
                            """,
                            (
                                AuthorityDisposition.RELEASED.value,
                                _serialize_timestamp(record.reference.observed_at),
                                serialized_evidence,
                                domain,
                                reservation_id,
                                AuthorityDisposition.RESERVED.value,
                            ),
                        )
                        if updated.rowcount != 1:
                            after_row = connection.execute(
                                """
                                SELECT disposition, disposition_evidence_json
                                FROM authority_reservations
                                WHERE control_domain = ? AND reservation_id = ?
                                """,
                                (domain, reservation_id),
                            ).fetchone()
                            if after_row is not None and after_row[0] == AuthorityDisposition.RELEASED.value:
                                if after_row[1] == serialized_evidence:
                                    return
                                raise ValueError(
                                    f"Reservation {reservation_id} already released with different evidence"
                                )
                            if after_row is not None:
                                raise ValueError(
                                    f"Reservation {reservation_id} in domain {domain} cannot regress from "
                                    f"{after_row[0]!r} to {AuthorityDisposition.RELEASED.value!r}"
                                )

                    # Delete from active reservations only after the state transition succeeds.
                    connection.execute(
                        """
                        DELETE FROM reservation_bindings
                        WHERE control_domain = ? AND authority_reservation_id = ?
                        """,
                        (domain, reservation_id),
                    )

                    # Track release
                    connection.execute(
                        """
                        INSERT INTO reservation_releases (
                            control_domain, reservation_id, effect_intent_id,
                            evidence_fingerprint
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (domain, reservation_id, effect_intent_id, evidence_pointer.record_fingerprint),
                    )

        except sqlite3.IntegrityError as e:
            raise StorageIntegrityError(f"Failed to release reservation: {e}") from e
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                raise ConcurrencyConflictError(
                    f"Database lock timeout while releasing reservation {reservation_id}"
                ) from e
            raise StorageIntegrityError(f"Failed to release reservation: {e}") from e

    def get_intent(self, effect_intent_id: str, control_domain: str) -> EffectIntent | None:
        """Retrieve committed intent by ID within specified domain.

        Args:
            effect_intent_id: Intent ID to retrieve
            control_domain: Control domain for isolation

        Returns:
            EffectIntent if found, None otherwise

        Raises:
            StorageIntegrityError: If stored data fails validation
        """
        domain = validate_domain_id(control_domain, "control_domain")

        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT decision_id, mission_id, task_id, attempt_id, operation_digest,
                           idempotency_key, provider_scope, authority_reservation_id,
                           compensation_strategy, evidence_reference, state, created_at
                    FROM effect_intents
                    WHERE control_domain = ? AND effect_intent_id = ?
                    """,
                    (domain, effect_intent_id),
                ).fetchone()

                if row is None:
                    return None

                try:
                    return EffectIntent(
                        effect_intent_id=effect_intent_id,
                        decision_id=row[0],
                        mission_id=row[1],
                        task_id=row[2],
                        attempt_id=row[3],
                        operation_digest=row[4],
                        idempotency_key=row[5],
                        provider_scope=row[6],
                        authority_reservation_id=row[7],
                        compensation_strategy=row[8],
                        evidence_reference=row[9],
                        state=row[10],
                        created_at=_deserialize_timestamp(row[11]),
                        control_domain=domain,
                    )
                except (ValueError, TypeError) as e:
                    raise StorageIntegrityError(
                        f"Stored intent {effect_intent_id} failed validation: {e}"
                    ) from e

        except sqlite3.OperationalError as e:
            raise StorageIntegrityError(f"Failed to retrieve intent: {e}") from e

    def get_dispatch(self, dispatch_id: str, control_domain: str) -> EffectDispatch | None:
        """Retrieve registered dispatch by ID within specified domain.

        Args:
            dispatch_id: Dispatch ID to retrieve
            control_domain: Control domain for isolation

        Returns:
            EffectDispatch if found, None otherwise

        Raises:
            StorageIntegrityError: If stored data fails validation
        """
        domain = validate_domain_id(control_domain, "control_domain")

        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT effect_intent_id, attempt_id, idempotency_key, provider_adapter,
                           capability_profile_version, transport_digest, posture,
                           provider_operation_id, evidence_reference, dispatched_at
                    FROM effect_dispatches
                    WHERE control_domain = ? AND dispatch_id = ?
                    """,
                    (domain, dispatch_id),
                ).fetchone()

                if row is None:
                    return None

                try:
                    return EffectDispatch(
                        dispatch_id=dispatch_id,
                        effect_intent_id=row[0],
                        attempt_id=row[1],
                        idempotency_key=row[2],
                        provider_adapter=row[3],
                        capability_profile_version=row[4],
                        transport_digest=row[5],
                        posture=row[6],
                        provider_operation_id=row[7],
                        evidence_reference=row[8],
                        dispatched_at=_deserialize_timestamp(row[9]),
                        control_domain=domain,
                    )
                except (ValueError, TypeError) as e:
                    raise StorageIntegrityError(
                        f"Stored dispatch {dispatch_id} failed validation: {e}"
                    ) from e

        except sqlite3.OperationalError as e:
            raise StorageIntegrityError(f"Failed to retrieve dispatch: {e}") from e

    def store_reservation(
        self,
        reservation: AuthorityReservation,
        evidence_spine: EvidenceSpine | None = None,
    ) -> None:
        """Store or update an authority reservation.

        For ASSUMED_CONSUMED_UNRECONCILED dispositions, evidence_spine must be provided
        to verify terminal decision evidence.

        Args:
            reservation: Authority reservation to store
            evidence_spine: Evidence spine for terminal disposition verification

        Raises:
            TypeError: If reservation is not an AuthorityReservation
            ValueError: If terminal disposition lacks required evidence
            ConcurrencyConflictError: If database lock cannot be acquired
            StorageIntegrityError: If transaction fails
        """
        if type(reservation) is not AuthorityReservation:
            raise TypeError("reservation must be an AuthorityReservation")

        domain = validate_domain_id(reservation.control_domain, "control_domain")
        self._ensure_open()

        try:
            with self._connection() as connection:
                with connection:
                    existing = connection.execute(
                        """
                        SELECT effect_intent_id, capability_type, amount, disposition,
                               reserved_at, disposition_at, disposition_evidence_json
                        FROM authority_reservations
                        WHERE control_domain = ? AND reservation_id = ?
                        """,
                        (domain, reservation.reservation_id),
                    ).fetchone()

                    serialized_disposition_at = (
                        _serialize_timestamp(reservation.disposition_at)
                        if reservation.disposition_at
                        else None
                    )
                    serialized_evidence = _serialize_evidence_pointer(reservation.disposition_evidence)

                    if existing is not None:
                        existing_payload = (
                            existing[0],
                            existing[1],
                            existing[2],
                            existing[3],
                            existing[4],
                            existing[5],
                            existing[6],
                        )
                        incoming_payload = (
                            reservation.effect_intent_id,
                            reservation.capability_type,
                            reservation.amount,
                            reservation.disposition.value,
                            _serialize_timestamp(reservation.reserved_at),
                            serialized_disposition_at,
                            serialized_evidence,
                        )
                        if existing_payload == incoming_payload:
                            return
                        if existing[3] != AuthorityDisposition.RESERVED.value:
                            raise ValueError(
                                f"reservation {reservation.reservation_id} in domain {domain} "
                                f"cannot regress from {existing[3]!r} to {reservation.disposition.value!r}"
                            )
                        updated = connection.execute(
                            """
                            UPDATE authority_reservations
                               SET effect_intent_id = ?,
                                   capability_type = ?,
                                   amount = ?,
                                   disposition = ?,
                                   reserved_at = ?,
                                   disposition_at = ?,
                                   disposition_evidence_json = ?
                             WHERE control_domain = ? AND reservation_id = ?
                               AND disposition = ?
                            """,
                            (
                                reservation.effect_intent_id,
                                reservation.capability_type,
                                reservation.amount,
                                reservation.disposition.value,
                                _serialize_timestamp(reservation.reserved_at),
                                serialized_disposition_at,
                                serialized_evidence,
                                domain,
                                reservation.reservation_id,
                                AuthorityDisposition.RESERVED.value,
                            ),
                        )
                        if updated.rowcount != 1:
                            after_row = connection.execute(
                                """
                                SELECT effect_intent_id, capability_type, amount, disposition,
                                       reserved_at, disposition_at, disposition_evidence_json
                                FROM authority_reservations
                                WHERE control_domain = ? AND reservation_id = ?
                                """,
                                (domain, reservation.reservation_id),
                            ).fetchone()
                            if after_row is None:
                                raise ValueError(
                                    f"reservation {reservation.reservation_id} in domain {domain} "
                                    "was removed during update"
                                )
                            after_payload = (
                                after_row[0],
                                after_row[1],
                                after_row[2],
                                after_row[3],
                                after_row[4],
                                after_row[5],
                                after_row[6],
                            )
                            if after_payload == incoming_payload:
                                return
                            if after_row[3] != AuthorityDisposition.RESERVED.value:
                                raise ValueError(
                                    f"reservation {reservation.reservation_id} in domain {domain} "
                                    f"cannot regress from {after_row[3]!r} to {reservation.disposition.value!r}"
                                )
                            raise ValueError(
                                f"reservation {reservation.reservation_id} in domain {domain} "
                                "was concurrently modified"
                            )
                    else:
                        connection.execute(
                            """
                            INSERT INTO authority_reservations (
                                control_domain, reservation_id, effect_intent_id,
                                capability_type, amount, disposition, reserved_at,
                                disposition_at, disposition_evidence_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                domain,
                                reservation.reservation_id,
                                reservation.effect_intent_id,
                                reservation.capability_type,
                                reservation.amount,
                                reservation.disposition.value,
                                _serialize_timestamp(reservation.reserved_at),
                                serialized_disposition_at,
                                serialized_evidence,
                            ),
                        )

        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                raise ConcurrencyConflictError(
                    f"Database lock timeout while storing reservation {reservation.reservation_id}"
                ) from e
            raise StorageIntegrityError(f"Failed to store reservation: {e}") from e

    def get_reservation(
        self,
        reservation_id: str,
        control_domain: str,
        evidence_spine: EvidenceSpine | None = None,
    ) -> AuthorityReservation | None:
        """Retrieve authority reservation by ID within specified domain.

        For ASSUMED_CONSUMED_UNRECONCILED dispositions, evidence_spine must be
        provided to reconstruct the reservation with verified terminal evidence.

        Args:
            reservation_id: Reservation ID to retrieve
            control_domain: Control domain for isolation
            evidence_spine: Evidence spine for terminal disposition reconstruction

        Returns:
            AuthorityReservation if found, None otherwise

        Raises:
            StorageIntegrityError: If stored data fails validation
            ValueError: If terminal disposition lacks required evidence spine
        """
        domain = validate_domain_id(control_domain, "control_domain")

        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT effect_intent_id, capability_type, amount, disposition,
                           reserved_at, disposition_at, disposition_evidence_json
                    FROM authority_reservations
                    WHERE control_domain = ? AND reservation_id = ?
                    """,
                    (domain, reservation_id),
                ).fetchone()

                if row is None:
                    return None

                try:
                    disposition = AuthorityDisposition(row[3])
                    disposition_evidence = _deserialize_evidence_pointer(row[6])

                    # Terminal disposition requires evidence spine
                    if disposition == AuthorityDisposition.ASSUMED_CONSUMED_UNRECONCILED:
                        from research_mission.evidence_spine import EvidenceSpine

                        if type(evidence_spine) is not EvidenceSpine:
                            raise ValueError(
                                "ASSUMED_CONSUMED_UNRECONCILED reservation requires "
                                "EvidenceSpine for reconstruction"
                            )
                        # Use factory method with evidence verification
                        return AuthorityReservation.from_verified_terminal_decision(
                            reservation_id=reservation_id,
                            effect_intent_id=row[0],
                            capability_type=row[1],
                            amount=row[2],
                            reserved_at=_deserialize_timestamp(row[4]),
                            disposition_at=_deserialize_timestamp(row[5]),
                            evidence_spine=evidence_spine,
                            evidence_pointer=disposition_evidence,
                            control_domain=domain,
                        )
                    else:
                        # Non-terminal disposition - direct construction
                        return AuthorityReservation(
                            reservation_id=reservation_id,
                            effect_intent_id=row[0],
                            capability_type=row[1],
                            amount=row[2],
                            disposition=disposition,
                            reserved_at=_deserialize_timestamp(row[4]),
                            disposition_at=_deserialize_timestamp(row[5]),
                            disposition_evidence=disposition_evidence,
                            control_domain=domain,
                        )

                except (ValueError, TypeError) as e:
                    raise StorageIntegrityError(
                        f"Stored reservation {reservation_id} failed validation: {e}"
                    ) from e

        except sqlite3.OperationalError as e:
            raise StorageIntegrityError(f"Failed to retrieve reservation: {e}") from e

    def store_obligation(
        self,
        obligation: ReconciliationObligation,
        evidence_spine: EvidenceSpine | None = None,
    ) -> None:
        """Store or update a reconciliation obligation.

        For obligations with terminal_disposition, evidence_spine must be provided
        to verify terminal decision evidence.

        Args:
            obligation: Reconciliation obligation to store
            evidence_spine: Evidence spine for terminal disposition verification

        Raises:
            TypeError: If obligation is not a ReconciliationObligation
            ValueError: If terminal disposition lacks required evidence
            ConcurrencyConflictError: If database lock cannot be acquired
            StorageIntegrityError: If transaction fails
        """
        if type(obligation) is not ReconciliationObligation:
            raise TypeError("obligation must be a ReconciliationObligation")

        domain = validate_domain_id(obligation.control_domain, "control_domain")
        self._ensure_open()

        try:
            with self._connection() as connection:
                with connection:
                    existing = connection.execute(
                        """
                        SELECT effect_intent_id, dispatch_id, state, provider_reconcilability,
                               next_probe_at, probe_history_json, terminal_disposition_json,
                               created_at
                        FROM reconciliation_obligations
                        WHERE control_domain = ? AND obligation_id = ?
                        """,
                        (domain, obligation.obligation_id),
                    ).fetchone()

                    serialized_next_probe_at = (
                        _serialize_timestamp(obligation.next_probe_at)
                        if obligation.next_probe_at
                        else None
                    )
                    serialized_terminal_disposition = _serialize_evidence_pointer(
                        obligation.terminal_disposition
                    )
                    serialized_probe_history = _serialize_probe_history(obligation.probe_history)
                    serialized_created_at = _serialize_timestamp(obligation.created_at)

                    if existing is not None:
                        existing_payload = (
                            existing[0],
                            existing[1],
                            existing[2],
                            existing[3],
                            existing[4],
                            existing[5],
                            existing[6],
                            existing[7],
                        )
                        incoming_payload = (
                            obligation.effect_intent_id,
                            obligation.dispatch_id,
                            obligation.state.value,
                            obligation.provider_reconcilability.value,
                            serialized_next_probe_at,
                            serialized_probe_history,
                            serialized_terminal_disposition,
                            serialized_created_at,
                        )
                        if existing_payload == incoming_payload:
                            return
                        if existing[2] not in (
                            ReconciliationState.PENDING.value,
                            ReconciliationState.IN_PROGRESS.value,
                        ):
                            raise ValueError(
                                f"obligation {obligation.obligation_id} in domain {domain} "
                                f"cannot regress from {existing[2]!r} to {obligation.state.value!r}"
                            )
                        connection.execute(
                            """
                            UPDATE reconciliation_obligations
                               SET effect_intent_id = ?,
                                   dispatch_id = ?,
                                   state = ?,
                                   provider_reconcilability = ?,
                                   next_probe_at = ?,
                                   probe_history_json = ?,
                                   terminal_disposition_json = ?,
                                   created_at = ?
                             WHERE control_domain = ? AND obligation_id = ?
                            """,
                            (
                                obligation.effect_intent_id,
                                obligation.dispatch_id,
                                obligation.state.value,
                                obligation.provider_reconcilability.value,
                                serialized_next_probe_at,
                                serialized_probe_history,
                                serialized_terminal_disposition,
                                serialized_created_at,
                                domain,
                                obligation.obligation_id,
                            ),
                        )
                    else:
                        connection.execute(
                            """
                            INSERT INTO reconciliation_obligations (
                                control_domain, obligation_id, effect_intent_id, dispatch_id,
                                state, provider_reconcilability, next_probe_at,
                                probe_history_json, terminal_disposition_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                domain,
                                obligation.obligation_id,
                                obligation.effect_intent_id,
                                obligation.dispatch_id,
                                obligation.state.value,
                                obligation.provider_reconcilability.value,
                                serialized_next_probe_at,
                                serialized_probe_history,
                                serialized_terminal_disposition,
                                serialized_created_at,
                            ),
                        )

        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                raise ConcurrencyConflictError(
                    f"Database lock timeout while storing obligation {obligation.obligation_id}"
                ) from e
            raise StorageIntegrityError(f"Failed to store obligation: {e}") from e

    def get_obligation(
        self,
        obligation_id: str,
        control_domain: str,
        evidence_spine: EvidenceSpine | None = None,
    ) -> ReconciliationObligation | None:
        """Retrieve reconciliation obligation by ID within specified domain.

        For obligations with terminal_disposition, evidence_spine must be provided
        to reconstruct the obligation with verified terminal evidence.

        Args:
            obligation_id: Obligation ID to retrieve
            control_domain: Control domain for isolation
            evidence_spine: Evidence spine for terminal disposition reconstruction

        Returns:
            ReconciliationObligation if found, None otherwise

        Raises:
            StorageIntegrityError: If stored data fails validation
            ValueError: If terminal disposition lacks required evidence spine
        """
        domain = validate_domain_id(control_domain, "control_domain")

        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT effect_intent_id, dispatch_id, state, provider_reconcilability,
                           next_probe_at, probe_history_json, terminal_disposition_json,
                           created_at
                    FROM reconciliation_obligations
                    WHERE control_domain = ? AND obligation_id = ?
                    """,
                    (domain, obligation_id),
                ).fetchone()

                if row is None:
                    return None

                try:
                    state = ReconciliationState(row[2])
                    provider_reconcilability = ProviderReconcilability(row[3])
                    terminal_disposition = _deserialize_evidence_pointer(row[6])

                    # Terminal disposition requires evidence spine
                    if terminal_disposition is not None:
                        from research_mission.evidence_spine import EvidenceSpine

                        if type(evidence_spine) is not EvidenceSpine:
                            raise ValueError(
                                "Obligation with terminal_disposition requires "
                                "EvidenceSpine for reconstruction"
                            )
                        # Use factory method with evidence verification
                        return ReconciliationObligation.from_verified_terminal_decision(
                            obligation_id=obligation_id,
                            effect_intent_id=row[0],
                            dispatch_id=row[1],
                            provider_reconcilability=provider_reconcilability,
                            probe_history=_deserialize_probe_history(row[5]),
                            created_at=_deserialize_timestamp(row[7]),
                            evidence_spine=evidence_spine,
                            evidence_pointer=terminal_disposition,
                            control_domain=domain,
                        )
                    else:
                        # No terminal disposition - direct construction
                        return ReconciliationObligation(
                            obligation_id=obligation_id,
                            effect_intent_id=row[0],
                            dispatch_id=row[1],
                            state=state,
                            provider_reconcilability=provider_reconcilability,
                            next_probe_at=_deserialize_timestamp(row[4]),
                            probe_history=_deserialize_probe_history(row[5]),
                            terminal_disposition=None,
                            created_at=_deserialize_timestamp(row[7]),
                            control_domain=domain,
                        )

                except (ValueError, TypeError) as e:
                    raise StorageIntegrityError(
                        f"Stored obligation {obligation_id} failed validation: {e}"
                    ) from e

        except sqlite3.OperationalError as e:
            raise StorageIntegrityError(f"Failed to retrieve obligation: {e}") from e

    def claim_gateway_dispatch(
        self,
        gateway_claim_id: str,
        request_fingerprint: str,
        effect_intent_id: str,
        effect_dispatch_id: str,
        authority_reservation_id: str,
        delegation_grant_id: str,
        delegation_grant_fingerprint: str,
        requested_capability: str,
        idempotency_key: str,
        operation_digest: str,
        provider_id: str,
        adapter_id: str,
        owner_identity: str,
        claimed_at: datetime,
        expires_at: datetime,
        control_domain: str,
    ) -> None:
        """Atomically claim gateway dispatch ownership (single-winner).

        Args:
            gateway_claim_id: Unique claim ID
            request_fingerprint: Request fingerprint
            effect_intent_id: Effect intent ID
            effect_dispatch_id: Effect dispatch ID
            authority_reservation_id: Authority reservation ID
            delegation_grant_id: Delegation grant ID
            delegation_grant_fingerprint: Delegation grant fingerprint
            requested_capability: Requested capability
            idempotency_key: Idempotency key
            operation_digest: Operation digest
            provider_id: Provider ID
            adapter_id: Adapter ID
            owner_identity: Owner identity
            claimed_at: Claim timestamp
            expires_at: Claim expiry
            control_domain: Control domain

        Raises:
            ConcurrencyConflictError: If claim already exists (another winner)
            ValueError: If authoritative records missing or mismatched
            StorageIntegrityError: If transaction fails
        """
        domain = validate_domain_id(control_domain, "control_domain")
        self._ensure_open()

        try:
            with self._connection() as connection:
                with connection:
                    # Verify intent exists
                    intent_row = connection.execute(
                        """
                        SELECT idempotency_key, operation_digest, authority_reservation_id
                        FROM effect_intents
                        WHERE control_domain = ? AND effect_intent_id = ?
                        """,
                        (domain, effect_intent_id),
                    ).fetchone()
                    if intent_row is None:
                        raise ValueError(
                            f"effect_intent_id {effect_intent_id!r} not found in domain {domain}"
                        )
                    if intent_row[0] != idempotency_key:
                        raise ValueError(
                            f"idempotency_key mismatch: intent has {intent_row[0]!r}, "
                            f"request has {idempotency_key!r}"
                        )
                    if intent_row[1] != operation_digest:
                        raise ValueError(
                            f"operation_digest mismatch: intent has {intent_row[1]!r}, "
                            f"request has {operation_digest!r}"
                        )
                    if intent_row[2] != authority_reservation_id:
                        raise ValueError(
                            f"authority_reservation_id mismatch: intent has {intent_row[2]!r}, "
                            f"request has {authority_reservation_id!r}"
                        )

                    # Verify dispatch exists
                    dispatch_row = connection.execute(
                        """
                        SELECT effect_intent_id, idempotency_key
                        FROM effect_dispatches
                        WHERE control_domain = ? AND dispatch_id = ?
                        """,
                        (domain, effect_dispatch_id),
                    ).fetchone()
                    if dispatch_row is None:
                        raise ValueError(
                            f"effect_dispatch_id {effect_dispatch_id!r} not found in domain {domain}"
                        )
                    if dispatch_row[0] != effect_intent_id:
                        raise ValueError(
                            f"dispatch intent mismatch: dispatch has {dispatch_row[0]!r}, "
                            f"request has {effect_intent_id!r}"
                        )
                    if dispatch_row[1] != idempotency_key:
                        raise ValueError(
                            f"dispatch idempotency_key mismatch: dispatch has {dispatch_row[1]!r}, "
                            f"request has {idempotency_key!r}"
                        )

                    # Verify reservation exists and is RESERVED
                    reservation_row = connection.execute(
                        """
                        SELECT effect_intent_id, disposition
                        FROM authority_reservations
                        WHERE control_domain = ? AND reservation_id = ?
                        """,
                        (domain, authority_reservation_id),
                    ).fetchone()
                    if reservation_row is None:
                        raise ValueError(
                            f"authority_reservation_id {authority_reservation_id!r} not found "
                            f"in domain {domain}"
                        )
                    if reservation_row[0] != effect_intent_id:
                        raise ValueError(
                            f"reservation intent mismatch: reservation has {reservation_row[0]!r}, "
                            f"request has {effect_intent_id!r}"
                        )
                    if reservation_row[1] != AuthorityDisposition.RESERVED.value:
                        raise ValueError(
                            f"authority_reservation_id {authority_reservation_id!r} has disposition "
                            f"{reservation_row[1]!r}, must be RESERVED"
                        )

                    # Check for idempotent retry
                    existing = connection.execute(
                        """
                        SELECT request_fingerprint, effect_intent_id, effect_dispatch_id,
                               authority_reservation_id, delegation_grant_id,
                               delegation_grant_fingerprint, requested_capability,
                               idempotency_key, operation_digest, provider_id, adapter_id,
                               owner_identity, claimed_at, expires_at
                        FROM effect_gateway_claims
                        WHERE control_domain = ? AND gateway_claim_id = ?
                        """,
                        (domain, gateway_claim_id),
                    ).fetchone()
                    if existing is not None:
                        # Verify exact match for idempotent retry
                        if (
                            existing[0] == request_fingerprint
                            and existing[1] == effect_intent_id
                            and existing[2] == effect_dispatch_id
                            and existing[3] == authority_reservation_id
                            and existing[4] == delegation_grant_id
                            and existing[5] == delegation_grant_fingerprint
                            and existing[6] == requested_capability
                            and existing[7] == idempotency_key
                            and existing[8] == operation_digest
                            and existing[9] == provider_id
                            and existing[10] == adapter_id
                            and existing[11] == owner_identity
                            and existing[12] == _serialize_timestamp(claimed_at)
                            and existing[13] == _serialize_timestamp(expires_at)
                        ):
                            return
                        raise ConcurrencyConflictError(
                            f"gateway_claim_id {gateway_claim_id!r} already exists with different payload"
                        )

                    # Insert claim (single-winner enforced by UNIQUE constraints)
                    try:
                        connection.execute(
                            """
                            INSERT INTO effect_gateway_claims (
                                control_domain, gateway_claim_id, request_fingerprint,
                                effect_intent_id, effect_dispatch_id, authority_reservation_id,
                                delegation_grant_id, delegation_grant_fingerprint,
                                requested_capability, idempotency_key, operation_digest,
                                provider_id, adapter_id, owner_identity, state,
                                claimed_at, expires_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                domain,
                                gateway_claim_id,
                                request_fingerprint,
                                effect_intent_id,
                                effect_dispatch_id,
                                authority_reservation_id,
                                delegation_grant_id,
                                delegation_grant_fingerprint,
                                requested_capability,
                                idempotency_key,
                                operation_digest,
                                provider_id,
                                adapter_id,
                                owner_identity,
                                "claimed",
                                _serialize_timestamp(claimed_at),
                                _serialize_timestamp(expires_at),
                            ),
                        )
                    except sqlite3.IntegrityError as integrity_error:
                        # UNIQUE constraint violation - another winner exists
                        raise ConcurrencyConflictError(
                            f"Claim conflict: another claim exists for effect_intent_id "
                            f"{effect_intent_id!r} in domain {domain}"
                        ) from integrity_error

        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                raise ConcurrencyConflictError(
                    f"Database lock timeout while claiming dispatch {gateway_claim_id}"
                ) from e
            raise StorageIntegrityError(f"Failed to claim dispatch: {e}") from e

    def issue_gateway_permit(
        self,
        permit_id: str,
        permit_verifier: str,
        request_fingerprint: str,
        effect_intent_id: str,
        effect_dispatch_id: str,
        authority_reservation_id: str,
        gateway_claim_id: str,
        delegation_grant_id: str,
        delegation_grant_fingerprint: str,
        requested_capability: str,
        operation_digest: str,
        idempotency_key: str,
        provider_id: str,
        adapter_id: str,
        credential_scope: tuple[str, ...],
        owner_identity: str,
        issued_at: datetime,
        expires_at: datetime,
        control_domain: str,
    ) -> None:
        """Issue a gateway dispatch permit for the winning claim.

        Args:
            permit_id: Unique permit ID
            permit_verifier: One-way hash of permit token (NOT the token itself)
            request_fingerprint: Request fingerprint
            effect_intent_id: Effect intent ID
            effect_dispatch_id: Effect dispatch ID
            authority_reservation_id: Authority reservation ID
            gateway_claim_id: Gateway claim ID
            delegation_grant_id: Delegation grant ID
            delegation_grant_fingerprint: Delegation grant fingerprint
            requested_capability: Requested capability
            operation_digest: Operation digest
            idempotency_key: Idempotency key
            provider_id: Provider ID
            adapter_id: Adapter ID
            credential_scope: Credential scope tuple
            owner_identity: Owner identity
            issued_at: Issue timestamp
            expires_at: Expiry timestamp
            control_domain: Control domain

        Raises:
            ValueError: If claim not found or not eligible
            ConcurrencyConflictError: If permit already exists
            StorageIntegrityError: If transaction fails
        """
        domain = validate_domain_id(control_domain, "control_domain")
        self._ensure_open()

        try:
            with self._connection() as connection:
                with connection:
                    # Verify claim exists and is in CLAIMED state
                    claim_row = connection.execute(
                        """
                        SELECT state, request_fingerprint, effect_intent_id,
                               effect_dispatch_id, authority_reservation_id
                        FROM effect_gateway_claims
                        WHERE control_domain = ? AND gateway_claim_id = ?
                        """,
                        (domain, gateway_claim_id),
                    ).fetchone()
                    if claim_row is None:
                        raise ValueError(
                            f"gateway_claim_id {gateway_claim_id!r} not found in domain {domain}"
                        )
                    if claim_row[0] != "claimed":
                        raise ValueError(
                            f"gateway_claim_id {gateway_claim_id!r} has state {claim_row[0]!r}, "
                            "must be 'claimed'"
                        )
                    if claim_row[1] != request_fingerprint:
                        raise ValueError(
                            f"request_fingerprint mismatch for claim {gateway_claim_id!r}"
                        )
                    if claim_row[2] != effect_intent_id:
                        raise ValueError(f"effect_intent_id mismatch for claim {gateway_claim_id!r}")
                    if claim_row[3] != effect_dispatch_id:
                        raise ValueError(
                            f"effect_dispatch_id mismatch for claim {gateway_claim_id!r}"
                        )
                    if claim_row[4] != authority_reservation_id:
                        raise ValueError(
                            f"authority_reservation_id mismatch for claim {gateway_claim_id!r}"
                        )

                    # Check for idempotent retry
                    existing = connection.execute(
                        """
                        SELECT permit_verifier
                        FROM effect_gateway_permits
                        WHERE control_domain = ? AND permit_id = ?
                        """,
                        (domain, permit_id),
                    ).fetchone()
                    if existing is not None:
                        if existing[0] == permit_verifier:
                            return
                        raise ConcurrencyConflictError(
                            f"permit_id {permit_id!r} already exists with different verifier"
                        )

                    # Insert permit
                    credential_scope_json = json.dumps(list(credential_scope))
                    connection.execute(
                        """
                        INSERT INTO effect_gateway_permits (
                            control_domain, permit_id, permit_verifier, request_fingerprint,
                            effect_intent_id, effect_dispatch_id, authority_reservation_id,
                            gateway_claim_id, delegation_grant_id, delegation_grant_fingerprint,
                            requested_capability, operation_digest, idempotency_key,
                            provider_id, adapter_id, credential_scope_json, owner_identity,
                            issued_at, expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            domain,
                            permit_id,
                            permit_verifier,
                            request_fingerprint,
                            effect_intent_id,
                            effect_dispatch_id,
                            authority_reservation_id,
                            gateway_claim_id,
                            delegation_grant_id,
                            delegation_grant_fingerprint,
                            requested_capability,
                            operation_digest,
                            idempotency_key,
                            provider_id,
                            adapter_id,
                            credential_scope_json,
                            owner_identity,
                            _serialize_timestamp(issued_at),
                            _serialize_timestamp(expires_at),
                        ),
                    )

                    # Store permit_verifier in claim
                    connection.execute(
                        """
                        UPDATE effect_gateway_claims
                        SET permit_verifier = ?
                        WHERE control_domain = ? AND gateway_claim_id = ?
                        """,
                        (permit_verifier, domain, gateway_claim_id),
                    )

        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                raise ConcurrencyConflictError(
                    f"Database lock timeout while issuing permit {permit_id}"
                ) from e
            raise StorageIntegrityError(f"Failed to issue permit: {e}") from e

    def verify_and_consume_permit(
        self,
        permit_verifier: str,
        control_domain: str,
        now: datetime,
    ) -> str:
        """Atomically verify and consume permit, transition claim to HANDOFF_STARTED.

        This is the CRITICAL HANDOFF AUTHORIZATION boundary.

        Args:
            permit_verifier: Permit verifier to validate
            control_domain: Control domain
            now: Current timestamp for expiry check

        Returns:
            gateway_claim_id of the winning claim

        Raises:
            ValueError: If permit invalid/expired/revoked/consumed
            ConcurrencyConflictError: If concurrent consumption
            StorageIntegrityError: If transaction fails
        """
        domain = validate_domain_id(control_domain, "control_domain")
        self._ensure_open()

        try:
            with self._connection() as connection:
                # Use BEGIN IMMEDIATE for writer ownership
                connection.execute("BEGIN IMMEDIATE")
                try:
                    # Find permit by verifier
                    permit_row = connection.execute(
                        """
                        SELECT permit_id, gateway_claim_id, expires_at, consumed_at, revoked_at
                        FROM effect_gateway_permits
                        WHERE control_domain = ? AND permit_verifier = ?
                        """,
                        (domain, permit_verifier),
                    ).fetchone()

                    if permit_row is None:
                        raise ValueError(f"permit not found in domain {domain}")

                    permit_id, gateway_claim_id, expires_at_str, consumed_at, revoked_at = permit_row

                    # Check expiry (strict: now >= expires_at means expired)
                    expires_at = _deserialize_timestamp(expires_at_str)
                    if now >= expires_at:
                        raise ValueError(f"permit {permit_id!r} expired at {expires_at_str}")

                    # Check revoked
                    if revoked_at is not None:
                        raise ValueError(f"permit {permit_id!r} was revoked at {revoked_at}")

                    # Check consumed
                    if consumed_at is not None:
                        raise ValueError(f"permit {permit_id!r} already consumed at {consumed_at}")

                    # Verify claim exists and is in CLAIMED state
                    claim_row = connection.execute(
                        """
                        SELECT state
                        FROM effect_gateway_claims
                        WHERE control_domain = ? AND gateway_claim_id = ?
                        """,
                        (domain, gateway_claim_id),
                    ).fetchone()
                    if claim_row is None:
                        raise ValueError(
                            f"gateway_claim_id {gateway_claim_id!r} not found in domain {domain}"
                        )
                    if claim_row[0] != "claimed":
                        raise ValueError(
                            f"gateway_claim_id {gateway_claim_id!r} has state {claim_row[0]!r}, "
                            "expected 'claimed'"
                        )

                    # ATOMIC: Mark permit consumed AND transition claim to HANDOFF_STARTED
                    connection.execute(
                        """
                        UPDATE effect_gateway_permits
                        SET consumed_at = ?
                        WHERE control_domain = ? AND permit_id = ? AND consumed_at IS NULL
                        """,
                        (_serialize_timestamp(now), domain, permit_id),
                    )

                    connection.execute(
                        """
                        UPDATE effect_gateway_claims
                        SET state = 'handoff_started', handoff_started_at = ?
                        WHERE control_domain = ? AND gateway_claim_id = ? AND state = 'claimed'
                        """,
                        (_serialize_timestamp(now), domain, gateway_claim_id),
                    )

                    connection.commit()
                    return gateway_claim_id

                except Exception:
                    connection.rollback()
                    raise

        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                raise ConcurrencyConflictError(
                    f"Database lock timeout while consuming permit in domain {domain}"
                ) from e
            raise StorageIntegrityError(f"Failed to consume permit: {e}") from e

    def get_gateway_claim(
        self, gateway_claim_id: str, control_domain: str
    ) -> dict[str, Any] | None:
        """Retrieve gateway claim by ID.

        Args:
            gateway_claim_id: Claim ID
            control_domain: Control domain

        Returns:
            Claim row dict if found, None otherwise
        """
        domain = validate_domain_id(control_domain, "control_domain")
        self._ensure_open()

        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT gateway_claim_id, request_fingerprint, effect_intent_id,
                           effect_dispatch_id, authority_reservation_id, delegation_grant_id,
                           delegation_grant_fingerprint, requested_capability,
                           idempotency_key, operation_digest, provider_id, adapter_id,
                           owner_identity, state, permit_verifier, claimed_at, expires_at,
                           handoff_started_at, receipt_recorded_at, terminal_at
                    FROM effect_gateway_claims
                    WHERE control_domain = ? AND gateway_claim_id = ?
                    """,
                    (domain, gateway_claim_id),
                ).fetchone()

                if row is None:
                    return None

                return {
                    "gateway_claim_id": row[0],
                    "request_fingerprint": row[1],
                    "effect_intent_id": row[2],
                    "effect_dispatch_id": row[3],
                    "authority_reservation_id": row[4],
                    "delegation_grant_id": row[5],
                    "delegation_grant_fingerprint": row[6],
                    "requested_capability": row[7],
                    "idempotency_key": row[8],
                    "operation_digest": row[9],
                    "provider_id": row[10],
                    "adapter_id": row[11],
                    "owner_identity": row[12],
                    "state": row[13],
                    "permit_verifier": row[14],
                    "claimed_at": row[15],
                    "expires_at": row[16],
                    "handoff_started_at": row[17],
                    "receipt_recorded_at": row[18],
                    "terminal_at": row[19],
                }
        except sqlite3.OperationalError as e:
            raise StorageIntegrityError(f"Failed to retrieve claim: {e}") from e

    def record_gateway_receipt(
        self,
        gateway_claim_id: str,
        control_domain: str,
        now: datetime,
    ) -> None:
        """Record receipt for gateway claim, transition to RECEIPT_RECORDED.

        Args:
            gateway_claim_id: Claim ID
            control_domain: Control domain
            now: Receipt timestamp

        Raises:
            ValueError: If claim not found or not in HANDOFF_STARTED state
            StorageIntegrityError: If transaction fails
        """
        domain = validate_domain_id(control_domain, "control_domain")
        self._ensure_open()

        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    # Verify claim exists and is in HANDOFF_STARTED
                    claim_row = connection.execute(
                        """
                        SELECT state FROM effect_gateway_claims
                        WHERE control_domain = ? AND gateway_claim_id = ?
                        """,
                        (domain, gateway_claim_id),
                    ).fetchone()

                    if claim_row is None:
                        raise ValueError(
                            f"gateway_claim_id {gateway_claim_id!r} not found in domain {domain}"
                        )

                    current_state = claim_row[0]
                    if current_state == "receipt_recorded":
                        # Idempotent: already recorded
                        connection.commit()
                        return

                    if current_state != "handoff_started":
                        raise ValueError(
                            f"Cannot record receipt for claim {gateway_claim_id!r} in state {current_state!r}, "
                            "expected 'handoff_started'"
                        )

                    # Transition to RECEIPT_RECORDED
                    connection.execute(
                        """
                        UPDATE effect_gateway_claims
                        SET state = 'receipt_recorded', receipt_recorded_at = ?
                        WHERE control_domain = ? AND gateway_claim_id = ? AND state = 'handoff_started'
                        """,
                        (_serialize_timestamp(now), domain, gateway_claim_id),
                    )

                    if connection.total_changes == 0:
                        raise ConcurrencyConflictError(
                            f"Claim {gateway_claim_id!r} state changed concurrently"
                        )

                    connection.commit()

                except Exception:
                    connection.rollback()
                    raise

        except sqlite3.OperationalError as e:
            raise StorageIntegrityError(f"Failed to record receipt: {e}") from e

    def record_gateway_result(
        self,
        gateway_claim_id: str,
        control_domain: str,
        effect_status: str,
        now: datetime,
    ) -> None:
        """Record final gateway result, transition to TERMINAL or INDETERMINATE.

        Args:
            gateway_claim_id: Claim ID
            control_domain: Control domain
            effect_status: "nothing_landed", "something_landed", or "indeterminate"
            now: Result timestamp

        Raises:
            ValueError: If claim not found or invalid state transition
            StorageIntegrityError: If transaction fails
        """
        domain = validate_domain_id(control_domain, "control_domain")
        self._ensure_open()

        valid_statuses = {"nothing_landed", "something_landed", "indeterminate"}
        if effect_status not in valid_statuses:
            raise ValueError(f"effect_status must be one of {valid_statuses}")

        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    # Verify claim exists
                    claim_row = connection.execute(
                        """
                        SELECT state FROM effect_gateway_claims
                        WHERE control_domain = ? AND gateway_claim_id = ?
                        """,
                        (domain, gateway_claim_id),
                    ).fetchone()

                    if claim_row is None:
                        raise ValueError(
                            f"gateway_claim_id {gateway_claim_id!r} not found in domain {domain}"
                        )

                    current_state = claim_row[0]

                    # Reject terminal rewrite
                    if current_state in ("terminal", "indeterminate"):
                        raise ValueError(
                            f"Cannot rewrite terminal state {current_state!r} for claim {gateway_claim_id!r}"
                        )

                    # INDETERMINATE requires HANDOFF_STARTED (no receipt)
                    if effect_status == "indeterminate":
                        if current_state != "handoff_started":
                            raise ValueError(
                                f"INDETERMINATE requires handoff_started, claim {gateway_claim_id!r} is {current_state!r}"
                            )
                        connection.execute(
                            """
                            UPDATE effect_gateway_claims
                            SET state = 'indeterminate', terminal_at = ?
                            WHERE control_domain = ? AND gateway_claim_id = ? AND state = 'handoff_started'
                            """,
                            (_serialize_timestamp(now), domain, gateway_claim_id),
                        )
                    else:
                        # Terminal results require RECEIPT_RECORDED
                        if current_state != "receipt_recorded":
                            raise ValueError(
                                f"Terminal result requires receipt_recorded, claim {gateway_claim_id!r} is {current_state!r}"
                            )
                        connection.execute(
                            """
                            UPDATE effect_gateway_claims
                            SET state = 'terminal', terminal_at = ?
                            WHERE control_domain = ? AND gateway_claim_id = ? AND state = 'receipt_recorded'
                            """,
                            (_serialize_timestamp(now), domain, gateway_claim_id),
                        )

                    if connection.total_changes == 0:
                        raise ConcurrencyConflictError(
                            f"Claim {gateway_claim_id!r} state changed concurrently"
                        )

                    connection.commit()

                except Exception:
                    connection.rollback()
                    raise

        except sqlite3.OperationalError as e:
            raise StorageIntegrityError(f"Failed to record result: {e}") from e

    def revoke_permit(
        self,
        permit_id: str,
        control_domain: str,
        now: datetime,
    ) -> None:
        """Revoke a gateway permit.

        Args:
            permit_id: Permit ID
            control_domain: Control domain
            now: Revocation timestamp

        Raises:
            ValueError: If permit not found or already consumed/revoked
            StorageIntegrityError: If transaction fails
        """
        domain = validate_domain_id(control_domain, "control_domain")
        self._ensure_open()

        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    # Find permit
                    permit_row = connection.execute(
                        """
                        SELECT consumed_at, revoked_at FROM effect_gateway_permits
                        WHERE control_domain = ? AND permit_id = ?
                        """,
                        (domain, permit_id),
                    ).fetchone()

                    if permit_row is None:
                        raise ValueError(f"permit_id {permit_id!r} not found in domain {domain}")

                    consumed_at, revoked_at = permit_row

                    if consumed_at is not None:
                        raise ValueError(f"Cannot revoke consumed permit {permit_id!r}")

                    if revoked_at is not None:
                        # Idempotent: already revoked
                        connection.commit()
                        return

                    # Mark revoked
                    connection.execute(
                        """
                        UPDATE effect_gateway_permits
                        SET revoked_at = ?
                        WHERE control_domain = ? AND permit_id = ? AND consumed_at IS NULL AND revoked_at IS NULL
                        """,
                        (_serialize_timestamp(now), domain, permit_id),
                    )

                    if connection.total_changes == 0:
                        raise ConcurrencyConflictError(f"Permit {permit_id!r} changed concurrently")

                    connection.commit()

                except Exception:
                    connection.rollback()
                    raise

        except sqlite3.OperationalError as e:
            raise StorageIntegrityError(f"Failed to revoke permit: {e}") from e

    def close(self) -> None:
        """Close the store and release resources.

        Marks the store closed and releases any persistent in-memory connection.
        """
        if self._closed:
            return
        self._closed = True
        if self._memory_connection is not None:
            self._memory_connection.close()


__all__ = [
    "DurableEffectStore",
    "DurableEffectStoreError",
    "SchemaVersionError",
    "StorageIntegrityError",
    "ConcurrencyConflictError",
    "SCHEMA_VERSION",
]
