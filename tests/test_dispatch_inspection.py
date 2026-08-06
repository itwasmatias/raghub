"""Read-only inspection tests for durable task dispatch."""

from dataclasses import FrozenInstanceError
from datetime import timedelta

import pytest

from federation import DispatchOfferNotFoundError, DispatchStatus
from tests.test_task_dispatcher import (
    MutableClock,
    INTEGRITY_KEY,
    build_coordinator,
    create_offer,
    make_assignment,
)


def test_inspection_returns_immutable_deterministic_values(tmp_path):
    clock = MutableClock()
    coordinator, first_assignment = build_coordinator(tmp_path, clock)
    first = create_offer(coordinator, first_assignment, clock)
    second_assignment = coordinator.assignment_store.record(
        make_assignment(task_id="task-2")[0],
    )
    second = create_offer(coordinator, second_assignment, clock)

    offers = coordinator.list_offers()
    history = coordinator.audit_log()

    assert isinstance(offers, tuple)
    assert isinstance(history, tuple)
    assert [offer.offer_id for offer in offers] == sorted(
        [first.offer_id, second.offer_id],
    )
    with pytest.raises(FrozenInstanceError):
        offers[0].status = DispatchStatus.CANCELLED
    with pytest.raises(FrozenInstanceError):
        history[0].reason = "changed"


def test_inspection_uses_one_snapshot_and_does_not_implicitly_expire(tmp_path):
    clock = MutableClock()
    coordinator, assignment = build_coordinator(tmp_path, clock)
    offer = create_offer(coordinator, assignment, clock)
    clock.advance(timedelta(hours=1))

    assert coordinator.inspect_offer(offer.offer_id).status is DispatchStatus.OFFERED
    assert len(coordinator.audit_history(offer.offer_id)) == 1


def test_read_only_inspection_of_missing_store_creates_nothing(tmp_path):
    coordinator, _ = build_coordinator(tmp_path / "seed")
    missing = tmp_path / "read-only" / "dispatch.jsonl"
    coordinator = coordinator.__class__(
        "coordinator-1",
        assignment_store=coordinator.assignment_store,
        dispatch_store_path=missing,
        integrity_key=INTEGRITY_KEY,
        heartbeat_registry=coordinator._heartbeat_registry,
        clock=MutableClock(),
    )

    assert coordinator.list_offers() == ()
    assert coordinator.audit_log() == ()
    assert not missing.parent.exists()


def test_physical_inspection_does_not_change_store(tmp_path):
    coordinator, assignment = build_coordinator(tmp_path)
    offer = create_offer(coordinator, assignment)
    path = tmp_path / "dispatch.jsonl"
    before_bytes = path.read_bytes()
    before_stat = path.stat()

    assert coordinator.inspect_offer(offer.offer_id) == offer
    assert coordinator.list_offers() == (offer,)
    assert coordinator.audit_history(offer.offer_id) == offer.audit_history
    assert coordinator.audit_log() == offer.audit_history

    after_stat = path.stat()
    assert path.read_bytes() == before_bytes
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_stat.st_size == before_stat.st_size


def test_inspection_filters_do_not_expose_internal_metadata(tmp_path):
    coordinator, assignment = build_coordinator(tmp_path)
    offer = create_offer(coordinator, assignment)

    assert coordinator.list_offers(
        status_filter="offered",
        mission_id="mission-1",
        worker_node_id="worker-1",
    ) == (offer,)
    assert coordinator.audit_log(
        offer_id=offer.offer_id,
        task_id="task-1",
        mission_id="mission-1",
        worker_node_id="worker-1",
    ) == offer.audit_history
    public_text = repr(offer) + repr(offer.audit_history)
    assert str(tmp_path) not in public_text
    assert "token" not in public_text.casefold()
    assert "prompt" not in public_text.casefold()


def test_unknown_offer_inspection_is_stable_and_sanitized(tmp_path):
    coordinator, _ = build_coordinator(tmp_path)

    with pytest.raises(DispatchOfferNotFoundError) as exc:
        coordinator.inspect_offer("missing")

    assert str(tmp_path) not in str(exc.value)
    assert "missing" in str(exc.value)
