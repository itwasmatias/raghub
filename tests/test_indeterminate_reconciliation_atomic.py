from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import sqlite3

import pytest

from federation.durable_effect_store import AuthorityReservation, DurableEffectStore
from federation.effect_gateway import GatewayEffectResult, GatewayStateError, GovernedEffectGateway
from federation.effect_safety import (
    AuthorityDisposition,
    EffectState,
    ProviderReconcilability,
    ReconciliationObligation,
    ReconciliationState,
)
from tests.test_governed_gateway_live import FrozenClock, _create_authoritative_records, _create_request, _utc


def _setup(tmp_path):
    store = DurableEffectStore(tmp_path / "atomic.sqlite3")
    gateway = GovernedEffectGateway(store, clock=FrozenClock(_utc(1, 10, 0)))
    _create_authoritative_records(store)
    request = _create_request()
    claim_id, _ = gateway.claim_dispatch(request, owner_identity="owner-1")
    _, token = gateway.issue_dispatch_permit(request, claim_id)
    gateway.verify_and_consume_permit(token, control_domain="domain-a")
    result = GatewayEffectResult(
        task_succeeded=False, task_error="timeout", effect_status=EffectState.INDETERMINATE,
        dispatch_attempted=True, handoff_started=True, receipt_recorded=False,
        authority_disposition=AuthorityDisposition.RESERVED, reconciliation_required=True,
        reconciliation_obligation_id="obligation-1", gateway_claim_id=claim_id,
        effect_intent_id=request.effect_intent_id, effect_dispatch_id=request.effect_dispatch_id,
    )
    obligation = ReconciliationObligation(
        obligation_id="obligation-1", effect_intent_id=request.effect_intent_id,
        dispatch_id=request.effect_dispatch_id, state=ReconciliationState.PENDING,
        provider_reconcilability=request.provider_reconcilability, next_probe_at=None,
        probe_history=(), terminal_disposition=None, created_at=datetime.now(timezone.utc),
        control_domain="domain-a",
    )
    return store, gateway, request, result, obligation


def test_atomic_indeterminate_survives_reopen(tmp_path):
    store, gateway, request, result, obligation = _setup(tmp_path)
    gateway.record_indeterminate_with_obligation(request, result, obligation)
    assert store.get_gateway_claim(result.gateway_claim_id, "domain-a")["state"] == "indeterminate"
    assert store.get_obligation("obligation-1", "domain-a").state is ReconciliationState.PENDING
    store.close()
    reopened = DurableEffectStore(tmp_path / "atomic.sqlite3")
    assert reopened.get_gateway_claim(result.gateway_claim_id, "domain-a")["state"] == "indeterminate"
    assert reopened.get_obligation("obligation-1", "domain-a") is not None


@pytest.mark.parametrize("field", ["effect_intent_id", "dispatch_id"])
def test_atomic_binding_mismatch_has_no_partial_write(tmp_path, field):
    store, gateway, request, result, obligation = _setup(tmp_path)
    bad = replace(obligation, **{field: "wrong-value"})
    with pytest.raises(GatewayStateError):
        gateway.record_indeterminate_with_obligation(request, result, bad)
    assert store.get_gateway_claim(result.gateway_claim_id, "domain-a")["state"] == "handoff_started"
    assert store.get_obligation("obligation-1", "domain-a") is None


def test_wrong_domain_and_nonpending_are_rejected_without_write(tmp_path):
    store, gateway, request, result, obligation = _setup(tmp_path)
    with pytest.raises(GatewayStateError):
        gateway.record_indeterminate_with_obligation(request, result, replace(obligation, control_domain="other"))
    with pytest.raises(GatewayStateError):
        gateway.record_indeterminate_with_obligation(request, result, replace(obligation, state=ReconciliationState.IN_PROGRESS))
    assert store.get_gateway_claim(result.gateway_claim_id, "domain-a")["state"] == "handoff_started"


def test_generic_indeterminate_path_fails_closed(tmp_path):
    store, gateway, request, result, _ = _setup(tmp_path)
    with pytest.raises(GatewayStateError):
        gateway.record_effect_result(result.gateway_claim_id, "domain-a", result)
    assert store.get_gateway_claim(result.gateway_claim_id, "domain-a")["state"] == "handoff_started"


def test_direct_store_indeterminate_path_fails_closed(tmp_path):
    store, _, _, result, _ = _setup(tmp_path)
    with pytest.raises(ValueError, match="requires record_indeterminate_with_obligation"):
        store.record_gateway_result(
            gateway_claim_id=result.gateway_claim_id,
            control_domain="domain-a",
            effect_status="indeterminate",
            now=_utc(1, 11, 0),
        )
    assert store.get_gateway_claim(result.gateway_claim_id, "domain-a")["state"] == "handoff_started"


def test_reconcilability_mismatch_is_rejected(tmp_path):
    store, gateway, request, result, obligation = _setup(tmp_path)
    bad = replace(obligation, provider_reconcilability=ProviderReconcilability.NONE)
    with pytest.raises(GatewayStateError):
        gateway.record_indeterminate_with_obligation(request, result, bad)
    assert store.get_obligation("obligation-1", "domain-a") is None


def test_request_reservation_substitution_is_rejected_without_write(tmp_path):
    store, gateway, request, result, obligation = _setup(tmp_path)
    alternate = AuthorityReservation(
        reservation_id="reservation-2", effect_intent_id=request.effect_intent_id,
        capability_type="effect:dispatch", amount=1.0,
        disposition=AuthorityDisposition.RESERVED, reserved_at=_utc(1),
        disposition_at=None, disposition_evidence=None, control_domain="domain-a",
    )
    store.store_reservation(alternate)

    substituted_request = replace(request, authority_reservation_id="reservation-2")
    with pytest.raises(GatewayStateError):
        gateway.record_indeterminate_with_obligation(
            substituted_request, result, obligation
        )

    assert store.get_gateway_claim(result.gateway_claim_id, "domain-a")["state"] == "handoff_started"
    assert store.get_obligation("obligation-1", "domain-a") is None
    assert store.get_reservation("reservation-1", "domain-a").disposition is AuthorityDisposition.RESERVED
    assert store.get_reservation("reservation-2", "domain-a").disposition is AuthorityDisposition.RESERVED
    store.close()
    reopened = DurableEffectStore(tmp_path / "atomic.sqlite3")
    try:
        assert reopened.get_gateway_claim(result.gateway_claim_id, "domain-a")["state"] == "handoff_started"
        assert reopened.get_obligation("obligation-1", "domain-a") is None
        assert reopened.get_reservation("reservation-1", "domain-a").disposition is AuthorityDisposition.RESERVED
        assert reopened.get_reservation("reservation-2", "domain-a").disposition is AuthorityDisposition.RESERVED
    finally:
        reopened.close()


def test_store_rejects_claim_reservation_intent_substitution(tmp_path):
    store, _, request, result, obligation = _setup(tmp_path)
    _create_authoritative_records(
        store, effect_intent_id="intent-2", effect_dispatch_id="dispatch-2",
        authority_reservation_id="reservation-2", idempotency_key="idem-2",
    )
    connection = sqlite3.connect(str(tmp_path / "atomic.sqlite3"))
    try:
        connection.execute(
            "UPDATE authority_reservations SET effect_intent_id = ? "
            "WHERE control_domain = ? AND reservation_id = ?",
            ("intent-2", "domain-a", "reservation-1"),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ValueError, match="reservation effect_intent_id"):
        store.record_indeterminate_with_obligation(
            gateway_claim_id=result.gateway_claim_id,
            control_domain=request.control_domain,
            obligation=obligation,
            now=_utc(1, 11),
        )
    assert store.get_gateway_claim(result.gateway_claim_id, "domain-a")["state"] == "handoff_started"
    assert store.get_obligation("obligation-1", "domain-a") is None


def _assert_rolled_back(path, claim_id):
    reopened = DurableEffectStore(path)
    try:
        assert reopened.get_gateway_claim(claim_id, "domain-a")["state"] == "handoff_started"
        assert reopened.get_obligation("obligation-1", "domain-a") is None
        reservation = reopened.get_reservation("reservation-1", "domain-a")
        assert reservation.disposition is AuthorityDisposition.RESERVED
    finally:
        reopened.close()


def test_obligation_write_failure_rolls_back_claim_and_survives_reopen(tmp_path):
    store, gateway, request, result, obligation = _setup(tmp_path)

    def fail_obligation_write():
        raise RuntimeError("injected obligation write failure")

    store._test_fail_indeterminate_obligation_write = fail_obligation_write
    with pytest.raises(RuntimeError, match="injected obligation write failure"):
        gateway.record_indeterminate_with_obligation(request, result, obligation)
    assert store.get_gateway_claim(result.gateway_claim_id, "domain-a")["state"] == "handoff_started"
    assert store.get_obligation("obligation-1", "domain-a") is None
    assert store.get_reservation("reservation-1", "domain-a").disposition is AuthorityDisposition.RESERVED
    store.close()
    _assert_rolled_back(tmp_path / "atomic.sqlite3", result.gateway_claim_id)


def test_claim_transition_failure_rolls_back_obligation_and_survives_reopen(tmp_path):
    store, gateway, request, result, obligation = _setup(tmp_path)

    def fail_claim_transition():
        raise RuntimeError("injected claim transition failure")

    store._test_fail_indeterminate_claim_transition = fail_claim_transition
    with pytest.raises(RuntimeError, match="injected claim transition failure"):
        gateway.record_indeterminate_with_obligation(request, result, obligation)
    assert store.get_gateway_claim(result.gateway_claim_id, "domain-a")["state"] == "handoff_started"
    assert store.get_obligation("obligation-1", "domain-a") is None
    assert store.get_reservation("reservation-1", "domain-a").disposition is AuthorityDisposition.RESERVED
    store.close()
    _assert_rolled_back(tmp_path / "atomic.sqlite3", result.gateway_claim_id)
