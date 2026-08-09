"""
Durable Proposal Store v0.1

Authenticated append-only storage for AI worker proposals with lifecycle tracking.

Provides restart-safe exact idempotency, conflict detection, and concurrency protection.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from federation.integrity import authentication_tag, authenticates, require_integrity_key
from federation.task_request import AuthorizationLevel
from tools.ai_controller._locking import FileLock


_PROPOSAL_SCHEMA = "proposal-store-v0.1"
_AUTHENTICATION_DOMAIN = b"raghub.proposal-store.v0.1"
_GENESIS_TAG = "0" * 64
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ProposalStoreError(Exception):
    """Base exception for proposal store errors."""
    pass


class ProposalConflict(ProposalStoreError):
    """Proposal identity conflicts with existing evidence."""
    pass


class ProposalNotFound(ProposalStoreError):
    """Proposal not found by identity."""
    pass


class ProposalIntegrityError(ProposalStoreError):
    """Proposal evidence is malformed or unauthentic."""
    pass


class ProposalLifecycleState(str, Enum):
    """Lifecycle states for AI worker proposals."""
    VALIDATED = "validated"              # Proposal validated, not yet routed
    ROUTED = "routed"                    # TaskRequest created, routed
    DISPATCHED = "dispatched"            # Dispatch offer created
    BLOCKED_ON_APPROVAL = "blocked_on_approval"  # Awaiting approval
    EXECUTION_PENDING = "execution_pending"  # Approved/ready, not yet executing
    EXECUTING = "executing"              # Currently executing
    COMPLETED = "completed"              # Execution completed
    FAILED = "failed"                    # Execution failed
    RECONCILIATION_REQUIRED = "reconciliation_required"  # Ambiguous outcome


def _require_identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field_name} must be a canonical non-empty identifier")
    return value


def _require_optional_identifier(value: Any, field_name: str) -> str | None:
    return None if value is None else _require_identifier(value, field_name)


def _require_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("proposal data must be finite JSON values") from exc


def _canonical_parameters(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("immutable_parameters must be a mapping")
    encoded = _canonical_json(dict(value))
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise TypeError("immutable_parameters must be an object")
    return decoded


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("proposal clock must return an aware datetime")
    return value.astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProposalIntegrityError(f"{field_name} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProposalIntegrityError(f"{field_name} is invalid") from exc
    if parsed.tzinfo is None:
        raise ProposalIntegrityError(f"{field_name} must include a timezone")
    canonical = parsed.astimezone(timezone.utc)
    if canonical.isoformat() != value:
        raise ProposalIntegrityError(f"{field_name} is not canonical UTC")
    return canonical


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    """Immutable proposal record with lifecycle tracking."""

    # Immutable identity
    proposal_id: str
    worker_identity: str
    mission_id: str
    task_id: str
    action_type: str
    workspace_id: str

    # Immutable subject
    immutable_parameters: Mapping[str, Any]
    expected_result: str

    # Authoritative policy (derived, not from proposal)
    authorization_level: AuthorizationLevel
    approval_required: bool
    required_capabilities: tuple[str, ...]

    # Fingerprints
    proposal_fingerprint: str
    execution_fingerprint: str | None
    approval_execution_fingerprint: str | None

    # Lifecycle state
    lifecycle_state: ProposalLifecycleState

    # Lifecycle identities (None until created)
    task_request_fingerprint: str | None = None
    assignment_id: str | None = None
    dispatch_offer_id: str | None = None
    target_node_id: str | None = None
    approval_request_id: str | None = None
    job_id: str | None = None
    result_reference: str | None = None

    # Temporal
    created_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None

    # Outcome
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Safe serialization."""
        return {
            "proposal_id": self.proposal_id,
            "worker_identity": self.worker_identity,
            "mission_id": self.mission_id,
            "task_id": self.task_id,
            "action_type": self.action_type,
            "workspace_id": self.workspace_id,
            "immutable_parameters": dict(self.immutable_parameters),
            "expected_result": self.expected_result,
            "authorization_level": self.authorization_level.value,
            "approval_required": self.approval_required,
            "required_capabilities": list(self.required_capabilities),
            "proposal_fingerprint": self.proposal_fingerprint,
            "execution_fingerprint": self.execution_fingerprint,
            "approval_execution_fingerprint": self.approval_execution_fingerprint,
            "lifecycle_state": self.lifecycle_state.value,
            "task_request_fingerprint": self.task_request_fingerprint,
            "assignment_id": self.assignment_id,
            "dispatch_offer_id": self.dispatch_offer_id,
            "target_node_id": self.target_node_id,
            "approval_request_id": self.approval_request_id,
            "job_id": self.job_id,
            "result_reference": self.result_reference,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True, slots=True)
class _ProposalSnapshot:
    records: tuple[dict[str, Any], ...]
    revision: str


class ProposalStore:
    """Authenticated append-only proposal and lifecycle evidence store."""

    def __init__(self, root: Path, *, integrity_key: bytes) -> None:
        self.root = Path(root)
        self.path = self.root / "proposal-store-v0.1.jsonl"
        self.lock_path = self.root / ".proposal-store-v0.1.lock"
        self._integrity_key = require_integrity_key(integrity_key)

    @staticmethod
    def _strict_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProposalIntegrityError("proposal evidence has duplicate JSON keys")
            result[key] = value
        return result

    def _decode(self, raw: bytes) -> _ProposalSnapshot:
        """Decode and validate authenticated proposal evidence."""
        if raw and not raw.endswith(b"\n"):
            raise ProposalIntegrityError("proposal evidence has an incomplete tail")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProposalIntegrityError("proposal evidence is not UTF-8") from exc

        records = []
        predecessor = _GENESIS_TAG
        for sequence, line in enumerate(text.splitlines(), start=1):
            if not line:
                raise ProposalIntegrityError("proposal evidence has an empty record")
            try:
                record = json.loads(
                    line,
                    object_pairs_hook=self._strict_object,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ProposalIntegrityError("proposal evidence is non-finite")
                    ),
                )
            except (json.JSONDecodeError, ProposalIntegrityError) as exc:
                raise ProposalIntegrityError("proposal evidence is malformed") from exc

            # Validate record schema
            if not isinstance(record, dict) or set(record) != {
                "schema_version", "sequence", "event_type", "payload",
                "predecessor_authentication_tag", "authentication_tag",
            }:
                raise ProposalIntegrityError("proposal evidence schema is invalid")

            # Validate chain integrity
            if (
                record["schema_version"] != _PROPOSAL_SCHEMA
                or record["sequence"] != sequence
                or record["event_type"] not in {"proposal_validated", "lifecycle_updated"}
                or record["predecessor_authentication_tag"] != predecessor
            ):
                raise ProposalIntegrityError("proposal evidence chain is invalid")

            # Validate authentication
            unsigned = {key: value for key, value in record.items() if key != "authentication_tag"}
            if not authenticates(
                self._integrity_key,
                _AUTHENTICATION_DOMAIN,
                _canonical_json(unsigned),
                record["authentication_tag"],
            ):
                raise ProposalIntegrityError("proposal evidence authentication failed")

            # Validate canonical encoding
            if (_canonical_json(record) + b"\n").decode("utf-8") != line + "\n":
                raise ProposalIntegrityError("proposal evidence is not canonical")

            predecessor = record["authentication_tag"]
            records.append(record)

        # Validate proposal history
        self._validate_history(records)

        return _ProposalSnapshot(tuple(records), hashlib.sha256(raw).hexdigest())

    @staticmethod
    def _validate_history(records: list[dict[str, Any]]) -> None:
        """Validate proposal history integrity."""
        proposals: dict[str, dict[str, Any]] = {}

        for record in records:
            payload = record["payload"]
            if not isinstance(payload, dict):
                raise ProposalIntegrityError("proposal payload is invalid")

            proposal_id = payload.get("proposal_id")
            try:
                _require_identifier(proposal_id, "proposal_id")
            except ValueError as exc:
                raise ProposalIntegrityError("proposal identity is invalid") from exc

            if record["event_type"] == "proposal_validated":
                # First proposal record
                if proposal_id in proposals:
                    raise ProposalIntegrityError("duplicate proposal_validated evidence")
                ProposalStore._validate_proposal_payload(payload)
                proposals[proposal_id] = payload
            elif record["event_type"] == "lifecycle_updated":
                # Lifecycle update
                if proposal_id not in proposals:
                    raise ProposalIntegrityError("lifecycle update for unknown proposal")
                ProposalStore._validate_lifecycle_payload(payload, proposals[proposal_id])
                # Update current state
                proposals[proposal_id] = {**proposals[proposal_id], **payload}

    @staticmethod
    def _validate_proposal_payload(payload: dict[str, Any]) -> None:
        """Validate initial proposal payload."""
        required_fields = {
            "proposal_id", "worker_identity", "mission_id", "task_id",
            "action_type", "workspace_id", "immutable_parameters",
            "expected_result", "authorization_level", "approval_required",
            "required_capabilities", "proposal_fingerprint",
            "execution_fingerprint", "approval_execution_fingerprint",
            "lifecycle_state", "created_at",
        }
        if not required_fields.issubset(set(payload)):
            raise ProposalIntegrityError("proposal payload missing required fields")

        try:
            # Validate identifiers
            for field in ["proposal_id", "worker_identity", "mission_id", "task_id", "action_type", "workspace_id"]:
                _require_identifier(payload[field], field)

            # Validate fingerprints
            _require_digest(payload["proposal_fingerprint"], "proposal_fingerprint")
            if payload["execution_fingerprint"] is not None:
                _require_digest(payload["execution_fingerprint"], "execution_fingerprint")
            if payload["approval_execution_fingerprint"] is not None:
                _require_digest(payload["approval_execution_fingerprint"], "approval_execution_fingerprint")

            # Validate authorization level
            AuthorizationLevel(payload["authorization_level"])

            # Validate lifecycle state
            ProposalLifecycleState(payload["lifecycle_state"])

            # Validate timestamp
            _parse_timestamp(payload["created_at"], "created_at")

            # Validate expected_result
            if not isinstance(payload["expected_result"], str) or not payload["expected_result"].strip():
                raise ValueError("expected_result is invalid")

            # Validate immutable_parameters
            _canonical_parameters(payload["immutable_parameters"])

        except (TypeError, ValueError, ProposalIntegrityError) as exc:
            raise ProposalIntegrityError("proposal payload is invalid") from exc

    @staticmethod
    def _validate_lifecycle_payload(payload: dict[str, Any], original: dict[str, Any]) -> None:
        """Validate lifecycle update payload."""
        # Must have proposal_id and at least one lifecycle field
        if "proposal_id" not in payload:
            raise ProposalIntegrityError("lifecycle update missing proposal_id")

        # Cannot change immutable fields
        immutable_fields = {
            "worker_identity", "mission_id", "task_id", "action_type",
            "workspace_id", "immutable_parameters", "authorization_level",
            "approval_required", "required_capabilities", "proposal_fingerprint",
            "execution_fingerprint", "approval_execution_fingerprint",
        }
        for field in immutable_fields:
            if field in payload and payload[field] != original.get(field):
                raise ProposalIntegrityError(f"lifecycle update cannot change {field}")

        # Validate lifecycle state if present
        if "lifecycle_state" in payload:
            try:
                ProposalLifecycleState(payload["lifecycle_state"])
            except ValueError as exc:
                raise ProposalIntegrityError("invalid lifecycle_state") from exc

        # Validate optional identifiers
        for field in ["assignment_id", "dispatch_offer_id", "target_node_id", "approval_request_id", "job_id"]:
            if field in payload and payload[field] is not None:
                try:
                    _require_identifier(payload[field], field)
                except ValueError as exc:
                    raise ProposalIntegrityError(f"invalid {field}") from exc

    def snapshot(self) -> _ProposalSnapshot:
        """Get current authenticated snapshot."""
        with FileLock(self.lock_path):
            try:
                raw = self.path.read_bytes()
            except FileNotFoundError:
                raw = b""
            return self._decode(raw)

    def transact(self, callback):
        """Execute transaction with cross-process safety."""
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path):
            try:
                raw = self.path.read_bytes()
            except FileNotFoundError:
                raw = b""
            snapshot = self._decode(raw)
            payload, event_type, result = callback(snapshot)

            # Callback may indicate no mutation needed (idempotent)
            if payload is None:
                return result

            # Create authenticated record
            unsigned = {
                "schema_version": _PROPOSAL_SCHEMA,
                "sequence": len(snapshot.records) + 1,
                "event_type": event_type,
                "payload": payload,
                "predecessor_authentication_tag": (
                    _GENESIS_TAG if not snapshot.records else snapshot.records[-1]["authentication_tag"]
                ),
            }
            record = {
                **unsigned,
                "authentication_tag": authentication_tag(
                    self._integrity_key, _AUTHENTICATION_DOMAIN, _canonical_json(unsigned)
                ),
            }

            # Validate updated history
            self._validate_history([*snapshot.records, record])

            # Atomic append with fsync
            with self.path.open("ab") as handle:
                handle.write(_canonical_json(record) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())

            # Fsync directory (POSIX only)
            if os.name != "nt":
                directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                directory_fd = os.open(self.root, directory_flags)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)

            return result

    def _proposals_from_snapshot(self, snapshot: _ProposalSnapshot) -> dict[str, dict[str, Any]]:
        """Extract current proposal states from snapshot."""
        proposals = {}
        for record in snapshot.records:
            payload = record["payload"]
            proposal_id = payload["proposal_id"]
            if record["event_type"] == "proposal_validated":
                proposals[proposal_id] = payload
            elif record["event_type"] == "lifecycle_updated":
                if proposal_id in proposals:
                    proposals[proposal_id] = {**proposals[proposal_id], **payload}
        return proposals

    def submit_proposal(
        self,
        *,
        proposal_id: str,
        worker_identity: str,
        mission_id: str,
        task_id: str,
        action_type: str,
        workspace_id: str,
        immutable_parameters: Mapping[str, Any],
        expected_result: str,
        authorization_level: AuthorizationLevel,
        approval_required: bool,
        required_capabilities: frozenset[str],
        proposal_fingerprint: str,
        execution_fingerprint: str | None,
        approval_execution_fingerprint: str | None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> ProposalRecord:
        """
        Submit a new proposal with exact idempotency.

        If an exact duplicate exists, returns existing record.
        If a conflicting duplicate exists, raises ProposalConflict.

        Args:
            proposal_id: Stable client-provided identifier
            worker_identity: AI worker node identity
            mission_id: Mission identifier
            task_id: Task identifier
            action_type: Action type
            workspace_id: Workspace identifier
            immutable_parameters: Immutable action parameters
            expected_result: Expected result description
            authorization_level: Authoritative authorization level
            approval_required: Authoritative approval requirement
            required_capabilities: Authoritative required capabilities
            proposal_fingerprint: Proposal fingerprint
            execution_fingerprint: Console execution fingerprint
            approval_execution_fingerprint: Approval execution fingerprint
            clock: Clock for timestamps

        Returns:
            ProposalRecord (new or existing)

        Raises:
            ProposalConflict: If conflicting proposal exists with same identity
        """
        _require_identifier(proposal_id, "proposal_id")
        _require_identifier(worker_identity, "worker_identity")
        _require_identifier(mission_id, "mission_id")
        _require_identifier(task_id, "task_id")
        _require_identifier(action_type, "action_type")
        _require_identifier(workspace_id, "workspace_id")
        _require_digest(proposal_fingerprint, "proposal_fingerprint")

        if execution_fingerprint is not None:
            _require_digest(execution_fingerprint, "execution_fingerprint")
        if approval_execution_fingerprint is not None:
            _require_digest(approval_execution_fingerprint, "approval_execution_fingerprint")

        if not isinstance(authorization_level, AuthorizationLevel):
            raise TypeError("authorization_level must be an AuthorizationLevel")
        if not isinstance(approval_required, bool):
            raise TypeError("approval_required must be a bool")
        if not isinstance(required_capabilities, frozenset):
            raise TypeError("required_capabilities must be a frozenset")

        canonical_params = _canonical_parameters(immutable_parameters)

        def _submit_callback(snapshot: _ProposalSnapshot):
            proposals = self._proposals_from_snapshot(snapshot)

            # Check for existing proposal
            if proposal_id in proposals:
                existing = proposals[proposal_id]

                # Exact idempotency: same immutable subject
                if existing["proposal_fingerprint"] == proposal_fingerprint:
                    # Return existing record (no mutation)
                    return None, None, self._record_from_payload(existing)

                # Conflict: same identity, different subject
                raise ProposalConflict(
                    f"proposal {proposal_id} conflicts with existing evidence "
                    f"(expected fingerprint {proposal_fingerprint}, "
                    f"found {existing['proposal_fingerprint']})"
                )

            # New proposal
            now = clock()
            payload = {
                "proposal_id": proposal_id,
                "worker_identity": worker_identity,
                "mission_id": mission_id,
                "task_id": task_id,
                "action_type": action_type,
                "workspace_id": workspace_id,
                "immutable_parameters": canonical_params,
                "expected_result": expected_result,
                "authorization_level": authorization_level.value,
                "approval_required": approval_required,
                "required_capabilities": list(sorted(required_capabilities)),
                "proposal_fingerprint": proposal_fingerprint,
                "execution_fingerprint": execution_fingerprint,
                "approval_execution_fingerprint": approval_execution_fingerprint,
                "lifecycle_state": ProposalLifecycleState.VALIDATED.value,
                "task_request_fingerprint": None,
                "assignment_id": None,
                "dispatch_offer_id": None,
                "target_node_id": None,
                "approval_request_id": None,
                "job_id": None,
                "result_reference": None,
                "created_at": _timestamp(now),
                "updated_at": _timestamp(now),
                "completed_at": None,
                "failure_reason": None,
            }

            return payload, "proposal_validated", self._record_from_payload(payload)

        return self.transact(_submit_callback)

    def update_lifecycle(
        self,
        proposal_id: str,
        *,
        lifecycle_state: ProposalLifecycleState | None = None,
        task_request_fingerprint: str | None = None,
        assignment_id: str | None = None,
        dispatch_offer_id: str | None = None,
        target_node_id: str | None = None,
        approval_request_id: str | None = None,
        job_id: str | None = None,
        result_reference: str | None = None,
        completed_at: str | None = None,
        failure_reason: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> ProposalRecord:
        """
        Update proposal lifecycle state.

        Args:
            proposal_id: Proposal identifier
            lifecycle_state: New lifecycle state
            task_request_fingerprint: TaskRequest fingerprint
            assignment_id: Assignment identifier
            dispatch_offer_id: Dispatch offer identifier
            target_node_id: Target node identifier
            approval_request_id: Approval request identifier
            job_id: Job identifier
            result_reference: Result reference
            completed_at: Completion timestamp
            failure_reason: Failure reason
            clock: Clock for timestamps

        Returns:
            Updated ProposalRecord

        Raises:
            ProposalNotFound: If proposal does not exist
        """
        _require_identifier(proposal_id, "proposal_id")

        def _update_callback(snapshot: _ProposalSnapshot):
            proposals = self._proposals_from_snapshot(snapshot)

            if proposal_id not in proposals:
                raise ProposalNotFound(f"proposal {proposal_id} not found")

            current = proposals[proposal_id]

            # Build update payload
            update = {"proposal_id": proposal_id}

            if lifecycle_state is not None:
                if not isinstance(lifecycle_state, ProposalLifecycleState):
                    raise TypeError("lifecycle_state must be a ProposalLifecycleState")
                update["lifecycle_state"] = lifecycle_state.value

            if task_request_fingerprint is not None:
                _require_digest(task_request_fingerprint, "task_request_fingerprint")
                update["task_request_fingerprint"] = task_request_fingerprint

            for field, value in [
                ("assignment_id", assignment_id),
                ("dispatch_offer_id", dispatch_offer_id),
                ("target_node_id", target_node_id),
                ("approval_request_id", approval_request_id),
                ("job_id", job_id),
            ]:
                if value is not None:
                    _require_identifier(value, field)
                    update[field] = value

            if result_reference is not None:
                update["result_reference"] = result_reference

            if completed_at is not None:
                _parse_timestamp(completed_at, "completed_at")
                update["completed_at"] = completed_at

            if failure_reason is not None:
                if not isinstance(failure_reason, str):
                    raise TypeError("failure_reason must be a str")
                update["failure_reason"] = failure_reason

            now = clock()
            update["updated_at"] = _timestamp(now)

            # Only write if there are actual changes beyond proposal_id and updated_at
            if len(update) == 2:
                # No actual changes
                return None, None, self._record_from_payload(current)

            updated = {**current, **update}
            return update, "lifecycle_updated", self._record_from_payload(updated)

        return self.transact(_update_callback)

    def get_proposal(self, proposal_id: str) -> ProposalRecord | None:
        """
        Get current proposal record by identity.

        Args:
            proposal_id: Proposal identifier

        Returns:
            ProposalRecord if found, None otherwise
        """
        _require_identifier(proposal_id, "proposal_id")
        snapshot = self.snapshot()
        proposals = self._proposals_from_snapshot(snapshot)
        if proposal_id not in proposals:
            return None
        return self._record_from_payload(proposals[proposal_id])

    def list_proposals(
        self,
        *,
        lifecycle_state: ProposalLifecycleState | None = None,
        worker_identity: str | None = None,
        mission_id: str | None = None,
    ) -> list[ProposalRecord]:
        """
        List proposals with optional filtering.

        Args:
            lifecycle_state: Filter by lifecycle state
            worker_identity: Filter by worker identity
            mission_id: Filter by mission

        Returns:
            List of matching ProposalRecords
        """
        snapshot = self.snapshot()
        proposals = self._proposals_from_snapshot(snapshot)

        results = []
        for payload in proposals.values():
            if lifecycle_state is not None and payload["lifecycle_state"] != lifecycle_state.value:
                continue
            if worker_identity is not None and payload["worker_identity"] != worker_identity:
                continue
            if mission_id is not None and payload["mission_id"] != mission_id:
                continue
            results.append(self._record_from_payload(payload))

        return results

    @staticmethod
    def _record_from_payload(payload: dict[str, Any]) -> ProposalRecord:
        """Convert payload to ProposalRecord."""
        return ProposalRecord(
            proposal_id=payload["proposal_id"],
            worker_identity=payload["worker_identity"],
            mission_id=payload["mission_id"],
            task_id=payload["task_id"],
            action_type=payload["action_type"],
            workspace_id=payload["workspace_id"],
            immutable_parameters=payload["immutable_parameters"],
            expected_result=payload["expected_result"],
            authorization_level=AuthorizationLevel(payload["authorization_level"]),
            approval_required=payload["approval_required"],
            required_capabilities=tuple(payload["required_capabilities"]),
            proposal_fingerprint=payload["proposal_fingerprint"],
            execution_fingerprint=payload["execution_fingerprint"],
            approval_execution_fingerprint=payload["approval_execution_fingerprint"],
            lifecycle_state=ProposalLifecycleState(payload["lifecycle_state"]),
            task_request_fingerprint=payload.get("task_request_fingerprint"),
            assignment_id=payload.get("assignment_id"),
            dispatch_offer_id=payload.get("dispatch_offer_id"),
            target_node_id=payload.get("target_node_id"),
            approval_request_id=payload.get("approval_request_id"),
            job_id=payload.get("job_id"),
            result_reference=payload.get("result_reference"),
            created_at=payload.get("created_at"),
            updated_at=payload.get("updated_at"),
            completed_at=payload.get("completed_at"),
            failure_reason=payload.get("failure_reason"),
        )
