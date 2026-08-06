"""Append-only authenticated worker heartbeat and lease registry."""

import fcntl
import hmac
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from federation.heartbeat import (
    GENESIS_AUTHENTICATION_TAG,
    HEARTBEAT_SUBMISSION_DOMAIN,
    Heartbeat,
    canonical_json,
    format_timestamp,
    parse_timestamp,
)
from federation.integrity import authentication_tag, authenticates, require_integrity_key
from federation.registry import NodeRegistry
from federation.worker_lease import HEARTBEAT_INTERVAL, WORKER_LEASE_DURATION
from federation.worker_liveness import LivenessState, PowerCapability, PowerState, WorkerLease


_SCHEMA_VERSION = 1
_EVENT_DOMAIN = b"raghub.worker-heartbeat-event.v1"
_FIELDS = {
    "schema_version",
    "event_sequence",
    "worker_id",
    "registry_id",
    "heartbeat_sequence",
    "session_id",
    "worker_timestamp",
    "health",
    "power_capabilities",
    "requested_power_state",
    "sleep_reason",
    "expected_wake_time",
    "wake_method",
    "active_work_checkpointed",
    "previous_authentication_tag",
    "submission_authentication_tag",
    "controller_received_at",
    "lease_expires_at",
    "predecessor_tag",
    "authentication_tag",
}


class HeartbeatError(Exception):
    """Base heartbeat evidence failure."""


class HeartbeatAuthenticationError(HeartbeatError):
    """Raised when submitted heartbeat identity or authentication is invalid."""


class HeartbeatConflictError(HeartbeatError):
    """Raised when submitted evidence conflicts with accepted history."""


class HeartbeatCorruptionError(HeartbeatError):
    """Raised when durable heartbeat evidence cannot be trusted."""


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise HeartbeatCorruptionError("heartbeat evidence has duplicate JSON keys")
        result[key] = value
    return result


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _state(lease, at):
    if lease.requested_power_state in {
        PowerState.SLEEP,
        PowerState.HIBERNATE,
        PowerState.SHUTDOWN,
    }:
        return LivenessState.INTENTIONALLY_SLEEPING
    if lease.requested_power_state is PowerState.WAKING:
        return LivenessState.WAKING
    if lease.health == "unhealthy" or at >= lease.lease_expires_at:
        return LivenessState.OFFLINE
    if lease.health == "degraded" or (
        at - lease.controller_received_at > HEARTBEAT_INTERVAL
    ):
        return LivenessState.STALE
    return LivenessState.ONLINE


class HeartbeatRegistry:
    """Durable controller-authoritative heartbeat evidence."""

    def __init__(
        self,
        path,
        *,
        registry_id: str,
        node_registry: NodeRegistry,
        integrity_key: bytes,
        clock=None,
    ):
        if not isinstance(registry_id, str) or not registry_id.strip():
            raise ValueError("registry_id must be a non-empty string")
        if not isinstance(node_registry, NodeRegistry):
            raise TypeError("node_registry must be a NodeRegistry")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self.path = Path(path)
        self.registry_id = registry_id
        self.node_registry = node_registry
        self._integrity_key = require_integrity_key(integrity_key)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _integrity_key_matches(self, integrity_key: bytes) -> bool:
        candidate = require_integrity_key(integrity_key)
        return hmac.compare_digest(self._integrity_key, candidate)

    def record(self, heartbeat: Heartbeat) -> WorkerLease:
        if not isinstance(heartbeat, Heartbeat):
            raise TypeError("heartbeat must be a Heartbeat")
        if heartbeat.registry_id != self.registry_id:
            raise HeartbeatAuthenticationError(
                "heartbeat registry identity does not match",
            )
        if self.node_registry.get(heartbeat.worker_id) is None:
            raise HeartbeatAuthenticationError(
                "heartbeat worker is not registered",
            )
        if not authenticates(
            self._integrity_key,
            HEARTBEAT_SUBMISSION_DOMAIN,
            canonical_json(heartbeat.unsigned_payload()),
            heartbeat.authentication_tag,
        ):
            raise HeartbeatAuthenticationError(
                "heartbeat authentication tag is invalid",
            )
        received_at = self._now()
        if heartbeat.worker_timestamp > received_at:
            raise ValueError("worker_timestamp cannot be in the controller's future")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                events = self._decode(handle.read())
                self._reject_clock_rollback(received_at, events)
                worker_events = [
                    event for event in events if event.worker_id == heartbeat.worker_id
                ]
                if worker_events:
                    latest = worker_events[-1]
                    if heartbeat.sequence == latest.sequence:
                        if self._submission_matches(heartbeat, latest):
                            return self._at(latest, received_at)
                        raise HeartbeatConflictError(
                            "duplicate heartbeat sequence has changed content",
                        )
                    if heartbeat.sequence < latest.sequence:
                        raise HeartbeatConflictError(
                            "heartbeat sequence is replayed or out of order",
                        )
                    if heartbeat.sequence != latest.sequence + 1:
                        raise HeartbeatConflictError(
                            "heartbeat sequence must be contiguous",
                        )
                    if (
                        heartbeat.previous_authentication_tag
                        != latest.authentication_tag
                    ):
                        raise HeartbeatConflictError(
                            "previous authentication tag does not match history",
                        )
                    if (
                        heartbeat.session_id == latest.session_id
                        and heartbeat.worker_timestamp < latest.worker_timestamp
                    ):
                        raise HeartbeatConflictError(
                            "worker timestamp moved backwards within one session",
                        )
                else:
                    if heartbeat.sequence != 1:
                        raise HeartbeatConflictError(
                            "first heartbeat sequence must be 1",
                        )
                    if (
                        heartbeat.previous_authentication_tag
                        != GENESIS_AUTHENTICATION_TAG
                    ):
                        raise HeartbeatConflictError(
                            "first heartbeat must use the genesis authentication tag",
                        )

                predecessor = (
                    GENESIS_AUTHENTICATION_TAG
                    if not events
                    else events[-1].authentication_tag
                )
                record = {
                    "schema_version": _SCHEMA_VERSION,
                    "event_sequence": len(events) + 1,
                    **heartbeat.unsigned_payload(),
                    "heartbeat_sequence": heartbeat.sequence,
                    "submission_authentication_tag": heartbeat.authentication_tag,
                    "controller_received_at": format_timestamp(received_at),
                    "lease_expires_at": format_timestamp(
                        received_at + WORKER_LEASE_DURATION,
                    ),
                    "predecessor_tag": predecessor,
                }
                record.pop("sequence")
                record["authentication_tag"] = authentication_tag(
                    self._integrity_key,
                    _EVENT_DOMAIN,
                    canonical_json(record),
                )
                handle.seek(0, 2)
                handle.write(canonical_json(record) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
                return self._at(self._from_record(record), received_at)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def inspect(self, worker_id: str, *, at=None):
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string")
        events = self._read()
        now = self._inspection_time(at)
        self._reject_clock_rollback(now, events)
        matches = [event for event in events if event.worker_id == worker_id]
        return None if not matches else self._at(matches[-1], now)

    def list_workers(self, *, at=None):
        events = self._read()
        now = self._inspection_time(at)
        self._reject_clock_rollback(now, events)
        latest = {}
        for event in events:
            latest[event.worker_id] = event
        return tuple(self._at(latest[key], now) for key in sorted(latest))

    def history(self, worker_id: str):
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string")
        events = self._read()
        return tuple(
            self._at(event, event.controller_received_at)
            for event in events
            if event.worker_id == worker_id
        )

    def is_routing_eligible(self, worker_id: str):
        lease = self.inspect(worker_id)
        return lease is not None and lease.routing_eligible

    def _inspection_time(self, at):
        return self._now() if at is None else self._normalize_controller_time(at)

    def _now(self):
        return self._normalize_controller_time(self._clock())

    @staticmethod
    def _normalize_controller_time(value):
        if not isinstance(value, datetime):
            raise TypeError("controller clock must return a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("controller clock must return a timezone-aware timestamp")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _reject_clock_rollback(now, events):
        if events and now < events[-1].controller_received_at:
            raise HeartbeatConflictError("controller clock moved backwards")

    def _read(self):
        if not self.path.exists():
            return ()
        with self.path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return self._decode(handle.read())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _decode(self, data):
        if not data:
            return ()
        if not data.endswith(b"\n"):
            raise HeartbeatCorruptionError("heartbeat evidence is incomplete")
        try:
            lines = data.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise HeartbeatCorruptionError("heartbeat evidence is not UTF-8") from exc
        events = []
        predecessor = GENESIS_AUTHENTICATION_TAG
        latest_by_worker = {}
        for expected_sequence, line in enumerate(lines, 1):
            try:
                record = json.loads(line, object_pairs_hook=_no_duplicate_keys)
            except (json.JSONDecodeError, HeartbeatCorruptionError) as exc:
                raise HeartbeatCorruptionError(
                    "heartbeat evidence is malformed",
                ) from exc
            self._validate_record(record, expected_sequence, predecessor)
            event = self._from_record(record)
            previous = latest_by_worker.get(event.worker_id)
            if previous is None:
                if (
                    event.sequence != 1
                    or event.previous_authentication_tag
                    != GENESIS_AUTHENTICATION_TAG
                ):
                    raise HeartbeatCorruptionError(
                        "heartbeat worker history has an invalid origin",
                    )
            elif (
                event.sequence != previous.sequence + 1
                or event.previous_authentication_tag
                != previous.authentication_tag
            ):
                raise HeartbeatCorruptionError(
                    "heartbeat worker history is contradictory",
                )
            if events and event.controller_received_at < events[-1].controller_received_at:
                raise HeartbeatCorruptionError(
                    "heartbeat controller time moved backwards",
                )
            latest_by_worker[event.worker_id] = event
            events.append(event)
            predecessor = event.authentication_tag
        return tuple(events)

    def _validate_record(self, record, expected_sequence, predecessor):
        if not isinstance(record, dict) or set(record) != _FIELDS:
            raise HeartbeatCorruptionError("heartbeat evidence schema is invalid")
        if record["schema_version"] != _SCHEMA_VERSION:
            raise HeartbeatCorruptionError("heartbeat evidence schema is invalid")
        if (
            not _is_int(record["event_sequence"])
            or record["event_sequence"] != expected_sequence
            or not _is_int(record["heartbeat_sequence"])
            or record["heartbeat_sequence"] < 1
        ):
            raise HeartbeatCorruptionError("heartbeat evidence sequence is invalid")
        if record["predecessor_tag"] != predecessor:
            raise HeartbeatCorruptionError("heartbeat authentication chain is broken")
        unsigned = dict(record)
        claimed = unsigned.pop("authentication_tag", None)
        if not authenticates(
            self._integrity_key,
            _EVENT_DOMAIN,
            canonical_json(unsigned),
            claimed,
        ):
            raise HeartbeatCorruptionError(
                "heartbeat evidence authentication tag is invalid",
            )
        try:
            heartbeat = self._heartbeat_from_record(record)
        except (TypeError, ValueError) as exc:
            raise HeartbeatCorruptionError(
                "heartbeat evidence values are invalid",
            ) from exc
        if heartbeat.registry_id != self.registry_id:
            raise HeartbeatCorruptionError(
                "heartbeat registry identity is invalid",
            )
        if not authenticates(
            self._integrity_key,
            HEARTBEAT_SUBMISSION_DOMAIN,
            canonical_json(heartbeat.unsigned_payload()),
            heartbeat.authentication_tag,
        ):
            raise HeartbeatCorruptionError(
                "heartbeat submission authentication tag is invalid",
            )
        try:
            received = parse_timestamp(
                record["controller_received_at"],
                "controller_received_at",
            )
            expires = parse_timestamp(record["lease_expires_at"], "lease_expires_at")
        except ValueError as exc:
            raise HeartbeatCorruptionError(
                "heartbeat controller timestamps are invalid",
            ) from exc
        if expires != received + WORKER_LEASE_DURATION:
            raise HeartbeatCorruptionError("heartbeat lease expiration is invalid")
        if heartbeat.worker_timestamp > received:
            raise HeartbeatCorruptionError("heartbeat worker timestamp is impossible")

    @staticmethod
    def _heartbeat_from_record(record):
        return Heartbeat(
            worker_id=record["worker_id"],
            registry_id=record["registry_id"],
            sequence=record["heartbeat_sequence"],
            session_id=record["session_id"],
            worker_timestamp=parse_timestamp(
                record["worker_timestamp"],
                "worker_timestamp",
            ),
            health=record["health"],
            power_capabilities=tuple(record["power_capabilities"]),
            requested_power_state=record["requested_power_state"],
            sleep_reason=record["sleep_reason"],
            expected_wake_time=(
                None
                if record["expected_wake_time"] is None
                else parse_timestamp(
                    record["expected_wake_time"],
                    "expected_wake_time",
                )
            ),
            wake_method=record["wake_method"],
            active_work_checkpointed=record["active_work_checkpointed"],
            previous_authentication_tag=record["previous_authentication_tag"],
            authentication_tag=record["submission_authentication_tag"],
        )

    def _from_record(self, record):
        heartbeat = self._heartbeat_from_record(record)
        received = parse_timestamp(
            record["controller_received_at"],
            "controller_received_at",
        )
        expires = parse_timestamp(record["lease_expires_at"], "lease_expires_at")
        placeholder = WorkerLease(
            worker_id=heartbeat.worker_id,
            registry_id=heartbeat.registry_id,
            sequence=heartbeat.sequence,
            session_id=heartbeat.session_id,
            worker_timestamp=heartbeat.worker_timestamp,
            health=heartbeat.health,
            power_capabilities=heartbeat.power_capabilities,
            requested_power_state=heartbeat.requested_power_state,
            sleep_reason=heartbeat.sleep_reason,
            expected_wake_time=heartbeat.expected_wake_time,
            wake_method=heartbeat.wake_method,
            active_work_checkpointed=heartbeat.active_work_checkpointed,
            previous_authentication_tag=heartbeat.previous_authentication_tag,
            submission_authentication_tag=heartbeat.authentication_tag,
            controller_received_at=received,
            lease_expires_at=expires,
            authentication_tag=record["authentication_tag"],
            state=LivenessState.OFFLINE,
            routing_eligible=False,
        )
        return self._at(placeholder, received)

    @staticmethod
    def _at(lease, at):
        state = _state(lease, at)
        return replace(
            lease,
            state=state,
            routing_eligible=state is LivenessState.ONLINE,
        )

    @staticmethod
    def _submission_matches(heartbeat, lease):
        return (
            heartbeat.unsigned_payload()
            == {
                "worker_id": lease.worker_id,
                "registry_id": lease.registry_id,
                "sequence": lease.sequence,
                "session_id": lease.session_id,
                "worker_timestamp": format_timestamp(lease.worker_timestamp),
                "health": lease.health,
                "power_capabilities": [
                    capability.value for capability in lease.power_capabilities
                ],
                "requested_power_state": lease.requested_power_state.value,
                "sleep_reason": lease.sleep_reason,
                "expected_wake_time": (
                    None
                    if lease.expected_wake_time is None
                    else format_timestamp(lease.expected_wake_time)
                ),
                "wake_method": lease.wake_method,
                "active_work_checkpointed": lease.active_work_checkpointed,
                "previous_authentication_tag": lease.previous_authentication_tag,
            }
            and heartbeat.authentication_tag == lease.submission_authentication_tag
        )
