"""Durable, deterministic mission checkpoint and advisory resume contracts."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from threading import RLock

from federation.integrity import authentication_tag, authenticates, require_integrity_key
from federation.dispatch_offer import DispatchOfferSnapshot
from federation.routing_decision import RoutingDecision, RoutingOutcome
from federation.worker_execution import WorkerExecutionAttempt, WorkerExecutionStatus
from research_mission.models import (
    ResearchMission,
    ResearchMissionPlan,
    ResearchMissionStatus,
    ResearchTaskSpec,
)
from research_mission.result_runtime import ResearchMissionResultState
from research_mission.results import ResearchTaskResultStatus


_SCHEMA = "raghub.mission-checkpoint.v0.1"
_DOMAIN = b"raghub.mission-checkpoint.v0.1"
_GENESIS = "0" * 64


class MissionCheckpointError(Exception):
    """Base checkpoint error."""


class MissionCheckpointConflictError(MissionCheckpointError):
    """Checkpoint identity or current plan conflicts with durable authority."""


class MissionCheckpointCorruptionError(MissionCheckpointError):
    """Durable checkpoint evidence cannot be trusted."""


class MissionCheckpointNotFoundError(MissionCheckpointError):
    """No authoritative checkpoint exists for an identity."""


class ResumeClassification(str, Enum):
    SAFE_TO_RESUME = "safe_to_resume"
    ALREADY_COMPLETED = "already_completed"
    PENDING = "pending"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    BLOCKED_ON_APPROVAL = "blocked_on_approval"
    FAILED_TERMINAL = "failed_terminal"


@dataclass(frozen=True, slots=True)
class TaskCheckpoint:
    task_id: str
    sequence: int
    classification: ResumeClassification
    assignment_id: str | None
    dispatch_offer_id: str | None
    execution_attempt_id: str | None
    execution_status: WorkerExecutionStatus | None
    approval_required: bool
    execution_fingerprint: str | None
    result_reference: str | None
    evidence_references: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MissionCheckpoint:
    checkpoint_id: str
    mission_id: str
    plan_fingerprint: str
    revision: int
    mission_status: str
    tasks: tuple[TaskCheckpoint, ...]
    created_at: datetime
    state_fingerprint: str


@dataclass(frozen=True, slots=True)
class ResumeDecision:
    checkpoint: MissionCheckpoint
    tasks: tuple[TaskCheckpoint, ...]

    @property
    def automatic_resume_safe(self) -> bool:
        if self.checkpoint.mission_status in {
            ResearchMissionStatus.COMPLETED.value,
            ResearchMissionStatus.FAILED.value,
            ResearchMissionStatus.CANCELLED.value,
        }:
            return False
        return any(item.classification is ResumeClassification.SAFE_TO_RESUME for item in self.tasks) and not any(
            item.classification is ResumeClassification.RECONCILIATION_REQUIRED
            for item in self.tasks
        )


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a canonical non-empty string")
    return value


def _digest(value, name):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{name} must be lowercase SHA-256 hex")
    return value


def _time(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _normalize(value):
    """Canonical type-tagged form that supports existing task context values."""
    if value is None: return ["none"]
    if type(value) is bool: return ["bool", value]
    if type(value) is int: return ["int", str(value)]
    if type(value) is float:
        if not math.isfinite(value): raise ValueError("plan context contains non-finite float")
        return ["float", value.hex()]
    if type(value) is Decimal:
        if not value.is_finite(): raise ValueError("plan context contains non-finite decimal")
        return ["decimal", str(value.normalize())]
    if type(value) is str: return ["str", value]
    if type(value) is bytes: return ["bytes", value.hex()]
    if type(value) is datetime: return ["datetime", _time(value, "context datetime").isoformat()]
    if isinstance(value, Enum): return ["enum", type(value).__qualname__, _normalize(value.value)]
    if isinstance(value, dict):
        entries = [(_normalize(key), _normalize(item)) for key, item in value.items()]
        entries.sort(key=lambda pair: _canonical(pair[0]))
        return ["dict", entries]
    if isinstance(value, (set, frozenset)):
        entries = [_normalize(item) for item in value]
        entries.sort(key=_canonical)
        return ["set", entries]
    if isinstance(value, (list, tuple)):
        return ["sequence", [_normalize(item) for item in value]]
    raise TypeError(f"plan context contains unsupported type: {type(value).__name__}")


def plan_fingerprint(plan: ResearchMissionPlan) -> str:
    if type(plan) is not ResearchMissionPlan:
        raise TypeError("plan must be a ResearchMissionPlan")
    tasks = []
    for task in plan.tasks:
        if type(task) is not ResearchTaskSpec:
            raise TypeError("plan contains invalid task")
        tasks.append({
            "task_id": task.task_id, "mission_id": task.mission_id,
            "role": task.role.value, "sequence": task.sequence,
            "objective": task.objective, "research_context": _normalize(task.research_context),
            "expected_result": task.expected_result,
            "authorization_level": task.authorization_level.value,
            "approval_required": task.approval_required,
            "required_capabilities": sorted(cap.name for cap in task.required_capabilities),
            "preferred_capabilities": sorted(cap.name for cap in task.preferred_capabilities),
            "depends_on": list(task.depends_on),
        })
    return hashlib.sha256(_canonical({"mission_id": plan.mission_id, "tasks": tasks})).hexdigest()


def _payload_state_fingerprint(payload):
    return hashlib.sha256(_canonical({
        "plan_fingerprint": payload["plan_fingerprint"],
        "mission_status": payload["mission_status"],
        "tasks": payload["tasks"],
    })).hexdigest()


def _payload_checkpoint_id(payload):
    return "checkpoint-" + hashlib.sha256(_canonical({
        "mission_id": payload["mission_id"],
        "plan_fingerprint": payload["plan_fingerprint"],
        "revision": payload["revision"],
        "state_fingerprint": payload["state_fingerprint"],
    })).hexdigest()


def _dispatch_execution_fingerprint(offer):
    values = []
    for metadata in (offer.authorization_metadata, offer.approval_metadata):
        for key, value in metadata:
            if key == "execution_fingerprint":
                values.append(_digest(value, "dispatch execution_fingerprint"))
    if not values:
        return None
    if len(set(values)) != 1:
        raise ValueError("dispatch execution fingerprint evidence conflicts")
    return values[0]


class MissionCheckpointStore:
    """Authenticated append-only checkpoint revisions protected across processes."""

    def __init__(self, path, *, integrity_key):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._key = require_integrity_key(integrity_key)
        self._thread_lock = RLock()

    def save(self, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock, self.lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                records = self._read_unlocked()
                checkpoints = [self._checkpoint(record["payload"]) for record in records]
                same_mission = [item for item in checkpoints if item.mission_id == payload["mission_id"]]
                latest = same_mission[-1] if same_mission else None
                if latest and latest.plan_fingerprint != payload["plan_fingerprint"]:
                    raise MissionCheckpointConflictError(
                        "checkpoint plan conflicts with existing mission authority"
                    )
                if latest and latest.state_fingerprint == payload["state_fingerprint"]:
                    return latest
                payload = dict(payload)
                payload["revision"] = 1 if latest is None else latest.revision + 1
                if payload["state_fingerprint"] != _payload_state_fingerprint(payload):
                    raise MissionCheckpointCorruptionError(
                        "checkpoint state fingerprint does not match payload"
                    )
                checkpoint_id = _payload_checkpoint_id(payload)
                payload["checkpoint_id"] = checkpoint_id
                unsigned = {"schema": _SCHEMA, "sequence": len(records) + 1,
                    "predecessor": records[-1]["authentication_tag"] if records else _GENESIS,
                    "payload": payload}
                record = {**unsigned, "authentication_tag": authentication_tag(
                    self._key, _DOMAIN, _canonical(unsigned))}
                self._validate(records + [record])
                with self.path.open("ab") as handle:
                    handle.write(_canonical(record) + b"\n"); handle.flush(); os.fsync(handle.fileno())
                directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try: os.fsync(directory_fd)
                finally: os.close(directory_fd)
                return self._checkpoint(payload)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def latest(self, mission_id):
        mission_id = _identifier(mission_id, "mission_id")
        records = self._read()
        matches = [self._checkpoint(record["payload"]) for record in records
                   if record["payload"].get("mission_id") == mission_id]
        if not matches: raise MissionCheckpointNotFoundError(f"no checkpoint for {mission_id!r}")
        return matches[-1]

    def get(self, checkpoint_id):
        checkpoint_id = _identifier(checkpoint_id, "checkpoint_id")
        matches = [self._checkpoint(record["payload"]) for record in self._read()
                   if record["payload"].get("checkpoint_id") == checkpoint_id]
        if len(matches) != 1:
            if not matches: raise MissionCheckpointNotFoundError("checkpoint not found")
            raise MissionCheckpointCorruptionError("checkpoint identity is ambiguous")
        return matches[0]

    def _read(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock, self.lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            try: return self._read_unlocked()
            finally: fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self):
        try: raw = self.path.read_bytes()
        except FileNotFoundError: return []
        if not raw: return []
        if not raw.endswith(b"\n"): raise MissionCheckpointCorruptionError("checkpoint tail is incomplete")
        records = []
        for line in raw.splitlines():
            try: records.append(json.loads(line, object_pairs_hook=self._unique))
            except Exception as exc:
                if isinstance(exc, MissionCheckpointCorruptionError): raise
                raise MissionCheckpointCorruptionError("checkpoint evidence is malformed") from exc
        self._validate(records)
        return records

    @staticmethod
    def _unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise MissionCheckpointCorruptionError("duplicate JSON key")
            result[key] = value
        return result

    def _validate(self, records):
        seen_ids = set(); revisions = {}
        for sequence, record in enumerate(records, 1):
            if not isinstance(record, dict) or set(record) != {"schema","sequence","predecessor","payload","authentication_tag"}:
                raise MissionCheckpointCorruptionError("checkpoint schema is invalid")
            expected_previous = records[sequence - 2]["authentication_tag"] if sequence > 1 else _GENESIS
            if record["schema"] != _SCHEMA or record["sequence"] != sequence or record["predecessor"] != expected_previous:
                raise MissionCheckpointCorruptionError("checkpoint chain is invalid")
            unsigned = {key: record[key] for key in ("schema","sequence","predecessor","payload")}
            if not authenticates(self._key, _DOMAIN, _canonical(unsigned), record["authentication_tag"]):
                raise MissionCheckpointCorruptionError("checkpoint authentication failed")
            checkpoint = self._checkpoint(record["payload"])
            if checkpoint.checkpoint_id in seen_ids: raise MissionCheckpointCorruptionError("duplicate checkpoint identity")
            seen_ids.add(checkpoint.checkpoint_id)
            expected_revision = revisions.get(checkpoint.mission_id, 0) + 1
            if checkpoint.revision != expected_revision: raise MissionCheckpointCorruptionError("checkpoint revision is invalid")
            revisions[checkpoint.mission_id] = checkpoint.revision

    @staticmethod
    def _checkpoint(payload):
        fields = {"checkpoint_id","mission_id","plan_fingerprint","revision","mission_status",
                  "tasks","created_at","state_fingerprint"}
        try:
            if not isinstance(payload, dict) or set(payload) != fields: raise ValueError("payload schema")
            _identifier(payload["checkpoint_id"], "checkpoint_id"); _identifier(payload["mission_id"], "mission_id")
            _digest(payload["plan_fingerprint"], "plan_fingerprint"); _digest(payload["state_fingerprint"], "state_fingerprint")
            if not isinstance(payload["revision"], int) or isinstance(payload["revision"], bool) or payload["revision"] < 1: raise ValueError("revision")
            created = _time(datetime.fromisoformat(payload["created_at"]), "created_at")
            tasks = tuple(MissionCheckpointStore._task(item) for item in payload["tasks"])
            if tuple(task.sequence for task in tasks) != tuple(range(1, len(tasks) + 1)): raise ValueError("task order")
            if payload["state_fingerprint"] != _payload_state_fingerprint(payload):
                raise ValueError("state fingerprint mismatch")
            if payload["checkpoint_id"] != _payload_checkpoint_id(payload):
                raise ValueError("checkpoint identity mismatch")
            return MissionCheckpoint(payload["checkpoint_id"], payload["mission_id"], payload["plan_fingerprint"],
                payload["revision"], _identifier(payload["mission_status"], "mission_status"), tasks, created,
                payload["state_fingerprint"])
        except (TypeError, ValueError, KeyError) as exc:
            raise MissionCheckpointCorruptionError("checkpoint payload is invalid") from exc

    @staticmethod
    def _task(value):
        fields = {"task_id","sequence","classification","assignment_id","dispatch_offer_id",
                  "execution_attempt_id","execution_status","approval_required","execution_fingerprint",
                  "result_reference","evidence_references"}
        if not isinstance(value, dict) or set(value) != fields: raise ValueError("task schema")
        if not isinstance(value["sequence"], int) or isinstance(value["sequence"], bool) or value["sequence"] < 1:
            raise ValueError("task sequence")
        for name in ("assignment_id","dispatch_offer_id","execution_attempt_id","result_reference"):
            if value[name] is not None: _identifier(value[name], name)
        fingerprint = value["execution_fingerprint"]
        if fingerprint is not None: _digest(fingerprint, "execution_fingerprint")
        if not isinstance(value["approval_required"], bool): raise ValueError("approval_required")
        linked = (value["assignment_id"], value["dispatch_offer_id"],
                  value["execution_attempt_id"], value["execution_status"])
        if any(item is not None for item in linked[2:]) and any(item is None for item in linked):
            raise ValueError("execution linkage is incomplete")
        refs = tuple(_identifier(item, "evidence_reference") for item in value["evidence_references"])
        return TaskCheckpoint(_identifier(value["task_id"], "task_id"), value["sequence"],
            ResumeClassification(value["classification"]), value["assignment_id"], value["dispatch_offer_id"],
            value["execution_attempt_id"], None if value["execution_status"] is None else WorkerExecutionStatus(value["execution_status"]),
            value["approval_required"], fingerprint, value["result_reference"], refs)


class MissionResumeCoordinator:
    """Create checkpoints and return deterministic non-executing resume decisions."""

    def __init__(self, store, *, clock=lambda: datetime.now(timezone.utc)):
        if not isinstance(store, MissionCheckpointStore): raise TypeError("store must be a MissionCheckpointStore")
        if not callable(clock): raise TypeError("clock must be callable")
        self.store = store; self.clock = clock

    def checkpoint(self, mission, *, result_state, attempts):
        if type(mission) is not ResearchMission or type(mission.plan) is not ResearchMissionPlan:
            raise TypeError("mission must have an authoritative ResearchMissionPlan")
        if mission.plan.mission_id != mission.mission_id: raise ValueError("mission and plan identity mismatch")
        if type(result_state) is not ResearchMissionResultState or result_state.mission_id != mission.mission_id:
            raise ValueError("result state belongs to a foreign mission")
        attempt_items = tuple(attempts)
        if not all(type(item) is WorkerExecutionAttempt for item in attempt_items): raise TypeError("attempts must contain WorkerExecutionAttempt values")
        by_task = {}
        planned = {task.task_id: task for task in mission.plan.tasks}
        routing = {}
        for decision in mission.routing_decisions:
            if type(decision) is not RoutingDecision:
                raise TypeError("routing_decisions contains non-authoritative value")
            request = decision.task_request
            if request.mission_id != mission.mission_id or request.task_id not in planned:
                raise ValueError("routing decision belongs to foreign mission/task")
            task = planned[request.task_id]
            if (
                request.authorization_level != task.authorization_level
                or request.approval_required != task.approval_required
                or request.expected_result != task.expected_result
                or request.required_capabilities != set(task.required_capabilities)
            ):
                raise ValueError("routing decision authority does not match planned task")
            if request.task_id in routing: raise ValueError("duplicate routing decision")
            if decision.outcome is RoutingOutcome.SUCCESS:
                _identifier(decision.assignment_id, "assignment_id")
            routing[request.task_id] = decision
        offers = {}
        for offer in mission.dispatch_offers:
            if type(offer) is not DispatchOfferSnapshot:
                raise TypeError("dispatch_offers contains non-authoritative value")
            if offer.mission_id != mission.mission_id or offer.task_id not in planned:
                raise ValueError("dispatch offer belongs to foreign mission/task")
            if offer.task_id in offers: raise ValueError("duplicate dispatch offer")
            decision = routing.get(offer.task_id)
            if decision is not None and decision.assignment_id != offer.assignment_id:
                raise ValueError("routing and dispatch linkage conflict")
            if decision is not None and decision.assigned_node_id != offer.worker_node_id:
                raise ValueError("routing and dispatch worker identity conflict")
            task = planned[offer.task_id]
            if (
                offer.authorization_level != task.authorization_level
                or offer.approval_required != task.approval_required
                or set(offer.required_capabilities)
                != {capability.name for capability in task.required_capabilities}
            ):
                raise ValueError("dispatch authority does not match planned task")
            _dispatch_execution_fingerprint(offer)
            offers[offer.task_id] = offer
        if mission.status in {
            ResearchMissionStatus.ROUTED,
            ResearchMissionStatus.DISPATCHED,
            ResearchMissionStatus.COMPLETED,
        } and set(routing) != set(planned):
            raise ValueError("mission routing linkage is incomplete")
        if mission.status in {
            ResearchMissionStatus.ROUTED,
            ResearchMissionStatus.DISPATCHED,
            ResearchMissionStatus.COMPLETED,
        } and any(
            decision.outcome is not RoutingOutcome.SUCCESS
            for decision in routing.values()
        ):
            raise ValueError("routed mission contains unsuccessful routing evidence")
        if mission.status in {
            ResearchMissionStatus.DISPATCHED,
            ResearchMissionStatus.COMPLETED,
        } and set(offers) != set(planned):
            raise ValueError("mission dispatch linkage is incomplete")
        result_items = tuple(result_state.results)
        if len({item.task_id for item in result_items}) != len(result_items):
            raise ValueError("result state contains duplicate task results")
        expected_completed = {
            item.task_id for item in result_items
            if item.status is ResearchTaskResultStatus.COMPLETED
        }
        if {item.task_id for item in result_state.completed_results} != expected_completed:
            raise ValueError("result state completed projection is contradictory")
        expected_failed = {
            item.task_id for item in result_items
            if item.status is ResearchTaskResultStatus.FAILED
        }
        expected_blocked = {
            item.task_id for item in result_items
            if item.status is ResearchTaskResultStatus.BLOCKED
        }
        if {item.task_id for item in result_state.failed_results} != expected_failed:
            raise ValueError("result state failed projection is contradictory")
        if {item.task_id for item in result_state.blocked_results} != expected_blocked:
            raise ValueError("result state blocked projection is contradictory")
        expected_pending = set(planned) - {item.task_id for item in result_items}
        if {item.task_id for item in result_state.pending_tasks} != expected_pending:
            raise ValueError("result state pending projection is contradictory")
        for item in attempt_items:
            req = item.request
            if req.mission_id != mission.mission_id or req.task_id not in planned:
                raise ValueError("execution attempt belongs to foreign mission/task")
            for name in ("assignment_id","dispatch_offer_id","execution_attempt_id","worker_node_id"):
                _identifier(getattr(req, name), name)
            if req.authorization_level != planned[req.task_id].authorization_level or req.approval_required != planned[req.task_id].approval_required:
                raise ValueError("execution authority does not match planned task")
            if req.expected_result != planned[req.task_id].expected_result:
                raise ValueError("execution expected result does not match planned task")
            if req.execution_fingerprint is not None:
                _digest(req.execution_fingerprint, "execution_fingerprint")
            if not isinstance(item.status, WorkerExecutionStatus):
                raise ValueError("execution status is invalid")
            terminal = item.status in {
                WorkerExecutionStatus.SUCCEEDED, WorkerExecutionStatus.FAILED,
                WorkerExecutionStatus.RECONCILIATION_REQUIRED,
            }
            if terminal != (item.terminal_at is not None):
                raise ValueError("execution terminal timestamp is contradictory")
            if (item.status is WorkerExecutionStatus.SUCCEEDED) != (item.result is not None):
                raise ValueError("execution result is contradictory")
            reason_required = item.status in {
                WorkerExecutionStatus.FAILED,
                WorkerExecutionStatus.RECONCILIATION_REQUIRED,
            }
            if reason_required != (
                isinstance(item.failure_reason, str) and bool(item.failure_reason.strip())
            ):
                raise ValueError("execution failure evidence is contradictory")
            if req.task_id in by_task: raise ValueError("task has ambiguous execution attempts")
            by_task[req.task_id] = item
        results = {item.task_id: item for item in result_items}
        if any(item.mission_id != mission.mission_id or item.task_id not in planned for item in result_state.results):
            raise ValueError("result state contains foreign result")
        if any(item.role is not planned[item.task_id].role for item in result_items):
            raise ValueError("result role does not match planned task")
        evidence = {}
        evidence_ids = set()
        for item in result_state.evidence:
            if item.mission_id != mission.mission_id or item.task_id not in planned: raise ValueError("foreign evidence")
            if item.task_id not in results: raise ValueError("evidence lacks an authoritative result")
            if item.evidence_id in evidence_ids: raise ValueError("duplicate evidence identity")
            evidence_ids.add(item.evidence_id)
            evidence.setdefault(item.task_id, []).append(item.evidence_id)
        completed = {task_id for task_id, item in results.items() if item.status is ResearchTaskResultStatus.COMPLETED}
        task_payloads = []
        for task in mission.plan.tasks:
            execution = by_task.get(task.task_id); result = results.get(task.task_id)
            classification = self._classify(
                task, execution, result, completed, mission.status
            )
            refs = set(evidence.get(task.task_id, ()))
            if execution and execution.result: refs.update(execution.result.evidence_references)
            req = execution.request if execution else None
            routed = routing.get(task.task_id)
            offer = offers.get(task.task_id)
            assignment_id = req.assignment_id if req else (
                offer.assignment_id if offer else (routed.assignment_id if routed else None)
            )
            dispatch_offer_id = req.dispatch_offer_id if req else (offer.offer_id if offer else None)
            if req and offer and (req.assignment_id != offer.assignment_id or req.dispatch_offer_id != offer.offer_id):
                raise ValueError("execution and dispatch linkage conflict")
            if req and offer and (
                req.worker_node_id != offer.worker_node_id
                or req.coordinator_node_id != offer.coordinator_node_id
            ):
                raise ValueError("execution and dispatch actor identity conflict")
            if req and routed and req.assignment_id != routed.assignment_id:
                raise ValueError("execution and routing assignment conflict")
            offer_fingerprint = _dispatch_execution_fingerprint(offer) if offer else None
            if req and offer and req.execution_fingerprint != offer_fingerprint:
                raise ValueError("execution fingerprint does not match dispatch evidence")
            task_payloads.append({"task_id": task.task_id, "sequence": task.sequence,
                "classification": classification.value,
                "assignment_id": assignment_id,
                "dispatch_offer_id": dispatch_offer_id,
                "execution_attempt_id": req.execution_attempt_id if req else None,
                "execution_status": execution.status.value if execution else None,
                "approval_required": task.approval_required,
                "execution_fingerprint": (
                    req.execution_fingerprint if req else offer_fingerprint
                ),
                "result_reference": f"result:{task.task_id}" if result else None,
                "evidence_references": sorted(refs)})
        if mission.status is ResearchMissionStatus.COMPLETED and any(
            item["classification"] != ResumeClassification.ALREADY_COMPLETED.value
            for item in task_payloads
        ):
            raise ValueError("completed mission contains unresolved tasks")
        fingerprint = plan_fingerprint(mission.plan)
        state = {"plan_fingerprint": fingerprint, "mission_status": mission.status.value,
                 "tasks": task_payloads}
        payload = {"checkpoint_id": "pending", "mission_id": mission.mission_id,
            "plan_fingerprint": fingerprint, "revision": 1,
            "mission_status": mission.status.value, "tasks": task_payloads,
            "created_at": _time(self.clock(), "clock result").isoformat(),
            "state_fingerprint": hashlib.sha256(_canonical(state)).hexdigest()}
        return self.store.save(payload)

    def resume(self, mission, *, checkpoint_id=None):
        if type(mission) is not ResearchMission or type(mission.plan) is not ResearchMissionPlan:
            raise TypeError("mission must have an authoritative ResearchMissionPlan")
        latest = self.store.latest(mission.mission_id)
        if checkpoint_id is not None and latest.checkpoint_id != _identifier(checkpoint_id, "checkpoint_id"):
            raise MissionCheckpointConflictError("requested checkpoint is stale")
        if latest.plan_fingerprint != plan_fingerprint(mission.plan):
            raise MissionCheckpointConflictError("current plan differs from authoritative checkpoint")
        return ResumeDecision(latest, latest.tasks)

    @staticmethod
    def _classify(task, execution, result, completed, mission_status):
        if result is not None:
            return (ResumeClassification.ALREADY_COMPLETED if result.status is ResearchTaskResultStatus.COMPLETED
                    else ResumeClassification.FAILED_TERMINAL)
        if execution is not None:
            if execution.status is WorkerExecutionStatus.SUCCEEDED: return ResumeClassification.ALREADY_COMPLETED
            if execution.status is WorkerExecutionStatus.FAILED: return ResumeClassification.FAILED_TERMINAL
            if execution.status in {WorkerExecutionStatus.CLAIMED, WorkerExecutionStatus.RUNNING,
                                    WorkerExecutionStatus.RECONCILIATION_REQUIRED}:
                return ResumeClassification.RECONCILIATION_REQUIRED
            if execution.status is not WorkerExecutionStatus.ACCEPTED: raise ValueError("unknown execution state")
        if mission_status in {
            ResearchMissionStatus.FAILED,
            ResearchMissionStatus.CANCELLED,
        }:
            return ResumeClassification.FAILED_TERMINAL
        if not set(task.depends_on).issubset(completed): return ResumeClassification.PENDING
        if task.approval_required: return ResumeClassification.BLOCKED_ON_APPROVAL
        return ResumeClassification.SAFE_TO_RESUME
