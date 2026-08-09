"""
Approval Center v0.1 - Foundation

Read-only projection for governed pending approval items.

This module provides deterministic query and serialization behavior
for pending approval items sourced from ApprovalLog proposal records.

Do NOT implement approve/reject mutations or execution transitions yet.
Integration with Console Action Execution Runtime authority model will
happen after that milestone is accepted.

Security Invariant:
  The read model must never manufacture authority. If required
  task/job/assignment/offer relationships cannot be proven using
  currently accepted repository state, items are marked non-actionable
  or omitted from actionable pending results.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from federation.integrity import authentication_tag, authenticates, require_integrity_key
from tools.ai_controller.mission.models import ApprovalPolicy
from tools.ai_controller._locking import FileLock
from tools.ai_controller.operations_api.approvals import (
    ApprovalEvidenceCorrupt,
    ApprovalLog,
)
from federation.task_request import AuthorizationLevel


class ApprovalCenterError(Exception):
    """Base exception for Approval Center errors."""
    pass


class ApprovalItemNotFound(ApprovalCenterError):
    """Approval item not found by stable identity."""
    pass


class ApprovalItemNotActionable(ApprovalCenterError):
    """Approval item exists but is not currently actionable."""
    pass


@dataclass(frozen=True, slots=True)
class ApprovalItem:
    """
    Deterministic read model projection for a pending governed action.

    Exposes sufficient information for a human operator to make an
    approval decision without exposing raw secrets or unrestricted
    execution parameters.

    Sourced from ApprovalLog proposal records with validated evidence linkage.
    """

    # Stable identities
    action_id: str
    action_type: str
    proposal_revision: str

    # Mission/task context
    mission_id: str
    mission_task_id: str | None

    # Governance metadata
    authorization_level: AuthorizationLevel | None
    approval_required: bool
    approval_policy: ApprovalPolicy | None

    # Lifecycle/temporal
    status: str
    created_at: str  # ISO-8601
    expires_at: str  # ISO-8601

    # Human-readable context
    finding: str
    authority: str

    # Evidence integrity
    evidence_fingerprint: str
    proposed_effect_fingerprint: str

    # Expected authoritative state at proposal time
    expected_mission_revision: str
    expected_event_revision: str
    expected_queue_identities: tuple[str, ...]
    expected_queue_revision: str

    # Actionability
    is_actionable: bool
    non_actionable_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        """Safe public serialization without secrets."""
        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "proposal_revision": self.proposal_revision,
            "mission_id": self.mission_id,
            "mission_task_id": self.mission_task_id,
            "authorization_level": self.authorization_level.value if self.authorization_level else None,
            "approval_required": self.approval_required,
            "approval_policy": self.approval_policy.value if self.approval_policy else None,
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "finding": self.finding[:500],  # Truncate for safety
            "authority": self.authority,
            "evidence_fingerprint": self.evidence_fingerprint,
            "proposed_effect_fingerprint": self.proposed_effect_fingerprint,
            "expected_mission_revision": self.expected_mission_revision,
            "expected_event_revision": self.expected_event_revision,
            "expected_queue_identities": list(self.expected_queue_identities),
            "expected_queue_revision": self.expected_queue_revision,
            "is_actionable": self.is_actionable,
            "non_actionable_reason": self.non_actionable_reason,
        }


@dataclass(frozen=True, slots=True)
class ApprovalItemQuery:
    """Query filter for pending approval items."""

    # Filter by status
    include_pending: bool = True
    include_expired: bool = False
    include_approved: bool = False
    include_rejected: bool = False

    # Filter by actionability
    actionable_only: bool = True

    # Filter by mission/task
    mission_id: str | None = None
    mission_task_id: str | None = None
    action_type: str | None = None

    # Ordering (future extension point)
    order_by: str = "created_at"  # created_at | expires_at
    ascending: bool = False  # newest first by default


class ApprovalCenter:
    """
    Read-only projection for governed pending approval items.

    Projects pending approval items from ApprovalLog proposal records
    with deterministic query/filtering behavior.

    Fails closed when required authoritative linkage is missing or inconsistent.
    """

    def __init__(self, root: Path) -> None:
        """
        Initialize Approval Center with ApprovalLog root.

        Args:
            root: Path to approval log root directory
        """
        self.root = Path(root)
        self.approval_log = ApprovalLog(root, create=False)

    @staticmethod
    def _parse_iso8601(timestamp: str) -> datetime:
        """Parse ISO-8601 timestamp, fail closed on parse errors."""
        try:
            # Handle both with and without microseconds
            if "." in timestamp:
                return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            else:
                return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            # Fail closed - treat as expired if we can't parse
            return datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _is_expired(expires_at: str) -> bool:
        """Check if proposal is expired."""
        expiry = ApprovalCenter._parse_iso8601(expires_at)
        now = datetime.now(timezone.utc)
        return now >= expiry

    @staticmethod
    def _extract_authorization_level(proposal: dict[str, Any]) -> AuthorizationLevel | None:
        """
        Extract authorization level from proposal metadata.

        Fails closed by returning None if authorization level cannot be determined.
        """
        # Authorization level may be in classification metadata (future extension)
        # For now, return None as it's not in the base proposal schema
        classification = proposal.get("classification")
        if isinstance(classification, dict):
            level_str = classification.get("authorization_level")
            if level_str:
                try:
                    return AuthorizationLevel(level_str)
                except ValueError:
                    return None
        return None

    @staticmethod
    def _extract_approval_policy(proposal: dict[str, Any]) -> ApprovalPolicy | None:
        """
        Extract approval policy from proposal metadata.

        Fails closed by returning None if approval policy cannot be determined.
        """
        # Approval policy may be in classification metadata (future extension)
        classification = proposal.get("classification")
        if isinstance(classification, dict):
            policy_str = classification.get("approval_policy")
            if policy_str:
                try:
                    return ApprovalPolicy(policy_str)
                except ValueError:
                    return None
        return None

    @staticmethod
    def _determine_actionability(
        proposal: dict[str, Any],
        status: str,
        is_expired: bool,
    ) -> tuple[bool, str | None]:
        """
        Determine if proposal is currently actionable.

        Args:
            proposal: Proposal record
            status: Proposal status
            is_expired: Whether proposal is expired

        Returns:
            Tuple of (is_actionable, non_actionable_reason)
        """
        # Terminal states are not actionable
        if status in {"approved", "rejected", "applied", "cancelled", "superseded"}:
            return False, f"already {status}"

        # Expired proposals are not actionable
        if is_expired:
            return False, "expired"

        # Proposals with missing required evidence are not actionable
        if not proposal.get("evidence_fingerprint"):
            return False, "missing evidence fingerprint"

        if not proposal.get("proposed_effect_fingerprint"):
            return False, "missing proposed effect fingerprint"

        # Pending proposals with valid evidence are actionable
        if status == "pending":
            return True, None

        # Unknown status - fail closed
        return False, f"unknown status: {status}"

    def _project_approval_item(
        self,
        proposal: dict[str, Any],
        decisions: dict[str, dict[str, Any]],
    ) -> ApprovalItem:
        """
        Project a single approval item from proposal record.

        Args:
            proposal: Proposal record from ApprovalLog
            decisions: Dict of action_id -> decision record

        Returns:
            ApprovalItem projection
        """
        action_id = proposal["action_id"]

        # Determine current status based on proposal status and any decision
        status = proposal.get("status", "pending")
        if action_id in decisions:
            decision = decisions[action_id]
            decision_type = decision.get("decision", "")
            if decision_type == "approve":
                status = "approved"
            elif decision_type == "reject":
                status = "rejected"

        # Check expiration
        expires_at = proposal.get("expires_at", "")
        is_expired = self._is_expired(expires_at)

        # Determine actionability
        is_actionable, non_actionable_reason = self._determine_actionability(
            proposal,
            status,
            is_expired,
        )

        # Extract governance metadata
        authorization_level = self._extract_authorization_level(proposal)
        approval_policy = self._extract_approval_policy(proposal)

        # For v0.1, assume approval_required=True for all proposals in the log
        # (proposals wouldn't be in the log if approval wasn't required)
        approval_required = True

        # Extract queue identities
        queue_identities = proposal.get("expected_queue_identities", [])
        if not isinstance(queue_identities, list):
            queue_identities = []

        return ApprovalItem(
            action_id=action_id,
            action_type=proposal.get("action_type", ""),
            proposal_revision=proposal.get("proposal_revision", ""),
            mission_id=proposal.get("mission_id", ""),
            mission_task_id=proposal.get("mission_task_id"),
            authorization_level=authorization_level,
            approval_required=approval_required,
            approval_policy=approval_policy,
            status=status,
            created_at=proposal.get("created_at", ""),
            expires_at=expires_at,
            finding=proposal.get("finding", ""),
            authority=proposal.get("authority", ""),
            evidence_fingerprint=proposal.get("evidence_fingerprint", ""),
            proposed_effect_fingerprint=proposal.get("proposed_effect_fingerprint", ""),
            expected_mission_revision=proposal.get("expected_mission_revision", ""),
            expected_event_revision=proposal.get("expected_event_revision", ""),
            expected_queue_identities=tuple(queue_identities),
            expected_queue_revision=proposal.get("expected_queue_revision", ""),
            is_actionable=is_actionable,
            non_actionable_reason=non_actionable_reason,
        )

    def list_pending_approvals(
        self,
        query: ApprovalItemQuery | None = None,
    ) -> list[ApprovalItem]:
        """
        List pending approval items with optional filtering.

        Args:
            query: Optional query filter (defaults to actionable pending items)

        Returns:
            List of ApprovalItem projections matching query criteria

        Raises:
            ApprovalEvidenceCorrupt: If approval log is corrupt
        """
        if query is None:
            query = ApprovalItemQuery()

        # Load approval log snapshot
        try:
            snapshot = self.approval_log.snapshot()
        except ApprovalEvidenceCorrupt:
            raise

        # Parse records into proposals and decisions
        proposals: dict[str, dict[str, Any]] = {}
        decisions: dict[str, dict[str, Any]] = {}

        for record in snapshot.records:
            record_type = record.get("record_type")
            if record_type == "proposal":
                proposal = record.get("proposal", {})
                action_id = proposal.get("action_id")
                if action_id:
                    proposals[action_id] = proposal
            elif record_type == "decision":
                decision = record.get("decision", {})
                action_id = decision.get("action_id")
                if action_id:
                    decisions[action_id] = decision

        # Project approval items
        items: list[ApprovalItem] = []
        for action_id, proposal in proposals.items():
            try:
                item = self._project_approval_item(proposal, decisions)
            except (KeyError, ValueError, TypeError):
                # Fail closed - skip items we can't project
                continue

            # Apply query filters
            if query.actionable_only and not item.is_actionable:
                continue

            # Filter by status
            if item.status == "pending" and not query.include_pending:
                continue
            if self._is_expired(item.expires_at) and not query.include_expired:
                continue
            if item.status == "approved" and not query.include_approved:
                continue
            if item.status == "rejected" and not query.include_rejected:
                continue

            # Filter by mission/task/action
            if query.mission_id and item.mission_id != query.mission_id:
                continue
            if query.mission_task_id and item.mission_task_id != query.mission_task_id:
                continue
            if query.action_type and item.action_type != query.action_type:
                continue

            items.append(item)

        # Apply stable deterministic ordering
        if query.order_by == "created_at":
            items.sort(
                key=lambda x: (x.created_at, x.action_id),
                reverse=not query.ascending,
            )
        elif query.order_by == "expires_at":
            items.sort(
                key=lambda x: (x.expires_at, x.action_id),
                reverse=not query.ascending,
            )
        else:
            # Default: order by created_at descending
            items.sort(
                key=lambda x: (x.created_at, x.action_id),
                reverse=True,
            )

        return items

    def get_approval_item(self, action_id: str) -> ApprovalItem:
        """
        Retrieve a single approval item by stable identity.

        Args:
            action_id: Unique action identifier

        Returns:
            ApprovalItem projection

        Raises:
            ApprovalItemNotFound: If item not found
            ApprovalEvidenceCorrupt: If approval log is corrupt
        """
        # Load approval log snapshot
        try:
            snapshot = self.approval_log.snapshot()
        except ApprovalEvidenceCorrupt:
            raise

        # Parse records to find proposal
        proposal: dict[str, Any] | None = None
        decisions: dict[str, dict[str, Any]] = {}

        for record in snapshot.records:
            record_type = record.get("record_type")
            if record_type == "proposal":
                prop = record.get("proposal", {})
                if prop.get("action_id") == action_id:
                    proposal = prop
            elif record_type == "decision":
                decision = record.get("decision", {})
                dec_action_id = decision.get("action_id")
                if dec_action_id:
                    decisions[dec_action_id] = decision

        if proposal is None:
            raise ApprovalItemNotFound(f"approval item not found: {action_id}")

        # Project approval item
        try:
            return self._project_approval_item(proposal, decisions)
        except (KeyError, ValueError, TypeError) as exc:
            # Fail closed - cannot project item with missing/invalid required fields
            raise ApprovalCenterError(
                f"cannot project approval item {action_id}: {exc}"
            ) from exc

    def count_pending_approvals(self, query: ApprovalItemQuery | None = None) -> int:
        """
        Count pending approval items matching query criteria.

        Args:
            query: Optional query filter

        Returns:
            Count of matching items
        """
        return len(self.list_pending_approvals(query))

    def has_pending_approvals(self, mission_id: str | None = None) -> bool:
        """
        Check if there are any actionable pending approvals.

        Args:
            mission_id: Optional mission filter

        Returns:
            True if any actionable pending approvals exist
        """
        query = ApprovalItemQuery(
            actionable_only=True,
            mission_id=mission_id,
        )
        return self.count_pending_approvals(query) > 0


# ---------------------------------------------------------------------------
# Durable Approval Center authority v0.1
# ---------------------------------------------------------------------------

_AUTHORITY_SCHEMA = "approval-center-v0.1"
_AUTHENTICATION_DOMAIN = b"raghub.approval-center.v0.1"
_GENESIS_TAG = "0" * 64
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalConflict(ApprovalCenterError):
    """An immutable request or terminal decision conflicts with evidence."""


class ApprovalUnauthorized(ApprovalCenterError):
    """The authenticated actor is not authorized for the requested operation."""


class ApprovalExpired(ApprovalCenterError):
    """The approval request is no longer valid."""


class ApprovalIntegrityError(ApprovalEvidenceCorrupt):
    """Durable Approval Center evidence is malformed or unauthentic."""


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
        raise ValueError("immutable parameters must be finite JSON values") from exc


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
        raise ValueError("approval clock must return an aware datetime")
    return value.astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ApprovalIntegrityError(f"{field_name} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApprovalIntegrityError(f"{field_name} is invalid") from exc
    if parsed.tzinfo is None:
        raise ApprovalIntegrityError(f"{field_name} must include a timezone")
    canonical = parsed.astimezone(timezone.utc)
    if canonical.isoformat() != value:
        raise ApprovalIntegrityError(f"{field_name} is not canonical UTC")
    return canonical


def make_execution_fingerprint(
    *,
    action_type: str,
    workspace_id: str | None,
    authorization_level: AuthorizationLevel | str,
    approval_required: bool,
    immutable_parameters: Mapping[str, Any],
) -> str:
    """Fingerprint the exact immutable action subject to approval."""
    _require_identifier(action_type, "action_type")
    workspace_id = _require_optional_identifier(workspace_id, "workspace_id")
    try:
        level = AuthorizationLevel(authorization_level).value
    except (TypeError, ValueError) as exc:
        raise ValueError("authorization_level is invalid") from exc
    if not isinstance(approval_required, bool):
        raise TypeError("approval_required must be a boolean")
    payload = {
        "schema_version": _AUTHORITY_SCHEMA,
        "action_type": action_type,
        "workspace_id": workspace_id,
        "authorization_level": level,
        "approval_required": approval_required,
        "immutable_parameters": _canonical_parameters(immutable_parameters),
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class ApprovalRequestDraft:
    approval_request_id: str
    mission_id: str
    task_id: str
    requester_identity: str
    action_type: str
    authorization_level: AuthorizationLevel
    approval_required: bool
    execution_fingerprint: str
    immutable_parameters: Mapping[str, Any]
    expected_result: str
    assignment_id: str | None = None
    dispatch_offer_id: str | None = None
    target_node_id: str | None = None
    workspace_id: str | None = None
    expires_in_seconds: int = 3600


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    approval_request_id: str
    mission_id: str
    task_id: str
    assignment_id: str | None
    dispatch_offer_id: str | None
    requester_identity: str
    target_node_id: str | None
    action_type: str
    authorization_level: AuthorizationLevel
    approval_required: bool
    workspace_id: str | None
    execution_fingerprint: str
    immutable_parameters_fingerprint: str
    expected_result: str
    created_at: str
    expires_at: str
    request_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_request_id": self.approval_request_id,
            "mission_id": self.mission_id,
            "task_id": self.task_id,
            "assignment_id": self.assignment_id,
            "dispatch_offer_id": self.dispatch_offer_id,
            "requester_identity": self.requester_identity,
            "target_node_id": self.target_node_id,
            "action_type": self.action_type,
            "authorization_level": self.authorization_level.value,
            "approval_required": self.approval_required,
            "workspace_id": self.workspace_id,
            "execution_fingerprint": self.execution_fingerprint,
            "immutable_parameters_fingerprint": self.immutable_parameters_fingerprint,
            "expected_result": self.expected_result,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "request_fingerprint": self.request_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    approval_request_id: str
    status: ApprovalStatus
    actor_identity: str
    decided_at: str
    reason: str | None
    execution_fingerprint: str
    request_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_request_id": self.approval_request_id,
            "status": self.status.value,
            "actor_identity": self.actor_identity,
            "decided_at": self.decided_at,
            "reason": self.reason,
            "execution_fingerprint": self.execution_fingerprint,
            "request_fingerprint": self.request_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class ApprovalEvidence:
    request: ApprovalRequest
    status: ApprovalStatus
    decision: ApprovalDecision | None
    evidence_revision: str
    valid: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "status": self.status.value,
            "decision": None if self.decision is None else self.decision.to_dict(),
            "evidence_revision": self.evidence_revision,
            "valid": self.valid,
        }


@dataclass(frozen=True, slots=True)
class _AuthoritySnapshot:
    records: tuple[dict[str, Any], ...]
    revision: str


class ApprovalStore:
    """Authenticated append-only approval request and decision evidence."""

    def __init__(self, root: Path, *, integrity_key: bytes) -> None:
        self.root = Path(root)
        self.path = self.root / "approval-center-v0.1.jsonl"
        self.lock_path = self.root / ".approval-center-v0.1.lock"
        self._integrity_key = require_integrity_key(integrity_key)

    @staticmethod
    def _strict_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ApprovalIntegrityError("approval evidence has duplicate JSON keys")
            result[key] = value
        return result

    def _decode(self, raw: bytes) -> _AuthoritySnapshot:
        if raw and not raw.endswith(b"\n"):
            raise ApprovalIntegrityError("approval evidence has an incomplete tail")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ApprovalIntegrityError("approval evidence is not UTF-8") from exc
        records = []
        predecessor = _GENESIS_TAG
        for sequence, line in enumerate(text.splitlines(), start=1):
            if not line:
                raise ApprovalIntegrityError("approval evidence has an empty record")
            try:
                record = json.loads(
                    line,
                    object_pairs_hook=self._strict_object,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ApprovalIntegrityError("approval evidence is non-finite")
                    ),
                )
            except (json.JSONDecodeError, ApprovalIntegrityError) as exc:
                raise ApprovalIntegrityError("approval evidence is malformed") from exc
            if not isinstance(record, dict) or set(record) != {
                "schema_version", "sequence", "event_type", "payload",
                "predecessor_authentication_tag", "authentication_tag",
            }:
                raise ApprovalIntegrityError("approval evidence schema is invalid")
            if (
                record["schema_version"] != _AUTHORITY_SCHEMA
                or record["sequence"] != sequence
                or record["event_type"] not in {"request_created", "decision_recorded"}
                or record["predecessor_authentication_tag"] != predecessor
            ):
                raise ApprovalIntegrityError("approval evidence chain is invalid")
            unsigned = {key: value for key, value in record.items() if key != "authentication_tag"}
            if not authenticates(
                self._integrity_key,
                _AUTHENTICATION_DOMAIN,
                _canonical_json(unsigned),
                record["authentication_tag"],
            ):
                raise ApprovalIntegrityError("approval evidence authentication failed")
            if (_canonical_json(record) + b"\n").decode("utf-8") != line + "\n":
                raise ApprovalIntegrityError("approval evidence is not canonical")
            predecessor = record["authentication_tag"]
            records.append(record)
        self._validate_history(records)
        return _AuthoritySnapshot(tuple(records), hashlib.sha256(raw).hexdigest())

    @staticmethod
    def _validate_history(records: list[dict[str, Any]]) -> None:
        requests: dict[str, dict[str, Any]] = {}
        decisions: dict[str, dict[str, Any]] = {}
        for record in records:
            payload = record["payload"]
            if not isinstance(payload, dict):
                raise ApprovalIntegrityError("approval payload is invalid")
            request_id = payload.get("approval_request_id")
            try:
                _require_identifier(request_id, "approval_request_id")
            except ValueError as exc:
                raise ApprovalIntegrityError("approval request identity is invalid") from exc
            if record["event_type"] == "request_created":
                if request_id in requests:
                    raise ApprovalIntegrityError("duplicate approval request evidence")
                ApprovalStore._validate_request_payload(payload)
                requests[request_id] = payload
            else:
                if request_id not in requests or request_id in decisions:
                    raise ApprovalIntegrityError("ambiguous approval decision evidence")
                ApprovalStore._validate_decision_payload(payload, requests[request_id])
                decisions[request_id] = payload

    @staticmethod
    def _validate_request_payload(payload: dict[str, Any]) -> None:
        fields = {
            "approval_request_id", "mission_id", "task_id", "assignment_id",
            "dispatch_offer_id", "requester_identity", "target_node_id",
            "action_type", "authorization_level", "approval_required",
            "workspace_id", "execution_fingerprint", "immutable_parameters",
            "expected_result", "created_at", "expires_at", "request_fingerprint",
        }
        if set(payload) != fields:
            raise ApprovalIntegrityError("approval request schema is invalid")
        try:
            for field in ("approval_request_id", "mission_id", "task_id", "requester_identity", "action_type"):
                _require_identifier(payload[field], field)
            for field in ("assignment_id", "dispatch_offer_id", "target_node_id", "workspace_id"):
                _require_optional_identifier(payload[field], field)
            _require_digest(payload["execution_fingerprint"], "execution_fingerprint")
            _require_digest(payload["request_fingerprint"], "request_fingerprint")
            AuthorizationLevel(payload["authorization_level"])
            if payload["approval_required"] is not True:
                raise ValueError("Approval Center accepts approval-required actions only")
            parameters = _canonical_parameters(payload["immutable_parameters"])
            expected = make_execution_fingerprint(
                action_type=payload["action_type"],
                workspace_id=payload["workspace_id"],
                authorization_level=payload["authorization_level"],
                approval_required=True,
                immutable_parameters=parameters,
            )
            if expected != payload["execution_fingerprint"]:
                raise ValueError("execution fingerprint does not bind request")
            created = _parse_timestamp(payload["created_at"], "created_at")
            expires = _parse_timestamp(payload["expires_at"], "expires_at")
            if expires <= created:
                raise ValueError("approval expiration must follow creation")
            if not isinstance(payload["expected_result"], str) or not payload["expected_result"].strip():
                raise ValueError("expected_result is invalid")
            bound = {key: value for key, value in payload.items() if key != "request_fingerprint"}
            if hashlib.sha256(_canonical_json(bound)).hexdigest() != payload["request_fingerprint"]:
                raise ValueError("request fingerprint is invalid")
        except (TypeError, ValueError, ApprovalIntegrityError) as exc:
            raise ApprovalIntegrityError("approval request evidence is invalid") from exc

    @staticmethod
    def _validate_decision_payload(payload: dict[str, Any], request: dict[str, Any]) -> None:
        if set(payload) != {
            "approval_request_id", "status", "actor_identity", "decided_at",
            "reason", "execution_fingerprint", "request_fingerprint",
        }:
            raise ApprovalIntegrityError("approval decision schema is invalid")
        try:
            _require_identifier(payload["actor_identity"], "actor_identity")
            status = ApprovalStatus(payload["status"])
            if status not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED}:
                raise ValueError("decision is not terminal")
            decided = _parse_timestamp(payload["decided_at"], "decided_at")
            created = _parse_timestamp(request["created_at"], "created_at")
            if decided < created:
                raise ValueError("decision predates request")
            if payload["reason"] is not None and (
                not isinstance(payload["reason"], str) or not payload["reason"].strip() or len(payload["reason"]) > 1000
            ):
                raise ValueError("decision reason is invalid")
            if (
                payload["execution_fingerprint"] != request["execution_fingerprint"]
                or payload["request_fingerprint"] != request["request_fingerprint"]
            ):
                raise ValueError("decision belongs to foreign request evidence")
        except (TypeError, ValueError, ApprovalIntegrityError) as exc:
            raise ApprovalIntegrityError("approval decision evidence is invalid") from exc

    def snapshot(self) -> _AuthoritySnapshot:
        with FileLock(self.lock_path):
            try:
                raw = self.path.read_bytes()
            except FileNotFoundError:
                raw = b""
            return self._decode(raw)

    def transact(self, callback):
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path):
            try:
                raw = self.path.read_bytes()
            except FileNotFoundError:
                raw = b""
            snapshot = self._decode(raw)
            payload, event_type, result = callback(snapshot)
            if payload is None:
                return result
            unsigned = {
                "schema_version": _AUTHORITY_SCHEMA,
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
            self._validate_history([*snapshot.records, record])
            with self.path.open("ab") as handle:
                handle.write(_canonical_json(record) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                directory_fd = os.open(self.root, directory_flags)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return result


class ApprovalCoordinator:
    """Create, decide, inspect, and verify exact durable approval evidence."""

    def __init__(
        self,
        store: ApprovalStore,
        *,
        authorized_approvers: set[str] | frozenset[str],
        authorized_requesters: set[str] | frozenset[str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not isinstance(store, ApprovalStore):
            raise TypeError("store must be an ApprovalStore")
        self.store = store
        self.authorized_approvers = frozenset(
            _require_identifier(item, "authorized approver") for item in authorized_approvers
        )
        if not self.authorized_approvers:
            raise ValueError("at least one authorized approver is required")
        self.authorized_requesters = None if authorized_requesters is None else frozenset(
            _require_identifier(item, "authorized requester") for item in authorized_requesters
        )
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.clock = clock

    def create_request(self, draft: ApprovalRequestDraft, *, actor_identity: str) -> ApprovalEvidence:
        if not isinstance(draft, ApprovalRequestDraft):
            raise TypeError("draft must be an ApprovalRequestDraft")
        actor = _require_identifier(actor_identity, "actor_identity")
        if actor != draft.requester_identity or (
            self.authorized_requesters is not None and actor not in self.authorized_requesters
        ):
            raise ApprovalUnauthorized("requester identity is not authoritative")
        if draft.approval_required is not True:
            raise ValueError("Approval Center accepts approval-required actions only")
        if not isinstance(draft.expires_in_seconds, int) or isinstance(draft.expires_in_seconds, bool) or not 1 <= draft.expires_in_seconds <= 86400:
            raise ValueError("expires_in_seconds must be between 1 and 86400")
        parameters = _canonical_parameters(draft.immutable_parameters)
        expected_execution = make_execution_fingerprint(
            action_type=draft.action_type,
            workspace_id=draft.workspace_id,
            authorization_level=draft.authorization_level,
            approval_required=True,
            immutable_parameters=parameters,
        )
        if _require_digest(draft.execution_fingerprint, "execution_fingerprint") != expected_execution:
            raise ValueError("execution fingerprint does not bind exact request")
        now = self._now()
        expires = now.timestamp() + draft.expires_in_seconds
        expires_at = datetime.fromtimestamp(expires, tz=timezone.utc)
        payload = {
            "approval_request_id": _require_identifier(draft.approval_request_id, "approval_request_id"),
            "mission_id": _require_identifier(draft.mission_id, "mission_id"),
            "task_id": _require_identifier(draft.task_id, "task_id"),
            "assignment_id": _require_optional_identifier(draft.assignment_id, "assignment_id"),
            "dispatch_offer_id": _require_optional_identifier(draft.dispatch_offer_id, "dispatch_offer_id"),
            "requester_identity": _require_identifier(draft.requester_identity, "requester_identity"),
            "target_node_id": _require_optional_identifier(draft.target_node_id, "target_node_id"),
            "action_type": _require_identifier(draft.action_type, "action_type"),
            "authorization_level": AuthorizationLevel(draft.authorization_level).value,
            "approval_required": True,
            "workspace_id": _require_optional_identifier(draft.workspace_id, "workspace_id"),
            "execution_fingerprint": expected_execution,
            "immutable_parameters": parameters,
            "expected_result": draft.expected_result,
            "created_at": _timestamp(now),
            "expires_at": _timestamp(expires_at),
        }
        payload["request_fingerprint"] = hashlib.sha256(_canonical_json(payload)).hexdigest()

        def mutate(snapshot):
            existing = self._payloads(snapshot)[0].get(payload["approval_request_id"])
            if existing is not None:
                immutable_fields = {
                    "approval_request_id", "mission_id", "task_id", "assignment_id",
                    "dispatch_offer_id", "requester_identity", "target_node_id",
                    "action_type", "authorization_level", "approval_required",
                    "workspace_id", "execution_fingerprint", "immutable_parameters",
                    "expected_result",
                }
                existing_ttl = int(
                    (
                        _parse_timestamp(existing["expires_at"], "expires_at")
                        - _parse_timestamp(existing["created_at"], "created_at")
                    ).total_seconds()
                )
                if (
                    all(existing[field] == payload[field] for field in immutable_fields)
                    and existing_ttl == draft.expires_in_seconds
                ):
                    return None, None, None
                raise ApprovalConflict("approval request identity conflicts with existing evidence")
            return payload, "request_created", None

        self.store.transact(mutate)
        return self.inspect(payload["approval_request_id"])

    def approve(self, request_id: str, *, actor_identity: str, execution_fingerprint: str, reason: str | None = None) -> ApprovalEvidence:
        return self._decide(request_id, ApprovalStatus.APPROVED, actor_identity, execution_fingerprint, reason)

    def reject(self, request_id: str, *, actor_identity: str, execution_fingerprint: str, reason: str | None = None) -> ApprovalEvidence:
        return self._decide(request_id, ApprovalStatus.REJECTED, actor_identity, execution_fingerprint, reason)

    def _decide(self, request_id, status, actor_identity, execution_fingerprint, reason):
        request_id = _require_identifier(request_id, "approval_request_id")
        actor = _require_identifier(actor_identity, "actor_identity")
        if actor not in self.authorized_approvers:
            raise ApprovalUnauthorized("actor is not an authorized approver")
        fingerprint = _require_digest(execution_fingerprint, "execution_fingerprint")

        def mutate(snapshot):
            now = self._now()
            requests, decisions = self._payloads(snapshot)
            request = requests.get(request_id)
            if request is None:
                raise ApprovalItemNotFound(f"approval request not found: {request_id}")
            if fingerprint != request["execution_fingerprint"]:
                raise ApprovalConflict("decision execution fingerprint does not match request")
            existing = decisions.get(request_id)
            if existing is not None:
                if existing["status"] == status.value and existing["actor_identity"] == actor and existing["reason"] == reason:
                    return None, None, None
                raise ApprovalConflict("approval request already has a terminal decision")
            expires = _parse_timestamp(request["expires_at"], "expires_at")
            terminal_status = status
            terminal_actor = actor
            terminal_reason = reason
            if now >= expires:
                terminal_status = ApprovalStatus.EXPIRED
                terminal_actor = "approval-center"
                terminal_reason = "approval request expired"
            payload = {
                "approval_request_id": request_id,
                "status": terminal_status.value,
                "actor_identity": terminal_actor,
                "decided_at": _timestamp(now),
                "reason": terminal_reason,
                "execution_fingerprint": request["execution_fingerprint"],
                "request_fingerprint": request["request_fingerprint"],
            }
            return payload, "decision_recorded", None

        self.store.transact(mutate)
        evidence = self.inspect(request_id)
        if evidence.status is ApprovalStatus.EXPIRED:
            raise ApprovalExpired("approval request is expired")
        return evidence

    def inspect(self, request_id: str) -> ApprovalEvidence:
        request_id = _require_identifier(request_id, "approval_request_id")
        snapshot = self.store.snapshot()
        requests, decisions = self._payloads(snapshot)
        payload = requests.get(request_id)
        if payload is None:
            raise ApprovalItemNotFound(f"approval request not found: {request_id}")
        request = self._request_from_payload(payload)
        decision_payload = decisions.get(request_id)
        decision = None if decision_payload is None else self._decision_from_payload(decision_payload)
        status = decision.status if decision else ApprovalStatus.PENDING
        if status is ApprovalStatus.PENDING and self._now() >= _parse_timestamp(request.expires_at, "expires_at"):
            status = ApprovalStatus.EXPIRED
        valid = status is ApprovalStatus.APPROVED and self._now() < _parse_timestamp(request.expires_at, "expires_at")
        return ApprovalEvidence(request, status, decision, snapshot.revision, valid)

    def verify_approval(
        self,
        request_id: str,
        *,
        execution_fingerprint: str,
        mission_id: str,
        task_id: str,
        assignment_id: str | None = None,
        dispatch_offer_id: str | None = None,
        target_node_id: str | None = None,
    ) -> ApprovalEvidence:
        evidence = self.inspect(request_id)
        request = evidence.request
        expected = (
            _require_digest(execution_fingerprint, "execution_fingerprint"),
            _require_identifier(mission_id, "mission_id"),
            _require_identifier(task_id, "task_id"),
            _require_optional_identifier(assignment_id, "assignment_id"),
            _require_optional_identifier(dispatch_offer_id, "dispatch_offer_id"),
            _require_optional_identifier(target_node_id, "target_node_id"),
        )
        actual = (
            request.execution_fingerprint, request.mission_id, request.task_id,
            request.assignment_id, request.dispatch_offer_id, request.target_node_id,
        )
        if expected != actual or evidence.status is not ApprovalStatus.APPROVED or not evidence.valid:
            raise ApprovalConflict("no valid approval evidence for exact request")
        return evidence

    def list_requests(self) -> tuple[ApprovalEvidence, ...]:
        snapshot = self.store.snapshot()
        requests, _ = self._payloads(snapshot)
        return tuple(self.inspect(request_id) for request_id in sorted(requests))

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("approval clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _payloads(snapshot: _AuthoritySnapshot):
        requests = {}
        decisions = {}
        for record in snapshot.records:
            payload = record["payload"]
            if record["event_type"] == "request_created":
                requests[payload["approval_request_id"]] = payload
            else:
                decisions[payload["approval_request_id"]] = payload
        return requests, decisions

    @staticmethod
    def _request_from_payload(payload):
        return ApprovalRequest(
            approval_request_id=payload["approval_request_id"], mission_id=payload["mission_id"],
            task_id=payload["task_id"], assignment_id=payload["assignment_id"],
            dispatch_offer_id=payload["dispatch_offer_id"], requester_identity=payload["requester_identity"],
            target_node_id=payload["target_node_id"], action_type=payload["action_type"],
            authorization_level=AuthorizationLevel(payload["authorization_level"]),
            approval_required=payload["approval_required"], workspace_id=payload["workspace_id"],
            execution_fingerprint=payload["execution_fingerprint"],
            immutable_parameters_fingerprint=hashlib.sha256(_canonical_json(payload["immutable_parameters"])).hexdigest(),
            expected_result=payload["expected_result"], created_at=payload["created_at"],
            expires_at=payload["expires_at"], request_fingerprint=payload["request_fingerprint"],
        )

    @staticmethod
    def _decision_from_payload(payload):
        return ApprovalDecision(
            approval_request_id=payload["approval_request_id"], status=ApprovalStatus(payload["status"]),
            actor_identity=payload["actor_identity"], decided_at=payload["decided_at"],
            reason=payload["reason"], execution_fingerprint=payload["execution_fingerprint"],
            request_fingerprint=payload["request_fingerprint"],
        )
