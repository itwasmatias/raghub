"""Durable mission runtime storage for MissionaryX v0.1.

This module provides SQLite-backed persistence for mission lifecycle state,
ensuring mission truth survives process restarts and supports concurrent access.

Critical architectural boundaries:
- Mission Runtime Store owns mission lifecycle state
- Governed Effect Gateway Store owns effect outcome state
- These stores are separate and must not be conflated

All mission operations are ControlDomain-scoped with optimistic concurrency control.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

from federation.control_domain import validate_domain_id
from federation.mission_controller_lease import (
    DEFAULT_CONTROLLER_LEASE_DURATION,
    MissionControllerLease,
    MissionControllerClockError,
    MissionControllerLeaseConflictError,
    MissionControllerLeaseExpiredError,
    MissionControllerLeaseNotFoundError,
    MissionControllerLeaseStaleError,
    validate_controller_id,
    validate_generation,
    validate_lease_duration,
)
from federation.mission_state import (
    EffectReference,
    MissionCheckpoint,
    MissionLifecycle,
    MissionSpecification,
    MissionTransition,
)


# Schema version for fail-closed compatibility checking
MISSION_SCHEMA_VERSION = 2


class MissionRuntimeStoreError(Exception):
    """Base error for mission runtime store operations."""


class MissionSchemaVersionError(MissionRuntimeStoreError):
    """Raised when database schema version is incompatible."""


class MissionNotFoundError(MissionRuntimeStoreError):
    """Raised when mission does not exist."""


class MissionRevisionConflictError(MissionRuntimeStoreError):
    """Raised when optimistic concurrency check fails (stale write)."""


class IllegalMissionTransitionError(MissionRuntimeStoreError):
    """Raised when state transition violates state machine rules."""


class DomainMismatchError(MissionRuntimeStoreError):
    """Raised when operation targets wrong ControlDomain."""


_FILE_JOURNAL_MODE = "wal"
_MAX_BUSY_TIMEOUT_MS = 30000


def _serialize_timestamp(dt: datetime | None) -> str | None:
    """Serialize datetime to ISO format UTC string."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def _deserialize_timestamp(value: str | None) -> datetime | None:
    """Deserialize ISO format UTC string to datetime."""
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _serialize_json(data: dict[str, Any] | MappingProxyType | None) -> str | None:
    """Serialize dictionary to JSON.

    Accepts dict or MappingProxyType (converts proxy to dict for serialization).
    """
    if data is None or data == {}:
        return None
    # Convert MappingProxyType to dict for JSON serialization
    if isinstance(data, MappingProxyType):
        data = dict(data)
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _deserialize_json(value: str | None) -> dict[str, Any]:
    """Deserialize JSON to dictionary."""
    if value is None:
        return {}
    return json.loads(value)


def _is_terminal_state(state: MissionLifecycle) -> bool:
    """Check if mission state is terminal (irreversible)."""
    return state in (
        MissionLifecycle.COMPLETED,
        MissionLifecycle.FAILED,
        MissionLifecycle.CANCELLED,
    )


def _validate_transition(from_state: MissionLifecycle, to_state: MissionLifecycle) -> None:
    """Validate state machine transition rules.

    Terminal states are irreversible and immutable.
    Specific transitions are explicitly allowed or denied.
    """
    # Terminal states cannot transition - not even to themselves
    # This preserves terminal evidence immutability
    if _is_terminal_state(from_state):
        if from_state == to_state:
            # Reject same-state terminal attempts to preserve immutability
            raise IllegalMissionTransitionError(
                f"Terminal state {from_state.value} is immutable - cannot re-transition to preserve evidence integrity"
            )
        else:
            # Reject terminal resurrection
            raise IllegalMissionTransitionError(
                f"Cannot transition from terminal state {from_state.value} to {to_state.value}"
            )

    # Non-terminal idempotent transitions are allowed
    if from_state == to_state:
        return

    # Define allowed transitions
    allowed_transitions = {
        MissionLifecycle.CREATED: {
            MissionLifecycle.RUNNING,
            MissionLifecycle.CANCELLED,
        },
        MissionLifecycle.RUNNING: {
            MissionLifecycle.PAUSED,
            MissionLifecycle.COMPLETED,
            MissionLifecycle.FAILED,
            MissionLifecycle.CANCELLED,
        },
        MissionLifecycle.PAUSED: {
            MissionLifecycle.RUNNING,
            MissionLifecycle.COMPLETED,
            MissionLifecycle.FAILED,
            MissionLifecycle.CANCELLED,
        },
    }

    allowed = allowed_transitions.get(from_state, set())
    if to_state not in allowed:
        raise IllegalMissionTransitionError(
            f"Illegal transition from {from_state.value} to {to_state.value}"
        )


# Schema contract definitions for migration safety
# Schema v2 includes all v1 tables plus mission_controller_leases
_EXPECTED_SCHEMA_TABLES_V2: dict[str, tuple[str, ...]] = {
    "mission_runtime_schema": ("version", "applied_at"),
    "missions": (
        "control_domain",
        "mission_id",
        "specification_fingerprint",
        "objective",
        "owner_identity",
        "agent_identity",
        "success_criteria",
        "constraints",
        "deadline",
        "metadata_json",
        "created_at",
    ),
    "mission_state": (
        "control_domain",
        "mission_id",
        "current_state",
        "revision",
        "updated_at",
        "started_at",
        "paused_at",
        "resumed_at",
        "terminal_at",
        "terminal_reason",
    ),
    "mission_checkpoints": (
        "control_domain",
        "checkpoint_id",
        "mission_id",
        "sequence",
        "mission_state",
        "progress_data_json",
        "reason",
        "created_at",
    ),
    "mission_transitions": (
        "control_domain",
        "transition_id",
        "mission_id",
        "from_state",
        "to_state",
        "revision",
        "reason",
        "checkpoint_id",
        "transitioned_at",
    ),
    "mission_effect_references": (
        "control_domain",
        "mission_id",
        "effect_intent_id",
        "effect_dispatch_id",
        "gateway_claim_id",
        "referenced_at",
    ),
    "mission_controller_leases": (
        "control_domain",
        "mission_id",
        "generation",
        "controller_id",
        "acquired_at",
        "renewed_at",
        "expires_at",
        "released_at",
    ),
}

_EXPECTED_INDEXES_V2: dict[str, tuple[str, tuple[str, ...]]] = {
    "idx_missions_state": ("mission_state", ("control_domain", "current_state", "updated_at")),
    "idx_checkpoints_mission": ("mission_checkpoints", ("control_domain", "mission_id", "sequence")),
    "idx_transitions_mission": ("mission_transitions", ("control_domain", "mission_id", "transitioned_at")),
}

# Schema v1 is v2 without the controller leases table
_EXPECTED_SCHEMA_TABLES_V1: dict[str, tuple[str, ...]] = {
    name: columns
    for name, columns in _EXPECTED_SCHEMA_TABLES_V2.items()
    if name != "mission_controller_leases"
}

_EXPECTED_INDEXES_V1 = _EXPECTED_INDEXES_V2


class MissionRuntimeStore:
    """Durable, concurrency-safe storage for mission lifecycle state.

    Provides transactional persistence for:
    - Mission specifications (immutable identity)
    - Mission lifecycle state (mutable with optimistic concurrency)
    - Mission checkpoints (append-only progress)
    - Mission transitions (append-only history)
    - Effect references (bindings without truth claims)

    All operations are ControlDomain-scoped and transaction-protected.
    Concurrent access from multiple processes/threads is safe via optimistic locking.
    """

    def __init__(self, db_path: Path | str | None = None, *, clock: Any = None) -> None:
        """Initialize mission runtime store.

        Args:
            db_path: Path to SQLite database file. If None, uses in-memory database.
            clock: Optional clock callable returning timezone-aware datetime.
                   If None, uses datetime.now(timezone.utc).
                   For testing only - production should use default clock.
        """
        self._db_path = Path(db_path) if db_path is not None else None
        self._memory_connection: sqlite3.Connection | None = None
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._wal_established = False

        if self._db_path is None:
            # In-memory database for testing
            self._memory_connection = sqlite3.connect(
                ":memory:",
                check_same_thread=False,
                timeout=_MAX_BUSY_TIMEOUT_MS / 1000.0,
            )
            self._memory_connection.execute("PRAGMA foreign_keys = ON")
            self._initialize_schema(self._memory_connection)
            self._wal_established = True  # Not applicable for memory DB
        else:
            # File-backed database - initialize schema first, then establish WAL
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
            try:
                self._initialize_schema(conn)
                self._establish_wal_mode(conn)
            finally:
                conn.close()

    def _connect(self) -> sqlite3.Connection:
        """Create a new database connection.

        For file-backed databases, WAL mode is established separately after
        schema initialization to avoid lock conflicts during concurrent fresh initialization.
        """
        if self._memory_connection is not None:
            return self._memory_connection

        if self._db_path is None:
            raise MissionRuntimeStoreError("No database path configured")

        conn = sqlite3.connect(
            self._db_path,
            timeout=_MAX_BUSY_TIMEOUT_MS / 1000.0,
        )
        conn.execute("PRAGMA foreign_keys = ON")

        # Establish WAL mode if not already done (for post-initialization connections)
        if _FILE_JOURNAL_MODE and self._wal_established:
            conn.execute(f"PRAGMA journal_mode = {_FILE_JOURNAL_MODE}")

        return conn

    def _establish_wal_mode(self, conn: sqlite3.Connection) -> None:
        """Establish WAL journal mode after schema initialization.

        This is called once during store initialization, after the schema is ready.
        Separating WAL establishment from connection creation prevents lock conflicts
        when multiple processes initialize a fresh database concurrently.
        """
        if not _FILE_JOURNAL_MODE or self._wal_established:
            return

        # WAL mode setup with bounded retry for lock conflicts
        max_retries = 10
        retry_delay = 0.01  # 10ms initial delay

        for attempt in range(max_retries):
            try:
                result = conn.execute(f"PRAGMA journal_mode = {_FILE_JOURNAL_MODE}").fetchone()
                if result and result[0].lower() == _FILE_JOURNAL_MODE.lower():
                    self._wal_established = True
                    return
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() and attempt < max_retries - 1:
                    # Database is locked, retry with exponential backoff
                    import time
                    time.sleep(retry_delay * (2 ** attempt))
                    continue
                else:
                    # Non-lock error or final retry exhausted
                    raise MissionRuntimeStoreError(
                        f"Failed to establish {_FILE_JOURNAL_MODE} journal mode after {attempt + 1} attempts"
                    ) from e

        # If we get here, we exhausted retries
        raise MissionRuntimeStoreError(
            f"Failed to establish {_FILE_JOURNAL_MODE} journal mode after {max_retries} attempts"
        )

    def _now_utc(self) -> datetime:
        """Get current time from configured clock with validation.

        Validates clock output before any database mutation to ensure
        transaction integrity. Invalid clock output raises before commit.

        Returns:
            Validated UTC datetime

        Raises:
            MissionControllerClockError: If clock output is invalid
        """
        now = self._clock()

        if not isinstance(now, datetime):
            raise MissionControllerClockError(
                f"Clock must return datetime, got {type(now).__name__}"
            )

        if now.tzinfo is None or now.utcoffset() is None:
            raise MissionControllerClockError(
                "Clock must return timezone-aware datetime with valid UTC offset"
            )

        # Normalize to UTC
        return now.astimezone(timezone.utc)

    def _validate_schema_contract(
        self,
        conn: sqlite3.Connection,
        *,
        expected_tables: dict[str, tuple[str, ...]] = _EXPECTED_SCHEMA_TABLES_V2,
        expected_indexes: dict[str, tuple[str, tuple[str, ...]]] = _EXPECTED_INDEXES_V2,
        expected_version: int = MISSION_SCHEMA_VERSION,
    ) -> None:
        """Validate database schema contract with structural integrity checks.

        Ensures exact table names, column order, column types, constraints,
        indexes, and version metadata. Fails closed on any mismatch.

        Validates:
        - Table existence and completeness
        - Column names, order, and types
        - NOT NULL constraints
        - PRIMARY KEY constraints
        - FOREIGN KEY constraints
        - CHECK constraints
        - UNIQUE constraints
        - Indexes
        - Schema version metadata
        """
        # Validate tables
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        expected_table_names = set(expected_tables)
        missing_tables = expected_table_names - tables
        unexpected_tables = tables - expected_table_names
        if missing_tables or unexpected_tables:
            raise MissionSchemaVersionError(
                "Database schema is incomplete; "
                f"missing tables: {sorted(missing_tables)}; "
                f"unexpected tables: {sorted(unexpected_tables)}"
            )

        # Validate column order and properties for each table
        for table_name, expected_columns in expected_tables.items():
            table_info = list(conn.execute(f"PRAGMA table_info({table_name})"))
            columns = tuple(row[1] for row in table_info)
            if columns != expected_columns:
                raise MissionSchemaVersionError(
                    f"Database schema table {table_name} has incompatible columns: {columns!r}"
                )

        # Validate structural constraints
        self._validate_table_constraints(conn, expected_version)

        # Validate indexes
        for index_name, (table_name, expected_columns) in expected_indexes.items():
            index_row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name = ?",
                (index_name,),
            ).fetchone()
            if index_row is None:
                raise MissionSchemaVersionError(f"Database schema is incomplete; missing index {index_name}")
            indexed_columns = tuple(
                row[2]
                for row in conn.execute(f"PRAGMA index_info({index_name})")
            )
            if indexed_columns != expected_columns:
                raise MissionSchemaVersionError(
                    f"Database index {index_name} on {table_name} has incompatible columns: "
                    f"{indexed_columns!r}"
                )

        # Validate version metadata
        try:
            version_rows = [
                int(row[0])
                for row in conn.execute("SELECT version FROM mission_runtime_schema ORDER BY version")
            ]
        except (ValueError, TypeError) as exc:
            raise MissionSchemaVersionError(
                "Database schema version metadata contains non-integer value"
            ) from exc
        if not version_rows:
            raise MissionSchemaVersionError("Database schema version metadata is empty")
        if version_rows != [expected_version]:
            raise MissionSchemaVersionError(
                f"Database schema version metadata must contain exactly [{expected_version}], "
                f"found {version_rows!r}"
            )

    def _validate_table_constraints(self, conn: sqlite3.Connection, schema_version: int) -> None:
        """Validate table structural constraints.

        Validates PRIMARY KEY, FOREIGN KEY, CHECK, NOT NULL, and UNIQUE constraints
        that the Mission Runtime safety model relies on.
        """
        # Define expected constraints per schema version
        if schema_version == 1:
            constraint_specs = self._get_v1_constraint_specs()
        elif schema_version == 2:
            constraint_specs = self._get_v2_constraint_specs()
        else:
            raise MissionSchemaVersionError(f"Unknown schema version {schema_version} for constraint validation")

        for table_name, specs in constraint_specs.items():
            # Validate PRIMARY KEY
            if "primary_key" in specs:
                self._validate_primary_key(conn, table_name, specs["primary_key"])

            # Validate FOREIGN KEYs
            if "foreign_keys" in specs:
                self._validate_foreign_keys(conn, table_name, specs["foreign_keys"])

            # Validate NOT NULL constraints
            if "not_null" in specs:
                self._validate_not_null(conn, table_name, specs["not_null"])

            # Validate CHECK constraints
            if "checks" in specs:
                self._validate_check_constraints(conn, table_name, specs["checks"])

            # Validate UNIQUE constraints
            if "unique" in specs:
                self._validate_unique_constraints(conn, table_name, specs["unique"])

    def _get_v1_constraint_specs(self) -> dict[str, dict]:
        """Get constraint specifications for v1 schema."""
        return {
            "mission_runtime_schema": {
                "primary_key": ["version"],
            },
            "missions": {
                "primary_key": ["control_domain", "mission_id"],
            },
            "mission_state": {
                "primary_key": ["control_domain", "mission_id"],
                "foreign_keys": [
                    {
                        "columns": ["control_domain", "mission_id"],
                        "ref_table": "missions",
                        "ref_columns": ["control_domain", "mission_id"],
                    }
                ],
                "checks": ["current_state IN", "revision >= 1"],
            },
            "mission_checkpoints": {
                "primary_key": ["control_domain", "checkpoint_id"],
                "foreign_keys": [
                    {
                        "columns": ["control_domain", "mission_id"],
                        "ref_table": "missions",
                        "ref_columns": ["control_domain", "mission_id"],
                    }
                ],
                "unique": [["control_domain", "mission_id", "sequence"]],
                "checks": ["sequence >= 0"],
            },
            "mission_transitions": {
                "primary_key": ["control_domain", "transition_id"],
                "foreign_keys": [
                    {
                        "columns": ["control_domain", "mission_id"],
                        "ref_table": "missions",
                        "ref_columns": ["control_domain", "mission_id"],
                    }
                ],
                "checks": ["revision >= 1"],
            },
            "mission_effect_references": {
                "primary_key": ["control_domain", "mission_id", "effect_intent_id"],
                "foreign_keys": [
                    {
                        "columns": ["control_domain", "mission_id"],
                        "ref_table": "missions",
                        "ref_columns": ["control_domain", "mission_id"],
                    }
                ],
            },
        }

    def _get_v2_constraint_specs(self) -> dict[str, dict]:
        """Get constraint specifications for v2 schema."""
        specs = self._get_v1_constraint_specs()
        specs["mission_controller_leases"] = {
            "primary_key": ["control_domain", "mission_id", "generation"],
            "foreign_keys": [
                {
                    "columns": ["control_domain", "mission_id"],
                    "ref_table": "missions",
                    "ref_columns": ["control_domain", "mission_id"],
                }
            ],
            "checks": ["generation >= 1"],
            "not_null": ["generation", "controller_id", "acquired_at", "renewed_at", "expires_at"],
        }
        return specs

    def _validate_primary_key(self, conn: sqlite3.Connection, table_name: str, expected_pk: list[str]) -> None:
        """Validate PRIMARY KEY constraint."""
        table_info = list(conn.execute(f"PRAGMA table_info({table_name})"))
        pk_columns = [row[1] for row in table_info if row[5] > 0]  # pk column is index 5
        pk_columns.sort(key=lambda col: next(row[5] for row in table_info if row[1] == col))

        if pk_columns != expected_pk:
            raise MissionSchemaVersionError(
                f"Table {table_name} has incorrect PRIMARY KEY: "
                f"expected {expected_pk}, found {pk_columns}"
            )

    def _validate_foreign_keys(
        self, conn: sqlite3.Connection, table_name: str, expected_fks: list[dict]
    ) -> None:
        """Validate FOREIGN KEY constraints."""
        fk_list = list(conn.execute(f"PRAGMA foreign_key_list({table_name})"))

        # Group by fk id
        fks_by_id: dict[int, list] = {}
        for row in fk_list:
            fk_id = row[0]
            if fk_id not in fks_by_id:
                fks_by_id[fk_id] = []
            fks_by_id[fk_id].append(row)

        if len(fks_by_id) != len(expected_fks):
            raise MissionSchemaVersionError(
                f"Table {table_name} has incorrect number of FOREIGN KEYs: "
                f"expected {len(expected_fks)}, found {len(fks_by_id)}"
            )

        for expected_fk in expected_fks:
            # Find matching FK
            found = False
            for fk_rows in fks_by_id.values():
                ref_table = fk_rows[0][2]
                from_cols = [row[3] for row in fk_rows]
                to_cols = [row[4] for row in fk_rows]

                if (ref_table == expected_fk["ref_table"] and
                    from_cols == expected_fk["columns"] and
                    to_cols == expected_fk["ref_columns"]):
                    found = True
                    break

            if not found:
                raise MissionSchemaVersionError(
                    f"Table {table_name} missing expected FOREIGN KEY: {expected_fk}"
                )

    def _validate_not_null(self, conn: sqlite3.Connection, table_name: str, expected_not_null: list[str]) -> None:
        """Validate NOT NULL constraints."""
        table_info = list(conn.execute(f"PRAGMA table_info({table_name})"))
        not_null_cols = [row[1] for row in table_info if row[3] == 1]  # notnull is index 3

        for col in expected_not_null:
            if col not in not_null_cols:
                raise MissionSchemaVersionError(
                    f"Table {table_name} column {col} missing required NOT NULL constraint"
                )

    def _validate_check_constraints(self, conn: sqlite3.Connection, table_name: str, expected_checks: list[str]) -> None:
        """Validate CHECK constraints by examining CREATE TABLE SQL."""
        cursor = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        row = cursor.fetchone()
        if not row:
            raise MissionSchemaVersionError(f"Table {table_name} not found in sqlite_master")

        create_sql = row[0]
        if not create_sql:
            raise MissionSchemaVersionError(f"Table {table_name} has no CREATE SQL")

        # Normalize whitespace for comparison
        normalized_sql = " ".join(create_sql.split()).upper()

        for check_clause in expected_checks:
            normalized_check = " ".join(check_clause.split()).upper()
            if normalized_check not in normalized_sql:
                raise MissionSchemaVersionError(
                    f"Table {table_name} missing required CHECK constraint: {check_clause}"
                )

    def _validate_unique_constraints(
        self, conn: sqlite3.Connection, table_name: str, expected_unique: list[list[str]]
    ) -> None:
        """Validate UNIQUE constraints."""
        # Get all indexes for this table
        index_list = list(conn.execute(f"PRAGMA index_list({table_name})"))

        for expected_cols in expected_unique:
            # Find a unique index matching these columns
            found = False
            for idx_row in index_list:
                idx_name = idx_row[1]
                is_unique = idx_row[2] == 1

                if not is_unique:
                    continue

                # Get columns in this index
                idx_info = list(conn.execute(f"PRAGMA index_info({idx_name})"))
                idx_cols = [row[2] for row in idx_info]

                if idx_cols == expected_cols:
                    found = True
                    break

            if not found:
                raise MissionSchemaVersionError(
                    f"Table {table_name} missing required UNIQUE constraint on {expected_cols}"
                )

    def _initialize_schema(self, conn: sqlite3.Connection) -> None:
        """Initialize or migrate database schema.

        Handles:
        - Fresh database creation (v2 schema)
        - v1 → v2 migration with concurrent safety
        - v2 validation

        Migration follows the accepted DurableEffectStore pattern:
        schema validation occurs AFTER BEGIN IMMEDIATE to ensure
        authoritative state observation.
        """
        with conn:
            # Check schema version
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='mission_runtime_schema'"
            )
            if cursor.fetchone() is None:
                # No schema table - could be fresh or invalid database
                existing_objects = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%'"
                    )
                }
                if existing_objects:
                    raise MissionSchemaVersionError(
                        "Database contains schema objects but no authoritative version metadata"
                    )

                # Fresh database - create v2 schema atomically
                conn.execute("BEGIN IMMEDIATE")
                try:
                    # Re-check after acquiring lock (another process may have initialized)
                    refreshed_cursor = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='mission_runtime_schema'"
                    )
                    if refreshed_cursor.fetchone() is not None:
                        # Another process initialized - validate and proceed
                        conn.rollback()
                        # Recursive call will handle validation
                        return self._initialize_schema(conn)

                    # Create fresh v2 schema
                    self._create_schema_v2(conn)
                    self._validate_schema_contract(conn)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            else:
                # Existing database - validate or migrate version
                cursor = conn.execute("SELECT version FROM mission_runtime_schema ORDER BY version")
                try:
                    version_rows = [int(row[0]) for row in cursor.fetchall()]
                except (ValueError, TypeError) as exc:
                    raise MissionSchemaVersionError(
                        "Database schema version metadata contains non-integer value"
                    ) from exc

                if not version_rows:
                    raise MissionSchemaVersionError("Database schema version metadata is empty")

                distinct_versions = sorted(set(version_rows))
                if len(distinct_versions) != 1:
                    raise MissionSchemaVersionError(
                        f"Database schema metadata contains contradictory versions: {distinct_versions}"
                    )

                current_version = distinct_versions[0]

                if current_version < 1:
                    raise MissionSchemaVersionError(
                        f"Database schema version {current_version} is older than supported versions"
                    )
                if current_version > MISSION_SCHEMA_VERSION:
                    raise MissionSchemaVersionError(
                        f"Database schema version {current_version} is newer than "
                        f"supported version {MISSION_SCHEMA_VERSION}. Upgrade required."
                    )

                if current_version == 1:
                    # Migrate v1 → v2
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        # Re-read version inside transaction (authoritative read)
                        refreshed_cursor = conn.execute(
                            "SELECT version FROM mission_runtime_schema ORDER BY version"
                        )
                        refreshed_versions = [int(row[0]) for row in refreshed_cursor.fetchall()]

                        if refreshed_versions != [1]:
                            # Another process already migrated
                            conn.rollback()
                            if refreshed_versions == [MISSION_SCHEMA_VERSION]:
                                # Migration complete - validate v2
                                self._validate_schema_contract(conn)
                                return
                            # Unexpected state - retry
                            return self._initialize_schema(conn)

                        # Validate v1 schema before migration
                        self._validate_schema_contract(
                            conn,
                            expected_tables=_EXPECTED_SCHEMA_TABLES_V1,
                            expected_indexes=_EXPECTED_INDEXES_V1,
                            expected_version=1,
                        )

                        # Apply v1 → v2 migration
                        self._migrate_v1_to_v2(conn)

                        # Update version metadata
                        conn.execute("DELETE FROM mission_runtime_schema")
                        conn.execute(
                            "INSERT INTO mission_runtime_schema (version) VALUES (?)",
                            (MISSION_SCHEMA_VERSION,),
                        )

                        # Validate v2 schema
                        self._validate_schema_contract(conn)
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                elif current_version == MISSION_SCHEMA_VERSION:
                    # Current version - validate schema
                    self._validate_schema_contract(conn)

    def _migrate_v1_to_v2(self, conn: sqlite3.Connection) -> None:
        """Migrate database from schema v1 to v2.

        Adds mission_controller_leases table for durable controller ownership tracking.
        All existing v1 data is preserved.
        """
        conn.execute("""
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                controller_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                PRIMARY KEY (control_domain, mission_id, generation),
                FOREIGN KEY (control_domain, mission_id)
                    REFERENCES missions(control_domain, mission_id)
            )
        """)

    def _create_schema_v2(self, conn: sqlite3.Connection) -> None:
        """Create fresh mission runtime schema v2.

        Uses statement-by-statement execution to preserve transaction atomicity.
        executescript() implicitly commits and would break rollback semantics.
        """
        # Schema version tracking
        conn.execute("""
            CREATE TABLE mission_runtime_schema (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute(
            "INSERT INTO mission_runtime_schema (version) VALUES (?)",
            (MISSION_SCHEMA_VERSION,)
        )

        # Mission specifications (immutable identity)
        conn.execute("""
            CREATE TABLE missions (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                specification_fingerprint TEXT NOT NULL,
                objective TEXT NOT NULL,
                owner_identity TEXT NOT NULL,
                agent_identity TEXT,
                success_criteria TEXT,
                constraints TEXT,
                deadline TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id)
            )
        """)

        # Mission lifecycle state (mutable with optimistic concurrency)
        conn.execute("""
            CREATE TABLE mission_state (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK (
                    current_state IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')
                ),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                updated_at TEXT NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                resumed_at TEXT,
                terminal_at TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (control_domain, mission_id),
                FOREIGN KEY (control_domain, mission_id)
                    REFERENCES missions(control_domain, mission_id)
            )
        """)

        # Mission checkpoints (append-only progress evidence)
        conn.execute("""
            CREATE TABLE mission_checkpoints (
                control_domain TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                mission_state TEXT NOT NULL,
                progress_data_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, checkpoint_id),
                FOREIGN KEY (control_domain, mission_id)
                    REFERENCES missions(control_domain, mission_id),
                UNIQUE (control_domain, mission_id, sequence)
            )
        """)

        # Mission transitions (append-only history)
        conn.execute("""
            CREATE TABLE mission_transitions (
                control_domain TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                reason TEXT,
                checkpoint_id TEXT,
                transitioned_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, transition_id),
                FOREIGN KEY (control_domain, mission_id)
                    REFERENCES missions(control_domain, mission_id)
            )
        """)

        # Effect references (bindings without truth claims)
        conn.execute("""
            CREATE TABLE mission_effect_references (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                effect_dispatch_id TEXT,
                gateway_claim_id TEXT,
                referenced_at TEXT NOT NULL,
                PRIMARY KEY (control_domain, mission_id, effect_intent_id),
                FOREIGN KEY (control_domain, mission_id)
                    REFERENCES missions(control_domain, mission_id)
            )
        """)

        # Mission controller leases (durable ownership generations)
        conn.execute("""
            CREATE TABLE mission_controller_leases (
                control_domain TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                controller_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                PRIMARY KEY (control_domain, mission_id, generation),
                FOREIGN KEY (control_domain, mission_id)
                    REFERENCES missions(control_domain, mission_id)
            )
        """)

        # Indexes for common queries
        conn.execute("""
            CREATE INDEX idx_missions_state
                ON mission_state(control_domain, current_state, updated_at)
        """)

        conn.execute("""
            CREATE INDEX idx_checkpoints_mission
                ON mission_checkpoints(control_domain, mission_id, sequence DESC)
        """)

        conn.execute("""
            CREATE INDEX idx_transitions_mission
                ON mission_transitions(control_domain, mission_id, transitioned_at DESC)
        """)

    def create_mission(self, specification: MissionSpecification) -> None:
        """Create a new mission with initial CREATED state.

        Args:
            specification: Immutable mission specification

        Raises:
            MissionRuntimeStoreError: If mission already exists
        """
        if not isinstance(specification, MissionSpecification):
            raise TypeError("specification must be a MissionSpecification")

        conn = self._connect()
        try:
            with conn:
                # Insert mission specification
                conn.execute(
                    """
                    INSERT INTO missions (
                        control_domain, mission_id, specification_fingerprint,
                        objective, owner_identity, agent_identity,
                        success_criteria, constraints, deadline, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        specification.control_domain,
                        specification.mission_id,
                        specification.specification_fingerprint(),
                        specification.objective,
                        specification.owner_identity,
                        specification.agent_identity,
                        specification.success_criteria,
                        specification.constraints,
                        _serialize_timestamp(specification.deadline),
                        _serialize_json(specification.metadata),
                        _serialize_timestamp(specification.created_at),
                    ),
                )

                # Initialize lifecycle state
                conn.execute(
                    """
                    INSERT INTO mission_state (
                        control_domain, mission_id, current_state, revision, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        specification.control_domain,
                        specification.mission_id,
                        MissionLifecycle.CREATED.value,
                        1,
                        _serialize_timestamp(datetime.now(timezone.utc)),
                    ),
                )

                # Record initial transition
                transition_id = f"transition-{secrets.token_urlsafe(16)}"
                conn.execute(
                    """
                    INSERT INTO mission_transitions (
                        control_domain, transition_id, mission_id,
                        from_state, to_state, revision, reason, transitioned_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        specification.control_domain,
                        transition_id,
                        specification.mission_id,
                        MissionLifecycle.CREATED.value,
                        MissionLifecycle.CREATED.value,
                        1,
                        "Mission created",
                        _serialize_timestamp(datetime.now(timezone.utc)),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise MissionRuntimeStoreError(
                f"Mission {specification.mission_id} already exists in domain {specification.control_domain}"
            ) from exc
        finally:
            if self._memory_connection is None:
                conn.close()

    def get_mission(
        self, control_domain: str, mission_id: str
    ) -> tuple[MissionSpecification, MissionLifecycle, int, datetime]:
        """Get mission specification and current state.

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier

        Returns:
            (specification, current_state, revision, updated_at)

        Raises:
            MissionNotFoundError: If mission does not exist
        """
        control_domain = validate_domain_id(control_domain, "control_domain")

        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                SELECT
                    m.mission_id, m.control_domain, m.objective, m.owner_identity,
                    m.agent_identity, m.success_criteria, m.constraints, m.deadline,
                    m.metadata_json, m.created_at,
                    s.current_state, s.revision, s.updated_at
                FROM missions m
                JOIN mission_state s ON m.control_domain = s.control_domain AND m.mission_id = s.mission_id
                WHERE m.control_domain = ? AND m.mission_id = ?
                """,
                (control_domain, mission_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise MissionNotFoundError(
                    f"Mission {mission_id} not found in domain {control_domain}"
                )

            specification = MissionSpecification(
                mission_id=row[0],
                control_domain=row[1],
                objective=row[2],
                owner_identity=row[3],
                agent_identity=row[4],
                success_criteria=row[5],
                constraints=row[6],
                deadline=_deserialize_timestamp(row[7]),
                metadata=_deserialize_json(row[8]),
                created_at=_deserialize_timestamp(row[9]),
            )

            current_state = MissionLifecycle(row[10])
            revision = row[11]
            updated_at = _deserialize_timestamp(row[12])

            return (specification, current_state, revision, updated_at)
        finally:
            if self._memory_connection is None:
                conn.close()

    def transition_mission(
        self,
        control_domain: str,
        mission_id: str,
        expected_revision: int,
        to_state: MissionLifecycle,
        reason: str | None = None,
        checkpoint_id: str | None = None,
    ) -> int:
        """Transition mission to new state with optimistic concurrency control.

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier
            expected_revision: Current revision (for concurrency control)
            to_state: Target state
            reason: Optional transition reason
            checkpoint_id: Optional associated checkpoint

        Returns:
            New revision number

        Raises:
            MissionNotFoundError: If mission does not exist
            MissionRevisionConflictError: If expected_revision is stale
            IllegalMissionTransitionError: If transition violates state machine
            DomainMismatchError: If control_domain does not match
        """
        control_domain = validate_domain_id(control_domain, "control_domain")

        if not isinstance(to_state, MissionLifecycle):
            raise TypeError("to_state must be a MissionLifecycle")

        # Validate expected_revision - exact type check to prevent int subclass attacks
        if type(expected_revision) is not int:
            raise ValueError("expected_revision must be an exact int (not bool, not subclass)")
        if expected_revision < 1:
            raise ValueError("expected_revision must be a positive integer (>= 1)")

        conn = self._connect()
        try:
            # Execute BEGIN IMMEDIATE explicitly to acquire write lock
            # This blocks other writers until we commit, preventing revision races
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Get current state and revision for validation
                # This is an early check - the atomic check is UPDATE rowcount
                cursor = conn.execute(
                    """
                    SELECT current_state, revision
                    FROM mission_state
                    WHERE control_domain = ? AND mission_id = ?
                    """,
                    (control_domain, mission_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise MissionNotFoundError(
                        f"Mission {mission_id} not found in domain {control_domain}"
                    )

                current_state = MissionLifecycle(row[0])
                current_revision = row[1]

                # Early optimistic concurrency check
                # The UPDATE rowcount is the final atomic authority
                if current_revision != expected_revision:
                    raise MissionRevisionConflictError(
                        f"Revision conflict: expected {expected_revision}, current {current_revision}"
                    )

                # Validate transition
                _validate_transition(current_state, to_state)

                # Compute new revision
                new_revision = current_revision + 1
                now = datetime.now(timezone.utc)

                # Update mission state
                updates = ["current_state = ?", "revision = ?", "updated_at = ?"]
                params: list[Any] = [to_state.value, new_revision, _serialize_timestamp(now)]

                # Track lifecycle timestamps
                if to_state == MissionLifecycle.RUNNING and current_state != MissionLifecycle.PAUSED:
                    updates.append("started_at = ?")
                    params.append(_serialize_timestamp(now))
                elif to_state == MissionLifecycle.PAUSED:
                    updates.append("paused_at = ?")
                    params.append(_serialize_timestamp(now))
                elif to_state == MissionLifecycle.RUNNING and current_state == MissionLifecycle.PAUSED:
                    updates.append("resumed_at = ?")
                    params.append(_serialize_timestamp(now))
                elif _is_terminal_state(to_state):
                    updates.append("terminal_at = ?")
                    updates.append("terminal_reason = ?")
                    params.extend([_serialize_timestamp(now), reason])

                params.extend([control_domain, mission_id, expected_revision])

                cursor = conn.execute(
                    f"""
                    UPDATE mission_state
                    SET {', '.join(updates)}
                    WHERE control_domain = ? AND mission_id = ? AND revision = ?
                    """,
                    params,
                )

                # Atomic optimistic concurrency check: exactly one row must be updated
                if cursor.rowcount != 1:
                    # Revision changed between SELECT and UPDATE - another writer won
                    raise MissionRevisionConflictError(
                        f"Revision conflict: expected {expected_revision}, but state was modified by concurrent writer"
                    )

                # Record transition
                transition_id = f"transition-{secrets.token_urlsafe(16)}"
                conn.execute(
                    """
                    INSERT INTO mission_transitions (
                        control_domain, transition_id, mission_id,
                        from_state, to_state, revision, reason, checkpoint_id, transitioned_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        control_domain,
                        transition_id,
                        mission_id,
                        current_state.value,
                        to_state.value,
                        new_revision,
                        reason,
                        checkpoint_id,
                        _serialize_timestamp(now),
                    ),
                )

                conn.commit()
                return new_revision
            except Exception:
                conn.rollback()
                raise
        finally:
            if self._memory_connection is None:
                conn.close()

    def create_checkpoint(self, checkpoint: MissionCheckpoint, *, expected_revision: int | None = None) -> None:
        """Create a new mission checkpoint.

        Checkpoints can only be created for non-terminal missions, and the
        checkpoint's claimed mission_state must match the actual current state.

        Args:
            checkpoint: Mission checkpoint
            expected_revision: Optional expected mission revision for concurrency control.
                If supplied, checkpoint creation fails with MissionRevisionConflictError
                if the mission revision has changed. MUST be a positive integer (not bool).

        Raises:
            MissionNotFoundError: If mission does not exist
            MissionRevisionConflictError: If expected_revision is stale
            MissionRuntimeStoreError: If checkpoint sequence already exists
            IllegalMissionTransitionError: If mission is terminal or state mismatch
            ValueError: If expected_revision validation fails
        """
        if not isinstance(checkpoint, MissionCheckpoint):
            raise TypeError("checkpoint must be a MissionCheckpoint")

        # Validate expected_revision if supplied - exact type check to prevent int subclass attacks
        if expected_revision is not None:
            if type(expected_revision) is not int:
                raise ValueError("expected_revision must be an exact int (not bool, not subclass)")
            if expected_revision < 1:
                raise ValueError("expected_revision must be a positive integer (>= 1)")

        conn = self._connect()
        try:
            # Execute BEGIN IMMEDIATE explicitly to acquire write lock
            # This blocks other writers until we commit, preventing revision races
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Verify mission exists and get current state AND revision
                cursor = conn.execute(
                    """
                    SELECT current_state, revision FROM mission_state
                    WHERE control_domain = ? AND mission_id = ?
                    """,
                    (checkpoint.control_domain, checkpoint.mission_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise MissionNotFoundError(
                        f"Mission {checkpoint.mission_id} not found in domain {checkpoint.control_domain}"
                    )

                current_state = MissionLifecycle(row[0])
                current_revision = row[1]

                # If expected_revision supplied, verify it matches current revision
                if expected_revision is not None:
                    if current_revision != expected_revision:
                        raise MissionRevisionConflictError(
                            f"Revision conflict: expected {expected_revision}, current {current_revision}"
                        )

                # Reject checkpoints for terminal missions
                if _is_terminal_state(current_state):
                    raise IllegalMissionTransitionError(
                        f"Cannot create checkpoint for terminal mission in state {current_state.value}"
                    )

                # Verify checkpoint state matches current state
                if checkpoint.mission_state != current_state:
                    raise IllegalMissionTransitionError(
                        f"Checkpoint mission_state {checkpoint.mission_state.value} does not match "
                        f"current mission state {current_state.value}"
                    )

                # Insert checkpoint
                conn.execute(
                    """
                    INSERT INTO mission_checkpoints (
                        control_domain, checkpoint_id, mission_id, sequence,
                        mission_state, progress_data_json, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint.control_domain,
                        checkpoint.checkpoint_id,
                        checkpoint.mission_id,
                        checkpoint.sequence,
                        checkpoint.mission_state.value,
                        _serialize_json(checkpoint.progress_data),
                        checkpoint.reason,
                        _serialize_timestamp(checkpoint.created_at),
                    ),
                )

                conn.commit()
            except Exception:
                conn.rollback()
                raise
        except sqlite3.IntegrityError as exc:
            raise MissionRuntimeStoreError(
                f"Checkpoint sequence {checkpoint.sequence} already exists for mission {checkpoint.mission_id}"
            ) from exc
        finally:
            if self._memory_connection is None:
                conn.close()

    def list_checkpoints(
        self, control_domain: str, mission_id: str
    ) -> list[MissionCheckpoint]:
        """List all checkpoints for a mission in sequence order.

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier

        Returns:
            List of checkpoints ordered by sequence
        """
        control_domain = validate_domain_id(control_domain, "control_domain")

        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                SELECT checkpoint_id, mission_id, control_domain, sequence,
                       mission_state, progress_data_json, reason, created_at
                FROM mission_checkpoints
                WHERE control_domain = ? AND mission_id = ?
                ORDER BY sequence ASC
                """,
                (control_domain, mission_id),
            )

            checkpoints = []
            for row in cursor.fetchall():
                checkpoint = MissionCheckpoint(
                    checkpoint_id=row[0],
                    mission_id=row[1],
                    control_domain=row[2],
                    sequence=row[3],
                    mission_state=MissionLifecycle(row[4]),
                    progress_data=_deserialize_json(row[5]),
                    reason=row[6],
                    created_at=_deserialize_timestamp(row[7]),
                )
                checkpoints.append(checkpoint)

            return checkpoints
        finally:
            if self._memory_connection is None:
                conn.close()

    def list_transitions(
        self, control_domain: str, mission_id: str
    ) -> list[MissionTransition]:
        """List all transitions for a mission in chronological order.

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier

        Returns:
            List of transitions ordered by time
        """
        control_domain = validate_domain_id(control_domain, "control_domain")

        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                SELECT transition_id, mission_id, control_domain, from_state, to_state,
                       revision, reason, checkpoint_id, transitioned_at
                FROM mission_transitions
                WHERE control_domain = ? AND mission_id = ?
                ORDER BY transitioned_at ASC
                """,
                (control_domain, mission_id),
            )

            transitions = []
            for row in cursor.fetchall():
                transition = MissionTransition(
                    transition_id=row[0],
                    mission_id=row[1],
                    control_domain=row[2],
                    from_state=MissionLifecycle(row[3]),
                    to_state=MissionLifecycle(row[4]),
                    revision=row[5],
                    reason=row[6],
                    checkpoint_id=row[7],
                    transitioned_at=_deserialize_timestamp(row[8]),
                )
                transitions.append(transition)

            return transitions
        finally:
            if self._memory_connection is None:
                conn.close()

    def add_effect_reference(self, effect_ref: EffectReference, mission_id: str, control_domain: str) -> None:
        """Add an effect reference to a mission.

        Effect references are immutable evidence bindings. If an identical reference
        already exists (same intent_id), this is a no-op. If a reference with the
        same intent_id but different dispatch_id or claim_id exists, this fails.

        Args:
            effect_ref: Effect reference
            mission_id: Mission identifier
            control_domain: ControlDomain identifier

        Raises:
            MissionNotFoundError: If mission does not exist
            MissionRuntimeStoreError: If conflicting reference exists
        """
        if not isinstance(effect_ref, EffectReference):
            raise TypeError("effect_ref must be an EffectReference")

        control_domain = validate_domain_id(control_domain, "control_domain")

        conn = self._connect()
        try:
            with conn:
                # Verify mission exists
                cursor = conn.execute(
                    """
                    SELECT 1 FROM missions
                    WHERE control_domain = ? AND mission_id = ?
                    """,
                    (control_domain, mission_id),
                )
                if cursor.fetchone() is None:
                    raise MissionNotFoundError(
                        f"Mission {mission_id} not found in domain {control_domain}"
                    )

                # Check for existing reference
                cursor = conn.execute(
                    """
                    SELECT effect_dispatch_id, gateway_claim_id
                    FROM mission_effect_references
                    WHERE control_domain = ? AND mission_id = ? AND effect_intent_id = ?
                    """,
                    (control_domain, mission_id, effect_ref.effect_intent_id),
                )
                existing = cursor.fetchone()

                if existing is not None:
                    # Reference exists - verify it's identical (idempotent)
                    if (existing[0] == effect_ref.effect_dispatch_id and
                        existing[1] == effect_ref.gateway_claim_id):
                        # Identical reference - idempotent no-op
                        return
                    else:
                        # Conflicting reference - fail closed
                        raise MissionRuntimeStoreError(
                            f"Effect reference {effect_ref.effect_intent_id} already exists with different "
                            f"dispatch_id or claim_id - effect references are immutable"
                        )

                # Insert new effect reference
                conn.execute(
                    """
                    INSERT INTO mission_effect_references (
                        control_domain, mission_id, effect_intent_id,
                        effect_dispatch_id, gateway_claim_id, referenced_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        control_domain,
                        mission_id,
                        effect_ref.effect_intent_id,
                        effect_ref.effect_dispatch_id,
                        effect_ref.gateway_claim_id,
                        _serialize_timestamp(effect_ref.referenced_at),
                    ),
                )
        finally:
            if self._memory_connection is None:
                conn.close()

    def list_effect_references(self, control_domain: str, mission_id: str) -> list[EffectReference]:
        """List all effect references for a mission.

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier

        Returns:
            List of effect references
        """
        control_domain = validate_domain_id(control_domain, "control_domain")

        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                SELECT effect_intent_id, effect_dispatch_id, gateway_claim_id, referenced_at
                FROM mission_effect_references
                WHERE control_domain = ? AND mission_id = ?
                ORDER BY referenced_at ASC
                """,
                (control_domain, mission_id),
            )

            references = []
            for row in cursor.fetchall():
                ref = EffectReference(
                    effect_intent_id=row[0],
                    effect_dispatch_id=row[1],
                    gateway_claim_id=row[2],
                    referenced_at=_deserialize_timestamp(row[3]),
                )
                references.append(ref)

            return references
        finally:
            if self._memory_connection is None:
                conn.close()

    def acquire_controller_lease(
        self,
        control_domain: str,
        mission_id: str,
        controller_id: str,
        *,
        lease_duration: timedelta = DEFAULT_CONTROLLER_LEASE_DURATION,
    ) -> MissionControllerLease:
        """Acquire controller lease for a mission.

        If no lease exists, creates generation 1.
        If latest lease is expired/released, creates generation N+1.
        If latest lease is active with same controller_id, returns existing lease (idempotent).
        If latest lease is active with different controller_id, fails with conflict.

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier
            controller_id: Controller instance identifier
            lease_duration: Lease duration (bounded, defaults to 30s)

        Returns:
            Acquired or existing controller lease

        Raises:
            MissionNotFoundError: If mission does not exist
            IllegalMissionTransitionError: If mission is terminal
            MissionControllerLeaseConflictError: If active lease held by different controller
            MissionControllerClockError: If clock rollback detected
        """
        control_domain = validate_domain_id(control_domain, "control_domain")
        controller_id = validate_controller_id(controller_id)
        lease_duration = validate_lease_duration(lease_duration)

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Obtain and validate authoritative time AFTER acquiring write lock
                # Clock validation must occur before any mutation
                now = self._now_utc()

                # Verify mission exists and not terminal
                cursor = conn.execute(
                    """
                    SELECT current_state FROM mission_state
                    WHERE control_domain = ? AND mission_id = ?
                    """,
                    (control_domain, mission_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise MissionNotFoundError(
                        f"Mission {mission_id} not found in domain {control_domain}"
                    )

                current_state = MissionLifecycle(row[0])
                if _is_terminal_state(current_state):
                    raise IllegalMissionTransitionError(
                        f"Cannot acquire lease for terminal mission in state {current_state.value}"
                    )

                # Load latest generation
                cursor = conn.execute(
                    """
                    SELECT generation, controller_id, acquired_at, renewed_at, expires_at, released_at
                    FROM mission_controller_leases
                    WHERE control_domain = ? AND mission_id = ?
                    ORDER BY generation DESC
                    LIMIT 1
                    """,
                    (control_domain, mission_id),
                )
                latest = cursor.fetchone()

                if latest is None:
                    # No prior lease - create generation 1
                    generation = 1
                    acquired_at = now
                    renewed_at = now
                    expires_at = now + lease_duration

                    # Clock rollback check
                    if expires_at <= renewed_at:
                        raise MissionControllerClockError("Clock rollback detected: expires_at <= renewed_at")

                    conn.execute(
                        """
                        INSERT INTO mission_controller_leases (
                            control_domain, mission_id, generation, controller_id,
                            acquired_at, renewed_at, expires_at, released_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            control_domain,
                            mission_id,
                            generation,
                            controller_id,
                            _serialize_timestamp(acquired_at),
                            _serialize_timestamp(renewed_at),
                            _serialize_timestamp(expires_at),
                            None,
                        ),
                    )

                    conn.commit()
                    return MissionControllerLease(
                        control_domain=control_domain,
                        mission_id=mission_id,
                        controller_id=controller_id,
                        generation=generation,
                        acquired_at=acquired_at,
                        renewed_at=renewed_at,
                        expires_at=expires_at,
                        released_at=None,
                    )

                # Parse latest lease
                latest_generation = latest[0]
                latest_controller_id = latest[1]
                latest_acquired_at = _deserialize_timestamp(latest[2])
                latest_renewed_at = _deserialize_timestamp(latest[3])
                latest_expires_at = _deserialize_timestamp(latest[4])
                latest_released_at = _deserialize_timestamp(latest[5])

                # Clock rollback check
                if now < latest_renewed_at or (latest_released_at and now < latest_released_at):
                    raise MissionControllerClockError(
                        f"Clock rollback detected: now {now} < latest lease timestamps"
                    )

                # Check if latest lease is active
                is_active = (latest_released_at is None) and (now < latest_expires_at)

                if is_active:
                    if latest_controller_id == controller_id:
                        # Same controller - return existing lease (idempotent)
                        conn.commit()
                        return MissionControllerLease(
                            control_domain=control_domain,
                            mission_id=mission_id,
                            controller_id=latest_controller_id,
                            generation=latest_generation,
                            acquired_at=latest_acquired_at,
                            renewed_at=latest_renewed_at,
                            expires_at=latest_expires_at,
                            released_at=latest_released_at,
                        )
                    else:
                        # Different controller - conflict
                        raise MissionControllerLeaseConflictError(
                            f"Lease held by controller {latest_controller_id} "
                            f"(generation {latest_generation}, expires {latest_expires_at})"
                        )

                # Latest lease is expired or released - create generation N+1
                new_generation = latest_generation + 1
                acquired_at = now
                renewed_at = now
                expires_at = now + lease_duration

                # Clock rollback check
                if expires_at <= renewed_at:
                    raise MissionControllerClockError("Clock rollback detected: expires_at <= renewed_at")

                conn.execute(
                    """
                    INSERT INTO mission_controller_leases (
                        control_domain, mission_id, generation, controller_id,
                        acquired_at, renewed_at, expires_at, released_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        control_domain,
                        mission_id,
                        new_generation,
                        controller_id,
                        _serialize_timestamp(acquired_at),
                        _serialize_timestamp(renewed_at),
                        _serialize_timestamp(expires_at),
                        None,
                    ),
                )

                conn.commit()
                return MissionControllerLease(
                    control_domain=control_domain,
                    mission_id=mission_id,
                    controller_id=controller_id,
                    generation=new_generation,
                    acquired_at=acquired_at,
                    renewed_at=renewed_at,
                    expires_at=expires_at,
                    released_at=None,
                )
            except Exception:
                conn.rollback()
                raise
        finally:
            if self._memory_connection is None:
                conn.close()

    def renew_controller_lease(
        self,
        control_domain: str,
        mission_id: str,
        controller_id: str,
        generation: int,
        *,
        lease_duration: timedelta = DEFAULT_CONTROLLER_LEASE_DURATION,
    ) -> MissionControllerLease:
        """Renew an active controller lease.

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier
            controller_id: Controller instance identifier
            generation: Expected current generation
            lease_duration: Lease duration (bounded, defaults to 30s)

        Returns:
            Renewed controller lease

        Raises:
            MissionControllerLeaseNotFoundError: If no lease exists
            MissionControllerLeaseStaleError: If generation is not current
            MissionControllerLeaseExpiredError: If lease has expired
            MissionControllerLeaseConflictError: If controller_id mismatch
            MissionControllerClockError: If clock rollback detected
        """
        control_domain = validate_domain_id(control_domain, "control_domain")
        controller_id = validate_controller_id(controller_id)
        generation = validate_generation(generation)
        lease_duration = validate_lease_duration(lease_duration)

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Obtain and validate authoritative time AFTER acquiring write lock
                # Clock validation must occur before any mutation
                now = self._now_utc()

                # Load latest generation
                cursor = conn.execute(
                    """
                    SELECT generation, controller_id, acquired_at, renewed_at, expires_at, released_at
                    FROM mission_controller_leases
                    WHERE control_domain = ? AND mission_id = ?
                    ORDER BY generation DESC
                    LIMIT 1
                    """,
                    (control_domain, mission_id),
                )
                latest = cursor.fetchone()

                if latest is None:
                    raise MissionControllerLeaseNotFoundError(
                        f"No controller lease exists for mission {mission_id} in domain {control_domain}"
                    )

                latest_generation = latest[0]
                latest_controller_id = latest[1]
                latest_acquired_at = _deserialize_timestamp(latest[2])
                latest_renewed_at = _deserialize_timestamp(latest[3])
                latest_expires_at = _deserialize_timestamp(latest[4])
                latest_released_at = _deserialize_timestamp(latest[5])

                # Clock rollback check
                if now < latest_renewed_at or (latest_released_at and now < latest_released_at):
                    raise MissionControllerClockError(
                        f"Clock rollback detected: now {now} < latest lease timestamps"
                    )

                # Validate generation
                if generation != latest_generation:
                    raise MissionControllerLeaseStaleError(
                        f"Stale generation: expected {generation}, current {latest_generation}"
                    )

                # Validate controller_id
                if controller_id != latest_controller_id:
                    raise MissionControllerLeaseConflictError(
                        f"Lease held by different controller: {latest_controller_id}"
                    )

                # Check if released
                if latest_released_at is not None:
                    raise MissionControllerLeaseExpiredError(
                        f"Lease was released at {latest_released_at}"
                    )

                # Check if expired
                if now >= latest_expires_at:
                    raise MissionControllerLeaseExpiredError(
                        f"Lease expired at {latest_expires_at} (now: {now})"
                    )

                # Renew lease
                renewed_at = now
                expires_at = now + lease_duration

                # Clock rollback check
                if expires_at <= renewed_at:
                    raise MissionControllerClockError("Clock rollback detected: expires_at <= renewed_at")

                conn.execute(
                    """
                    UPDATE mission_controller_leases
                    SET renewed_at = ?, expires_at = ?
                    WHERE control_domain = ? AND mission_id = ? AND generation = ?
                    """,
                    (
                        _serialize_timestamp(renewed_at),
                        _serialize_timestamp(expires_at),
                        control_domain,
                        mission_id,
                        generation,
                    ),
                )

                conn.commit()
                return MissionControllerLease(
                    control_domain=control_domain,
                    mission_id=mission_id,
                    controller_id=controller_id,
                    generation=generation,
                    acquired_at=latest_acquired_at,
                    renewed_at=renewed_at,
                    expires_at=expires_at,
                    released_at=None,
                )
            except Exception:
                conn.rollback()
                raise
        finally:
            if self._memory_connection is None:
                conn.close()

    def release_controller_lease(
        self,
        control_domain: str,
        mission_id: str,
        controller_id: str,
        generation: int,
    ) -> MissionControllerLease:
        """Release a controller lease.

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier
            controller_id: Controller instance identifier
            generation: Expected current generation

        Returns:
            Released controller lease

        Raises:
            MissionControllerLeaseNotFoundError: If no lease exists
            MissionControllerLeaseStaleError: If generation is not current
            MissionControllerLeaseConflictError: If controller_id mismatch
            MissionControllerClockError: If clock rollback detected
        """
        control_domain = validate_domain_id(control_domain, "control_domain")
        controller_id = validate_controller_id(controller_id)
        generation = validate_generation(generation)

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Obtain and validate authoritative time AFTER acquiring write lock
                # Clock validation must occur before any mutation
                now = self._now_utc()

                # Load latest generation
                cursor = conn.execute(
                    """
                    SELECT generation, controller_id, acquired_at, renewed_at, expires_at, released_at
                    FROM mission_controller_leases
                    WHERE control_domain = ? AND mission_id = ?
                    ORDER BY generation DESC
                    LIMIT 1
                    """,
                    (control_domain, mission_id),
                )
                latest = cursor.fetchone()

                if latest is None:
                    raise MissionControllerLeaseNotFoundError(
                        f"No controller lease exists for mission {mission_id} in domain {control_domain}"
                    )

                latest_generation = latest[0]
                latest_controller_id = latest[1]
                latest_acquired_at = _deserialize_timestamp(latest[2])
                latest_renewed_at = _deserialize_timestamp(latest[3])
                latest_expires_at = _deserialize_timestamp(latest[4])
                latest_released_at = _deserialize_timestamp(latest[5])

                # Clock rollback check
                if now < latest_renewed_at or (latest_released_at and now < latest_released_at):
                    raise MissionControllerClockError(
                        f"Clock rollback detected: now {now} < latest lease timestamps"
                    )

                # Validate generation
                if generation != latest_generation:
                    raise MissionControllerLeaseStaleError(
                        f"Stale generation: expected {generation}, current {latest_generation}"
                    )

                # Validate controller_id
                if controller_id != latest_controller_id:
                    raise MissionControllerLeaseConflictError(
                        f"Lease held by different controller: {latest_controller_id}"
                    )

                # Idempotent release check
                if latest_released_at is not None:
                    # Already released - idempotent return
                    conn.commit()
                    return MissionControllerLease(
                        control_domain=control_domain,
                        mission_id=mission_id,
                        controller_id=controller_id,
                        generation=generation,
                        acquired_at=latest_acquired_at,
                        renewed_at=latest_renewed_at,
                        expires_at=latest_expires_at,
                        released_at=latest_released_at,
                    )

                # Release lease
                released_at = now

                conn.execute(
                    """
                    UPDATE mission_controller_leases
                    SET released_at = ?
                    WHERE control_domain = ? AND mission_id = ? AND generation = ?
                    """,
                    (
                        _serialize_timestamp(released_at),
                        control_domain,
                        mission_id,
                        generation,
                    ),
                )

                conn.commit()
                return MissionControllerLease(
                    control_domain=control_domain,
                    mission_id=mission_id,
                    controller_id=controller_id,
                    generation=generation,
                    acquired_at=latest_acquired_at,
                    renewed_at=latest_renewed_at,
                    expires_at=latest_expires_at,
                    released_at=released_at,
                )
            except Exception:
                conn.rollback()
                raise
        finally:
            if self._memory_connection is None:
                conn.close()

    def get_current_controller_lease(
        self,
        control_domain: str,
        mission_id: str,
    ) -> MissionControllerLease | None:
        """Get current controller lease for a mission.

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier

        Returns:
            Current controller lease, or None if no lease exists
        """
        control_domain = validate_domain_id(control_domain, "control_domain")

        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                SELECT generation, controller_id, acquired_at, renewed_at, expires_at, released_at
                FROM mission_controller_leases
                WHERE control_domain = ? AND mission_id = ?
                ORDER BY generation DESC
                LIMIT 1
                """,
                (control_domain, mission_id),
            )
            row = cursor.fetchone()

            if row is None:
                return None

            return MissionControllerLease(
                control_domain=control_domain,
                mission_id=mission_id,
                controller_id=row[1],
                generation=row[0],
                acquired_at=_deserialize_timestamp(row[2]),
                renewed_at=_deserialize_timestamp(row[3]),
                expires_at=_deserialize_timestamp(row[4]),
                released_at=_deserialize_timestamp(row[5]),
            )
        finally:
            if self._memory_connection is None:
                conn.close()

    def list_controller_lease_history(
        self,
        control_domain: str,
        mission_id: str,
    ) -> tuple[MissionControllerLease, ...]:
        """List all controller lease generations for a mission.

        Args:
            control_domain: ControlDomain identifier
            mission_id: Mission identifier

        Returns:
            Tuple of controller leases ordered by generation (oldest first)
        """
        control_domain = validate_domain_id(control_domain, "control_domain")

        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                SELECT generation, controller_id, acquired_at, renewed_at, expires_at, released_at
                FROM mission_controller_leases
                WHERE control_domain = ? AND mission_id = ?
                ORDER BY generation ASC
                """,
                (control_domain, mission_id),
            )

            leases = []
            for row in cursor.fetchall():
                lease = MissionControllerLease(
                    control_domain=control_domain,
                    mission_id=mission_id,
                    controller_id=row[1],
                    generation=row[0],
                    acquired_at=_deserialize_timestamp(row[2]),
                    renewed_at=_deserialize_timestamp(row[3]),
                    expires_at=_deserialize_timestamp(row[4]),
                    released_at=_deserialize_timestamp(row[5]),
                )
                leases.append(lease)

            return tuple(leases)
        finally:
            if self._memory_connection is None:
                conn.close()


__all__ = [
    "MissionRuntimeStore",
    "MissionRuntimeStoreError",
    "MissionSchemaVersionError",
    "MissionNotFoundError",
    "MissionRevisionConflictError",
    "IllegalMissionTransitionError",
    "DomainMismatchError",
]
