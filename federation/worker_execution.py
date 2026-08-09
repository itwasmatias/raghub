"""Durable, transport-neutral worker execution lifecycle protocol."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import RLock

from federation.dispatch_offer import DispatchStatus
from federation.integrity import authentication_tag, authenticates, require_integrity_key
from federation.task_dispatcher import DispatchError, TaskDispatchCoordinator
from federation.task_request import AuthorizationLevel


_SCHEMA_VERSION = 1
_DOMAIN = b"raghub.worker-execution.v1"
_FINGERPRINT_KEY = "execution_fingerprint"


class WorkerExecutionError(Exception):
    """Base protocol error."""


class WorkerExecutionConflictError(WorkerExecutionError):
    """Existing authority conflicts with the requested operation."""


class WorkerExecutionIdentityError(WorkerExecutionError):
    """Actor or upstream evidence identity is not authoritative."""


class WorkerExecutionStateError(WorkerExecutionError):
    """Requested lifecycle transition is not permitted."""


class WorkerExecutionCorruptionError(WorkerExecutionError):
    """Durable execution evidence cannot be trusted."""


class WorkerExecutionStatus(str, Enum):
    ACCEPTED = "accepted"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


TERMINAL_WORKER_EXECUTION_STATUSES = frozenset(
    {WorkerExecutionStatus.SUCCEEDED, WorkerExecutionStatus.FAILED,
     WorkerExecutionStatus.RECONCILIATION_REQUIRED}
)


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{name} must not contain surrounding whitespace")
    return value


def _fingerprint(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ValueError("execution_fingerprint must be lowercase SHA-256 hex")
    return value


def _timestamp(value, name="timestamp"):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _metadata_fingerprint(offer):
    found = []
    for metadata in (offer.authorization_metadata, offer.approval_metadata):
        for key, value in metadata:
            if key == _FINGERPRINT_KEY:
                found.append(_fingerprint(value))
    if not found:
        return None
    if len(set(found)) != 1:
        raise WorkerExecutionIdentityError("upstream execution fingerprints conflict")
    return found[0]


@dataclass(frozen=True, slots=True)
class WorkerExecutionRequest:
    execution_attempt_id: str
    mission_id: str
    task_id: str
    assignment_id: str
    dispatch_offer_id: str
    worker_node_id: str
    coordinator_node_id: str
    authorization_level: AuthorizationLevel
    approval_required: bool
    expected_result: str
    execution_fingerprint: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class WorkerExecutionResultEnvelope:
    outcome: str
    evidence_references: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.outcome, str) or not self.outcome.strip():
            raise ValueError("outcome must be a non-empty string")
        refs = tuple(self.evidence_references)
        for ref in refs:
            _identifier(ref, "evidence_reference")
        object.__setattr__(self, "evidence_references", refs)


@dataclass(frozen=True, slots=True)
class WorkerExecutionAttempt:
    request: WorkerExecutionRequest
    status: WorkerExecutionStatus
    claimed_at: datetime | None = None
    started_at: datetime | None = None
    terminal_at: datetime | None = None
    result: WorkerExecutionResultEnvelope | None = None
    failure_reason: str | None = None


class WorkerExecutionCoordinator:
    """Persist and validate lifecycle evidence without executing any payload."""

    def __init__(self, coordinator_node_id, *, dispatch_coordinator,
                 store_path, integrity_key, clock=None, approval_verifier=None):
        self.coordinator_node_id = _identifier(coordinator_node_id, "coordinator_node_id")
        if not isinstance(dispatch_coordinator, TaskDispatchCoordinator):
            raise TypeError("dispatch_coordinator must be a TaskDispatchCoordinator")
        self.dispatch = dispatch_coordinator
        self.path = Path(store_path)
        self._key = require_integrity_key(integrity_key)
        if not dispatch_coordinator.assignment_store._integrity_key_matches(self._key):
            raise ValueError("execution and upstream evidence must use the same integrity key")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if not callable(self._clock):
            raise TypeError("clock must be callable")
        if approval_verifier is not None and not callable(approval_verifier):
            raise TypeError("approval_verifier must be callable")
        self._approval_verifier = approval_verifier
        self._thread_lock = RLock()

    def register(self, *, dispatch_offer_id, actor_node_id, expected_result):
        """Register the coordinator-owned request represented by an accepted offer."""
        _identifier(dispatch_offer_id, "dispatch_offer_id")
        if _identifier(actor_node_id, "actor_node_id") != self.coordinator_node_id:
            raise WorkerExecutionIdentityError("only the coordinator may register execution")
        if not isinstance(expected_result, str):
            raise TypeError("expected_result must be a string")
        offer = self._accepted_offer(dispatch_offer_id)
        fingerprint = _metadata_fingerprint(offer)
        attempt_id = "execution-" + hashlib.sha256(
            _canonical({"offer_id": offer.offer_id, "assignment_id": offer.assignment_id,
                        "worker_node_id": offer.worker_node_id,
                        "execution_fingerprint": fingerprint,
                        "expected_result": expected_result})
        ).hexdigest()
        now = self._now()
        payload = {
            "execution_attempt_id": attempt_id, "mission_id": offer.mission_id,
            "task_id": offer.task_id, "assignment_id": offer.assignment_id,
            "dispatch_offer_id": offer.offer_id, "worker_node_id": offer.worker_node_id,
            "coordinator_node_id": offer.coordinator_node_id,
            "authorization_level": offer.authorization_level.value,
            "approval_required": offer.approval_required,
            "expected_result": expected_result, "execution_fingerprint": fingerprint,
            "created_at": now.isoformat(),
        }
        return self._mutate(lambda records: self._register(records, payload, now))

    def claim(self, *, execution_attempt_id, actor_node_id):
        return self._transition(execution_attempt_id, actor_node_id,
                                WorkerExecutionStatus.CLAIMED)

    def start(self, *, execution_attempt_id, actor_node_id):
        return self._transition(execution_attempt_id, actor_node_id,
                                WorkerExecutionStatus.RUNNING)

    def succeed(self, *, execution_attempt_id, actor_node_id, result):
        if not isinstance(result, WorkerExecutionResultEnvelope):
            raise TypeError("result must be a WorkerExecutionResultEnvelope")
        return self._transition(execution_attempt_id, actor_node_id,
                                WorkerExecutionStatus.SUCCEEDED, result=result)

    def fail(self, *, execution_attempt_id, actor_node_id, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        return self._transition(execution_attempt_id, actor_node_id,
                                WorkerExecutionStatus.FAILED, reason=reason)

    def reconcile_interrupted(self, *, execution_attempt_id, actor_node_id):
        if _identifier(actor_node_id, "actor_node_id") != self.coordinator_node_id:
            raise WorkerExecutionIdentityError("only the coordinator may reconcile execution")
        return self._transition(execution_attempt_id, actor_node_id,
                                WorkerExecutionStatus.RECONCILIATION_REQUIRED,
                                coordinator_transition=True,
                                reason="running execution outcome is ambiguous after interruption")

    def inspect(self, execution_attempt_id):
        execution_attempt_id = _identifier(execution_attempt_id, "execution_attempt_id")
        attempts = self._attempts(self._read())
        try:
            return attempts[execution_attempt_id]
        except KeyError as exc:
            raise WorkerExecutionIdentityError("unknown execution attempt") from exc

    def list_attempts(self):
        return tuple(sorted(self._attempts(self._read()).values(),
                            key=lambda item: item.request.execution_attempt_id))

    def _accepted_offer(self, offer_id):
        try:
            offer = self.dispatch.inspect_offer(offer_id)
            assignment = self.dispatch.assignment_store.resolve(offer.assignment_id)
        except Exception as exc:
            if isinstance(exc, WorkerExecutionError):
                raise
            raise WorkerExecutionIdentityError("authoritative dispatch evidence unavailable") from exc
        if offer.status is not DispatchStatus.ACCEPTED:
            raise WorkerExecutionStateError("dispatch offer is not accepted")
        if offer.coordinator_node_id != self.coordinator_node_id:
            raise WorkerExecutionIdentityError("foreign coordinator evidence rejected")
        if (assignment.assignment_fingerprint != offer.assignment_fingerprint or
                assignment.mission_id != offer.mission_id or assignment.task_id != offer.task_id or
                assignment.worker_node_id != offer.worker_node_id):
            raise WorkerExecutionIdentityError("assignment and offer evidence do not compose")
        return offer

    def _register(self, records, payload, now):
        attempts = self._attempts(records)
        by_offer = {a.request.dispatch_offer_id: a for a in attempts.values()}
        existing = by_offer.get(payload["dispatch_offer_id"])
        if existing:
            comparable = self._request_payload(existing.request)
            comparable.pop("created_at")
            proposed = dict(payload)
            proposed.pop("created_at")
            if comparable == proposed:
                return existing, None
            raise WorkerExecutionConflictError("dispatch offer already has conflicting execution authority")
        record = self._event(payload, WorkerExecutionStatus.ACCEPTED, now, len(records) + 1)
        return self._attempts(records + [record])[payload["execution_attempt_id"]], record

    def _transition(self, attempt_id, actor, status, *, result=None, reason=None,
                    coordinator_transition=False):
        attempt_id = _identifier(attempt_id, "execution_attempt_id")
        actor = _identifier(actor, "actor_node_id")
        now = self._now()
        def action(records):
            attempts = self._attempts(records)
            if attempt_id not in attempts:
                raise WorkerExecutionIdentityError("unknown execution attempt")
            current = attempts[attempt_id]
            expected_actor = (current.request.coordinator_node_id if coordinator_transition
                              else current.request.worker_node_id)
            if actor != expected_actor:
                raise WorkerExecutionIdentityError("actor is not authorized for this execution")
            expected = {WorkerExecutionStatus.CLAIMED: WorkerExecutionStatus.ACCEPTED,
                        WorkerExecutionStatus.RUNNING: WorkerExecutionStatus.CLAIMED,
                        WorkerExecutionStatus.SUCCEEDED: WorkerExecutionStatus.RUNNING,
                        WorkerExecutionStatus.FAILED: WorkerExecutionStatus.RUNNING,
                        WorkerExecutionStatus.RECONCILIATION_REQUIRED: WorkerExecutionStatus.RUNNING}[status]
            if current.status is status:
                if current.result == result and current.failure_reason == reason:
                    return current, None
                raise WorkerExecutionConflictError("conflicting duplicate transition")
            if current.status is not expected:
                raise WorkerExecutionStateError("execution transition is not permitted")
            if status is WorkerExecutionStatus.CLAIMED and current.request.approval_required:
                if self._approval_verifier is None or not self._approval_verifier(current.request):
                    raise WorkerExecutionStateError("authoritative approval is required")
            event = self._event(self._request_payload(current.request), status, now,
                                len(records) + 1, result=result, reason=reason)
            return self._attempts(records + [event])[attempt_id], event
        return self._mutate(action)

    def _event(self, request, status, occurred_at, sequence, result=None, reason=None):
        record = {"schema_version": _SCHEMA_VERSION, "sequence": sequence,
                  "previous_tag": None, "status": status.value,
                  "occurred_at": occurred_at.isoformat(), "request": request,
                  "result": None if result is None else {
                      "outcome": result.outcome,
                      "evidence_references": list(result.evidence_references)},
                  "failure_reason": reason}
        return record

    def _mutate(self, operation):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock, self.path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0); records = self._decode(handle.read())
                value, record = operation(records)
                if record is not None:
                    record["previous_tag"] = records[-1]["authentication_tag"] if records else None
                    unsigned = dict(record)
                    record["authentication_tag"] = authentication_tag(self._key, _DOMAIN, _canonical(unsigned))
                    handle.seek(0, 2); handle.write(_canonical(record) + b"\n")
                    handle.flush(); os.fsync(handle.fileno())
                return value
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self):
        if not self.path.exists(): return []
        with self.path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try: return self._decode(handle.read())
            finally: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _decode(self, data):
        if not data: return []
        if not data.endswith(b"\n"):
            raise WorkerExecutionCorruptionError("execution evidence is incomplete")
        records = []; previous = None
        for sequence, line in enumerate(data.splitlines(), 1):
            try:
                pairs = json.loads(line, object_pairs_hook=lambda p: self._unique(p))
            except Exception as exc:
                raise WorkerExecutionCorruptionError("execution evidence is corrupt") from exc
            required = {"schema_version","sequence","previous_tag","status","occurred_at",
                        "request","result","failure_reason","authentication_tag"}
            if not isinstance(pairs, dict) or set(pairs) != required or pairs["schema_version"] != 1 or pairs["sequence"] != sequence or pairs["previous_tag"] != previous:
                raise WorkerExecutionCorruptionError("execution evidence schema or chain is invalid")
            unsigned = dict(pairs); tag = unsigned.pop("authentication_tag")
            if not authenticates(self._key, _DOMAIN, _canonical(unsigned), tag):
                raise WorkerExecutionCorruptionError("execution evidence authentication failed")
            previous = tag; pairs["authentication_tag"] = tag; records.append(pairs)
        self._attempts(records)
        return records

    @staticmethod
    def _unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise WorkerExecutionCorruptionError("duplicate JSON key")
            result[key] = value
        return result

    def _attempts(self, records):
        attempts = {}
        transitions = {WorkerExecutionStatus.ACCEPTED: None,
                       WorkerExecutionStatus.CLAIMED: WorkerExecutionStatus.ACCEPTED,
                       WorkerExecutionStatus.RUNNING: WorkerExecutionStatus.CLAIMED,
                       WorkerExecutionStatus.SUCCEEDED: WorkerExecutionStatus.RUNNING,
                       WorkerExecutionStatus.FAILED: WorkerExecutionStatus.RUNNING,
                       WorkerExecutionStatus.RECONCILIATION_REQUIRED: WorkerExecutionStatus.RUNNING}
        for record in records:
            try:
                request = self._request(record["request"])
                status = WorkerExecutionStatus(record["status"])
                occurred = datetime.fromisoformat(record["occurred_at"])
                _timestamp(occurred)
            except Exception as exc:
                raise WorkerExecutionCorruptionError("execution evidence payload is invalid") from exc
            existing = attempts.get(request.execution_attempt_id)
            if (existing is None) != (status is WorkerExecutionStatus.ACCEPTED):
                raise WorkerExecutionCorruptionError("execution lifecycle is ambiguous")
            if existing and existing.request != request:
                raise WorkerExecutionCorruptionError("execution request mutated")
            if existing and existing.status is not transitions[status]:
                raise WorkerExecutionCorruptionError("execution lifecycle order is invalid")
            if existing is None and request.created_at != occurred:
                raise WorkerExecutionCorruptionError("request creation timestamp is inconsistent")
            result = None
            if record["result"] is not None:
                if set(record["result"]) != {"outcome","evidence_references"}:
                    raise WorkerExecutionCorruptionError("result schema is invalid")
                result = WorkerExecutionResultEnvelope(record["result"]["outcome"], tuple(record["result"]["evidence_references"]))
            if (result is not None) != (status is WorkerExecutionStatus.SUCCEEDED):
                raise WorkerExecutionCorruptionError("result does not match execution status")
            reason_required = status in {
                WorkerExecutionStatus.FAILED,
                WorkerExecutionStatus.RECONCILIATION_REQUIRED,
            }
            if reason_required != (
                isinstance(record["failure_reason"], str)
                and bool(record["failure_reason"].strip())
            ):
                raise WorkerExecutionCorruptionError("failure reason does not match execution status")
            attempts[request.execution_attempt_id] = WorkerExecutionAttempt(
                request, status,
                claimed_at=occurred if status is WorkerExecutionStatus.CLAIMED else (existing.claimed_at if existing else None),
                started_at=occurred if status is WorkerExecutionStatus.RUNNING else (existing.started_at if existing else None),
                terminal_at=occurred if status in TERMINAL_WORKER_EXECUTION_STATUSES else None,
                result=result, failure_reason=record["failure_reason"])
        return attempts

    def _request(self, value):
        fields = {"execution_attempt_id","mission_id","task_id","assignment_id","dispatch_offer_id",
                  "worker_node_id","coordinator_node_id","authorization_level","approval_required",
                  "expected_result","execution_fingerprint","created_at"}
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("request schema invalid")
        for name in fields - {"authorization_level","approval_required","expected_result","execution_fingerprint","created_at"}:
            _identifier(value[name], name)
        if not isinstance(value["approval_required"], bool) or not isinstance(value["expected_result"], str):
            raise TypeError("request metadata invalid")
        normalized = dict(value)
        normalized["authorization_level"] = AuthorizationLevel(value["authorization_level"])
        normalized["execution_fingerprint"] = _fingerprint(value["execution_fingerprint"])
        normalized["created_at"] = _timestamp(datetime.fromisoformat(value["created_at"]), "created_at")
        return WorkerExecutionRequest(**normalized)

    @staticmethod
    def _request_payload(request):
        return {"execution_attempt_id": request.execution_attempt_id,
                "mission_id": request.mission_id, "task_id": request.task_id,
                "assignment_id": request.assignment_id, "dispatch_offer_id": request.dispatch_offer_id,
                "worker_node_id": request.worker_node_id, "coordinator_node_id": request.coordinator_node_id,
                "authorization_level": request.authorization_level.value,
                "approval_required": request.approval_required,
                "expected_result": request.expected_result,
                "execution_fingerprint": request.execution_fingerprint,
                "created_at": request.created_at.isoformat()}

    def _now(self):
        return _timestamp(self._clock(), "clock result")
