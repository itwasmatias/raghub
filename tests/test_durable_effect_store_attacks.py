"""Adversarial tests for durable effect store regression and boundary cases."""

from __future__ import annotations

import json
import multiprocessing as mp
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from federation.durable_effect_store import DurableEffectStore, SchemaVersionError, DurableEffectStoreError
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


class TestAdditionalDurabilityAttacks:
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
            assert stored_intent == intent
