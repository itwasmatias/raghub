"""
AI Work Bridge v0.1 - Structured bridge from AI worker proposals to governed execution.

This module provides a domain-neutral structured bridge that converts AI worker
proposals into the existing governed execution chain:

  AI worker proposal
  → validate proposal
  → TaskRequest
  → TaskRouter
  → TaskDispatchCoordinator
  → Approval Center (when required)
  → Console governed execution (when executable)
  → durable result/evidence reference

Security invariants:
- Policy authority comes from ActionCatalog, not from client proposals
- Approval requirements cannot be bypassed or downgraded by clients
- Authorization levels cannot be lowered by clients
- Required capabilities cannot be removed by clients
- Execution fingerprints bind immutable parameters
- Proposal identity is deterministic from immutable subject
- Idempotent: exact duplicate proposals map to same work identity
- Fail closed on malformed, unauthorized, or conflicting proposals
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from federation.capability import NodeCapability
from federation.task_request import AuthorizationLevel, TaskRequest
from federation.integrity import require_integrity_key
from tools.ai_controller.operations_api.console_server.actions import ActionCatalog


_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class ProposalError(Exception):
    """Base exception for proposal validation errors."""
    pass


class ProposalValidationError(ProposalError):
    """Raised when proposal fails validation."""
    pass


class ProposalConflictError(ProposalError):
    """Raised when proposal conflicts with existing evidence."""
    pass


class ProposalUnauthorizedError(ProposalError):
    """Raised when proposal is unauthorized."""
    pass


class ProposalStatus(str, Enum):
    """Status of an AI worker proposal."""
    PENDING = "pending"           # Awaiting routing/dispatch
    ROUTED = "routed"            # Routed to node, awaiting dispatch
    DISPATCHED = "dispatched"    # Dispatch offer created
    AWAITING_APPROVAL = "awaiting_approval"  # Awaiting human approval
    APPROVED = "approved"        # Approved, ready for execution
    REJECTED = "rejected"        # Rejected by approver
    EXECUTING = "executing"      # Currently executing
    COMPLETED = "completed"      # Execution completed
    FAILED = "failed"           # Execution failed
    EXPIRED = "expired"          # Proposal or approval expired


def _require_identifier(value: Any, field_name: str) -> str:
    """Require a valid non-empty identifier."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ProposalValidationError(
            f"{field_name} must be a canonical non-empty identifier"
        )
    return value


def _require_optional_identifier(value: Any, field_name: str) -> str | None:
    """Require a valid identifier or None."""
    return None if value is None else _require_identifier(value, field_name)


def _canonical_json(value: Any) -> bytes:
    """Return canonical JSON encoding of value."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProposalValidationError(
            "immutable parameters must be finite JSON values"
        ) from exc


def _canonical_parameters(value: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize immutable parameters."""
    if not isinstance(value, Mapping):
        raise TypeError("immutable_parameters must be a mapping")
    encoded = _canonical_json(dict(value))
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise TypeError("immutable_parameters must be an object")
    return decoded


@dataclass(frozen=True, slots=True)
class AIWorkerProposal:
    """
    Immutable AI worker action proposal.

    This is the structured contract that AI workers submit to request
    governed actions. All execution-shaping fields (authorization level,
    approval requirements, capabilities) are derived from authoritative
    policy, not from this proposal.
    """

    # Stable identity
    proposal_id: str  # Client-provided stable identifier
    worker_identity: str  # AI worker node identity
    mission_id: str  # Mission this proposal belongs to
    task_id: str  # Task within mission

    # Action specification
    action_type: str  # e.g., "inspect_git_status", "run_test_target"
    workspace_id: str  # Workspace to execute in

    # Action parameters (immutable, bound to proposal identity)
    immutable_parameters: Mapping[str, Any]  # e.g., {"test_target": "tests/"}

    # Expected outcome
    expected_result: str  # Human-readable expected result description

    # Optional metadata (informational only, not authoritative)
    preferred_capabilities: frozenset[NodeCapability] = frozenset()
    timeout_seconds: int | None = None  # Bounded by action catalog policy

    # Provenance
    created_at: str | None = None  # ISO-8601 timestamp

    def __post_init__(self):
        """Validate proposal fields."""
        _require_identifier(self.proposal_id, "proposal_id")
        _require_identifier(self.worker_identity, "worker_identity")
        _require_identifier(self.mission_id, "mission_id")
        _require_identifier(self.task_id, "task_id")
        _require_identifier(self.action_type, "action_type")
        _require_identifier(self.workspace_id, "workspace_id")

        # Validate immutable_parameters
        if not isinstance(self.immutable_parameters, Mapping):
            raise TypeError("immutable_parameters must be a mapping")

        # Validate expected_result
        if not isinstance(self.expected_result, str) or not self.expected_result.strip():
            raise ProposalValidationError("expected_result must be a non-empty string")

        # Validate preferred_capabilities
        if not isinstance(self.preferred_capabilities, (set, frozenset)):
            raise TypeError("preferred_capabilities must be a set or frozenset")
        if not all(isinstance(cap, NodeCapability) for cap in self.preferred_capabilities):
            raise TypeError("preferred_capabilities must contain only NodeCapability values")

        # Validate timeout_seconds
        if self.timeout_seconds is not None:
            if not isinstance(self.timeout_seconds, int) or isinstance(self.timeout_seconds, bool):
                raise TypeError("timeout_seconds must be an integer or None")
            if self.timeout_seconds <= 0:
                raise ProposalValidationError("timeout_seconds must be positive")

        # Validate created_at
        if self.created_at is not None:
            if not isinstance(self.created_at, str):
                raise TypeError("created_at must be a string or None")
            # Parse to validate format
            try:
                datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ProposalValidationError("created_at must be valid ISO-8601") from exc

    def proposal_fingerprint(self) -> str:
        """
        Return deterministic fingerprint of immutable proposal subject.

        This binds the exact action request to prevent substitution attacks.
        """
        payload = {
            "proposal_id": self.proposal_id,
            "mission_id": self.mission_id,
            "task_id": self.task_id,
            "action_type": self.action_type,
            "workspace_id": self.workspace_id,
            "immutable_parameters": _canonical_parameters(self.immutable_parameters),
        }
        return hashlib.sha256(_canonical_json(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class ProposalEvidence:
    """
    Durable evidence of an AI worker proposal and its lifecycle.

    Tracks the proposal through routing, dispatch, approval, and execution.
    """

    proposal: AIWorkerProposal
    status: ProposalStatus

    # Authoritative policy (derived from ActionCatalog, not from proposal)
    authorization_level: AuthorizationLevel
    approval_required: bool
    required_capabilities: frozenset[NodeCapability]

    # Governance fingerprints
    proposal_fingerprint: str
    execution_fingerprint: str | None  # Console execution fingerprint
    approval_execution_fingerprint: str | None  # Approval Center fingerprint

    # Lifecycle identities (None until created)
    task_request_fingerprint: str | None = None
    assignment_id: str | None = None
    dispatch_offer_id: str | None = None
    approval_request_id: str | None = None
    job_id: str | None = None

    # Lifecycle timestamps
    created_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None

    # Result (None until completed)
    result_evidence: Mapping[str, Any] | None = None
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Safe public serialization."""
        return {
            "proposal_id": self.proposal.proposal_id,
            "worker_identity": self.proposal.worker_identity,
            "mission_id": self.proposal.mission_id,
            "task_id": self.proposal.task_id,
            "action_type": self.proposal.action_type,
            "workspace_id": self.proposal.workspace_id,
            "expected_result": self.proposal.expected_result,
            "status": self.status.value,
            "authorization_level": self.authorization_level.value,
            "approval_required": self.approval_required,
            "required_capabilities": [str(cap) for cap in sorted(self.required_capabilities)],
            "proposal_fingerprint": self.proposal_fingerprint,
            "execution_fingerprint": self.execution_fingerprint,
            "approval_execution_fingerprint": self.approval_execution_fingerprint,
            "assignment_id": self.assignment_id,
            "dispatch_offer_id": self.dispatch_offer_id,
            "approval_request_id": self.approval_request_id,
            "job_id": self.job_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "failure_reason": self.failure_reason,
        }


class AIWorkBridge:
    """
    Structured bridge from AI worker proposals to governed execution.

    This bridge:
    - Validates AI worker proposals against authoritative action catalog
    - Derives authorization level and approval requirements from policy
    - Creates TaskRequest objects for routing
    - Does NOT execute anything directly
    - Does NOT allow bypassing approval requirements
    - Does NOT allow lowering authorization levels
    - Fails closed on invalid, unauthorized, or conflicting proposals
    """

    def __init__(
        self,
        *,
        action_catalog: ActionCatalog,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        """
        Initialize AI Work Bridge.

        Args:
            action_catalog: Authoritative action catalog for policy
            clock: Clock for timestamps (for testing)
        """
        if not isinstance(action_catalog, ActionCatalog):
            raise TypeError("action_catalog must be an ActionCatalog")
        if not callable(clock):
            raise TypeError("clock must be callable")

        self.action_catalog = action_catalog
        self.clock = clock

    def validate_proposal(self, proposal: AIWorkerProposal) -> ProposalEvidence:
        """
        Validate an AI worker proposal and return evidence with authoritative policy.

        This method:
        1. Validates proposal structure and fields
        2. Looks up authoritative action specification from catalog
        3. Derives authorization_level and approval_required from policy (not proposal)
        4. Derives required_capabilities from action spec
        5. Validates timeout against policy maximum
        6. Creates execution fingerprints
        7. Returns ProposalEvidence with authoritative policy

        Args:
            proposal: AI worker proposal to validate

        Returns:
            ProposalEvidence with status PENDING and authoritative policy

        Raises:
            ProposalValidationError: If proposal is invalid
            ProposalUnauthorizedError: If action is not allowed
        """
        if not isinstance(proposal, AIWorkerProposal):
            raise TypeError("proposal must be an AIWorkerProposal")

        # Look up authoritative action specification
        try:
            action_spec = self.action_catalog.get_spec(proposal.action_type)
        except ValueError as exc:
            raise ProposalUnauthorizedError(
                f"action type not allowed: {proposal.action_type}"
            ) from exc

        # Derive authoritative policy (client cannot override these)
        authorization_level = action_spec.authorization_level
        approval_required = action_spec.approval_required
        required_capabilities = frozenset([action_spec.required_capability])

        # Validate timeout against policy maximum
        if proposal.timeout_seconds is not None:
            try:
                validated_timeout = self.action_catalog.validate_timeout(
                    action_spec.action_type,
                    proposal.timeout_seconds,
                )
            except (TypeError, ValueError) as exc:
                raise ProposalValidationError(
                    f"timeout validation failed: {exc}"
                ) from exc
        else:
            validated_timeout = action_spec.max_timeout_seconds

        # Validate action-specific immutable parameters
        validated_parameters = self._validate_action_parameters(
            action_spec.action_type,
            proposal.immutable_parameters,
        )

        # Create proposal fingerprint (binds immutable subject)
        proposal_fingerprint = proposal.proposal_fingerprint()

        # Create Console execution fingerprint (if action is executable)
        # This binds action_type + workspace_id + timeout + immutable_parameters
        execution_fingerprint = None
        if action_spec.allowed_in_v0_1:
            from tools.ai_controller.operations_api.console_server.actions import (
                execution_fingerprint as make_console_fingerprint,
            )
            execution_fingerprint = make_console_fingerprint(
                action_type=action_spec.action_type,
                workspace_id=proposal.workspace_id,
                timeout_seconds=validated_timeout,
                immutable_parameters=validated_parameters,
            )

        # Create Approval Center execution fingerprint (if approval required)
        # This uses Approval Center's fingerprint schema
        approval_execution_fingerprint = None
        if approval_required:
            from tools.ai_controller.operations_api.approval_center import (
                make_execution_fingerprint as make_approval_fingerprint,
            )
            approval_execution_fingerprint = make_approval_fingerprint(
                action_type=proposal.action_type,
                workspace_id=proposal.workspace_id,
                authorization_level=authorization_level,
                approval_required=True,
                immutable_parameters=validated_parameters,
            )

        # Create evidence with authoritative policy
        now = self.clock()
        return ProposalEvidence(
            proposal=proposal,
            status=ProposalStatus.PENDING,
            authorization_level=authorization_level,
            approval_required=approval_required,
            required_capabilities=required_capabilities,
            proposal_fingerprint=proposal_fingerprint,
            execution_fingerprint=execution_fingerprint,
            approval_execution_fingerprint=approval_execution_fingerprint,
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )

    def create_task_request(
        self,
        evidence: ProposalEvidence,
    ) -> TaskRequest:
        """
        Create a TaskRequest from validated proposal evidence.

        This converts the AI worker proposal into a TaskRequest that can be
        routed through the existing federation routing system.

        Args:
            evidence: Validated proposal evidence

        Returns:
            TaskRequest for federation routing

        Raises:
            ProposalValidationError: If evidence is invalid
        """
        if not isinstance(evidence, ProposalEvidence):
            raise TypeError("evidence must be a ProposalEvidence")

        if evidence.status is not ProposalStatus.PENDING:
            raise ProposalValidationError(
                f"cannot create task request from {evidence.status.value} proposal"
            )

        proposal = evidence.proposal

        # Create TaskRequest with authoritative policy from evidence
        # (not from proposal, which is untrusted)
        return TaskRequest(
            task_id=proposal.task_id,
            mission_id=proposal.mission_id,
            required_capabilities=evidence.required_capabilities,
            preferred_capabilities=proposal.preferred_capabilities,
            authorization_level=evidence.authorization_level,
            approval_required=evidence.approval_required,
            input_data={
                "proposal_id": proposal.proposal_id,
                "worker_identity": proposal.worker_identity,
                "action_type": proposal.action_type,
                "workspace_id": proposal.workspace_id,
                "immutable_parameters": dict(proposal.immutable_parameters),
                "expected_result": proposal.expected_result,
                "proposal_fingerprint": evidence.proposal_fingerprint,
                "execution_fingerprint": evidence.execution_fingerprint,
                "approval_execution_fingerprint": evidence.approval_execution_fingerprint,
            },
            expected_result=proposal.expected_result,
        )

    def _validate_action_parameters(
        self,
        action_type: str,
        immutable_parameters: Mapping[str, Any],
    ) -> dict[str, Any]:
        """
        Validate action-specific immutable parameters.

        Args:
            action_type: Action type
            immutable_parameters: Immutable parameters to validate

        Returns:
            Canonicalized immutable parameters

        Raises:
            ProposalValidationError: If parameters are invalid
        """
        # Canonicalize parameters first
        try:
            params = _canonical_parameters(immutable_parameters)
        except (TypeError, ValueError) as exc:
            raise ProposalValidationError(
                f"invalid immutable_parameters: {exc}"
            ) from exc

        # Action-specific validation
        if action_type == "run_test_target":
            # Validate test_target parameter
            if "test_target" not in params:
                raise ProposalValidationError(
                    "run_test_target requires test_target parameter"
                )
            try:
                validated = self.action_catalog.validate_test_target(params["test_target"])
                params["test_target"] = validated
            except (TypeError, ValueError) as exc:
                raise ProposalValidationError(
                    f"invalid test_target: {exc}"
                ) from exc

        elif action_type == "inspect_git_status":
            # inspect_git_status has no immutable parameters
            if params:
                raise ProposalValidationError(
                    "inspect_git_status must have empty immutable_parameters"
                )

        elif action_type == "inspect_git_log":
            # inspect_git_log has no immutable parameters
            if params:
                raise ProposalValidationError(
                    "inspect_git_log must have empty immutable_parameters"
                )

        elif action_type == "run_repository_tests":
            # run_repository_tests has no immutable parameters
            if params:
                raise ProposalValidationError(
                    "run_repository_tests must have empty immutable_parameters"
                )

        elif action_type == "inspect_runtime_status":
            # inspect_runtime_status has no immutable parameters
            if params:
                raise ProposalValidationError(
                    "inspect_runtime_status must have empty immutable_parameters"
                )

        else:
            raise ProposalValidationError(f"unknown action type: {action_type}")

        return params
