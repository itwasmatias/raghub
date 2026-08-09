"""Lock-protected durable task-dispatch state machine."""


import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from federation.file_lock import fcntl
from federation.assignment_registry import (
    AssignmentCorruptionError,
    DurableAssignmentRegistry,
)
from federation.dispatch_offer import (
    DispatchActorType,
    DispatchAuditEvent,
    DispatchEventType,
    DispatchOfferSnapshot,
    DispatchStatus,
    normalize_dispatch_timestamp,
)
from federation.integrity import (
    authentication_tag,
    authenticates,
    require_integrity_key,
)
from federation.heartbeat_registry import HeartbeatRegistry
from federation.task_request import AuthorizationLevel


Clock = Callable[[], datetime]
_SCHEMA_VERSION = 1
_GENESIS_DIGEST = "0" * 64
_AUTHENTICATION_DOMAIN = b"raghub.dispatch-event.v1"
_EXPIRATION_REASON = "Offer reached its expiration deadline"
_FIELDS = {
    "schema_version",
    "sequence",
    "event_type",
    "offer_id",
    "routing_assignment_id",
    "routing_assignment_fingerprint",
    "mission_id",
    "task_id",
    "coordinator_node_id",
    "worker_node_id",
    "required_capabilities",
    "previous_state",
    "new_state",
    "actor_type",
    "actor_node_id",
    "authorization_metadata",
    "approval_metadata",
    "timestamp",
    "expires_at",
    "reason",
    "predecessor_digest",
    "resulting_digest",
}


class DispatchError(Exception):
    """Base dispatch failure."""


class DispatchCorruptionError(DispatchError):
    """Raised when durable evidence cannot be safely reconstructed."""


class DispatchIdentityMismatchError(DispatchError):
    """Raised when an exact actor or assignment identity does not match."""


class DispatchOfferNotFoundError(DispatchError):
    """Raised when an offer is absent from authoritative evidence."""


class DispatchOfferConflictError(DispatchError):
    """Raised when offer creation conflicts with authoritative history."""


class DispatchTerminalStateError(DispatchError):
    """Raised when a different terminal decision already won."""

    def __init__(self, offer):
        self.offer = offer
        super().__init__(
            f"Dispatch offer {offer.offer_id!r} is already terminal "
            f"with status {offer.status.value!r}",
        )


class DispatchOfferExpiredError(DispatchTerminalStateError):
    """Raised when expiration wins at the response boundary."""


class DispatchWorkerUnavailableError(DispatchError):
    """Raised when a new offer targets a worker without a live lease."""


@dataclass(slots=True, frozen=True)
class _Snapshot:
    offers: dict[str, DispatchOfferSnapshot]
    offer_ids_by_task: dict[tuple[str, str], str]
    events: tuple[DispatchAuditEvent, ...]
    last_digest: str


def _canonical(value: dict) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value, field_name):
    if not isinstance(value, str):
        raise DispatchCorruptionError("dispatch evidence has invalid field types")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return normalize_dispatch_timestamp(parsed, field_name)
    except (TypeError, ValueError) as exc:
        raise DispatchCorruptionError("dispatch evidence has invalid timestamps") from exc


def _pairs(value):
    return tuple(sorted(value.items()))


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DispatchCorruptionError("dispatch evidence has duplicate JSON keys")
        result[key] = value
    return result


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _require_identifier(value, field_name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_reason(value):
    if not isinstance(value, str):
        raise TypeError("reason must be a string")
    result = value.strip()
    if not result or len(result) > 1024:
        raise ValueError("reason must contain 1 to 1024 characters")
    return result


def _offer_id(assignment) -> str:
    identity = {
        "routing_assignment_id": assignment.assignment_id,
        "routing_assignment_fingerprint": assignment.assignment_fingerprint,
        "mission_id": assignment.mission_id,
        "task_id": assignment.task_id,
    }
    return f"dispatch-{hashlib.sha256(_canonical(identity)).hexdigest()}"


class TaskDispatchCoordinator:
    """Coordinate offers solely through authoritative durable evidence."""

    def __init__(
        self,
        coordinator_node_id: str,
        *,
        assignment_store: DurableAssignmentRegistry,
        dispatch_store_path,
        integrity_key: bytes,
        heartbeat_registry: HeartbeatRegistry,
        clock: Clock | None = None,
    ):
        self._coordinator_node_id = _require_identifier(
            coordinator_node_id,
            "coordinator_node_id",
        )
        if not isinstance(assignment_store, DurableAssignmentRegistry):
            raise TypeError("assignment_store must be a DurableAssignmentRegistry")
        self._integrity_key = require_integrity_key(integrity_key)
        if not assignment_store._integrity_key_matches(self._integrity_key):
            raise ValueError(
                "assignment_store and dispatch persistence must use the same integrity key",
            )
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if type(heartbeat_registry) is not HeartbeatRegistry:
            raise TypeError("heartbeat_registry must be a HeartbeatRegistry")
        if not heartbeat_registry._integrity_key_matches(self._integrity_key):
            raise ValueError(
                "heartbeat_registry and dispatch persistence must use "
                "the same integrity key",
            )
        self.assignment_store = assignment_store
        self._heartbeat_registry = heartbeat_registry
        self._path = Path(dispatch_store_path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._last_observed_at = None

    def create_offer(
        self,
        *,
        assignment_id: str,
        actor_node_id: str,
        expires_at: datetime,
    ) -> DispatchOfferSnapshot:
        self._require_coordinator(actor_node_id)
        try:
            assignment = self.assignment_store.resolve(assignment_id)
        except AssignmentCorruptionError as exc:
            raise DispatchCorruptionError(
                "authoritative assignment evidence is corrupt",
            ) from exc
        if assignment.coordinator_node_id != self._coordinator_node_id:
            raise DispatchIdentityMismatchError(
                "assignment coordinator does not match this coordinator",
            )
        now = self._now()
        expires_at = normalize_dispatch_timestamp(expires_at, "expires_at")
        if expires_at <= now:
            raise ValueError("expires_at must be after creation time")
        deterministic_id = _offer_id(assignment)
        if not self._path.exists() and not (
            self._heartbeat_registry.is_routing_eligible(
                assignment.worker_node_id,
            )
        ):
            raise DispatchWorkerUnavailableError(
                f"Worker {assignment.worker_node_id!r} is not available "
                "for a new dispatch offer",
            )

        def mutate(handle, snapshot):
            existing = snapshot.offers.get(deterministic_id)
            if existing is not None:
                if (
                    existing.assignment_id == assignment.assignment_id
                    and existing.expires_at == expires_at
                ):
                    return existing
                raise DispatchOfferConflictError(
                    "deterministic offer identity conflicts with existing evidence",
                )
            if not self._heartbeat_registry.is_routing_eligible(
                assignment.worker_node_id,
            ):
                raise DispatchWorkerUnavailableError(
                    f"Worker {assignment.worker_node_id!r} is not available "
                    "for a new dispatch offer",
                )
            task_key = (assignment.mission_id, assignment.task_id)
            if task_key in snapshot.offer_ids_by_task:
                raise DispatchOfferConflictError(
                    "mission/task already has a dispatch offer",
                )
            event = self._append_event(
                handle,
                snapshot,
                assignment=assignment,
                offer_id=deterministic_id,
                event_type=DispatchEventType.OFFER_CREATED,
                previous_state=None,
                new_state=DispatchStatus.OFFERED,
                actor_type=DispatchActorType.COORDINATOR,
                actor_node_id=self._coordinator_node_id,
                occurred_at=now,
                expires_at=expires_at,
                reason=None,
            )
            return self._offer_from_history((event,))

        return self._locked_mutation(mutate)

    def accept_offer(self, *, offer_id: str, actor_node_id: str):
        return self._terminal(
            offer_id,
            actor_node_id,
            status=DispatchStatus.ACCEPTED,
            event_type=DispatchEventType.OFFER_ACCEPTED,
            actor_type=DispatchActorType.WORKER,
            reason=None,
        )

    def reject_offer(self, *, offer_id: str, actor_node_id: str, reason: str):
        return self._terminal(
            offer_id,
            actor_node_id,
            status=DispatchStatus.REJECTED,
            event_type=DispatchEventType.OFFER_REJECTED,
            actor_type=DispatchActorType.WORKER,
            reason=_require_reason(reason),
        )

    def cancel_offer(self, *, offer_id: str, actor_node_id: str, reason: str):
        return self._terminal(
            offer_id,
            actor_node_id,
            status=DispatchStatus.CANCELLED,
            event_type=DispatchEventType.OFFER_CANCELLED,
            actor_type=DispatchActorType.COORDINATOR,
            reason=_require_reason(reason),
        )

    def _terminal(
        self,
        offer_id,
        actor_node_id,
        *,
        status,
        event_type,
        actor_type,
        reason,
    ):
        offer_id = _require_identifier(offer_id, "offer_id")
        actor_node_id = _require_identifier(actor_node_id, "actor_node_id")
        now = self._now()
        expired_result = None

        def mutate(handle, snapshot):
            nonlocal expired_result
            offer = self._get(snapshot, offer_id)
            expected_actor = (
                offer.worker_node_id
                if actor_type is DispatchActorType.WORKER
                else offer.coordinator_node_id
            )
            if actor_node_id != expected_actor:
                raise DispatchIdentityMismatchError(
                    "actor_node_id does not exactly match the authorized node",
                )
            if offer.status is not DispatchStatus.OFFERED:
                final = offer.audit_history[-1]
                if (
                    offer.status is status
                    and final.actor_node_id == actor_node_id
                    and final.reason == reason
                ):
                    return offer
                if offer.status is DispatchStatus.EXPIRED:
                    raise DispatchOfferExpiredError(offer)
                raise DispatchTerminalStateError(offer)
            if now >= offer.expires_at:
                expired_result = self._append_transition(
                    handle,
                    snapshot,
                    offer,
                    status=DispatchStatus.EXPIRED,
                    event_type=DispatchEventType.OFFER_EXPIRED,
                    actor_type=DispatchActorType.SYSTEM,
                    actor_node_id=None,
                    occurred_at=now,
                    reason=_EXPIRATION_REASON,
                )
                return expired_result
            return self._append_transition(
                handle,
                snapshot,
                offer,
                status=status,
                event_type=event_type,
                actor_type=actor_type,
                actor_node_id=actor_node_id,
                occurred_at=now,
                reason=reason,
            )

        result = self._locked_mutation(mutate)
        if expired_result is not None:
            raise DispatchOfferExpiredError(expired_result)
        return result

    def expire_due_offers(self):
        now = self._now()

        def mutate(handle, snapshot):
            expired = []
            current = snapshot
            for offer in sorted(
                snapshot.offers.values(),
                key=lambda item: item.offer_id,
            ):
                if offer.status is DispatchStatus.OFFERED and now >= offer.expires_at:
                    updated = self._append_transition(
                        handle,
                        current,
                        offer,
                        status=DispatchStatus.EXPIRED,
                        event_type=DispatchEventType.OFFER_EXPIRED,
                        actor_type=DispatchActorType.SYSTEM,
                        actor_node_id=None,
                        occurred_at=now,
                        reason=_EXPIRATION_REASON,
                    )
                    expired.append(updated)
                    current = self._snapshot_with_event(current, updated.audit_history[-1])
            return tuple(expired)

        return self._locked_mutation(mutate)

    def expire_offer(self, *, offer_id: str):
        """Expire one due offer, returning the same result on exact replay."""
        offer_id = _require_identifier(offer_id, "offer_id")
        now = self._now()

        def mutate(handle, snapshot):
            offer = self._get(snapshot, offer_id)
            if offer.status is DispatchStatus.EXPIRED:
                return offer
            if offer.status is not DispatchStatus.OFFERED:
                raise DispatchTerminalStateError(offer)
            if now < offer.expires_at:
                raise ValueError("offer has not reached its expiration deadline")
            return self._append_transition(
                handle,
                snapshot,
                offer,
                status=DispatchStatus.EXPIRED,
                event_type=DispatchEventType.OFFER_EXPIRED,
                actor_type=DispatchActorType.SYSTEM,
                actor_node_id=None,
                occurred_at=now,
                reason=_EXPIRATION_REASON,
            )

        return self._locked_mutation(mutate)

    def inspect_offer(self, offer_id):
        return self._get(self._read_snapshot(), _require_identifier(offer_id, "offer_id"))

    def list_offers(
        self,
        *,
        status_filter=None,
        mission_id=None,
        worker_node_id=None,
    ):
        if status_filter is not None:
            try:
                status_filter = DispatchStatus(status_filter)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid status_filter: {status_filter!r}") from exc
        snapshot = self._read_snapshot()
        offers = sorted(snapshot.offers.values(), key=lambda item: item.offer_id)
        return tuple(
            offer
            for offer in offers
            if (status_filter is None or offer.status is status_filter)
            and (mission_id is None or offer.mission_id == mission_id)
            and (worker_node_id is None or offer.worker_node_id == worker_node_id)
        )

    def audit_history(self, offer_id):
        return self.inspect_offer(offer_id).audit_history

    def audit_log(
        self,
        *,
        offer_id=None,
        task_id=None,
        mission_id=None,
        worker_node_id=None,
    ):
        events = self._read_snapshot().events
        return tuple(
            event
            for event in events
            if (offer_id is None or event.offer_id == offer_id)
            and (task_id is None or event.task_id == task_id)
            and (mission_id is None or event.mission_id == mission_id)
            and (worker_node_id is None or event.worker_node_id == worker_node_id)
        )

    def _now(self):
        now = normalize_dispatch_timestamp(self._clock(), "clock result")
        if self._last_observed_at is not None and now < self._last_observed_at:
            raise RuntimeError("dispatch clock must not move backwards")
        self._last_observed_at = now
        return now

    def _require_coordinator(self, actor_node_id):
        if _require_identifier(actor_node_id, "actor_node_id") != (
            self._coordinator_node_id
        ):
            raise DispatchIdentityMismatchError(
                "actor_node_id does not exactly match the dispatch coordinator",
            )

    def _locked_mutation(self, callback):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                snapshot = self._decode(handle.read())
                result = callback(handle, snapshot)
                return result
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_snapshot(self):
        if not self._path.exists():
            return _Snapshot({}, {}, (), _GENESIS_DIGEST)
        with self._path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return self._decode(handle.read())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _append_transition(
        self,
        handle,
        snapshot,
        offer,
        *,
        status,
        event_type,
        actor_type,
        actor_node_id,
        occurred_at,
        reason,
    ):
        assignment = type("_Assignment", (), {
            "assignment_id": offer.assignment_id,
            "assignment_fingerprint": offer.assignment_fingerprint,
            "mission_id": offer.mission_id,
            "task_id": offer.task_id,
            "coordinator_node_id": offer.coordinator_node_id,
            "worker_node_id": offer.worker_node_id,
            "required_capabilities": offer.required_capabilities,
            "authorization_metadata": offer.authorization_metadata,
            "approval_metadata": offer.approval_metadata,
        })()
        event = self._append_event(
            handle,
            snapshot,
            assignment=assignment,
            offer_id=offer.offer_id,
            event_type=event_type,
            previous_state=offer.status,
            new_state=status,
            actor_type=actor_type,
            actor_node_id=actor_node_id,
            occurred_at=occurred_at,
            expires_at=offer.expires_at,
            reason=reason,
        )
        return self._offer_from_history(offer.audit_history + (event,))

    def _append_event(
        self,
        handle,
        snapshot,
        *,
        assignment,
        offer_id,
        event_type,
        previous_state,
        new_state,
        actor_type,
        actor_node_id,
        occurred_at,
        expires_at,
        reason,
    ):
        record = {
            "schema_version": _SCHEMA_VERSION,
            "sequence": len(snapshot.events) + 1,
            "event_type": event_type.value,
            "offer_id": offer_id,
            "routing_assignment_id": assignment.assignment_id,
            "routing_assignment_fingerprint": assignment.assignment_fingerprint,
            "mission_id": assignment.mission_id,
            "task_id": assignment.task_id,
            "coordinator_node_id": assignment.coordinator_node_id,
            "worker_node_id": assignment.worker_node_id,
            "required_capabilities": list(assignment.required_capabilities),
            "previous_state": (
                None if previous_state is None else previous_state.value
            ),
            "new_state": new_state.value,
            "actor_type": actor_type.value,
            "actor_node_id": actor_node_id,
            "authorization_metadata": dict(assignment.authorization_metadata),
            "approval_metadata": dict(assignment.approval_metadata),
            "timestamp": _timestamp(occurred_at),
            "expires_at": _timestamp(expires_at),
            "reason": reason,
            "predecessor_digest": snapshot.last_digest,
        }
        record["resulting_digest"] = authentication_tag(
            self._integrity_key,
            _AUTHENTICATION_DOMAIN,
            _canonical(record),
        )
        encoded = _canonical(record) + b"\n"
        handle.seek(0, 2)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
        return self._event_from_record(record)

    def _decode(self, data):
        if not data:
            return _Snapshot({}, {}, (), _GENESIS_DIGEST)
        if not data.endswith(b"\n"):
            raise DispatchCorruptionError("dispatch evidence is incomplete")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DispatchCorruptionError("dispatch evidence is not UTF-8") from exc
        events = []
        predecessor = _GENESIS_DIGEST
        for expected_sequence, line in enumerate(text.splitlines(), 1):
            try:
                record = json.loads(line, object_pairs_hook=_no_duplicate_keys)
            except (json.JSONDecodeError, DispatchCorruptionError) as exc:
                raise DispatchCorruptionError("dispatch evidence is malformed") from exc
            self._validate_record(record, expected_sequence, predecessor)
            event = self._event_from_record(record)
            events.append(event)
            predecessor = event.resulting_digest
        return self._reconstruct(tuple(events), predecessor)

    def _validate_record(self, record, expected_sequence, predecessor):
        if not isinstance(record, dict) or set(record) != _FIELDS:
            raise DispatchCorruptionError("dispatch evidence schema is invalid")
        if record["schema_version"] != _SCHEMA_VERSION:
            raise DispatchCorruptionError("dispatch evidence schema is invalid")
        if not _is_int(record["sequence"]) or record["sequence"] != expected_sequence:
            raise DispatchCorruptionError("dispatch event sequence is broken")
        string_fields = (
            "event_type",
            "offer_id",
            "routing_assignment_id",
            "routing_assignment_fingerprint",
            "mission_id",
            "task_id",
            "coordinator_node_id",
            "worker_node_id",
            "new_state",
            "actor_type",
            "timestamp",
            "expires_at",
            "predecessor_digest",
            "resulting_digest",
        )
        if any(not isinstance(record[field], str) for field in string_fields):
            raise DispatchCorruptionError("dispatch evidence has invalid field types")
        if record["previous_state"] is not None and not isinstance(
            record["previous_state"],
            str,
        ):
            raise DispatchCorruptionError("dispatch evidence has invalid field types")
        if record["actor_node_id"] is not None and not isinstance(
            record["actor_node_id"],
            str,
        ):
            raise DispatchCorruptionError("dispatch evidence has invalid field types")
        if record["reason"] is not None and not isinstance(record["reason"], str):
            raise DispatchCorruptionError("dispatch evidence has invalid field types")
        if (
            not isinstance(record["required_capabilities"], list)
            or any(not isinstance(item, str) for item in record["required_capabilities"])
            or not isinstance(record["authorization_metadata"], dict)
            or not isinstance(record["approval_metadata"], dict)
        ):
            raise DispatchCorruptionError("dispatch evidence has invalid field types")
        authorization_metadata = record["authorization_metadata"]
        approval_metadata = record["approval_metadata"]

        if set(authorization_metadata) not in (
            {"level"},
            {"level", "execution_fingerprint"},
        ) or set(approval_metadata) != {"required"}:
            raise DispatchCorruptionError("dispatch metadata schema is invalid")

        if not isinstance(authorization_metadata["level"], str) or (
            not isinstance(approval_metadata["required"], bool)
        ):
            raise DispatchCorruptionError("dispatch metadata types are invalid")

        execution_fingerprint = authorization_metadata.get(
            "execution_fingerprint"
        )
        if execution_fingerprint is not None and (
            not isinstance(execution_fingerprint, str)
            or len(execution_fingerprint) != 64
            or any(
                character not in "0123456789abcdef"
                for character in execution_fingerprint
            )
        ):
            raise DispatchCorruptionError("dispatch metadata types are invalid")
        if record["predecessor_digest"] != predecessor:
            raise DispatchCorruptionError("dispatch digest chain is broken")
        unsigned = dict(record)
        resulting = unsigned.pop("resulting_digest")
        if not authenticates(
            self._integrity_key,
            _AUTHENTICATION_DOMAIN,
            _canonical(unsigned),
            resulting,
        ):
            raise DispatchCorruptionError(
                "dispatch event authentication tag is invalid",
            )

    def _event_from_record(self, record):
        try:
            return DispatchAuditEvent(
                schema_version=record["schema_version"],
                sequence=record["sequence"],
                event_type=record["event_type"],
                offer_id=record["offer_id"],
                routing_assignment_id=record["routing_assignment_id"],
                routing_assignment_fingerprint=record[
                    "routing_assignment_fingerprint"
                ],
                mission_id=record["mission_id"],
                task_id=record["task_id"],
                coordinator_node_id=record["coordinator_node_id"],
                worker_node_id=record["worker_node_id"],
                required_capabilities=tuple(record["required_capabilities"]),
                previous_state=record["previous_state"],
                new_state=record["new_state"],
                actor_type=record["actor_type"],
                actor_node_id=record["actor_node_id"],
                authorization_metadata=_pairs(record["authorization_metadata"]),
                approval_metadata=_pairs(record["approval_metadata"]),
                occurred_at=_parse_timestamp(record["timestamp"], "timestamp"),
                expires_at=_parse_timestamp(record["expires_at"], "expires_at"),
                reason=record["reason"],
                predecessor_digest=record["predecessor_digest"],
                resulting_digest=record["resulting_digest"],
            )
        except (TypeError, ValueError) as exc:
            raise DispatchCorruptionError(
                "dispatch event violates the state-machine schema",
            ) from exc

    def _reconstruct(self, events, last_digest):
        histories = {}
        task_ids = {}
        for event in events:
            history = histories.get(event.offer_id)
            if event.event_type is DispatchEventType.OFFER_CREATED:
                if history is not None:
                    raise DispatchCorruptionError("duplicate offer creation")
                key = (event.mission_id, event.task_id)
                if key in task_ids:
                    raise DispatchCorruptionError("conflicting offer creation")
                expected_offer_id = _offer_id(
                    type("_Assignment", (), {
                        "assignment_id": event.routing_assignment_id,
                        "assignment_fingerprint": event.routing_assignment_fingerprint,
                        "mission_id": event.mission_id,
                        "task_id": event.task_id,
                    })(),
                )
                if event.offer_id != expected_offer_id:
                    raise DispatchCorruptionError("offer identity is not deterministic")
                histories[event.offer_id] = [event]
                task_ids[key] = event.offer_id
                continue
            if history is None:
                raise DispatchCorruptionError("foreign-offer event")
            original = history[0]
            bound_identity = (
                event.routing_assignment_id,
                event.routing_assignment_fingerprint,
                event.mission_id,
                event.task_id,
                event.coordinator_node_id,
                event.worker_node_id,
                event.required_capabilities,
                event.authorization_metadata,
                event.approval_metadata,
                event.expires_at,
            )
            original_identity = (
                original.routing_assignment_id,
                original.routing_assignment_fingerprint,
                original.mission_id,
                original.task_id,
                original.coordinator_node_id,
                original.worker_node_id,
                original.required_capabilities,
                original.authorization_metadata,
                original.approval_metadata,
                original.expires_at,
            )
            if bound_identity != original_identity:
                raise DispatchCorruptionError("foreign-offer event identity")
            if len(history) != 1:
                raise DispatchCorruptionError("contradictory terminal events")
            if event.previous_state is not DispatchStatus.OFFERED:
                raise DispatchCorruptionError("impossible dispatch transition")
            if event.predecessor_digest != history[-1].resulting_digest:
                raise DispatchCorruptionError("offer digest predecessor is invalid")
            history.append(event)
        offers = {
            offer_id: self._offer_from_history(tuple(history))
            for offer_id, history in histories.items()
        }
        return _Snapshot(offers, task_ids, events, last_digest)

    def _offer_from_history(self, history):
        first = history[0]
        final = history[-1]
        authorization = dict(first.authorization_metadata)
        approval = dict(first.approval_metadata)
        try:
            authorization_level = AuthorizationLevel(authorization["level"])
        except (KeyError, ValueError) as exc:
            raise DispatchCorruptionError("authorization metadata is invalid") from exc
        status = final.new_state
        return DispatchOfferSnapshot(
            offer_id=first.offer_id,
            assignment_id=first.routing_assignment_id,
            assignment_fingerprint=first.routing_assignment_fingerprint,
            task_id=first.task_id,
            mission_id=first.mission_id,
            coordinator_node_id=first.coordinator_node_id,
            worker_node_id=first.worker_node_id,
            required_capabilities=first.required_capabilities,
            authorization_level=authorization_level,
            approval_required=approval["required"],
            authorization_metadata=first.authorization_metadata,
            approval_metadata=first.approval_metadata,
            created_at=first.occurred_at,
            expires_at=first.expires_at,
            audit_history=history,
            status=status,
            terminal_at=None if status is DispatchStatus.OFFERED else final.occurred_at,
            resolution_reason=final.reason,
        )

    def _snapshot_with_event(self, snapshot, event):
        return self._reconstruct(snapshot.events + (event,), event.resulting_digest)

    @staticmethod
    def _get(snapshot, offer_id):
        try:
            return snapshot.offers[offer_id]
        except KeyError as exc:
            raise DispatchOfferNotFoundError(
                f"Unknown dispatch offer: {offer_id!r}",
            ) from exc
