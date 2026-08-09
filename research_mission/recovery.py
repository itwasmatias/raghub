"""Durable evidence describing mission interruption and governed recovery."""

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

from federation.integrity import authentication_tag, authenticates, require_integrity_key
from federation.routing_decision import RoutingDecision, RoutingOutcome
from federation.worker_execution import WorkerExecutionAttempt, WorkerExecutionStatus
from federation.worker_governance import ExecutionLocality
from research_mission.checkpoint import (
    MissionCheckpointStore,
    ResumeClassification,
)
from research_mission.replication import ResearchReplicationResult
from research_mission.result_runtime import ResearchMissionResultState
from research_mission.results import ResearchTaskResultStatus


_SCHEMA = "raghub.mission-recovery-evidence.v0.1"
_DOMAIN = b"raghub.mission-recovery-evidence.v0.1"
_GENESIS = "0" * 64


class MissionRecoveryError(Exception):
    """Base recovery evidence error."""


class MissionRecoveryCorruptionError(MissionRecoveryError):
    """Durable recovery evidence cannot be trusted."""


class MissionRecoveryNotFoundError(MissionRecoveryError):
    """No recovery evidence exists for the requested identity."""


class RecoveryOutcome(str, Enum):
    RESUMED_SAFELY = "resumed_safely"
    ALREADY_COMPLETE = "already_complete"
    PENDING = "pending"
    BLOCKED_ON_APPROVAL = "blocked_on_approval"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    TERMINAL_FAILURE = "terminal_failure"
    RECOVERED_AFTER_WORKER_UNAVAILABLE = "recovered_after_worker_unavailability"
    RECOVERED_USING_LOCAL_FALLBACK = "recovered_using_local_fallback"
    CLOUD_ESCALATION_PREVENTED = "cloud_escalation_prevented_by_policy"
    CLOUD_ESCALATION_AUTHORIZED = "cloud_escalation_authorized"


_OUTCOME_ORDER = {value: index for index, value in enumerate(RecoveryOutcome)}


@dataclass(frozen=True, slots=True)
class TaskRecoveryEvidence:
    task_id: str
    outcomes: tuple[RecoveryOutcome, ...]
    checkpoint_classification: ResumeClassification
    checkpoint_attempt_id: str | None
    checkpoint_worker_node_id: str | None
    current_attempt_id: str | None
    current_execution_status: WorkerExecutionStatus | None
    selected_worker_node_id: str | None
    assignment_id: str | None
    dispatch_offer_id: str | None
    execution_fingerprint: str | None
    result_reference: str | None
    evidence_references: tuple[str, ...]
    policy_reasons: tuple[str, ...]
    budget_evidence_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class MissionRecoveryEvidence:
    recovery_id: str
    mission_id: str
    checkpoint_id: str
    checkpoint_revision: int
    plan_fingerprint: str
    tasks: tuple[TaskRecoveryEvidence, ...]
    source_result_task_ids: tuple[str, ...]
    replication_id: str | None
    replication_claim_statuses: tuple[tuple[str, str], ...]
    replication_evidence_ids: tuple[str, ...]
    created_at: datetime


def _identifier(value, name, *, optional=False):
    if value is None and optional: return None
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


def _identity_payload(payload):
    return {key: payload[key] for key in (
        "mission_id", "checkpoint_id", "checkpoint_revision", "plan_fingerprint",
        "tasks", "source_result_task_ids", "replication_id",
        "replication_claim_statuses", "replication_evidence_ids",
    )}


def _recovery_id(payload):
    return "recovery-" + hashlib.sha256(_canonical(_identity_payload(payload))).hexdigest()


class MissionRecoveryEvidenceStore:
    """Append-only authenticated recovery evidence with process-safe idempotency."""

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
                payload = dict(payload)
                payload["recovery_id"] = _recovery_id(payload)
                existing = [record for record in records
                    if record["payload"]["recovery_id"] == payload["recovery_id"]]
                if existing: return self._evidence(existing[0]["payload"])
                unsigned = {"schema": _SCHEMA, "sequence": len(records) + 1,
                    "predecessor": records[-1]["authentication_tag"] if records else _GENESIS,
                    "payload": payload}
                record = {**unsigned, "authentication_tag": authentication_tag(
                    self._key, _DOMAIN, _canonical(unsigned))}
                self._validate(records + [record])
                with self.path.open("ab") as handle:
                    handle.write(_canonical(record) + b"\n"); handle.flush(); os.fsync(handle.fileno())
                directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try: os.fsync(directory)
                finally: os.close(directory)
                return self._evidence(payload)
            finally: fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def latest(self, mission_id):
        mission_id = _identifier(mission_id, "mission_id")
        matches = [self._evidence(record["payload"]) for record in self._read()
                   if record["payload"].get("mission_id") == mission_id]
        if not matches: raise MissionRecoveryNotFoundError("recovery evidence not found")
        return matches[-1]

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
        if not raw.endswith(b"\n"): raise MissionRecoveryCorruptionError("recovery evidence is incomplete")
        records = []
        for line in raw.splitlines():
            try: records.append(json.loads(line, object_pairs_hook=self._unique))
            except Exception as exc:
                if isinstance(exc, MissionRecoveryCorruptionError): raise
                raise MissionRecoveryCorruptionError("recovery evidence is malformed") from exc
        self._validate(records); return records

    @staticmethod
    def _unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise MissionRecoveryCorruptionError("duplicate JSON key")
            result[key] = value
        return result

    def _validate(self, records):
        seen = set()
        for sequence, record in enumerate(records, 1):
            if not isinstance(record, dict) or set(record) != {
                "schema", "sequence", "predecessor", "payload", "authentication_tag"}:
                raise MissionRecoveryCorruptionError("recovery evidence schema is invalid")
            previous = records[sequence - 2]["authentication_tag"] if sequence > 1 else _GENESIS
            if record["schema"] != _SCHEMA or record["sequence"] != sequence or record["predecessor"] != previous:
                raise MissionRecoveryCorruptionError("recovery evidence chain is invalid")
            unsigned = {key: record[key] for key in ("schema","sequence","predecessor","payload")}
            if not authenticates(self._key, _DOMAIN, _canonical(unsigned), record["authentication_tag"]):
                raise MissionRecoveryCorruptionError("recovery evidence authentication failed")
            evidence = self._evidence(record["payload"])
            if evidence.recovery_id in seen: raise MissionRecoveryCorruptionError("duplicate recovery evidence")
            seen.add(evidence.recovery_id)

    @staticmethod
    def _evidence(payload):
        fields = {"recovery_id","mission_id","checkpoint_id","checkpoint_revision",
                  "plan_fingerprint","tasks","source_result_task_ids","replication_id",
                  "replication_claim_statuses","replication_evidence_ids","created_at"}
        try:
            if not isinstance(payload, dict) or set(payload) != fields: raise ValueError("schema")
            if payload["recovery_id"] != _recovery_id(payload): raise ValueError("identity")
            tasks = tuple(MissionRecoveryEvidenceStore._task(value) for value in payload["tasks"])
            if len({item.task_id for item in tasks}) != len(tasks): raise ValueError("duplicate task")
            statuses = tuple((_identifier(item[0], "claim_id"), _identifier(item[1], "claim status"))
                             for item in payload["replication_claim_statuses"])
            return MissionRecoveryEvidence(_identifier(payload["recovery_id"], "recovery_id"),
                _identifier(payload["mission_id"], "mission_id"),
                _identifier(payload["checkpoint_id"], "checkpoint_id"),
                payload["checkpoint_revision"], _digest(payload["plan_fingerprint"], "plan_fingerprint"),
                tasks, tuple(_identifier(item, "source result task") for item in payload["source_result_task_ids"]),
                _identifier(payload["replication_id"], "replication_id", optional=True), statuses,
                tuple(_identifier(item, "replication evidence") for item in payload["replication_evidence_ids"]),
                _time(datetime.fromisoformat(payload["created_at"]), "created_at"))
        except (TypeError, ValueError, KeyError) as exc:
            raise MissionRecoveryCorruptionError("recovery payload is invalid") from exc

    @staticmethod
    def _task(value):
        fields = {"task_id","outcomes","checkpoint_classification","checkpoint_attempt_id",
                  "checkpoint_worker_node_id","current_attempt_id","current_execution_status",
                  "selected_worker_node_id","assignment_id","dispatch_offer_id",
                  "execution_fingerprint","result_reference","evidence_references","policy_reasons"}
        fields.add("budget_evidence_fingerprint")
        if not isinstance(value, dict) or set(value) != fields: raise ValueError("task schema")
        optional = ("checkpoint_attempt_id","checkpoint_worker_node_id","current_attempt_id",
                    "selected_worker_node_id","assignment_id","dispatch_offer_id","result_reference")
        for name in optional: _identifier(value[name], name, optional=True)
        fingerprint = value["execution_fingerprint"]
        if fingerprint is not None: _digest(fingerprint, "execution_fingerprint")
        budget_fingerprint = value["budget_evidence_fingerprint"]
        if budget_fingerprint is not None:
            _digest(budget_fingerprint, "budget_evidence_fingerprint")
        outcomes = tuple(RecoveryOutcome(item) for item in value["outcomes"])
        if not outcomes or tuple(sorted(set(outcomes), key=_OUTCOME_ORDER.get)) != outcomes:
            raise ValueError("outcomes are invalid")
        return TaskRecoveryEvidence(_identifier(value["task_id"], "task_id"), outcomes,
            ResumeClassification(value["checkpoint_classification"]), value["checkpoint_attempt_id"],
            value["checkpoint_worker_node_id"], value["current_attempt_id"],
            None if value["current_execution_status"] is None else WorkerExecutionStatus(value["current_execution_status"]),
            value["selected_worker_node_id"], value["assignment_id"], value["dispatch_offer_id"],
            fingerprint, value["result_reference"],
            tuple(_identifier(item, "evidence reference") for item in value["evidence_references"]),
            tuple(_identifier(item, "policy reason") for item in value["policy_reasons"]),
            budget_fingerprint)


class MissionRecoveryEvidenceRuntime:
    """Compose recovery facts from existing authority without executing work."""

    def __init__(self, checkpoint_store, store, *, clock=lambda: datetime.now(timezone.utc)):
        if not isinstance(checkpoint_store, MissionCheckpointStore):
            raise TypeError("checkpoint_store must be MissionCheckpointStore")
        if not isinstance(store, MissionRecoveryEvidenceStore):
            raise TypeError("store must be MissionRecoveryEvidenceStore")
        if not callable(clock): raise TypeError("clock must be callable")
        self.checkpoint_store = checkpoint_store; self.store = store; self.clock = clock

    def record(self, mission_id, *, checkpoint_id, result_state, attempts,
               routing_decisions, replication_result=None):
        mission_id = _identifier(mission_id, "mission_id")
        checkpoint = self.checkpoint_store.get(_identifier(checkpoint_id, "checkpoint_id"))
        if checkpoint.mission_id != mission_id: raise ValueError("checkpoint belongs to foreign mission")
        if type(result_state) is not ResearchMissionResultState or result_state.mission_id != mission_id:
            raise ValueError("result state belongs to foreign mission")
        checkpoint_tasks = {item.task_id: item for item in checkpoint.tasks}
        results = {}
        for result in result_state.results:
            if result.mission_id != mission_id or result.task_id not in checkpoint_tasks:
                raise ValueError("foreign result evidence")
            if result.task_id in results: raise ValueError("duplicate task result")
            results[result.task_id] = result
        for prior in checkpoint.tasks:
            if prior.result_reference is not None and prior.task_id not in results:
                raise ValueError("checkpoint result linkage is missing from recovery state")
        expected_completed = {
            item.task_id for item in result_state.results
            if item.status is ResearchTaskResultStatus.COMPLETED
        }
        if {item.task_id for item in result_state.completed_results} != expected_completed:
            raise ValueError("result completion projection is contradictory")
        evidence = {}
        evidence_ids = set()
        for item in result_state.evidence:
            if item.mission_id != mission_id or item.task_id not in results:
                raise ValueError("foreign or unlinked research evidence")
            if item.evidence_id in evidence_ids: raise ValueError("duplicate evidence identity")
            evidence_ids.add(item.evidence_id); evidence.setdefault(item.task_id, []).append(item.evidence_id)
        attempt_by_task = {}
        for item in tuple(attempts):
            if type(item) is not WorkerExecutionAttempt: raise TypeError("invalid worker attempt")
            request = item.request
            if request.mission_id != mission_id or request.task_id not in checkpoint_tasks:
                raise ValueError("foreign worker attempt")
            if request.task_id in attempt_by_task: raise ValueError("ambiguous worker attempts")
            if not isinstance(item.status, WorkerExecutionStatus):
                raise ValueError("worker execution status is invalid")
            terminal = item.status in {
                WorkerExecutionStatus.SUCCEEDED,
                WorkerExecutionStatus.FAILED,
                WorkerExecutionStatus.RECONCILIATION_REQUIRED,
            }
            if terminal != (item.terminal_at is not None):
                raise ValueError("worker terminal timestamp is contradictory")
            if (item.status is WorkerExecutionStatus.SUCCEEDED) != (item.result is not None):
                raise ValueError("worker result is contradictory")
            reason_required = item.status in {
                WorkerExecutionStatus.FAILED,
                WorkerExecutionStatus.RECONCILIATION_REQUIRED,
            }
            if reason_required != (
                isinstance(item.failure_reason, str)
                and bool(item.failure_reason.strip())
            ):
                raise ValueError("worker failure evidence is contradictory")
            prior = checkpoint_tasks[request.task_id]
            if prior.execution_attempt_id == request.execution_attempt_id and (
                prior.assignment_id != request.assignment_id
                or prior.dispatch_offer_id != request.dispatch_offer_id
                or prior.worker_node_id != request.worker_node_id
                or prior.execution_fingerprint != request.execution_fingerprint
            ):
                raise ValueError("worker attempt conflicts with checkpoint authority")
            if (
                prior.execution_attempt_id != request.execution_attempt_id
                and request.created_at < checkpoint.created_at
            ):
                raise ValueError("new recovery attempt predates checkpoint authority")
            attempt_by_task[request.task_id] = item
        routing = {}
        for decision in tuple(routing_decisions):
            if type(decision) is not RoutingDecision: raise TypeError("invalid routing evidence")
            request = decision.task_request
            if request.mission_id != mission_id or request.task_id not in checkpoint_tasks:
                raise ValueError("foreign routing evidence")
            if request.task_id in routing: raise ValueError("duplicate routing evidence")
            routing[request.task_id] = decision
        for task_id, current in attempt_by_task.items():
            decision = routing.get(task_id)
            if decision is not None and decision.outcome is RoutingOutcome.SUCCESS and (
                decision.assignment_id != current.request.assignment_id
                or decision.assigned_node_id != current.request.worker_node_id
            ):
                raise ValueError("recovery attempt conflicts with routing authority")
        replication_statuses = ()
        replication_id = None
        replication_evidence_ids = ()
        if replication_result is not None:
            if type(replication_result) is not ResearchReplicationResult or replication_result.mission_id != mission_id:
                raise ValueError("foreign replication evidence")
            if not set(replication_result.source_evidence_ids).issubset(evidence_ids):
                raise ValueError("replication evidence does not link to research evidence")
            if len({item.claim_id for item in replication_result.claims}) != len(replication_result.claims):
                raise ValueError("replication claim evidence is ambiguous")
            if len(set(replication_result.replication_evidence_ids)) != len(
                replication_result.replication_evidence_ids
            ):
                raise ValueError("replication evidence identity is ambiguous")
            replication_id = replication_result.replication_id
            replication_statuses = tuple((item.claim_id, item.status.value)
                                         for item in replication_result.claims)
            replication_evidence_ids = replication_result.replication_evidence_ids
        tasks = []
        for prior in checkpoint.tasks:
            current = attempt_by_task.get(prior.task_id)
            result = results.get(prior.task_id)
            decision = routing.get(prior.task_id)
            outcomes = {self._base_outcome(prior.classification)}
            if current is not None:
                if current.status is WorkerExecutionStatus.FAILED:
                    outcomes.add(RecoveryOutcome.TERMINAL_FAILURE)
                elif current.status is WorkerExecutionStatus.RECONCILIATION_REQUIRED:
                    outcomes.add(RecoveryOutcome.RECONCILIATION_REQUIRED)
                elif current.status in {WorkerExecutionStatus.CLAIMED, WorkerExecutionStatus.RUNNING,
                                        WorkerExecutionStatus.SUCCEEDED} and prior.classification in {
                    ResumeClassification.SAFE_TO_RESUME, ResumeClassification.PENDING,
                    ResumeClassification.BLOCKED_ON_APPROVAL}:
                    outcomes.add(RecoveryOutcome.RESUMED_SAFELY)
            if result is not None and result.status is ResearchTaskResultStatus.COMPLETED and prior.classification is not ResumeClassification.ALREADY_COMPLETED:
                if result.produced_at < checkpoint.created_at:
                    raise ValueError("new recovery result predates checkpoint authority")
                outcomes.add(RecoveryOutcome.RESUMED_SAFELY)
            selected = decision.assigned_node_id if decision and decision.outcome is RoutingOutcome.SUCCESS else None
            reasons = ()
            budget_fingerprint = None
            if decision and decision.budget_evidence is not None:
                preference = decision.budget_evidence.preference
                budget_fingerprint = hashlib.sha256(_canonical({
                    "policy": {
                        "local_only": decision.budget_evidence.policy.local_only,
                        "cloud_budget_exhausted": decision.budget_evidence.policy.cloud_budget_exhausted,
                        "max_cost_class": decision.budget_evidence.policy.max_cost_class.value,
                        "allow_cloud_escalation": decision.budget_evidence.policy.allow_cloud_escalation,
                        "prefer_local": decision.budget_evidence.policy.prefer_local,
                        "require_local_fallback_eligibility": decision.budget_evidence.policy.require_local_fallback_eligibility,
                    },
                    "eligible": [(item.node_id, item.locality.value, item.provider_id,
                                  item.cost_class.value, item.preferred_capabilities_matched)
                                 for item in preference.eligible_workers],
                    "decisions": [(item.node_id, item.eligible, item.reasons)
                                  for item in preference.decisions],
                })).hexdigest()
                reasons = tuple(sorted(reason for item in preference.decisions for reason in item.reasons))
                selected_governed = next((item for item in preference.eligible_workers
                    if item.node_id == selected), None)
                cloud_policy = {"local_only", "cloud_budget_exhausted", "cloud_escalation_not_permitted"}
                if cloud_policy & set(reasons): outcomes.add(RecoveryOutcome.CLOUD_ESCALATION_PREVENTED)
                if selected_governed and selected_governed.locality is ExecutionLocality.CLOUD and decision.budget_evidence.policy.allow_cloud_escalation:
                    outcomes.add(RecoveryOutcome.CLOUD_ESCALATION_AUTHORIZED)
                old_unavailable = prior.worker_node_id and prior.worker_node_id != selected and any(
                    item.node_id == prior.worker_node_id and "worker_unavailable" in item.reasons
                    for item in preference.decisions)
                recovery_observed = (
                    RecoveryOutcome.RESUMED_SAFELY in outcomes
                    and current is not None
                    and current.request.worker_node_id == selected
                )
                if old_unavailable and recovery_observed:
                    outcomes.add(RecoveryOutcome.RECOVERED_AFTER_WORKER_UNAVAILABLE)
                    if selected_governed and selected_governed.locality is ExecutionLocality.LOCAL:
                        outcomes.add(RecoveryOutcome.RECOVERED_USING_LOCAL_FALLBACK)
                elif recovery_observed and selected_governed and selected_governed.locality is ExecutionLocality.LOCAL and cloud_policy & set(reasons):
                    outcomes.add(RecoveryOutcome.RECOVERED_USING_LOCAL_FALLBACK)
            if outcomes & {
                RecoveryOutcome.RESUMED_SAFELY,
                RecoveryOutcome.TERMINAL_FAILURE,
                RecoveryOutcome.RECONCILIATION_REQUIRED,
            }:
                outcomes.discard(RecoveryOutcome.PENDING)
            if RecoveryOutcome.RESUMED_SAFELY in outcomes:
                outcomes.discard(RecoveryOutcome.BLOCKED_ON_APPROVAL)
            request = current.request if current else None
            refs = set(prior.evidence_references); refs.update(evidence.get(prior.task_id, ()))
            tasks.append({"task_id": prior.task_id,
                "outcomes": [item.value for item in sorted(outcomes, key=_OUTCOME_ORDER.get)],
                "checkpoint_classification": prior.classification.value,
                "checkpoint_attempt_id": prior.execution_attempt_id,
                "checkpoint_worker_node_id": prior.worker_node_id,
                "current_attempt_id": request.execution_attempt_id if request else None,
                "current_execution_status": current.status.value if current else None,
                "selected_worker_node_id": selected,
                "assignment_id": request.assignment_id if request else prior.assignment_id,
                "dispatch_offer_id": request.dispatch_offer_id if request else prior.dispatch_offer_id,
                "execution_fingerprint": request.execution_fingerprint if request else prior.execution_fingerprint,
                "result_reference": f"result:{prior.task_id}" if result else prior.result_reference,
                "evidence_references": sorted(refs), "policy_reasons": list(reasons),
                "budget_evidence_fingerprint": budget_fingerprint})
        payload = {"recovery_id": "pending", "mission_id": mission_id,
            "checkpoint_id": checkpoint.checkpoint_id, "checkpoint_revision": checkpoint.revision,
            "plan_fingerprint": checkpoint.plan_fingerprint, "tasks": tasks,
            "source_result_task_ids": sorted(results), "replication_id": replication_id,
            "replication_claim_statuses": [list(item) for item in replication_statuses],
            "replication_evidence_ids": list(replication_evidence_ids),
            "created_at": _time(self.clock(), "clock result").isoformat()}
        return self.store.save(payload)

    @staticmethod
    def _base_outcome(classification):
        return {
            ResumeClassification.SAFE_TO_RESUME: RecoveryOutcome.PENDING,
            ResumeClassification.PENDING: RecoveryOutcome.PENDING,
            ResumeClassification.ALREADY_COMPLETED: RecoveryOutcome.ALREADY_COMPLETE,
            ResumeClassification.BLOCKED_ON_APPROVAL: RecoveryOutcome.BLOCKED_ON_APPROVAL,
            ResumeClassification.RECONCILIATION_REQUIRED: RecoveryOutcome.RECONCILIATION_REQUIRED,
            ResumeClassification.FAILED_TERMINAL: RecoveryOutcome.TERMINAL_FAILURE,
        }[classification]
