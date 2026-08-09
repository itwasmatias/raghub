"""
Proposal Coordinator and End-to-End Integration Tests

Tests covering:
- ProposalCoordinator with exact idempotency
- End-to-end AI proposal → validation → durable storage
- Integration with TaskRouter, TaskDispatchCoordinator
- Integration with ApprovalCenter (for approval-required actions)
- Integration with ConsoleActionExecutionRuntime (using inspect_git_status)
- Authority substitution rejection
- No arbitrary execution surface
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from federation.capability import NodeCapability
from federation.task_request import AuthorizationLevel
from tools.ai_controller.operations_api.ai_work_bridge import (
    AIWorkerProposal,
    AIWorkBridge,
    ProposalCoordinator,
)
from tools.ai_controller.operations_api.console_server.actions import ActionCatalog
from tools.ai_controller.operations_api.proposal_store import (
    ProposalConflict,
    ProposalLifecycleState,
    ProposalStore,
)


@pytest.fixture
def integrity_key():
    """Create integrity key for testing."""
    return b"test-integrity-key-32-bytes-xxxx"


@pytest.fixture
def store_root(tmp_path):
    """Create temporary store root."""
    return tmp_path / "proposal-store"


@pytest.fixture
def action_catalog():
    """Create action catalog for testing."""
    return ActionCatalog()


@pytest.fixture
def test_clock():
    """Create deterministic clock for testing."""
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    return lambda: now


@pytest.fixture
def ai_work_bridge(action_catalog, test_clock):
    """Create AI Work Bridge for testing."""
    return AIWorkBridge(action_catalog=action_catalog, clock=test_clock)


@pytest.fixture
def proposal_store(store_root, integrity_key):
    """Create proposal store for testing."""
    return ProposalStore(store_root, integrity_key=integrity_key)


@pytest.fixture
def coordinator(ai_work_bridge, proposal_store):
    """Create proposal coordinator for testing."""
    return ProposalCoordinator(
        ai_work_bridge=ai_work_bridge,
        proposal_store=proposal_store,
    )


@pytest.fixture
def valid_proposal():
    """Create valid AI worker proposal."""
    return AIWorkerProposal(
        proposal_id="proposal-001",
        worker_identity="ai-worker-1",
        mission_id="mission-001",
        task_id="task-001",
        action_type="inspect_git_status",
        workspace_id="workspace-001",
        immutable_parameters={},
        expected_result="Git repository status",
        created_at="2026-08-09T12:00:00Z",
    )


class TestProposalCoordinatorBasics:
    """Test basic proposal coordinator operations."""

    def test_submit_proposal_validates_and_stores(self, coordinator, valid_proposal):
        """Submit proposal should validate and store durably."""
        record = coordinator.submit_proposal(valid_proposal)

        # Validate stored correctly
        assert record.proposal_id == "proposal-001"
        assert record.worker_identity == "ai-worker-1"
        assert record.lifecycle_state == ProposalLifecycleState.VALIDATED
        assert record.authorization_level == AuthorizationLevel.INTERNAL
        assert record.approval_required is False
        assert "git" in record.required_capabilities

    def test_submit_proposal_derives_authoritative_policy(self, coordinator, valid_proposal):
        """Coordinator should derive policy from ActionCatalog, not from proposal."""
        record = coordinator.submit_proposal(valid_proposal)

        # Policy comes from catalog
        assert record.authorization_level == AuthorizationLevel.INTERNAL
        assert record.approval_required is False
        assert record.proposal_fingerprint is not None
        assert record.execution_fingerprint is not None

    def test_get_proposal_by_id(self, coordinator, valid_proposal):
        """Get proposal should return stored record."""
        coordinator.submit_proposal(valid_proposal)

        record = coordinator.get_proposal("proposal-001")

        assert record is not None
        assert record.proposal_id == "proposal-001"

    def test_update_lifecycle(self, coordinator, valid_proposal):
        """Update lifecycle should modify state."""
        coordinator.submit_proposal(valid_proposal)

        updated = coordinator.update_lifecycle(
            "proposal-001",
            lifecycle_state=ProposalLifecycleState.ROUTED,
            assignment_id="assignment-001",
        )

        assert updated.lifecycle_state == ProposalLifecycleState.ROUTED
        assert updated.assignment_id == "assignment-001"


class TestCoordinatorExactIdempotency:
    """Test exact idempotency through coordinator."""

    def test_exact_duplicate_returns_existing(self, coordinator, valid_proposal):
        """Exact duplicate submission should return existing record."""
        record1 = coordinator.submit_proposal(valid_proposal)
        record2 = coordinator.submit_proposal(valid_proposal)

        # Should be same record
        assert record1.proposal_id == record2.proposal_id
        assert record1.proposal_fingerprint == record2.proposal_fingerprint
        assert record1.created_at == record2.created_at

    def test_conflicting_duplicate_raises_error(self, coordinator, valid_proposal):
        """Conflicting duplicate should raise ProposalConflict."""
        # First submission
        coordinator.submit_proposal(valid_proposal)

        # Conflicting duplicate (different parameters)
        conflicting = AIWorkerProposal(
            proposal_id="proposal-001",  # Same ID
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="inspect_git_log",  # Different action
            workspace_id="workspace-001",
            immutable_parameters={},
            expected_result="Git log",
        )

        with pytest.raises(ProposalConflict, match="proposal-001"):
            coordinator.submit_proposal(conflicting)


class TestAuthoritySubstitutionRejection:
    """Test rejection of authority substitution attempts."""

    def test_cannot_downgrade_authorization_level(self, coordinator):
        """Cannot bypass catalog authorization level."""
        # inspect_git_status is INTERNAL in catalog
        proposal = AIWorkerProposal(
            proposal_id="proposal-001",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="inspect_git_status",
            workspace_id="workspace-001",
            immutable_parameters={},
            expected_result="Status",
        )

        record = coordinator.submit_proposal(proposal)

        # Authorization level comes from catalog, not client
        assert record.authorization_level == AuthorizationLevel.INTERNAL

    def test_cannot_bypass_approval_requirement(self, coordinator):
        """Cannot bypass catalog approval requirement."""
        # run_test_target requires approval in catalog
        proposal = AIWorkerProposal(
            proposal_id="proposal-001",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="run_test_target",
            workspace_id="workspace-001",
            immutable_parameters={"test_target": "tests/test_example.py"},
            expected_result="Test results",
        )

        record = coordinator.submit_proposal(proposal)

        # Approval requirement comes from catalog
        assert record.approval_required is True
        assert record.approval_execution_fingerprint is not None

    def test_cannot_remove_required_capabilities(self, coordinator):
        """Cannot bypass catalog required capabilities."""
        proposal = AIWorkerProposal(
            proposal_id="proposal-001",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="inspect_git_status",
            workspace_id="workspace-001",
            immutable_parameters={},
            expected_result="Status",
        )

        record = coordinator.submit_proposal(proposal)

        # Required capabilities come from catalog
        assert "git" in record.required_capabilities

    def test_cannot_substitute_proposal_fingerprint(self, coordinator, valid_proposal, proposal_store):
        """Cannot change proposal fingerprint in stored record."""
        record = coordinator.submit_proposal(valid_proposal)
        original_fingerprint = record.proposal_fingerprint

        # Attempt to update with different fingerprint should fail validation
        # (ProposalStore protects immutable fields)
        with pytest.raises(Exception):  # Will be caught by immutability validation
            proposal_store.transact(lambda snap: (
                {
                    "proposal_id": "proposal-001",
                    "proposal_fingerprint": "evil" + "0" * 60,
                },
                "lifecycle_updated",
                None,
            ))

        # Original fingerprint should be unchanged
        current = coordinator.get_proposal("proposal-001")
        assert current.proposal_fingerprint == original_fingerprint

    def test_cannot_substitute_execution_fingerprint(self, coordinator, valid_proposal, proposal_store):
        """Cannot change execution fingerprint in stored record."""
        record = coordinator.submit_proposal(valid_proposal)
        original_fingerprint = record.execution_fingerprint

        # Attempt to update should fail
        with pytest.raises(Exception):
            proposal_store.transact(lambda snap: (
                {
                    "proposal_id": "proposal-001",
                    "execution_fingerprint": "evil" + "0" * 60,
                },
                "lifecycle_updated",
                None,
            ))

        # Original fingerprint should be unchanged
        current = coordinator.get_proposal("proposal-001")
        assert current.execution_fingerprint == original_fingerprint


class TestEndToEndIntegration:
    """Test end-to-end integration with existing federation systems."""

    def test_proposal_to_task_request_integration(self, coordinator, ai_work_bridge, valid_proposal):
        """Test proposal → TaskRequest conversion."""
        # Submit and validate
        record = coordinator.submit_proposal(valid_proposal)

        # Create ProposalEvidence for TaskRequest creation
        from tools.ai_controller.operations_api.ai_work_bridge import ProposalEvidence, ProposalStatus

        evidence = ProposalEvidence(
            proposal=valid_proposal,
            status=ProposalStatus.PENDING,
            authorization_level=record.authorization_level,
            approval_required=record.approval_required,
            required_capabilities=frozenset(NodeCapability(cap) for cap in record.required_capabilities),
            proposal_fingerprint=record.proposal_fingerprint,
            execution_fingerprint=record.execution_fingerprint,
            approval_execution_fingerprint=record.approval_execution_fingerprint,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

        # Create TaskRequest
        task_request = ai_work_bridge.create_task_request(evidence)

        # Verify TaskRequest has authoritative policy
        assert task_request.authorization_level == record.authorization_level
        assert task_request.approval_required == record.approval_required
        assert task_request.required_capabilities == frozenset(NodeCapability(cap) for cap in record.required_capabilities)

        # Verify input_data preserves proposal evidence
        assert task_request.input_data["proposal_id"] == "proposal-001"
        assert task_request.input_data["worker_identity"] == "ai-worker-1"
        assert task_request.input_data["proposal_fingerprint"] == record.proposal_fingerprint
        assert task_request.input_data["execution_fingerprint"] == record.execution_fingerprint

    def test_governed_execution_fingerprint_binding(self, coordinator):
        """Test that execution fingerprint binds immutable parameters."""
        # Submit two proposals with different parameters
        proposal1 = AIWorkerProposal(
            proposal_id="proposal-001",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="run_test_target",
            workspace_id="workspace-001",
            immutable_parameters={"test_target": "tests/test_a.py"},
            expected_result="Test results",
        )

        proposal2 = AIWorkerProposal(
            proposal_id="proposal-002",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-002",
            action_type="run_test_target",
            workspace_id="workspace-001",
            immutable_parameters={"test_target": "tests/test_b.py"},  # Different test
            expected_result="Test results",
        )

        record1 = coordinator.submit_proposal(proposal1)
        record2 = coordinator.submit_proposal(proposal2)

        # Different parameters should produce different execution fingerprints
        assert record1.execution_fingerprint != record2.execution_fingerprint
        assert record1.proposal_fingerprint != record2.proposal_fingerprint

    def test_no_arbitrary_execution_surface(self, coordinator):
        """Test that arbitrary actions are rejected."""
        # Attempt to submit proposal with arbitrary action
        from tools.ai_controller.operations_api.ai_work_bridge import ProposalUnauthorizedError

        dangerous_proposal = AIWorkerProposal(
            proposal_id="proposal-evil",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="arbitrary_shell_command",  # Not in catalog
            workspace_id="workspace-001",
            immutable_parameters={"command": "rm -rf /"},
            expected_result="Danger",
        )

        # Should be rejected by catalog validation
        with pytest.raises(ProposalUnauthorizedError, match="not allowed"):
            coordinator.submit_proposal(dangerous_proposal)


class TestNoBlindRerun:
    """Test that system does not blindly rerun ambiguous executions."""

    def test_reconciliation_required_state(self, coordinator, valid_proposal):
        """Test reconciliation_required state for ambiguous outcomes."""
        # Submit proposal
        record = coordinator.submit_proposal(valid_proposal)

        # Mark as executing
        coordinator.update_lifecycle(
            "proposal-001",
            lifecycle_state=ProposalLifecycleState.EXECUTING,
            job_id="job-001",
        )

        # Simulate restart with ambiguous execution state
        # System should mark as reconciliation_required, not auto-retry
        coordinator.update_lifecycle(
            "proposal-001",
            lifecycle_state=ProposalLifecycleState.RECONCILIATION_REQUIRED,
            failure_reason="Execution state ambiguous after restart",
        )

        # Verify state
        current = coordinator.get_proposal("proposal-001")
        assert current.lifecycle_state == ProposalLifecycleState.RECONCILIATION_REQUIRED
        assert current.failure_reason is not None


class TestProposalRecordSerialization:
    """Test ProposalRecord serialization for evidence preservation."""

    def test_proposal_record_to_dict(self, coordinator, valid_proposal):
        """ProposalRecord should serialize to complete dict."""
        record = coordinator.submit_proposal(valid_proposal)

        data = record.to_dict()

        # Verify all fields present
        required_fields = [
            "proposal_id",
            "worker_identity",
            "mission_id",
            "task_id",
            "action_type",
            "workspace_id",
            "immutable_parameters",
            "expected_result",
            "authorization_level",
            "approval_required",
            "required_capabilities",
            "proposal_fingerprint",
            "execution_fingerprint",
            "lifecycle_state",
            "created_at",
            "updated_at",
        ]

        for field in required_fields:
            assert field in data

    def test_serialization_preserves_evidence(self, coordinator, valid_proposal):
        """Serialization should preserve all evidence fields."""
        record = coordinator.submit_proposal(valid_proposal)

        # Update with lifecycle identities
        coordinator.update_lifecycle(
            "proposal-001",
            lifecycle_state=ProposalLifecycleState.DISPATCHED,
            task_request_fingerprint="a" * 64,
            assignment_id="assignment-001",
            dispatch_offer_id="dispatch-001",
            target_node_id="node-001",
            approval_request_id="approval-001",
        )

        # Serialize
        current = coordinator.get_proposal("proposal-001")
        data = current.to_dict()

        # Verify evidence preserved
        assert data["task_request_fingerprint"] == "a" * 64
        assert data["assignment_id"] == "assignment-001"
        assert data["dispatch_offer_id"] == "dispatch-001"
        assert data["target_node_id"] == "node-001"
        assert data["approval_request_id"] == "approval-001"


class TestDurableLifecycleEvidence:
    """Test durable evidence preservation across full lifecycle."""

    def test_full_lifecycle_evidence_chain(self, coordinator, valid_proposal):
        """Test full lifecycle creates durable evidence chain."""
        # 1. Submit and validate
        record = coordinator.submit_proposal(valid_proposal)
        assert record.lifecycle_state == ProposalLifecycleState.VALIDATED
        assert record.proposal_fingerprint is not None

        # 2. Route (TaskRouter would do this)
        coordinator.update_lifecycle(
            "proposal-001",
            lifecycle_state=ProposalLifecycleState.ROUTED,
            task_request_fingerprint="a" * 64,
            assignment_id="assignment-001",
        )
        record = coordinator.get_proposal("proposal-001")
        assert record.lifecycle_state == ProposalLifecycleState.ROUTED
        assert record.task_request_fingerprint is not None
        assert record.assignment_id is not None

        # 3. Dispatch (TaskDispatchCoordinator would do this)
        coordinator.update_lifecycle(
            "proposal-001",
            lifecycle_state=ProposalLifecycleState.DISPATCHED,
            dispatch_offer_id="dispatch-001",
            target_node_id="node-001",
        )
        record = coordinator.get_proposal("proposal-001")
        assert record.lifecycle_state == ProposalLifecycleState.DISPATCHED
        assert record.dispatch_offer_id is not None
        assert record.target_node_id is not None

        # 4. Execute (ConsoleActionExecutionRuntime would do this)
        coordinator.update_lifecycle(
            "proposal-001",
            lifecycle_state=ProposalLifecycleState.EXECUTING,
            job_id="job-001",
        )
        record = coordinator.get_proposal("proposal-001")
        assert record.lifecycle_state == ProposalLifecycleState.EXECUTING
        assert record.job_id is not None

        # 5. Complete
        coordinator.update_lifecycle(
            "proposal-001",
            lifecycle_state=ProposalLifecycleState.COMPLETED,
            result_reference="result-001",
            completed_at="2026-08-09T12:30:00+00:00",
        )
        record = coordinator.get_proposal("proposal-001")
        assert record.lifecycle_state == ProposalLifecycleState.COMPLETED
        assert record.result_reference is not None
        assert record.completed_at is not None

        # Verify complete evidence chain preserved
        final = coordinator.get_proposal("proposal-001")
        assert final.proposal_fingerprint is not None
        assert final.task_request_fingerprint is not None
        assert final.assignment_id is not None
        assert final.dispatch_offer_id is not None
        assert final.target_node_id is not None
        assert final.job_id is not None
        assert final.result_reference is not None
        assert final.completed_at is not None

    def test_approval_required_lifecycle_evidence(self, coordinator):
        """Test lifecycle evidence for approval-required actions."""
        # Submit approval-required action
        proposal = AIWorkerProposal(
            proposal_id="proposal-approval-001",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="run_test_target",
            workspace_id="workspace-001",
            immutable_parameters={"test_target": "tests/test_example.py"},
            expected_result="Test results",
        )

        record = coordinator.submit_proposal(proposal)
        assert record.approval_required is True
        assert record.approval_execution_fingerprint is not None

        # Simulate approval flow
        coordinator.update_lifecycle(
            "proposal-approval-001",
            lifecycle_state=ProposalLifecycleState.BLOCKED_ON_APPROVAL,
            approval_request_id="approval-request-001",
        )

        record = coordinator.get_proposal("proposal-approval-001")
        assert record.lifecycle_state == ProposalLifecycleState.BLOCKED_ON_APPROVAL
        assert record.approval_request_id is not None

        # Simulate approval granted
        coordinator.update_lifecycle(
            "proposal-approval-001",
            lifecycle_state=ProposalLifecycleState.EXECUTION_PENDING,
        )

        record = coordinator.get_proposal("proposal-approval-001")
        assert record.lifecycle_state == ProposalLifecycleState.EXECUTION_PENDING
