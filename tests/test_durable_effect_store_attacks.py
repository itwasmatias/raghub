"""Adversarial tests for durable effect store regression and boundary cases."""

from __future__ import annotations

import json
import math
import multiprocessing as mp
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from federation.durable_effect_store import (
    DurableEffectStore,
    SchemaVersionError,
    DurableEffectStoreError,
    ConcurrencyConflictError,
    StorageIntegrityError,
)
from federation.effect_safety import (
    AuthorityDisposition,
    AuthorityReservation,
    EffectIntent,
    EffectIntentRegistry,
    ReconciliationObligation,
    ReconciliationState,
    ProviderReconcilability,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


def _build_intent(
    *,
    effect_intent_id: str,
    authority_reservation_id: str,
    control_domain: str,
    decision_id: str = "decision",
    attempt_id: str = "attempt",
    idempotency_key: str = "idem",
    operation_digest: str = "{\"op\":\"test\"}",
) -> EffectIntent:
    return EffectIntent(
        effect_intent_id=effect_intent_id,
        decision_id=decision_id,
        mission_id="mission",
        task_id="task",
        attempt_id=attempt_id,
        operation_digest=operation_digest,
        idempotency_key=idempotency_key,
        provider_scope="provider",
        authority_reservation_id=authority_reservation_id,
        compensation_strategy=None,
        evidence_reference="evidence",
        state="committed_not_dispatched",
        created_at=_now(),
        control_domain=control_domain,
    )


def _process_commit_intent(db_path: str, intent: EffectIntent, result_queue: mp.Queue) -> None:
    registry = EffectIntentRegistry(db_path)
    registry.commit_intent(intent)
    result_queue.put(("ok", intent.effect_intent_id))


def _process_read_intent(db_path: str, effect_intent_id: str, control_domain: str, result_queue: mp.Queue) -> None:
    registry = EffectIntentRegistry(db_path)
    intent = registry.get_intent(effect_intent_id, control_domain)
    result_queue.put(None if intent is None else intent.to_dict())


def _process_commit_identical_intent(
    db_path: str,
    intent: EffectIntent,
    start_event: mp.Event,
    result_queue: mp.Queue,
) -> None:
    registry = EffectIntentRegistry(db_path)
    start_event.wait(timeout=10)
    registry.commit_intent(intent)
    result_queue.put(("ok", intent.effect_intent_id))


def _process_commit_conflicting_intent(
    db_path: str,
    intent: EffectIntent,
    start_event: mp.Event,
    result_queue: mp.Queue,
) -> None:
    registry = EffectIntentRegistry(db_path)
    start_event.wait(timeout=10)
    try:
        registry.commit_intent(intent)
    except Exception as exc:  # pragma: no cover - transported to queue
        result_queue.put(("error", type(exc).__name__, str(exc), intent.decision_id))
    else:
        result_queue.put(("ok", intent.decision_id))


def _build_nothing_landed_evidence_bundle(
    *,
    control_domain: str,
    effect_intent_id: str,
    reservation_id: str,
    reconciliation_id: str,
    dispatch_id: str,
    idempotency_key: str,
    provider_scope: str,
    mission_id: str,
) -> tuple[Any, Any, Any]:
    from research_mission import (
        EvidencePointer,
        EvidenceSpine,
        ProviderBoundaryReconciliationEvidence,
        provider_boundary_reconciliation_record,
    )

    evidence = ProviderBoundaryReconciliationEvidence(
        reconciliation_id=reconciliation_id,
        effect_intent_id=effect_intent_id,
        dispatch_id=dispatch_id,
        idempotency_key=idempotency_key,
        provider_operation_id=None,
        reconciliation_outcome="no_operation_committed",
        reconciled_at=_now(),
        provider_scope=provider_scope,
        reconciliation_method="idempotency_key_lookup",
    )
    record = provider_boundary_reconciliation_record(
        evidence,
        mission_id=mission_id,
        domain_id=control_domain,
    )
    pointer = EvidencePointer.from_record(record)
    spine = EvidenceSpine.from_records([record])
    return record, pointer, spine


def _build_terminal_decision_bundle(
    *,
    control_domain: str,
    effect_intent_id: str,
    reservation_id: str,
    decision_id: str,
    mission_id: str,
) -> tuple[Any, Any, Any]:
    from research_mission import (
        EvidencePointer,
        EvidenceSpine,
        TerminalEffectDecisionEvidence,
        terminal_effect_decision_record,
    )

    evidence = TerminalEffectDecisionEvidence(
        decision_id=decision_id,
        decision_type="assume_consumed_unreconciled",
        decision_kind="operator_decision",
        effect_intent_id=effect_intent_id,
        reservation_id=reservation_id,
        obligation_id=None,
        dispatch_id=None,
        disposition="assumed_consumed_unreconciled",
        decided_at=_now(),
        mission_id=mission_id,
        task_id="task-terminal",
        domain_id=control_domain,
        decision_rationale="test terminal decision",
    )
    record = terminal_effect_decision_record(evidence)
    pointer = EvidencePointer.from_record(record)
    spine = EvidenceSpine.from_records([record])
    return record, pointer, spine


def _process_release_reservation_transition(
    db_path: str,
    reservation_id: str,
    control_domain: str,
    evidence_spine: Any,
    evidence_pointer: Any,
    start_event: mp.Event,
    ready_queue: mp.Queue,
    result_queue: mp.Queue,
) -> None:
    registry = EffectIntentRegistry(db_path)
    ready_queue.put("release-ready")
    start_event.wait(timeout=10)
    try:
        registry.release_reservation(
            reservation_id,
            evidence_spine,
            evidence_pointer,
            control_domain,
        )
    except Exception as exc:  # pragma: no cover - transported to queue
        result_queue.put(("error", type(exc).__name__, str(exc), "release"))
    else:
        result_queue.put(("ok", "release"))


def _process_store_reservation_transition(
    db_path: str,
    reservation: AuthorityReservation,
    start_event: mp.Event,
    ready_queue: mp.Queue,
    result_queue: mp.Queue,
) -> None:
    store = DurableEffectStore(db_path)
    ready_queue.put(reservation.disposition.value)
    start_event.wait(timeout=10)
    try:
        store.store_reservation(reservation)
    except Exception as exc:  # pragma: no cover - transported to queue
        result_queue.put(("error", type(exc).__name__, str(exc), reservation.disposition.value))
    else:
        result_queue.put(("ok", reservation.disposition.value))


class TestAdditionalDurabilityAttacks:
    def test_boolean_authority_amount_is_rejected(self):
        with pytest.raises(ValueError, match="non-negative number"):
            AuthorityReservation(
                reservation_id="reservation-bool",
                effect_intent_id="intent-bool",
                capability_type="compute",
                amount=True,
                disposition=AuthorityDisposition.RESERVED,
                reserved_at=_now(),
                disposition_at=None,
                disposition_evidence=None,
                control_domain="test-domain",
            )

    def test_finite_integer_and_float_amounts_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "store.sqlite3"
            registry = EffectIntentRegistry(str(db_path))

            integer_intent = _build_intent(
                effect_intent_id="intent-integer",
                authority_reservation_id="reservation-integer",
                control_domain="test-domain",
            )
            registry.commit_intent(integer_intent)
            store = DurableEffectStore(db_path)
            store.store_reservation(
                AuthorityReservation(
                    reservation_id="reservation-integer",
                    effect_intent_id="intent-integer",
                    capability_type="compute",
                    amount=3,
                    disposition=AuthorityDisposition.RESERVED,
                    reserved_at=_now(),
                    disposition_at=None,
                    disposition_evidence=None,
                    control_domain="test-domain",
                )
            )
            assert store.get_reservation("reservation-integer", "test-domain").amount == 3

            float_intent = _build_intent(
                effect_intent_id="intent-float",
                authority_reservation_id="reservation-float",
                control_domain="test-domain",
            )
            registry.commit_intent(float_intent)
            store.store_reservation(
                AuthorityReservation(
                    reservation_id="reservation-float",
                    effect_intent_id="intent-float",
                    capability_type="compute",
                    amount=2.5,
                    disposition=AuthorityDisposition.RESERVED,
                    reserved_at=_now(),
                    disposition_at=None,
                    disposition_evidence=None,
                    control_domain="test-domain",
                )
            )
            assert store.get_reservation("reservation-float", "test-domain").amount == 2.5

    def test_busy_timeout_validation_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "store.sqlite3"

            with pytest.raises(TypeError, match="busy_timeout_ms must be an int"):
                DurableEffectStore(db_path, busy_timeout_ms=True)

            with pytest.raises(ValueError, match="busy_timeout_ms must be positive"):
                DurableEffectStore(db_path, busy_timeout_ms=0)

            with pytest.raises(ValueError, match="busy_timeout_ms must be positive"):
                DurableEffectStore(db_path, busy_timeout_ms=-1)

            with pytest.raises(ValueError, match="busy_timeout_ms exceeds"):
                DurableEffectStore(db_path, busy_timeout_ms=30001)

    def test_partial_current_version_schema_fails_closed_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "store.sqlite3"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE effect_store_schema (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute("INSERT INTO effect_store_schema(version) VALUES (1)")
            conn.commit()
            conn.close()

            before = db_path.read_bytes()
            with pytest.raises(SchemaVersionError, match="incomplete|partial|missing"):
                DurableEffectStore(db_path)
            after = db_path.read_bytes()
            assert after == before
            conn = sqlite3.connect(db_path)
            try:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                assert tables == {"effect_store_schema"}
                version_rows = conn.execute(
                    "SELECT version FROM effect_store_schema ORDER BY version"
                ).fetchall()
                assert version_rows == [(1,)]
            finally:
                conn.close()

    def test_wal_initialization_requires_verified_mode_and_bounded_timeout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "store.sqlite3"
            lock_conn = sqlite3.connect(db_path, timeout=0.1)
            try:
                lock_conn.execute("BEGIN EXCLUSIVE")
                start = time.monotonic()
                with pytest.raises(DurableEffectStoreError, match="journal mode|WAL|locked"):
                    DurableEffectStore(db_path, busy_timeout_ms=50)
                elapsed = time.monotonic() - start
                assert elapsed < 2.0
            finally:
                lock_conn.rollback()
                lock_conn.close()

    def test_commit_intent_rolls_back_when_second_write_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "store.sqlite3"
            store = DurableEffectStore(db_path)
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TRIGGER fail_reservation_bindings_insert
                    BEFORE INSERT ON reservation_bindings
                    BEGIN
                        SELECT RAISE(ABORT, 'injected reservation binding failure');
                    END;
                    """
                )
                conn.commit()
                intent = _build_intent(
                    effect_intent_id="intent-rollback-commit",
                    authority_reservation_id="reservation-rollback-commit",
                    control_domain="test-domain",
                )
                with pytest.raises(DurableEffectStoreError, match="reservation binding failure|Integrity"):
                    store.commit_intent(intent)
            finally:
                conn.close()

            reopened = DurableEffectStore(db_path)
            assert reopened.get_intent("intent-rollback-commit", "test-domain") is None
            conn = sqlite3.connect(db_path)
            try:
                assert conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM reservation_bindings
                    WHERE control_domain = ? AND authority_reservation_id = ?
                    """,
                    ("test-domain", "reservation-rollback-commit"),
                ).fetchone()[0] == 0
            finally:
                conn.close()

    def test_release_reservation_rolls_back_when_release_insert_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "store.sqlite3"
            registry = EffectIntentRegistry(str(db_path))
            intent = _build_intent(
                effect_intent_id="intent-rollback-release",
                authority_reservation_id="reservation-rollback-release",
                control_domain="test-domain",
            )
            registry.commit_intent(intent)
            from research_mission import (
                ProviderBoundaryReconciliationEvidence,
                provider_boundary_reconciliation_record,
                EvidencePointer,
                EvidenceSpine,
            )

            boundary_evidence = ProviderBoundaryReconciliationEvidence(
                reconciliation_id="recon-rollback-release",
                effect_intent_id="intent-rollback-release",
                dispatch_id="dispatch-rollback-release",
                idempotency_key="idem",
                provider_operation_id=None,
                reconciliation_outcome="no_operation_committed",
                reconciled_at=_now(),
                provider_scope="test",
                reconciliation_method="idempotency_key_lookup",
            )
            record = provider_boundary_reconciliation_record(
                boundary_evidence,
                mission_id="mission-rollback-release",
                domain_id="test-domain",
            )
            pointer = EvidencePointer.from_record(record)
            spine = EvidenceSpine.from_records([record])

            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TRIGGER fail_reservation_releases_insert
                    BEFORE INSERT ON reservation_releases
                    BEGIN
                        SELECT RAISE(ABORT, 'injected release failure');
                    END;
                    """
                )
                conn.commit()

                with pytest.raises(DurableEffectStoreError, match="release failure|Integrity"):
                    registry.release_reservation(
                        "reservation-rollback-release",
                        spine,
                        pointer,
                        "test-domain",
                    )
            finally:
                conn.close()

            reopened = DurableEffectStore(db_path)
            assert reopened.get_intent("intent-rollback-release", "test-domain") is not None
            with pytest.raises(ValueError, match="already committed"):
                registry.commit_intent(
                    _build_intent(
                        effect_intent_id="intent-rollback-release-reuse",
                        authority_reservation_id="reservation-rollback-release",
                        control_domain="test-domain",
                    )
                )
            conn = sqlite3.connect(db_path)
            try:
                assert conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM reservation_bindings
                    WHERE control_domain = ? AND authority_reservation_id = ?
                    """,
                    ("test-domain", "reservation-rollback-release"),
                ).fetchone()[0] == 1
                assert conn.execute(
                    "SELECT COUNT(*) FROM reservation_releases WHERE control_domain = ? AND reservation_id = ?",
                    ("test-domain", "reservation-rollback-release"),
                ).fetchone()[0] == 0
            finally:
                conn.close()

    def test_reader_sees_only_committed_state_during_uncommitted_writer_transaction(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "store.sqlite3"
            DurableEffectStore(db_path)
            writer = sqlite3.connect(db_path, timeout=0.1)
            try:
                writer.execute("PRAGMA foreign_keys=ON")
                writer.execute("BEGIN IMMEDIATE")
                writer.execute(
                    """
                    INSERT INTO effect_intents (
                        control_domain, effect_intent_id, decision_id, mission_id, task_id,
                        attempt_id, operation_digest, idempotency_key, provider_scope,
                        authority_reservation_id, compensation_strategy, evidence_reference,
                        state, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "test-domain",
                        "intent-reader-isolation",
                        "decision-reader-isolation",
                        "mission-reader-isolation",
                        "task-reader-isolation",
                        "attempt-reader-isolation",
                        "{\"op\":\"reader\"}",
                        "idem-reader-isolation",
                        "provider",
                        "reservation-reader-isolation",
                        None,
                        "evidence-reader-isolation",
                        "committed_not_dispatched",
                        _now().isoformat(),
                    ),
                )

                store = DurableEffectStore(db_path)
                assert store.get_intent("intent-reader-isolation", "test-domain") is None
            finally:
                writer.rollback()
                writer.close()

            store = DurableEffectStore(db_path)
            assert store.get_intent("intent-reader-isolation", "test-domain") is None

    def test_lock_contention_maps_to_concurrency_conflict_quickly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "store.sqlite3"
            DurableEffectStore(db_path)
            blocker = sqlite3.connect(db_path, timeout=0.1)
            try:
                blocker.execute("BEGIN EXCLUSIVE")
                store = DurableEffectStore(db_path, busy_timeout_ms=50)
                intent = _build_intent(
                    effect_intent_id="intent-lock-contention",
                    authority_reservation_id="reservation-lock-contention",
                    control_domain="test-domain",
                )
                start = time.monotonic()
                with pytest.raises(ConcurrencyConflictError, match="Database lock timeout"):
                    store.commit_intent(intent)
                assert time.monotonic() - start < 2.0
            finally:
                blocker.rollback()
                blocker.close()

    def test_release_vs_consume_concurrent_race_yields_exactly_one_terminal_result(self):
        ctx = mp.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "store.sqlite3")
            registry = EffectIntentRegistry(db_path)
            intent = _build_intent(
                effect_intent_id="intent-release-consume-race",
                authority_reservation_id="reservation-release-consume-race",
                control_domain="test-domain",
            )
            registry.commit_intent(intent)

            store = DurableEffectStore(db_path)
            store.store_reservation(
                AuthorityReservation(
                    reservation_id="reservation-release-consume-race",
                    effect_intent_id="intent-release-consume-race",
                    capability_type="compute",
                    amount=1.0,
                    disposition=AuthorityDisposition.RESERVED,
                    reserved_at=_now(),
                    disposition_at=None,
                    disposition_evidence=None,
                    control_domain="test-domain",
                )
            )

            record, pointer, spine = _build_nothing_landed_evidence_bundle(
                control_domain="test-domain",
                effect_intent_id="intent-release-consume-race",
                reservation_id="reservation-release-consume-race",
                reconciliation_id="recon-release-consume-race",
                dispatch_id="dispatch-release-consume-race",
                idempotency_key="idem-release-consume-race",
                provider_scope="test-provider",
                mission_id="mission-release-consume-race",
            )

            consumed_reservation = AuthorityReservation(
                reservation_id="reservation-release-consume-race",
                effect_intent_id="intent-release-consume-race",
                capability_type="compute",
                amount=1.0,
                disposition=AuthorityDisposition.CONSUMED,
                reserved_at=_now(),
                disposition_at=_now(),
                disposition_evidence=None,
                control_domain="test-domain",
            )

            start_event = ctx.Event()
            ready_queue = ctx.Queue()
            result_queue = ctx.Queue()
            workers = [
                ctx.Process(
                    target=_process_release_reservation_transition,
                    args=(
                        db_path,
                        "reservation-release-consume-race",
                        "test-domain",
                        spine,
                        pointer,
                        start_event,
                        ready_queue,
                        result_queue,
                    ),
                ),
                ctx.Process(
                    target=_process_store_reservation_transition,
                    args=(db_path, consumed_reservation, start_event, ready_queue, result_queue),
                ),
            ]
            for worker in workers:
                worker.start()

            assert {ready_queue.get(timeout=10), ready_queue.get(timeout=10)} == {
                "release-ready",
                AuthorityDisposition.CONSUMED.value,
            }

            start_event.set()
            for worker in workers:
                worker.join(timeout=20)
                assert worker.exitcode == 0

            results = [result_queue.get(timeout=10) for _ in workers]
            ok_results = [result for result in results if result[0] == "ok"]
            error_results = [result for result in results if result[0] == "error"]
            assert len(ok_results) == 1
            assert len(error_results) == 1
            assert error_results[0][1] in {"ValueError", "ConcurrencyConflictError", "StorageIntegrityError"}
            assert "sqlite" not in error_results[0][1].lower()

            winner = ok_results[0][1]
            store = DurableEffectStore(db_path)
            reopened = store.get_reservation("reservation-release-consume-race", "test-domain")
            assert reopened is not None
            if winner == "release":
                assert reopened.disposition == AuthorityDisposition.RELEASED
                assert reopened.disposition_evidence == pointer
                release_retry = EffectIntentRegistry(db_path)
                release_retry.release_reservation(
                    "reservation-release-consume-race",
                    spine,
                    pointer,
                    "test-domain",
                )
                with pytest.raises(ValueError, match="cannot regress|already committed|already released"):
                    store.store_reservation(consumed_reservation)
            elif winner == "consumed":
                assert reopened.disposition == AuthorityDisposition.CONSUMED
                assert reopened.disposition_evidence is None
                store.store_reservation(consumed_reservation)
                with pytest.raises(ValueError, match="cannot regress|already committed"):
                    registry.release_reservation(
                        "reservation-release-consume-race",
                        spine,
                        pointer,
                        "test-domain",
                    )
            else:
                pytest.fail(f"unexpected winner {winner!r}")

            conn = sqlite3.connect(db_path)
            try:
                release_rows = conn.execute(
                    "SELECT COUNT(*) FROM reservation_releases WHERE control_domain = ? AND reservation_id = ?",
                    ("test-domain", "reservation-release-consume-race"),
                ).fetchone()[0]
                stored_row = conn.execute(
                    "SELECT disposition FROM authority_reservations WHERE control_domain = ? AND reservation_id = ?",
                    ("test-domain", "reservation-release-consume-race"),
                ).fetchone()
                assert stored_row is not None
                if winner == "release":
                    assert release_rows == 1
                    assert stored_row[0] == AuthorityDisposition.RELEASED.value
                else:
                    assert release_rows == 0
                    assert stored_row[0] == AuthorityDisposition.CONSUMED.value
            finally:
                conn.close()

            reopened_store = DurableEffectStore(db_path)
            reopened_row = reopened_store.get_reservation("reservation-release-consume-race", "test-domain")
            assert reopened_row is not None
            assert reopened_row.disposition == reopened.disposition

    def test_contradictory_terminal_dispositions_do_not_both_land(self):
        ctx = mp.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "store.sqlite3")
            registry = EffectIntentRegistry(db_path)
            intent = _build_intent(
                effect_intent_id="intent-terminal-race",
                authority_reservation_id="reservation-terminal-race",
                control_domain="test-domain",
            )
            registry.commit_intent(intent)

            store = DurableEffectStore(db_path)
            store.store_reservation(
                AuthorityReservation(
                    reservation_id="reservation-terminal-race",
                    effect_intent_id="intent-terminal-race",
                    capability_type="compute",
                    amount=1.0,
                    disposition=AuthorityDisposition.RESERVED,
                    reserved_at=_now(),
                    disposition_at=None,
                    disposition_evidence=None,
                    control_domain="test-domain",
                )
            )

            _, pointer, spine = _build_terminal_decision_bundle(
                control_domain="test-domain",
                effect_intent_id="intent-terminal-race",
                reservation_id="reservation-terminal-race",
                decision_id="decision-terminal-race",
                mission_id="mission-terminal-race",
            )

            terminal_decision_reservation = AuthorityReservation.from_verified_terminal_decision(
                reservation_id="reservation-terminal-race",
                effect_intent_id="intent-terminal-race",
                capability_type="compute",
                amount=1.0,
                reserved_at=_now(),
                disposition_at=_now(),
                evidence_spine=spine,
                evidence_pointer=pointer,
                control_domain="test-domain",
            )

            start_event = ctx.Event()
            ready_queue = ctx.Queue()
            result_queue = ctx.Queue()
            workers = [
                ctx.Process(
                    target=_process_release_reservation_transition,
                    args=(
                        db_path,
                        "reservation-terminal-race",
                        "test-domain",
                        spine,
                        pointer,
                        start_event,
                        ready_queue,
                        result_queue,
                    ),
                ),
                ctx.Process(
                    target=_process_store_reservation_transition,
                    args=(db_path, terminal_decision_reservation, start_event, ready_queue, result_queue),
                ),
            ]
            for worker in workers:
                worker.start()

            assert {ready_queue.get(timeout=10), ready_queue.get(timeout=10)} == {
                "release-ready",
                AuthorityDisposition.ASSUMED_CONSUMED_UNRECONCILED.value,
            }

            start_event.set()
            for worker in workers:
                worker.join(timeout=20)
                assert worker.exitcode == 0

            results = [result_queue.get(timeout=10) for _ in workers]
            ok_results = [result for result in results if result[0] == "ok"]
            error_results = [result for result in results if result[0] == "error"]
            assert len(ok_results) == 1
            assert len(error_results) == 1
            assert error_results[0][1] in {"ValueError", "ConcurrencyConflictError", "StorageIntegrityError"}
            assert "sqlite" not in error_results[0][1].lower()

            store = DurableEffectStore(db_path)
            reopened = store.get_reservation("reservation-terminal-race", "test-domain", evidence_spine=spine)
            assert reopened is not None
            assert reopened.disposition in {
                AuthorityDisposition.RELEASED,
                AuthorityDisposition.ASSUMED_CONSUMED_UNRECONCILED,
            }
            assert reopened.disposition_evidence == pointer

            if reopened.disposition == AuthorityDisposition.RELEASED:
                registry.release_reservation("reservation-terminal-race", spine, pointer, "test-domain")
                with pytest.raises(ValueError, match="cannot regress|already committed"):
                    store.store_reservation(terminal_decision_reservation, evidence_spine=spine)
            else:
                store.store_reservation(terminal_decision_reservation, evidence_spine=spine)
                with pytest.raises(ValueError, match="terminal_effect_decision|cannot regress|already released"):
                    registry.release_reservation("reservation-terminal-race", spine, pointer, "test-domain")

            reopened_again = DurableEffectStore(db_path)
            assert reopened_again.get_reservation("reservation-terminal-race", "test-domain", evidence_spine=spine) == reopened

    def test_nonfinite_authority_amount_is_rejected(self):
        with pytest.raises(ValueError, match="finite number"):
            AuthorityReservation(
                reservation_id="reservation-nan",
                effect_intent_id="intent-nan",
                capability_type="compute",
                amount=float("nan"),
                disposition=AuthorityDisposition.RESERVED,
                reserved_at=_now(),
                disposition_at=None,
                disposition_evidence=None,
                control_domain="test-domain",
            )

    def test_infinite_amount_in_storage_fails_closed_on_read(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "store.sqlite3"
            registry = EffectIntentRegistry(str(db_path))
            intent = _build_intent(
                effect_intent_id="intent-infinite",
                authority_reservation_id="reservation-infinite",
                control_domain="test-domain",
            )
            registry.commit_intent(intent)

            store = DurableEffectStore(db_path)
            store.store_reservation(
                AuthorityReservation(
                    reservation_id="reservation-infinite",
                    effect_intent_id="intent-infinite",
                    capability_type="compute",
                    amount=1.0,
                    disposition=AuthorityDisposition.RESERVED,
                    reserved_at=_now(),
                    disposition_at=None,
                    disposition_evidence=None,
                    control_domain="test-domain",
                )
            )

            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "UPDATE authority_reservations SET amount = 1e9999 WHERE control_domain = ? AND reservation_id = ?",
                    ("test-domain", "reservation-infinite"),
                )
                conn.commit()
            finally:
                conn.close()

            with pytest.raises(StorageIntegrityError, match="failed validation"):
                store.get_reservation("reservation-infinite", "test-domain")

            intent_negative = _build_intent(
                effect_intent_id="intent-negative-infinite",
                authority_reservation_id="reservation-negative-infinite",
                control_domain="test-domain",
            )
            registry.commit_intent(intent_negative)
            store.store_reservation(
                AuthorityReservation(
                    reservation_id="reservation-negative-infinite",
                    effect_intent_id="intent-negative-infinite",
                    capability_type="compute",
                    amount=1.0,
                    disposition=AuthorityDisposition.RESERVED,
                    reserved_at=_now(),
                    disposition_at=None,
                    disposition_evidence=None,
                    control_domain="test-domain",
                )
            )

            conn = sqlite3.connect(db_path)
            try:
                with pytest.raises(sqlite3.IntegrityError, match="amount >= 0"):
                    conn.execute(
                        "UPDATE authority_reservations SET amount = -1e9999 WHERE control_domain = ? AND reservation_id = ?",
                        ("test-domain", "reservation-negative-infinite"),
                    )
            finally:
                conn.close()

    def test_contradictory_schema_versions_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "store.sqlite3"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE effect_store_schema (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute("INSERT INTO effect_store_schema(version) VALUES (0)")
            conn.execute("INSERT INTO effect_store_schema(version) VALUES (1)")
            conn.commit()
            conn.close()

            with pytest.raises(SchemaVersionError, match="contradictory versions"):
                DurableEffectStore(db_path)

    def test_terminal_evidence_pointer_reference_fingerprint_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "store.sqlite3"
            registry = EffectIntentRegistry(str(db_path))
            intent = _build_intent(
                effect_intent_id="intent-reference-corruption",
                authority_reservation_id="reservation-reference-corruption",
                control_domain="test-domain",
            )
            registry.commit_intent(intent)

            _, pointer, spine = _build_terminal_decision_bundle(
                control_domain="test-domain",
                effect_intent_id="intent-reference-corruption",
                reservation_id="reservation-reference-corruption",
                decision_id="decision-reference-corruption",
                mission_id="mission-reference-corruption",
            )
            terminal_reservation = AuthorityReservation.from_verified_terminal_decision(
                reservation_id="reservation-reference-corruption",
                effect_intent_id="intent-reference-corruption",
                capability_type="compute",
                amount=1.0,
                reserved_at=_now(),
                disposition_at=_now(),
                evidence_spine=spine,
                evidence_pointer=pointer,
                control_domain="test-domain",
            )
            store = DurableEffectStore(db_path)
            store.store_reservation(terminal_reservation, evidence_spine=spine)

            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    """
                    SELECT disposition_evidence_json
                    FROM authority_reservations
                    WHERE control_domain = ? AND reservation_id = ?
                    """,
                    ("test-domain", "reservation-reference-corruption"),
                ).fetchone()
                payload = json.loads(row[0])
                payload["reference_fingerprint"] = "0" * 64
                conn.execute(
                    """
                    UPDATE authority_reservations
                       SET disposition_evidence_json = ?
                     WHERE control_domain = ? AND reservation_id = ?
                    """,
                    (json.dumps(payload, sort_keys=True), "test-domain", "reservation-reference-corruption"),
                )
                conn.commit()
            finally:
                conn.close()

            from research_mission import EvidenceSpineError

            with pytest.raises(EvidenceSpineError, match="Reference fingerprint mismatch"):
                store.get_reservation("reservation-reference-corruption", "test-domain", evidence_spine=spine)

    def test_terminal_evidence_pointer_record_fingerprint_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "store.sqlite3"
            registry = EffectIntentRegistry(str(db_path))
            intent = _build_intent(
                effect_intent_id="intent-record-corruption",
                authority_reservation_id="reservation-record-corruption",
                control_domain="test-domain",
            )
            registry.commit_intent(intent)

            _, pointer, spine = _build_terminal_decision_bundle(
                control_domain="test-domain",
                effect_intent_id="intent-record-corruption",
                reservation_id="reservation-record-corruption",
                decision_id="decision-record-corruption",
                mission_id="mission-record-corruption",
            )
            terminal_reservation = AuthorityReservation.from_verified_terminal_decision(
                reservation_id="reservation-record-corruption",
                effect_intent_id="intent-record-corruption",
                capability_type="compute",
                amount=1.0,
                reserved_at=_now(),
                disposition_at=_now(),
                evidence_spine=spine,
                evidence_pointer=pointer,
                control_domain="test-domain",
            )
            store = DurableEffectStore(db_path)
            store.store_reservation(terminal_reservation, evidence_spine=spine)

            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    """
                    SELECT disposition_evidence_json
                    FROM authority_reservations
                    WHERE control_domain = ? AND reservation_id = ?
                    """,
                    ("test-domain", "reservation-record-corruption"),
                ).fetchone()
                payload = json.loads(row[0])
                payload["record_fingerprint"] = "1" * 64
                conn.execute(
                    """
                    UPDATE authority_reservations
                       SET disposition_evidence_json = ?
                     WHERE control_domain = ? AND reservation_id = ?
                    """,
                    (json.dumps(payload, sort_keys=True), "test-domain", "reservation-record-corruption"),
                )
                conn.commit()
            finally:
                conn.close()

            from research_mission import EvidenceSpineError

            with pytest.raises(EvidenceSpineError, match="Record fingerprint mismatch"):
                store.get_reservation("reservation-record-corruption", "test-domain", evidence_spine=spine)

    def test_terminal_evidence_pointer_control_domain_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "store.sqlite3"
            registry = EffectIntentRegistry(str(db_path))
            intent = _build_intent(
                effect_intent_id="intent-domain-corruption",
                authority_reservation_id="reservation-domain-corruption",
                control_domain="test-domain",
            )
            registry.commit_intent(intent)

            _, pointer, spine = _build_terminal_decision_bundle(
                control_domain="test-domain",
                effect_intent_id="intent-domain-corruption",
                reservation_id="reservation-domain-corruption",
                decision_id="decision-domain-corruption",
                mission_id="mission-domain-corruption",
            )
            terminal_reservation = AuthorityReservation.from_verified_terminal_decision(
                reservation_id="reservation-domain-corruption",
                effect_intent_id="intent-domain-corruption",
                capability_type="compute",
                amount=1.0,
                reserved_at=_now(),
                disposition_at=_now(),
                evidence_spine=spine,
                evidence_pointer=pointer,
                control_domain="test-domain",
            )
            store = DurableEffectStore(db_path)
            store.store_reservation(terminal_reservation, evidence_spine=spine)

            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    """
                    SELECT disposition_evidence_json
                    FROM authority_reservations
                    WHERE control_domain = ? AND reservation_id = ?
                    """,
                    ("test-domain", "reservation-domain-corruption"),
                ).fetchone()
                payload = json.loads(row[0])
                payload["key"]["domain_id"] = "other-domain"
                conn.execute(
                    """
                    UPDATE authority_reservations
                       SET disposition_evidence_json = ?
                     WHERE control_domain = ? AND reservation_id = ?
                    """,
                    (json.dumps(payload, sort_keys=True), "test-domain", "reservation-domain-corruption"),
                )
                conn.commit()
            finally:
                conn.close()

            from research_mission import EvidenceSpineError

            with pytest.raises(EvidenceSpineError, match="Evidence not found"):
                store.get_reservation("reservation-domain-corruption", "test-domain", evidence_spine=spine)

    def test_invalid_stored_control_domain_fails_closed_on_open(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "store.sqlite3"
            registry = EffectIntentRegistry(str(db_path))
            intent = _build_intent(
                effect_intent_id="intent-invalid-control-domain",
                authority_reservation_id="reservation-invalid-control-domain",
                control_domain="test-domain",
            )
            registry.commit_intent(intent)

            conn = sqlite3.connect(db_path)
            try:
                invalid_domain = "x" * 256
                conn.execute(
                    """
                    UPDATE effect_intents
                       SET control_domain = ?
                     WHERE control_domain = ? AND effect_intent_id = ?
                    """,
                    (invalid_domain, "test-domain", "intent-invalid-control-domain"),
                )
                conn.commit()
            finally:
                conn.close()

            with pytest.raises(StorageIntegrityError, match="control_domain"):
                DurableEffectStore(db_path)

    @pytest.mark.parametrize(
        "label, stored_value",
        [
            ("positive-infinity", float("inf")),
            ("negative-infinity", float("-inf")),
            ("nan-or-null", float("nan")),
        ],
    )
    def test_nonfinite_storage_corruption_fails_closed_on_read(self, label: str, stored_value: float):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "store.sqlite3"
            registry = EffectIntentRegistry(str(db_path))
            intent = _build_intent(
                effect_intent_id=f"intent-{label}",
                authority_reservation_id=f"reservation-{label}",
                control_domain="test-domain",
            )
            registry.commit_intent(intent)
            store = DurableEffectStore(db_path)
            store.store_reservation(
                AuthorityReservation(
                    reservation_id=f"reservation-{label}",
                    effect_intent_id=f"intent-{label}",
                    capability_type="compute",
                    amount=1.0,
                    disposition=AuthorityDisposition.RESERVED,
                    reserved_at=_now(),
                    disposition_at=None,
                    disposition_evidence=None,
                    control_domain="test-domain",
                )
            )

            conn = sqlite3.connect(db_path)
            try:
                if label == "nan-or-null":
                    conn.execute("PRAGMA writable_schema = ON")
                    conn.execute(
                        """
                        UPDATE sqlite_master
                           SET sql = replace(
                               sql,
                               'amount REAL NOT NULL CHECK (amount >= 0)',
                               'amount REAL CHECK (amount >= 0)'
                           )
                         WHERE name = 'authority_reservations'
                        """
                    )
                    conn.execute("PRAGMA writable_schema = OFF")
                    conn.commit()
                    conn.close()
                    conn = sqlite3.connect(db_path)
                conn.execute("PRAGMA ignore_check_constraints = ON")
                injected_value = None if label == "nan-or-null" else stored_value
                conn.execute(
                    """
                    UPDATE authority_reservations
                       SET amount = ?
                     WHERE control_domain = ? AND reservation_id = ?
                    """,
                    (injected_value, "test-domain", f"reservation-{label}"),
                )
                conn.commit()
                raw_amount = conn.execute(
                    """
                    SELECT amount
                    FROM authority_reservations
                    WHERE control_domain = ? AND reservation_id = ?
                    """,
                    ("test-domain", f"reservation-{label}"),
                ).fetchone()[0]
            finally:
                conn.close()

            if label == "nan-or-null":
                assert raw_amount is None or math.isnan(raw_amount)
            else:
                assert not math.isfinite(raw_amount)

            with pytest.raises(StorageIntegrityError, match="failed validation"):
                store.get_reservation(f"reservation-{label}", "test-domain")

    def test_reservation_state_cannot_regress_after_terminal_transition(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "store.sqlite3"
            registry = EffectIntentRegistry(str(db_path))
            intent = _build_intent(
                effect_intent_id="intent-reservation-regression",
                authority_reservation_id="reservation-regression",
                control_domain="test-domain",
            )
            registry.commit_intent(intent)

            store = DurableEffectStore(db_path)
            reserved = AuthorityReservation(
                reservation_id="reservation-regression",
                effect_intent_id="intent-reservation-regression",
                capability_type="compute",
                amount=1.0,
                disposition=AuthorityDisposition.RESERVED,
                reserved_at=_now(),
                disposition_at=None,
                disposition_evidence=None,
                control_domain="test-domain",
            )
            released = AuthorityReservation(
                reservation_id="reservation-regression",
                effect_intent_id="intent-reservation-regression",
                capability_type="compute",
                amount=1.0,
                disposition=AuthorityDisposition.RELEASED,
                reserved_at=reserved.reserved_at,
                disposition_at=_now(),
                disposition_evidence=None,
                control_domain="test-domain",
            )

            store.store_reservation(reserved)
            store.store_reservation(released)

            with pytest.raises(ValueError, match="cannot regress"):
                store.store_reservation(reserved)

            assert store.get_reservation("reservation-regression", "test-domain").disposition == AuthorityDisposition.RELEASED

    def test_obligation_state_cannot_regress_after_terminal_transition(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "store.sqlite3"
            registry = EffectIntentRegistry(str(db_path))
            intent = _build_intent(
                effect_intent_id="intent-obligation-regression",
                authority_reservation_id="reservation-obligation-regression",
                control_domain="test-domain",
            )
            registry.commit_intent(intent)

            store = DurableEffectStore(db_path)
            pending = ReconciliationObligation(
                obligation_id="obligation-regression",
                effect_intent_id="intent-obligation-regression",
                dispatch_id=None,
                state=ReconciliationState.PENDING,
                provider_reconcilability=ProviderReconcilability.NONE,
                next_probe_at=None,
                probe_history=(),
                terminal_disposition=None,
                created_at=_now(),
                control_domain="test-domain",
            )
            resolved = ReconciliationObligation(
                obligation_id="obligation-regression",
                effect_intent_id="intent-obligation-regression",
                dispatch_id=None,
                state=ReconciliationState.RESOLVED,
                provider_reconcilability=ProviderReconcilability.NONE,
                next_probe_at=None,
                probe_history=(),
                terminal_disposition=None,
                created_at=pending.created_at,
                control_domain="test-domain",
            )

            store.store_obligation(pending)
            store.store_obligation(resolved)

            with pytest.raises(ValueError, match="cannot regress"):
                store.store_obligation(pending)

            assert store.get_obligation("obligation-regression", "test-domain").state == ReconciliationState.RESOLVED

    def test_store_close_refuses_follow_on_operations(self):
        store = DurableEffectStore(":memory:")
        store.close()

        with pytest.raises(DurableEffectStoreError, match="closed"):
            store.get_intent("intent-any", "test-domain")

    def test_process_restart_persists_exact_committed_state(self):
        ctx = mp.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "store.sqlite3")
            intent = _build_intent(
                effect_intent_id="intent-process-persist",
                authority_reservation_id="reservation-process-persist",
                control_domain="test-domain",
            )
            result_queue = ctx.Queue()

            writer = ctx.Process(target=_process_commit_intent, args=(db_path, intent, result_queue))
            writer.start()
            writer.join(timeout=20)
            assert writer.exitcode == 0
            assert result_queue.get(timeout=5) == ("ok", "intent-process-persist")

            reader = ctx.Process(
                target=_process_read_intent,
                args=(db_path, "intent-process-persist", "test-domain", result_queue),
            )
            reader.start()
            reader.join(timeout=20)
            assert reader.exitcode == 0
            row = result_queue.get(timeout=5)
            assert row is not None
            assert row["effect_intent_id"] == "intent-process-persist"
            assert row["control_domain"] == "test-domain"
            assert row["authority_reservation_id"] == "reservation-process-persist"

    def test_two_processes_commit_identical_semantic_intent(self):
        ctx = mp.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "store.sqlite3")
            intent = _build_intent(
                effect_intent_id="intent-process-concurrent",
                authority_reservation_id="reservation-process-concurrent",
                control_domain="test-domain",
            )
            start_event = ctx.Event()
            result_queue = ctx.Queue()

            workers = [
                ctx.Process(
                    target=_process_commit_identical_intent,
                    args=(db_path, intent, start_event, result_queue),
                )
                for _ in range(2)
            ]
            for worker in workers:
                worker.start()
            start_event.set()
            for worker in workers:
                worker.join(timeout=20)
                assert worker.exitcode == 0

            results = [result_queue.get(timeout=5), result_queue.get(timeout=5)]
            assert results.count(("ok", "intent-process-concurrent")) == 2

            store = DurableEffectStore(db_path)
            stored_intent = store.get_intent("intent-process-concurrent", "test-domain")
            assert stored_intent is not None

    def test_two_processes_conflicting_payloads_produce_one_winner(self):
        ctx = mp.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "store.sqlite3")
            start_event = ctx.Event()
            result_queue = ctx.Queue()

            winner_intent = _build_intent(
                effect_intent_id="intent-process-conflict",
                authority_reservation_id="reservation-process-conflict",
                control_domain="test-domain",
                decision_id="decision-winner",
            )
            loser_intent = _build_intent(
                effect_intent_id="intent-process-conflict",
                authority_reservation_id="reservation-process-conflict",
                control_domain="test-domain",
                decision_id="decision-loser",
                operation_digest="{\"op\":\"different\"}",
            )

            workers = [
                ctx.Process(target=_process_commit_conflicting_intent, args=(db_path, winner_intent, start_event, result_queue)),
                ctx.Process(target=_process_commit_conflicting_intent, args=(db_path, loser_intent, start_event, result_queue)),
            ]
            for worker in workers:
                worker.start()
            start_event.set()
            for worker in workers:
                worker.join(timeout=20)
                assert worker.exitcode == 0

            results = [result_queue.get(timeout=5) for _ in workers]
            ok_results = [result for result in results if result[0] == "ok"]
            error_results = [result for result in results if result[0] == "error"]
            assert len(ok_results) == 1
            assert len(error_results) == 1
            assert {result[1] for result in ok_results} <= {"decision-winner", "decision-loser"}
            assert {result[3] for result in error_results} <= {"decision-winner", "decision-loser"}
            assert ok_results[0][1] != error_results[0][3]
            assert error_results[0][1] in {"ValueError", "IntegrityError"}

            store = DurableEffectStore(db_path)
            stored = store.get_intent("intent-process-conflict", "test-domain")
            assert stored is not None
            assert stored.decision_id == ok_results[0][1]
