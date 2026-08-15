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
from typing import Any

from federation.control_domain import validate_domain_id
from federation.mission_state import (
    EffectReference,
    MissionCheckpoint,
    MissionLifecycle,
    MissionSpecification,
    MissionTransition,
)


# Schema version for fail-closed compatibility checking
MISSION_SCHEMA_VERSION = 1


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


def _serialize_json(data: dict[str, Any] | None) -> str | None:
    """Serialize dictionary to JSON."""
    if data is None or data == {}:
        return None
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

    Terminal states are irreversible.
    Specific transitions are explicitly allowed or denied.
    """
    if from_state == to_state:
        # Idempotent transitions are allowed
        return

    # Terminal states cannot transition
    if _is_terminal_state(from_state):
        raise IllegalMissionTransitionError(
            f"Cannot transition from terminal state {from_state.value} to {to_state.value}"
        )

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

    def __init__(self, db_path: Path | str | None = None) -> None:
        """Initialize mission runtime store.

        Args:
            db_path: Path to SQLite database file. If None, uses in-memory database.
        """
        self._db_path = Path(db_path) if db_path is not None else None
        self._memory_connection: sqlite3.Connection | None = None

        if self._db_path is None:
            # In-memory database for testing
            self._memory_connection = sqlite3.connect(
                ":memory:",
                check_same_thread=False,
                timeout=_MAX_BUSY_TIMEOUT_MS / 1000.0,
            )
            self._memory_connection.execute("PRAGMA foreign_keys = ON")
            self._initialize_schema(self._memory_connection)
        else:
            # File-backed database
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
            try:
                self._initialize_schema(conn)
            finally:
                conn.close()

    def _connect(self) -> sqlite3.Connection:
        """Create a new database connection."""
        if self._memory_connection is not None:
            return self._memory_connection

        if self._db_path is None:
            raise MissionRuntimeStoreError("No database path configured")

        conn = sqlite3.connect(
            self._db_path,
            timeout=_MAX_BUSY_TIMEOUT_MS / 1000.0,
        )
        conn.execute("PRAGMA foreign_keys = ON")
        if _FILE_JOURNAL_MODE:
            conn.execute(f"PRAGMA journal_mode = {_FILE_JOURNAL_MODE}")
        return conn

    def _initialize_schema(self, conn: sqlite3.Connection) -> None:
        """Initialize or validate database schema."""
        with conn:
            # Check schema version
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='mission_runtime_schema'"
            )
            if cursor.fetchone() is None:
                # Fresh database - create schema
                self._create_schema(conn)
            else:
                # Existing database - validate version
                cursor = conn.execute("SELECT version FROM mission_runtime_schema ORDER BY version DESC LIMIT 1")
                row = cursor.fetchone()
                if row is None or row[0] != MISSION_SCHEMA_VERSION:
                    found_version = row[0] if row else None
                    raise MissionSchemaVersionError(
                        f"Incompatible schema version: found {found_version}, expected {MISSION_SCHEMA_VERSION}"
                    )

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        """Create mission runtime schema."""
        conn.executescript(f"""
            -- Schema version tracking
            CREATE TABLE mission_runtime_schema (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            INSERT INTO mission_runtime_schema (version) VALUES ({MISSION_SCHEMA_VERSION});

            -- Mission specifications (immutable identity)
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
            );

            -- Mission lifecycle state (mutable with optimistic concurrency)
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
            );

            -- Mission checkpoints (append-only progress evidence)
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
            );

            -- Mission transitions (append-only history)
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
            );

            -- Effect references (bindings without truth claims)
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
            );

            -- Indexes for common queries
            CREATE INDEX idx_missions_state
                ON mission_state(control_domain, current_state, updated_at);
            CREATE INDEX idx_checkpoints_mission
                ON mission_checkpoints(control_domain, mission_id, sequence DESC);
            CREATE INDEX idx_transitions_mission
                ON mission_transitions(control_domain, mission_id, transitioned_at DESC);
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

        if not isinstance(expected_revision, int) or expected_revision < 1:
            raise ValueError("expected_revision must be a positive integer")

        conn = self._connect()
        try:
            with conn:
                # Get current state with lock
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

                # Optimistic concurrency check
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

                params.extend([control_domain, mission_id])

                conn.execute(
                    f"""
                    UPDATE mission_state
                    SET {', '.join(updates)}
                    WHERE control_domain = ? AND mission_id = ?
                    """,
                    params,
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

                return new_revision
        finally:
            if self._memory_connection is None:
                conn.close()

    def create_checkpoint(self, checkpoint: MissionCheckpoint) -> None:
        """Create a new mission checkpoint.

        Args:
            checkpoint: Mission checkpoint

        Raises:
            MissionNotFoundError: If mission does not exist
            MissionRuntimeStoreError: If checkpoint sequence already exists
        """
        if not isinstance(checkpoint, MissionCheckpoint):
            raise TypeError("checkpoint must be a MissionCheckpoint")

        conn = self._connect()
        try:
            with conn:
                # Verify mission exists and domain matches
                cursor = conn.execute(
                    """
                    SELECT 1 FROM missions
                    WHERE control_domain = ? AND mission_id = ?
                    """,
                    (checkpoint.control_domain, checkpoint.mission_id),
                )
                if cursor.fetchone() is None:
                    raise MissionNotFoundError(
                        f"Mission {checkpoint.mission_id} not found in domain {checkpoint.control_domain}"
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

        Args:
            effect_ref: Effect reference
            mission_id: Mission identifier
            control_domain: ControlDomain identifier

        Raises:
            MissionNotFoundError: If mission does not exist
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

                # Insert effect reference (idempotent on effect_intent_id)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO mission_effect_references (
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


__all__ = [
    "MissionRuntimeStore",
    "MissionRuntimeStoreError",
    "MissionSchemaVersionError",
    "MissionNotFoundError",
    "MissionRevisionConflictError",
    "IllegalMissionTransitionError",
    "DomainMismatchError",
]
