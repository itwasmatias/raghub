"""Immutable dispatch offer and audit-event contract tests."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from federation.dispatch_offer import (
    TERMINAL_DISPATCH_STATUSES,
    DispatchActorType,
    DispatchAuditEvent,
    DispatchEventType,
    DispatchOfferSnapshot,
    DispatchStatus,
)
from federation.task_request import AuthorizationLevel


CREATED = datetime(2026, 8, 5, 20, 0, tzinfo=timezone.utc)
EXPIRES = CREATED + timedelta(minutes=5)


def event(**overrides):
    values = {
        "schema_version": 1,
        "sequence": 1,
        "event_type": DispatchEventType.OFFER_CREATED,
        "offer_id": "dispatch-1",
        "routing_assignment_id": "assignment-1",
        "routing_assignment_fingerprint": "a" * 64,
        "mission_id": "mission-1",
        "task_id": "task-1",
        "coordinator_node_id": "coordinator-1",
        "worker_node_id": "worker-1",
        "required_capabilities": ("python_execution",),
        "previous_state": None,
        "new_state": DispatchStatus.OFFERED,
        "actor_type": DispatchActorType.COORDINATOR,
        "actor_node_id": "coordinator-1",
        "authorization_metadata": (("level", "restricted"),),
        "approval_metadata": (("required", True),),
        "occurred_at": CREATED,
        "expires_at": EXPIRES,
        "reason": None,
        "predecessor_digest": "0" * 64,
        "resulting_digest": "b" * 64,
    }
    values.update(overrides)
    return DispatchAuditEvent(**values)


def snapshot(**overrides):
    values = {
        "offer_id": "dispatch-1",
        "assignment_id": "assignment-1",
        "assignment_fingerprint": "a" * 64,
        "task_id": "task-1",
        "mission_id": "mission-1",
        "coordinator_node_id": "coordinator-1",
        "worker_node_id": "worker-1",
        "required_capabilities": ("python_execution",),
        "authorization_level": AuthorizationLevel.RESTRICTED,
        "approval_required": True,
        "authorization_metadata": (("level", "restricted"),),
        "approval_metadata": (("required", True),),
        "created_at": CREATED,
        "expires_at": EXPIRES,
        "audit_history": (event(),),
    }
    values.update(overrides)
    return DispatchOfferSnapshot(**values)


def test_terminal_status_contract_is_complete():
    assert set(DispatchStatus) - TERMINAL_DISPATCH_STATUSES == {
        DispatchStatus.OFFERED,
    }


def test_event_and_snapshot_are_deeply_immutable():
    audit_event = event()
    offer = snapshot()

    with pytest.raises(FrozenInstanceError):
        audit_event.reason = "changed"
    with pytest.raises(FrozenInstanceError):
        offer.status = DispatchStatus.ACCEPTED
    assert isinstance(offer.required_capabilities, tuple)
    assert isinstance(offer.authorization_metadata, tuple)
    assert isinstance(offer.audit_history, tuple)


@pytest.mark.parametrize("sequence", [True, 0, -1, 1.5])
def test_event_requires_positive_integer_sequence(sequence):
    with pytest.raises((TypeError, ValueError)):
        event(sequence=sequence)


def test_event_requires_complete_digest_values():
    with pytest.raises(ValueError, match="digest"):
        event(resulting_digest="not-a-digest")


def test_event_shape_binds_actor_and_transition():
    with pytest.raises(ValueError, match="event_type"):
        event(actor_type=DispatchActorType.WORKER, actor_node_id="worker-1")


def test_reason_is_bounded():
    with pytest.raises(ValueError, match="reason"):
        event(reason="x" * 1025)


def test_snapshot_rejects_foreign_event_identity():
    with pytest.raises(ValueError, match="worker"):
        snapshot(audit_history=(event(worker_node_id="worker-2"),))


def test_snapshot_requires_continuous_transition_history():
    accepted = event(
        sequence=2,
        event_type=DispatchEventType.OFFER_ACCEPTED,
        previous_state=DispatchStatus.OFFERED,
        new_state=DispatchStatus.ACCEPTED,
        actor_type=DispatchActorType.WORKER,
        actor_node_id="worker-1",
        occurred_at=CREATED + timedelta(seconds=1),
        predecessor_digest="b" * 64,
        resulting_digest="c" * 64,
    )
    offer = snapshot(
        status=DispatchStatus.ACCEPTED,
        terminal_at=accepted.occurred_at,
        audit_history=(event(), accepted),
    )

    assert offer.is_terminal
    assert offer.audit_history[-1] == accepted

    with pytest.raises(ValueError):
        snapshot(
            status=DispatchStatus.ACCEPTED,
            terminal_at=accepted.occurred_at,
            audit_history=(event(), replace(accepted, previous_state=None)),
        )


def test_expiration_boundary_is_deterministic():
    expired = event(
        sequence=2,
        event_type=DispatchEventType.OFFER_EXPIRED,
        previous_state=DispatchStatus.OFFERED,
        new_state=DispatchStatus.EXPIRED,
        actor_type=DispatchActorType.SYSTEM,
        actor_node_id=None,
        occurred_at=EXPIRES,
        reason="Offer reached its expiration deadline",
        predecessor_digest="b" * 64,
        resulting_digest="c" * 64,
    )

    assert snapshot(
        status=DispatchStatus.EXPIRED,
        terminal_at=EXPIRES,
        resolution_reason=expired.reason,
        audit_history=(event(), expired),
    ).status is DispatchStatus.EXPIRED
