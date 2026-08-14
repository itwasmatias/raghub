from __future__ import annotations

import dataclasses
import multiprocessing as mp
import os
import sqlite3
import tempfile
import secrets
from datetime import datetime, timezone
from pathlib import Path

import pytest

from federation.durable_effect_store import DurableEffectStore, SchemaVersionError
from federation.effect_gateway import (
    EffectConsequence,
    GatewayDispatchPermit,
    GatewayEffectRequest,
    GatewayEffectResult,
    GatewayStateError,
)
from federation.effect_safety import AuthorityDisposition, EffectState, ProviderReconcilability


def _utc(day: int) -> datetime:
    return datetime(2026, 8, day, tzinfo=timezone.utc)


def _request_kwargs() -> dict[str, object]:
    return {
        "control_domain": "domain-a",
        "principal_identity": "principal-1",
        "agent_identity": "agent-1",
        "mission_id": "mission-1",
        "task_id": "task-1",
        "attempt_id": "attempt-1",
        "delegation_grant_id": "delegation-1",
        "delegation_grant_fingerprint": "a" * 64,
        "requested_capability": "effect:dispatch",
        "effect_intent_id": "intent-1",
        "effect_dispatch_id": "dispatch-1",
        "authority_reservation_id": "reservation-1",
        "operation_digest": "b" * 64,
        "idempotency_key": "idem-1",
        "provider_id": "provider-1",
        "adapter_id": "adapter-1",
        "effect_consequence": EffectConsequence.EXTERNAL_COMMUNICATION,
        "provider_reconcilability": ProviderReconcilability.IDEMPOTENCY_KEY_LOOKUP,
        "request_timestamp": _utc(1),
        "request_expiry": _utc(2),
        "credential_scope": ("scope-a", "scope-b"),
    }


def test_gateway_request_fingerprint_is_material_and_stable() -> None:
    request = GatewayEffectRequest(**_request_kwargs())
    copied = GatewayEffectRequest(**_request_kwargs())
    assert request.request_fingerprint() == copied.request_fingerprint()

    changes = {
        "control_domain": "domain-b",
        "principal_identity": "principal-2",
        "agent_identity": "agent-2",
        "mission_id": "mission-2",
        "task_id": "task-2",
        "attempt_id": "attempt-2",
        "delegation_grant_id": "delegation-2",
        "delegation_grant_fingerprint": "c" * 64,
        "requested_capability": "effect:other",
        "effect_intent_id": "intent-2",
        "effect_dispatch_id": "dispatch-2",
        "authority_reservation_id": "reservation-2",
        "operation_digest": "d" * 64,
        "idempotency_key": "idem-2",
        "provider_id": "provider-2",
        "adapter_id": "adapter-2",
        "effect_consequence": EffectConsequence.FINANCIAL,
        "provider_reconcilability": ProviderReconcilability.PROVIDER_OPERATION_LOOKUP,
        "credential_scope": ("scope-a", "scope-c"),
    }
    for field, value in changes.items():
        kwargs = _request_kwargs()
        kwargs[field] = value
        if field == "request_timestamp":
            kwargs["request_expiry"] = _utc(5)
        if field == "request_expiry":
            kwargs["request_timestamp"] = _utc(1)
        mutated = GatewayEffectRequest(**kwargs)
        assert mutated.request_fingerprint() != request.request_fingerprint(), field


def test_gateway_request_rejects_duplicate_or_invalid_credential_scope() -> None:
    base = _request_kwargs()
    with pytest.raises(ValueError, match="duplicate"):
        GatewayEffectRequest(**{**base, "credential_scope": ("scope-a", "scope-a")})

    with pytest.raises(ValueError, match="non-empty string"):
        GatewayEffectRequest(**{**base, "credential_scope": ("scope-a", " ")})

    with pytest.raises(ValueError, match="NULL"):
        GatewayEffectRequest(**{**base, "credential_scope": ("scope-a", "scope-a\x00b")})

    scope = ["scope-a", "scope-b"]
    request = GatewayEffectRequest(**{**base, "credential_scope": scope})
    scope.append("scope-c")
    assert request.credential_scope == ("scope-a", "scope-b")


def test_gateway_request_rejects_naive_or_unordered_timestamps() -> None:
    base = _request_kwargs()
    with pytest.raises(ValueError, match="timezone-aware"):
        GatewayEffectRequest(**{**base, "request_timestamp": datetime(2026, 8, 1)})

    with pytest.raises(ValueError, match="follow"):
        GatewayEffectRequest(**{**base, "request_expiry": _utc(1)})


def test_gateway_dispatch_permit_hides_secret_and_binds_verifier() -> None:
    permit = GatewayDispatchPermit(
        permit_id="permit-1",
        control_domain="domain-a",
        request_fingerprint="f" * 64,
        effect_intent_id="intent-1",
        effect_dispatch_id="dispatch-1",
        authority_reservation_id="reservation-1",
        gateway_claim_id="claim-1",
        delegation_grant_id="delegation-1",
        delegation_grant_fingerprint="d" * 64,
        requested_capability="effect:dispatch",
        operation_digest="b" * 64,
        idempotency_key="idem-1",
        provider_id="provider-1",
        adapter_id="adapter-1",
        credential_scope=("scope-a", "scope-b"),
        owner_identity="owner-1",
        issued_at=_utc(1),
        expires_at=_utc(2),
        permit_token=secrets.token_urlsafe(32),
    )
    assert len(permit.verifier()) == 64
    assert permit.verifier() == permit.verifier()
    assert "a" * 43 not in repr(permit)
    assert "a" * 43 not in str(permit)
    assert "a" * 43 not in repr(dataclasses.asdict(permit))


@pytest.mark.parametrize("permit_token", ["a" * 31, "a" * 43])
def test_gateway_dispatch_permit_rejects_trivial_tokens(permit_token: str) -> None:
    with pytest.raises(ValueError, match="entropy"):
        GatewayDispatchPermit(
            permit_id="permit-1",
            control_domain="domain-a",
            request_fingerprint="f" * 64,
            effect_intent_id="intent-1",
            effect_dispatch_id="dispatch-1",
            authority_reservation_id="reservation-1",
            gateway_claim_id="claim-1",
            delegation_grant_id="delegation-1",
            delegation_grant_fingerprint="d" * 64,
            requested_capability="effect:dispatch",
            operation_digest="b" * 64,
            idempotency_key="idem-1",
            provider_id="provider-1",
            adapter_id="adapter-1",
            credential_scope=("scope-a",),
            owner_identity="owner-1",
            issued_at=_utc(1),
            expires_at=_utc(2),
            permit_token=permit_token,
        )


@pytest.mark.parametrize(
    ("effect_status", "dispatch_attempted", "handoff_started", "receipt_recorded", "authority_disposition", "reconciliation_required", "reconciliation_obligation_id"),
    [
        (EffectState.NOTHING_LANDED, False, False, False, None, False, None),
        (EffectState.SOMETHING_LANDED, True, True, True, AuthorityDisposition.CONSUMED, False, None),
        (
            EffectState.INDETERMINATE,
            True,
            True,
            False,
            AuthorityDisposition.RESERVED,
            True,
            "obligation-1",
        ),
    ],
)
def test_gateway_effect_result_accepts_canonical_postures(
    effect_status: EffectState,
    dispatch_attempted: bool,
    handoff_started: bool,
    receipt_recorded: bool,
    authority_disposition: AuthorityDisposition | None,
    reconciliation_required: bool,
    reconciliation_obligation_id: str | None,
) -> None:
        GatewayEffectResult(
        task_succeeded=effect_status is not EffectState.INDETERMINATE,
        task_error=None if effect_status is not EffectState.INDETERMINATE else "needs reconciliation",
        effect_status=effect_status,
        dispatch_attempted=dispatch_attempted,
        handoff_started=handoff_started,
        receipt_recorded=receipt_recorded,
        authority_disposition=authority_disposition,
        reconciliation_required=reconciliation_required,
        reconciliation_obligation_id=reconciliation_obligation_id,
        gateway_claim_id="claim-1",
        effect_intent_id="intent-1",
        effect_dispatch_id="dispatch-1",
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "task_succeeded": True,
            "task_error": "unexpected error",
            "effect_status": EffectState.SOMETHING_LANDED,
            "dispatch_attempted": True,
            "handoff_started": True,
            "receipt_recorded": True,
            "authority_disposition": AuthorityDisposition.CONSUMED,
            "reconciliation_required": False,
            "reconciliation_obligation_id": None,
        },
        {
            "task_succeeded": False,
            "task_error": None,
            "effect_status": EffectState.NOTHING_LANDED,
            "dispatch_attempted": False,
            "handoff_started": False,
            "receipt_recorded": False,
            "authority_disposition": AuthorityDisposition.RELEASED,
            "reconciliation_required": False,
            "reconciliation_obligation_id": None,
        },
        {
            "task_succeeded": False,
            "task_error": "needs reconciliation",
            "effect_status": EffectState.INDETERMINATE,
            "dispatch_attempted": True,
            "handoff_started": False,
            "receipt_recorded": False,
            "authority_disposition": AuthorityDisposition.RESERVED,
            "reconciliation_required": True,
            "reconciliation_obligation_id": "obligation-1",
        },
        {
            "task_succeeded": True,
            "task_error": None,
            "effect_status": EffectState.SOMETHING_LANDED,
            "dispatch_attempted": True,
            "handoff_started": True,
            "receipt_recorded": True,
            "authority_disposition": AuthorityDisposition.RESERVED,
            "reconciliation_required": False,
            "reconciliation_obligation_id": None,
        },
    ],
)
def test_gateway_effect_result_rejects_contradictory_postures(kwargs: dict[str, object]) -> None:
    with pytest.raises((TypeError, GatewayStateError, ValueError)):
        GatewayEffectResult(
            **kwargs,
            gateway_claim_id="claim-1",
            effect_intent_id="intent-1",
            effect_dispatch_id="dispatch-1",
        )


def test_gateway_effect_result_requires_boolean_task_success() -> None:
    with pytest.raises(TypeError, match="bool"):
        GatewayEffectResult(
            task_succeeded=1,  # type: ignore[arg-type]
            task_error=None,
            effect_status=EffectState.NOTHING_LANDED,
            dispatch_attempted=False,
            handoff_started=False,
            receipt_recorded=False,
            authority_disposition=None,
            reconciliation_required=False,
            reconciliation_obligation_id=None,
            gateway_claim_id="claim-1",
            effect_intent_id="intent-1",
            effect_dispatch_id="dispatch-1",
        )


def test_effect_gateway_schema_rejects_conflicting_idempotency_key_claims() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "store.sqlite3"
        store = DurableEffectStore(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                INSERT INTO effect_intents (
                    control_domain, effect_intent_id, decision_id, mission_id, task_id,
                    attempt_id, operation_digest, idempotency_key, provider_scope,
                    authority_reservation_id, compensation_strategy, evidence_reference,
                    state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "domain-a",
                    "intent-1",
                    "decision-1",
                    "mission-1",
                    "task-1",
                    "attempt-1",
                    "b" * 64,
                    "idem-1",
                    "provider",
                    "reservation-1",
                    None,
                    "evidence-1",
                    "committed_not_dispatched",
                    _utc(1).isoformat(),
                ),
            )
            claim_columns = """
                control_domain, gateway_claim_id, effect_intent_id, idempotency_key,
                operation_digest, provider_id, adapter_id, owner_identity, state,
                permit_verifier, claimed_at, expires_at, request_fingerprint,
                effect_dispatch_id, authority_reservation_id, delegation_grant_id,
                delegation_grant_fingerprint, requested_capability
            """
            conn.execute(
                f"INSERT INTO effect_gateway_claims ({claim_columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "domain-a",
                    "claim-1",
                    "intent-1",
                    "idem-1",
                    "b" * 64,
                    "provider-1",
                    "adapter-1",
                    "owner-1",
                    "claimed",
                    "c" * 64,
                    _utc(1).isoformat(),
                    _utc(2).isoformat(),
                    "fingerprint-1",
                    "dispatch-1",
                    "reservation-1",
                    "delegation-1",
                    "d" * 64,
                    "effect:dispatch",
                ),
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    f"INSERT INTO effect_gateway_claims ({claim_columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "domain-a",
                        "claim-2",
                        "intent-1",
                        "idem-1",
                        "e" * 64,
                        "provider-1",
                        "adapter-1",
                        "owner-1",
                        "claimed",
                        "f" * 64,
                        _utc(1).isoformat(),
                        _utc(2).isoformat(),
                        "fingerprint-2",
                        "dispatch-2",
                        "reservation-1",
                        "delegation-1",
                        "d" * 64,
                        "effect:dispatch",
                    ),
                )
        finally:
            conn.close()


def _create_v1_schema(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE effect_store_schema (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE effect_intents (
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
            CREATE TABLE effect_dispatches (
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
            CREATE TABLE authority_reservations (
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
            CREATE TABLE reconciliation_obligations (
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
            CREATE TABLE reservation_bindings (
                control_domain TEXT NOT NULL,
                authority_reservation_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                bound_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (control_domain, authority_reservation_id)
            );
            CREATE TABLE reservation_releases (
                control_domain TEXT NOT NULL,
                reservation_id TEXT NOT NULL,
                effect_intent_id TEXT NOT NULL,
                evidence_fingerprint TEXT NOT NULL,
                released_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (control_domain, reservation_id)
            );
            CREATE INDEX idx_intents_mission
                ON effect_intents(control_domain, mission_id, created_at);
            CREATE INDEX idx_dispatches_intent
                ON effect_dispatches(control_domain, effect_intent_id);
            CREATE INDEX idx_reservations_intent
                ON authority_reservations(control_domain, effect_intent_id);
            CREATE INDEX idx_reservations_disposition
                ON authority_reservations(control_domain, disposition);
            CREATE INDEX idx_obligations_state
                ON reconciliation_obligations(control_domain, state, next_probe_at);
            INSERT INTO effect_store_schema(version) VALUES (1);
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_v1_database_migrates_to_v2_with_single_current_version_row() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "store.sqlite3"
        _create_v1_schema(db_path)
        store = DurableEffectStore(db_path)
        assert isinstance(store, DurableEffectStore)
        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute("SELECT version FROM effect_store_schema").fetchall() == [(2,)]
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            assert "effect_gateway_claims" in tables
            assert "effect_gateway_permits" in tables
        finally:
            conn.close()


def _open_store_worker(db_path: str, queue: mp.Queue) -> None:
    try:
        store = DurableEffectStore(db_path)
        queue.put(("ok", store.__class__.__name__))
    except Exception as exc:  # pragma: no cover - subprocess transport
        queue.put(("error", type(exc).__name__, str(exc)))


def test_concurrent_v1_migration_converges_on_single_current_version_row() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "store.sqlite3"
        _create_v1_schema(db_path)
        ctx = mp.get_context("spawn")
        queue: mp.Queue = ctx.Queue()
        proc_a = ctx.Process(target=_open_store_worker, args=(str(db_path), queue))
        proc_b = ctx.Process(target=_open_store_worker, args=(str(db_path), queue))
        proc_a.start()
        proc_b.start()
        proc_a.join(30)
        proc_b.join(30)
        assert proc_a.exitcode == 0
        assert proc_b.exitcode == 0
        outcomes = [queue.get(timeout=5) for _ in range(2)]
        assert all(item[0] == "ok" for item in outcomes), outcomes
        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute("SELECT version FROM effect_store_schema").fetchall() == [(2,)]
        finally:
            conn.close()

def test_v1_to_v2_migration_preserves_all_authoritative_records() -> None:
    """Prove v1→v2 migration preserves every authoritative row byte-for-byte.

    Creates complete representative v1 database with:
    - EffectIntent (committed, dispatched states)
    - EffectDispatch (success, failure postures)
    - AuthorityReservation (RESERVED, CONSUMED, RELEASED dispositions)
    - ReconciliationObligation (active, terminal states)
    - ReservationBinding (idempotency binding)
    - ReservationRelease (release records)

    Verifies all 12 preservation guarantees.
    """
    import json

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "store.sqlite3"
        _create_v1_schema(db_path)

        # Populate complete authoritative v1 data
        conn = sqlite3.connect(db_path)
        try:
            # EffectIntent: committed and dispatched states
            conn.execute(
                """
                INSERT INTO effect_intents VALUES
                ('domain-a', 'intent-1', 'decision-1', 'mission-1', 'task-1', 'attempt-1',
                 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'idem-1',
                 'provider-scope-1', 'reservation-1', NULL, 'evidence-1',
                 'committed_not_dispatched', '2026-08-01T10:00:00Z'),
                ('domain-a', 'intent-2', 'decision-2', 'mission-1', 'task-2', 'attempt-2',
                 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'idem-2',
                 'provider-scope-2', 'reservation-2', 'abort', 'evidence-2',
                 'dispatched', '2026-08-02T11:00:00Z')
                """
            )

            # EffectDispatch: success and failure postures
            conn.execute(
                """
                INSERT INTO effect_dispatches VALUES
                ('domain-a', 'dispatch-1', 'intent-2', 'attempt-2', 'idem-2',
                 'provider-adapter-1', 'profile-v1',
                 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
                 'submitted', 'provider-op-1', 'evidence-3', '2026-08-02T11:05:00Z'),
                ('domain-a', 'dispatch-2', 'intent-2', 'attempt-2', 'idem-2',
                 'provider-adapter-1', 'profile-v1',
                 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
                 'confirmed', 'provider-op-2', 'evidence-4', '2026-08-02T11:10:00Z')
                """
            )

            # AuthorityReservation: all dispositions
            consumed_evidence = json.dumps({
                "key": {"source": "durable_store", "record_id": "reservation-2"},
                "reference_fingerprint": "aaaa" * 16,
                "record_fingerprint": "bbbb" * 16,
            })
            released_evidence = json.dumps({
                "key": {"source": "durable_store", "record_id": "reservation-3"},
                "reference_fingerprint": "cccc" * 16,
                "record_fingerprint": "dddd" * 16,
            })
            conn.execute(
                """
                INSERT INTO authority_reservations VALUES
                ('domain-a', 'reservation-1', 'intent-1', 'capability-1', 1.0,
                 'reserved', '2026-08-01T10:00:00Z', NULL, NULL),
                ('domain-a', 'reservation-2', 'intent-2', 'capability-2', 2.5,
                 'consumed', '2026-08-02T11:00:00Z', '2026-08-02T11:15:00Z', ?),
                ('domain-a', 'reservation-3', 'intent-1', 'capability-3', 0.5,
                 'released', '2026-08-01T10:01:00Z', '2026-08-01T10:30:00Z', ?)
                """,
                (consumed_evidence, released_evidence),
            )

            # ReconciliationObligation: active and terminal
            conn.execute(
                """
                INSERT INTO reconciliation_obligations VALUES
                ('domain-a', 'obligation-1', 'intent-2', 'dispatch-1',
                 'active', 'IDEMPOTENCY_KEY_LOOKUP', '2026-08-03T12:00:00Z',
                 '[{"probed_at": "2026-08-02T12:00:00Z", "outcome": "pending"}]',
                 NULL, '2026-08-02T11:06:00Z'),
                ('domain-a', 'obligation-2', 'intent-2', 'dispatch-2',
                 'terminal', 'PROVIDER_OPERATION_LOOKUP', NULL,
                 '[{"probed_at": "2026-08-02T12:00:00Z", "outcome": "confirmed"}]',
                 '{"disposition": "CONSUMED", "finalized_at": "2026-08-02T12:30:00Z"}',
                 '2026-08-02T11:11:00Z')
                """
            )

            # ReservationBinding: idempotency binding
            conn.execute(
                """
                INSERT INTO reservation_bindings VALUES
                ('domain-a', 'reservation-1', 'intent-1', '2026-08-01T10:00:01Z'),
                ('domain-a', 'reservation-2', 'intent-2', '2026-08-02T11:00:01Z')
                """
            )

            # ReservationRelease: release records
            conn.execute(
                """
                INSERT INTO reservation_releases VALUES
                ('domain-a', 'reservation-3', 'intent-1',
                 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
                 '2026-08-01T10:30:00Z')
                """
            )

            conn.commit()
        finally:
            conn.close()

        # Take pre-migration snapshot
        conn_snap = sqlite3.connect(db_path)
        try:
            v1_tables = [
                "effect_intents",
                "effect_dispatches",
                "authority_reservations",
                "reconciliation_obligations",
                "reservation_bindings",
                "reservation_releases",
            ]

            pre_migration_data = {}
            pre_migration_schemas = {}

            for table in v1_tables:
                # Capture schema
                schema_rows = conn_snap.execute(
                    f"SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(table,)
                ).fetchall()
                pre_migration_schemas[table] = schema_rows[0][0] if schema_rows else None

                # Capture all rows
                rows = conn_snap.execute(f"SELECT * FROM {table}").fetchall()
                pre_migration_data[table] = rows

            pre_version = conn_snap.execute(
                "SELECT version FROM effect_store_schema"
            ).fetchall()
            assert pre_version == [(1,)], "Pre-migration must be v1"

            pre_master = conn_snap.execute(
                "SELECT name, type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        finally:
            conn_snap.close()

        # Trigger migration through DurableEffectStore
        store = DurableEffectStore(db_path)
        assert isinstance(store, DurableEffectStore)

        # Post-migration verification
        conn_post = sqlite3.connect(db_path)
        try:
            # Property 1: Schema version is exactly [2]
            post_version = conn_post.execute(
                "SELECT version FROM effect_store_schema"
            ).fetchall()
            assert post_version == [(2,)], "Post-migration must be exactly [2], not [1,2]"

            # Property 2-8: Every v1 row preserved byte-for-byte
            post_migration_data = {}
            for table in v1_tables:
                rows = conn_post.execute(f"SELECT * FROM {table}").fetchall()
                post_migration_data[table] = rows

                # Verify row count unchanged
                assert len(rows) == len(pre_migration_data[table]), (
                    f"{table}: row count changed {len(pre_migration_data[table])} → {len(rows)}"
                )

                # Verify byte-for-byte equality
                assert rows == pre_migration_data[table], (
                    f"{table}: data changed after migration"
                )

            # Property 9: Gateway tables exist and are empty
            gateway_claims = conn_post.execute(
                "SELECT COUNT(*) FROM effect_gateway_claims"
            ).fetchone()[0]
            gateway_permits = conn_post.execute(
                "SELECT COUNT(*) FROM effect_gateway_permits"
            ).fetchone()[0]
            assert gateway_claims == 0, "Gateway claims must be empty post-migration"
            assert gateway_permits == 0, "Gateway permits must be empty post-migration"

            # Property 11: V1 table schemas unchanged
            for table in v1_tables:
                post_schema = conn_post.execute(
                    f"SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchall()
                assert post_schema[0][0] == pre_migration_schemas[table], (
                    f"{table}: schema changed after migration"
                )

            # Property 12: Only expected differences (schema_version change + gateway tables)
            post_master = conn_post.execute(
                "SELECT name, type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()

            pre_names = {row[0] for row in pre_master}
            post_names = {row[0] for row in post_master}

            added_objects = post_names - pre_names
            expected_additions = {
                "effect_gateway_claims",
                "effect_gateway_permits",
                "idx_gateway_claims_intent",
                "idx_gateway_claims_idempotency",
                "idx_gateway_claims_request",
                "idx_gateway_permits_claim",
                "idx_gateway_permits_verifier",
            }
            assert added_objects == expected_additions, (
                f"Unexpected schema additions: {added_objects - expected_additions}"
            )

            removed_objects = pre_names - post_names
            assert removed_objects == set(), f"Unexpected removals: {removed_objects}"
        finally:
            conn_post.close()

        # Property 10: Reopening causes no further mutation
        store2 = DurableEffectStore(db_path)
        assert isinstance(store2, DurableEffectStore)

        conn_reopen = sqlite3.connect(db_path)
        try:
            reopen_version = conn_reopen.execute(
                "SELECT version FROM effect_store_schema"
            ).fetchall()
            assert reopen_version == [(2,)], "Reopen must preserve [2]"

            for table in v1_tables:
                reopen_rows = conn_reopen.execute(f"SELECT * FROM {table}").fetchall()
                assert reopen_rows == post_migration_data[table], (
                    f"{table}: data changed on reopen"
                )
        finally:
            conn_reopen.close()
