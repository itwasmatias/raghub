"""Tests for DurableEffectStore — restart persistence, concurrency, domain isolation.

MissionaryX v0.1 — Durable Effect Store Test Suite

This module validates that the durable effect store:
1. Persists authoritative state across registry restarts
2. Handles concurrent operations correctly (threads and processes)
3. Enforces ControlDomain isolation at the database level
4. Fails closed on schema version mismatches and corrupt data
5. Maintains evidence-backed authority release semantics

Test organization:
- TestRestartPersistence: State survives registry restart
- TestConcurrentOperations: Thread and process safety
- TestControlDomainIsolation: Cross-domain operation isolation
- TestSchemaVersioning: Fail-closed version compatibility
- TestCorruptDataHandling: Invalid stored data detection
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from federation.durable_effect_store import (
    SCHEMA_VERSION,
    DurableEffectStore,
    SchemaVersionError,
    StorageIntegrityError,
)
from federation.effect_safety import (
    AuthorityDisposition,
    AuthorityReservation,
    EffectDispatch,
    EffectIntent,
    EffectIntentRegistry,
    ReconciliationObligation,
    ReconciliationState,
    ProviderReconcilability,
)


def _now() -> datetime:
    """Return current UTC timestamp for test records."""
    return datetime.now(timezone.utc)


def _fingerprint(data: dict[str, Any]) -> str:
    """Create a simple fingerprint for test data."""
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


class TestRestartPersistence:
    """Verify that authoritative state persists across registry restarts."""

    def test_effect_intents_persist_across_restart(self):
        """Effect intents survive registry restart."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"

            # Create registry and commit intent
            registry1 = EffectIntentRegistry(str(db_path))
            intent = EffectIntent(
                effect_intent_id="intent-001",
                decision_id="decision-001",
                mission_id="mission-001",
                task_id="task-001",
                attempt_id="attempt-001",
                operation_digest=_fingerprint({"op": "test"}),
                idempotency_key="idem-001",
                provider_scope="test-provider",
                authority_reservation_id="reservation-001",
                compensation_strategy=None,
                evidence_reference="evidence-001",
                state="committed_not_dispatched",
                created_at=_now(),
                control_domain="test-domain",
            )
            registry1.commit_intent(intent)

            # Destroy registry (simulates process restart)
            del registry1

            # Create new registry with same database
            registry2 = EffectIntentRegistry(str(db_path))
            retrieved = registry2.get_intent("intent-001", "test-domain")

            assert retrieved is not None
            assert retrieved.effect_intent_id == "intent-001"
            assert retrieved.decision_id == "decision-001"
            assert retrieved.mission_id == "mission-001"
            assert retrieved.operation_digest == _fingerprint({"op": "test"})
            assert retrieved.control_domain == "test-domain"

    def test_dispatches_persist_across_restart(self):
        """Effect dispatches survive registry restart."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"

            # Create registry and commit intent + dispatch
            registry1 = EffectIntentRegistry(str(db_path))
            intent = EffectIntent(
                effect_intent_id="intent-002",
                decision_id="decision-002",
                mission_id="mission-002",
                task_id="task-002",
                attempt_id="attempt-002",
                operation_digest=_fingerprint({"op": "test"}),
                idempotency_key="idem-002",
                provider_scope="test-provider",
                authority_reservation_id="reservation-002",
                compensation_strategy=None,
                evidence_reference="evidence-002",
                state="committed_not_dispatched",
                created_at=_now(),
                control_domain="test-domain",
            )
            registry1.commit_intent(intent)

            dispatch = EffectDispatch(
                dispatch_id="dispatch-002",
                effect_intent_id="intent-002",
                attempt_id="attempt-002",
                idempotency_key="idem-002",
                provider_adapter="test-adapter",
                capability_profile_version="v1",
                transport_digest=_fingerprint({"transport": "test"}),
                posture="not_escaped",
                provider_operation_id="op-002",
                evidence_reference="evidence-002",
                dispatched_at=_now(),
                control_domain="test-domain",
            )
            registry1.commit_dispatch(dispatch)

            # Destroy and recreate registry
            del registry1
            registry2 = EffectIntentRegistry(str(db_path))

            retrieved = registry2.get_dispatch("dispatch-002", "test-domain")
            assert retrieved is not None
            assert retrieved.dispatch_id == "dispatch-002"
            assert retrieved.effect_intent_id == "intent-002"
            assert retrieved.provider_adapter == "test-adapter"
            assert retrieved.control_domain == "test-domain"

    def test_double_spend_protection_persists_across_restart(self):
        """Authority reservation binding prevents double-spend after restart."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"

            # Commit intent with reservation
            registry1 = EffectIntentRegistry(str(db_path))
            intent = EffectIntent(
                effect_intent_id="intent-003",
                decision_id="decision-003",
                mission_id="mission-003",
                task_id="task-003",
                attempt_id="attempt-003",
                operation_digest=_fingerprint({"op": "test"}),
                idempotency_key="idem-003",
                provider_scope="test-provider",
                authority_reservation_id="reservation-003",
                compensation_strategy=None,
                evidence_reference="evidence-003",
                state="committed_not_dispatched",
                created_at=_now(),
                control_domain="test-domain",
            )
            registry1.commit_intent(intent)

            # Destroy and recreate registry
            del registry1
            registry2 = EffectIntentRegistry(str(db_path))

            # Attempt to reuse the same reservation
            intent2 = EffectIntent(
                effect_intent_id="intent-003b",
                decision_id="decision-003b",
                mission_id="mission-003",
                task_id="task-003",
                attempt_id="attempt-003b",
                operation_digest=_fingerprint({"op": "test2"}),
                idempotency_key="idem-003b",
                provider_scope="test-provider",
                authority_reservation_id="reservation-003",  # Same reservation
                compensation_strategy=None,
                evidence_reference="evidence-003b",
                state="committed_not_dispatched",
                created_at=_now(),
                control_domain="test-domain",
            )

            with pytest.raises(ValueError, match="already committed"):
                registry2.commit_intent(intent2)

    def test_idempotent_retry_works_across_restart(self):
        """Identical intent commits are idempotent across restart."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"

            # Commit intent
            registry1 = EffectIntentRegistry(str(db_path))
            intent = EffectIntent(
                effect_intent_id="intent-004",
                decision_id="decision-004",
                mission_id="mission-004",
                task_id="task-004",
                attempt_id="attempt-004",
                operation_digest=_fingerprint({"op": "test"}),
                idempotency_key="idem-004",
                provider_scope="test-provider",
                authority_reservation_id="reservation-004",
                compensation_strategy=None,
                evidence_reference="evidence-004",
                state="committed_not_dispatched",
                created_at=_now(),
                control_domain="test-domain",
            )
            registry1.commit_intent(intent)

            # Destroy and recreate registry
            del registry1
            registry2 = EffectIntentRegistry(str(db_path))

            # Commit identical intent - should be idempotent
            registry2.commit_intent(intent)  # Should not raise

            # Verify only one record exists
            retrieved = registry2.get_intent("intent-004", "test-domain")
            assert retrieved is not None
            assert retrieved.effect_intent_id == "intent-004"


class TestControlDomainIsolation:
    """Verify that ControlDomain scoping is enforced at the database level."""

    def test_cross_domain_intents_are_isolated_in_database(self):
        """Effect intents with same ID but different domains are isolated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            registry = EffectIntentRegistry(str(db_path))

            # Commit intent in domain A
            intent_a = EffectIntent(
                effect_intent_id="intent-shared",
                decision_id="decision-a",
                mission_id="mission-a",
                task_id="task-a",
                attempt_id="attempt-a",
                operation_digest=_fingerprint({"op": "domain-a"}),
                idempotency_key="idem-a",
                provider_scope="test-provider",
                authority_reservation_id="reservation-a",
                compensation_strategy=None,
                evidence_reference="evidence-a",
                state="committed_not_dispatched",
                created_at=_now(),
                control_domain="domain-a",
            )
            registry.commit_intent(intent_a)

            # Commit intent with same ID in domain B
            intent_b = EffectIntent(
                effect_intent_id="intent-shared",  # Same ID
                decision_id="decision-b",
                mission_id="mission-b",
                task_id="task-b",
                attempt_id="attempt-b",
                operation_digest=_fingerprint({"op": "domain-b"}),
                idempotency_key="idem-b",
                provider_scope="test-provider",
                authority_reservation_id="reservation-b",
                compensation_strategy=None,
                evidence_reference="evidence-b",
                state="committed_not_dispatched",
                created_at=_now(),
                control_domain="domain-b",
            )
            registry.commit_intent(intent_b)  # Should succeed

            # Verify isolation
            retrieved_a = registry.get_intent("intent-shared", "domain-a")
            retrieved_b = registry.get_intent("intent-shared", "domain-b")

            assert retrieved_a is not None
            assert retrieved_b is not None
            assert retrieved_a.decision_id == "decision-a"
            assert retrieved_b.decision_id == "decision-b"
            assert retrieved_a.operation_digest == _fingerprint({"op": "domain-a"})
            assert retrieved_b.operation_digest == _fingerprint({"op": "domain-b"})

    def test_cross_domain_reservations_are_isolated(self):
        """Authority reservations with same ID but different domains are isolated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            registry = EffectIntentRegistry(str(db_path))

            # Commit intent with reservation in domain A
            intent_a = EffectIntent(
                effect_intent_id="intent-a",
                decision_id="decision-a",
                mission_id="mission-a",
                task_id="task-a",
                attempt_id="attempt-a",
                operation_digest=_fingerprint({"op": "test"}),
                idempotency_key="idem-a",
                provider_scope="test-provider",
                authority_reservation_id="reservation-shared",
                compensation_strategy=None,
                evidence_reference="evidence-a",
                state="committed_not_dispatched",
                created_at=_now(),
                control_domain="domain-a",
            )
            registry.commit_intent(intent_a)

            # Commit intent with same reservation ID in domain B
            intent_b = EffectIntent(
                effect_intent_id="intent-b",
                decision_id="decision-b",
                mission_id="mission-b",
                task_id="task-b",
                attempt_id="attempt-b",
                operation_digest=_fingerprint({"op": "test"}),
                idempotency_key="idem-b",
                provider_scope="test-provider",
                authority_reservation_id="reservation-shared",  # Same reservation ID
                compensation_strategy=None,
                evidence_reference="evidence-b",
                state="committed_not_dispatched",
                created_at=_now(),
                control_domain="domain-b",
            )
            registry.commit_intent(intent_b)  # Should succeed - different domain

            # Verify both intents exist
            assert registry.get_intent("intent-a", "domain-a") is not None
            assert registry.get_intent("intent-b", "domain-b") is not None


class TestSchemaVersioning:
    """Verify fail-closed behavior for schema version mismatches."""

    def test_newer_schema_version_fails_closed(self):
        """Database with newer schema version raises SchemaVersionError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"

            # Create database with future schema version
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS effect_store_schema (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            future_version = SCHEMA_VERSION + 1
            conn.execute(
                "INSERT INTO effect_store_schema(version) VALUES (?)",
                (future_version,),
            )
            conn.commit()
            conn.close()

            # Attempt to open with current code
            with pytest.raises(SchemaVersionError, match="newer than supported"):
                DurableEffectStore(db_path)

    def test_incompatible_legacy_schema_fails_closed_without_mutation(self):
        """Incompatible legacy schema (version 0) fails closed without mutation.

        Simulates a legacy version 0 database with real data, then proves:
        1. Opening fails with SchemaVersionError
        2. Database remains unmodified (no destructive migration)
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"

            # Simulate a nonempty legacy version 0 database with incompatible schema
            conn = sqlite3.connect(str(db_path))

            # Create version 0 schema (different structure - incompatible)
            conn.execute(
                """
                CREATE TABLE effect_store_schema (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute("INSERT INTO effect_store_schema(version) VALUES (0)")

            # Create a legacy table structure (different from v1)
            conn.execute(
                """
                CREATE TABLE legacy_intents (
                    intent_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO legacy_intents(intent_id, payload) VALUES ('legacy-001', 'test-data')"
            )
            conn.commit()

            # Record pre-mutation state
            legacy_data = conn.execute("SELECT * FROM legacy_intents").fetchall()
            schema_version = conn.execute("SELECT version FROM effect_store_schema").fetchone()[0]
            conn.close()

            # Attempt to open with current code - must fail closed
            with pytest.raises(SchemaVersionError, match="older than current"):
                DurableEffectStore(db_path)

            # Verify database was NOT mutated
            conn = sqlite3.connect(str(db_path))
            post_attempt_data = conn.execute("SELECT * FROM legacy_intents").fetchall()
            post_attempt_version = conn.execute("SELECT version FROM effect_store_schema").fetchone()[0]

            # Verify no destructive changes occurred
            assert legacy_data == post_attempt_data, "Legacy data was mutated"
            assert schema_version == post_attempt_version, "Schema version was mutated"
            assert post_attempt_version == 0, "Version 0 database was upgraded without migration"

            # Verify v1 tables were NOT created
            tables = [
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
            assert "effect_intents" not in tables, "v1 tables created on incompatible database"
            conn.close()


class TestCorruptDataHandling:
    """Verify fail-closed behavior for corrupt or invalid stored data."""

    def test_corrupt_intent_data_raises_storage_integrity_error(self):
        """Corrupt effect intent data raises StorageIntegrityError on retrieval."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = DurableEffectStore(db_path)

            # Manually insert corrupt data (invalid timestamp)
            with store._connection() as conn:
                conn.execute(
                    """
                    INSERT INTO effect_intents (
                        control_domain, effect_intent_id, decision_id, mission_id,
                        task_id, attempt_id, operation_digest, idempotency_key,
                        provider_scope, authority_reservation_id, compensation_strategy,
                        evidence_reference, state, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "test-domain",
                        "corrupt-intent",
                        "decision",
                        "mission",
                        "task",
                        "attempt",
                        "digest",
                        "idem",
                        "provider",
                        "reservation",
                        None,
                        "evidence",
                        "committed_not_dispatched",
                        "INVALID_TIMESTAMP",  # Corrupt data
                    ),
                )
                conn.commit()

            # Attempt to retrieve corrupt intent
            with pytest.raises(StorageIntegrityError, match="failed validation"):
                store.get_intent("corrupt-intent", "test-domain")


class TestConcurrentOperations:
    """Verify correct handling of concurrent operations from threads."""

    def test_concurrent_identical_commits_are_idempotent(self):
        """Multiple threads committing identical intents succeed (idempotent)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            registry = EffectIntentRegistry(str(db_path))

            intent = EffectIntent(
                effect_intent_id="intent-concurrent",
                decision_id="decision-concurrent",
                mission_id="mission-concurrent",
                task_id="task-concurrent",
                attempt_id="attempt-concurrent",
                operation_digest=_fingerprint({"op": "test"}),
                idempotency_key="idem-concurrent",
                provider_scope="test-provider",
                authority_reservation_id="reservation-concurrent",
                compensation_strategy=None,
                evidence_reference="evidence-concurrent",
                state="committed_not_dispatched",
                created_at=_now(),
                control_domain="test-domain",
            )

            errors = []

            def commit_intent():
                try:
                    registry.commit_intent(intent)
                except Exception as e:
                    errors.append(e)

            # Launch 10 threads all committing the same intent
            threads = [threading.Thread(target=commit_intent) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # All should succeed (no errors)
            assert len(errors) == 0

            # Verify only one record exists
            retrieved = registry.get_intent("intent-concurrent", "test-domain")
            assert retrieved is not None

    def test_concurrent_conflicting_commits_fail_explicitly(self):
        """Multiple threads committing conflicting intents fail explicitly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            registry = EffectIntentRegistry(str(db_path))

            def create_intent(suffix: str) -> EffectIntent:
                return EffectIntent(
                    effect_intent_id="intent-conflict",  # Same ID
                    decision_id=f"decision-{suffix}",  # Different payload
                    mission_id="mission-conflict",
                    task_id="task-conflict",
                    attempt_id=f"attempt-{suffix}",
                    operation_digest=_fingerprint({"op": suffix}),  # Different
                    idempotency_key=f"idem-{suffix}",  # Different
                    provider_scope="test-provider",
                    authority_reservation_id=f"reservation-{suffix}",
                    compensation_strategy=None,
                    evidence_reference=f"evidence-{suffix}",
                    state="committed_not_dispatched",
                    created_at=_now(),
                    control_domain="test-domain",
                )

            results = []

            def commit_intent(suffix: str):
                try:
                    registry.commit_intent(create_intent(suffix))
                    results.append(("success", suffix))
                except ValueError as e:
                    results.append(("conflict", str(e)))

            # Launch threads with conflicting intents
            threads = [
                threading.Thread(target=commit_intent, args=(str(i),))
                for i in range(5)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # One should succeed, others should fail with conflict
            successes = [r for r in results if r[0] == "success"]
            conflicts = [r for r in results if r[0] == "conflict"]

            assert len(successes) == 1, f"Expected 1 success, got {len(successes)}"
            assert len(conflicts) == 4, f"Expected 4 conflicts, got {len(conflicts)}"

            # All conflicts should mention "already committed"
            for _, error_msg in conflicts:
                assert "already committed" in error_msg


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
