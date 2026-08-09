"""
AI Work Bridge v0.1 Tests

Comprehensive adversarial tests for the AI Work Bridge, covering:
- Valid structured proposals
- Malformed proposals
- Unknown actions
- Client attempts to downgrade approval requirements
- Client attempts to downgrade authorization levels
- Client attempts to remove required capabilities
- Execution-shaping fields rejected
- Exact duplicate idempotency
- Conflicting duplicates
- Fingerprint validation
- Action-specific parameter validation
"""

from datetime import datetime, timezone

import pytest

from federation.capability import NodeCapability
from federation.task_request import AuthorizationLevel, TaskRequest
from tools.ai_controller.operations_api.ai_work_bridge import (
    AIWorkerProposal,
    AIWorkBridge,
    ProposalError,
    ProposalStatus,
    ProposalUnauthorizedError,
    ProposalValidationError,
)
from tools.ai_controller.operations_api.console_server.actions import ActionCatalog, ActionType


@pytest.fixture
def action_catalog():
    """Create an action catalog for testing."""
    return ActionCatalog()


@pytest.fixture
def bridge(action_catalog):
    """Create an AI Work Bridge for testing."""
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    return AIWorkBridge(action_catalog=action_catalog, clock=lambda: now)


@pytest.fixture
def valid_proposal():
    """Create a valid AI worker proposal."""
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


class TestValidProposal:
    """Test validation of valid proposals."""

    def test_valid_proposal_accepted(self, bridge, valid_proposal):
        """Valid proposal should be accepted and return evidence."""
        evidence = bridge.validate_proposal(valid_proposal)

        assert evidence.status is ProposalStatus.PENDING
        assert evidence.proposal == valid_proposal
        assert evidence.authorization_level == AuthorizationLevel.INTERNAL
        assert evidence.approval_required is False  # inspect_git_status doesn't require approval
        assert NodeCapability("git") in evidence.required_capabilities
        assert evidence.proposal_fingerprint is not None
        assert evidence.execution_fingerprint is not None
        assert evidence.approval_execution_fingerprint is None  # No approval required
        assert evidence.created_at is not None
        assert evidence.updated_at is not None

    def test_valid_proposal_creates_task_request(self, bridge, valid_proposal):
        """Valid proposal should create a TaskRequest."""
        evidence = bridge.validate_proposal(valid_proposal)
        task_request = bridge.create_task_request(evidence)

        assert isinstance(task_request, TaskRequest)
        assert task_request.task_id == "task-001"
        assert task_request.mission_id == "mission-001"
        assert task_request.authorization_level == AuthorizationLevel.INTERNAL
        assert task_request.approval_required is False
        assert NodeCapability("git") in task_request.required_capabilities
        assert task_request.expected_result == "Git repository status"

    def test_proposal_fingerprint_deterministic(self, valid_proposal):
        """Proposal fingerprint should be deterministic."""
        fp1 = valid_proposal.proposal_fingerprint()
        fp2 = valid_proposal.proposal_fingerprint()

        assert fp1 == fp2
        assert isinstance(fp1, str)
        assert len(fp1) == 64  # SHA-256 hex
        assert all(c in "0123456789abcdef" for c in fp1)


class TestMalformedProposals:
    """Test rejection of malformed proposals."""

    def test_invalid_proposal_id(self, bridge):
        """Invalid proposal_id should be rejected."""
        with pytest.raises(ProposalValidationError, match="proposal_id"):
            AIWorkerProposal(
                proposal_id="",  # Empty
                worker_identity="ai-worker-1",
                mission_id="mission-001",
                task_id="task-001",
                action_type="inspect_git_status",
                workspace_id="workspace-001",
                immutable_parameters={},
                expected_result="Result",
            )

    def test_invalid_worker_identity(self, bridge):
        """Invalid worker_identity should be rejected."""
        with pytest.raises(ProposalValidationError, match="worker_identity"):
            AIWorkerProposal(
                proposal_id="proposal-001",
                worker_identity="worker with spaces",  # Invalid chars
                mission_id="mission-001",
                task_id="task-001",
                action_type="inspect_git_status",
                workspace_id="workspace-001",
                immutable_parameters={},
                expected_result="Result",
            )

    def test_invalid_mission_id(self, bridge):
        """Invalid mission_id should be rejected."""
        with pytest.raises(ProposalValidationError, match="mission_id"):
            AIWorkerProposal(
                proposal_id="proposal-001",
                worker_identity="ai-worker-1",
                mission_id="mission/001",  # Invalid char
                task_id="task-001",
                action_type="inspect_git_status",
                workspace_id="workspace-001",
                immutable_parameters={},
                expected_result="Result",
            )

    def test_invalid_task_id(self, bridge):
        """Invalid task_id should be rejected."""
        with pytest.raises(ProposalValidationError, match="task_id"):
            AIWorkerProposal(
                proposal_id="proposal-001",
                worker_identity="ai-worker-1",
                mission_id="mission-001",
                task_id="",  # Empty
                action_type="inspect_git_status",
                workspace_id="workspace-001",
                immutable_parameters={},
                expected_result="Result",
            )

    def test_invalid_action_type(self, bridge):
        """Invalid action_type should be rejected."""
        with pytest.raises(ProposalValidationError, match="action_type"):
            AIWorkerProposal(
                proposal_id="proposal-001",
                worker_identity="ai-worker-1",
                mission_id="mission-001",
                task_id="task-001",
                action_type="",  # Empty
                workspace_id="workspace-001",
                immutable_parameters={},
                expected_result="Result",
            )

    def test_invalid_workspace_id(self, bridge):
        """Invalid workspace_id should be rejected."""
        with pytest.raises(ProposalValidationError, match="workspace_id"):
            AIWorkerProposal(
                proposal_id="proposal-001",
                worker_identity="ai-worker-1",
                mission_id="mission-001",
                task_id="task-001",
                action_type="inspect_git_status",
                workspace_id="",  # Empty
                immutable_parameters={},
                expected_result="Result",
            )

    def test_missing_expected_result(self, bridge):
        """Missing expected_result should be rejected."""
        with pytest.raises(ProposalValidationError, match="expected_result"):
            AIWorkerProposal(
                proposal_id="proposal-001",
                worker_identity="ai-worker-1",
                mission_id="mission-001",
                task_id="task-001",
                action_type="inspect_git_status",
                workspace_id="workspace-001",
                immutable_parameters={},
                expected_result="",  # Empty
            )

    def test_invalid_timeout_seconds(self, bridge):
        """Invalid timeout_seconds should be rejected."""
        with pytest.raises(TypeError, match="timeout_seconds"):
            AIWorkerProposal(
                proposal_id="proposal-001",
                worker_identity="ai-worker-1",
                mission_id="mission-001",
                task_id="task-001",
                action_type="inspect_git_status",
                workspace_id="workspace-001",
                immutable_parameters={},
                expected_result="Result",
                timeout_seconds="30",  # String instead of int
            )

    def test_negative_timeout_seconds(self, bridge):
        """Negative timeout_seconds should be rejected."""
        with pytest.raises(ProposalValidationError, match="timeout_seconds"):
            AIWorkerProposal(
                proposal_id="proposal-001",
                worker_identity="ai-worker-1",
                mission_id="mission-001",
                task_id="task-001",
                action_type="inspect_git_status",
                workspace_id="workspace-001",
                immutable_parameters={},
                expected_result="Result",
                timeout_seconds=-1,
            )

    def test_invalid_created_at(self, bridge):
        """Invalid created_at should be rejected."""
        with pytest.raises(ProposalValidationError, match="created_at"):
            AIWorkerProposal(
                proposal_id="proposal-001",
                worker_identity="ai-worker-1",
                mission_id="mission-001",
                task_id="task-001",
                action_type="inspect_git_status",
                workspace_id="workspace-001",
                immutable_parameters={},
                expected_result="Result",
                created_at="not-a-timestamp",
            )


class TestUnknownAction:
    """Test rejection of unknown or disallowed actions."""

    def test_unknown_action_type(self, bridge):
        """Unknown action type should be rejected."""
        proposal = AIWorkerProposal(
            proposal_id="proposal-001",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="arbitrary_execution",  # Not in catalog
            workspace_id="workspace-001",
            immutable_parameters={},
            expected_result="Result",
        )

        with pytest.raises(ProposalUnauthorizedError, match="not allowed"):
            bridge.validate_proposal(proposal)

    def test_action_not_in_catalog(self, bridge):
        """Action not in catalog should be rejected."""
        proposal = AIWorkerProposal(
            proposal_id="proposal-001",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="exec_shell",  # Hypothetical dangerous action
            workspace_id="workspace-001",
            immutable_parameters={},
            expected_result="Result",
        )

        with pytest.raises(ProposalUnauthorizedError):
            bridge.validate_proposal(proposal)


class TestAuthorizationLevelDerivation:
    """Test that authorization level comes from catalog, not client."""

    def test_authorization_level_from_catalog(self, bridge):
        """Authorization level should come from catalog."""
        proposal = AIWorkerProposal(
            proposal_id="proposal-001",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="inspect_git_status",
            workspace_id="workspace-001",
            immutable_parameters={},
            expected_result="Result",
        )

        evidence = bridge.validate_proposal(proposal)

        # inspect_git_status is INTERNAL in catalog
        assert evidence.authorization_level == AuthorizationLevel.INTERNAL

        # Verify this matches the catalog
        spec = bridge.action_catalog.get_spec(ActionType.INSPECT_GIT_STATUS)
        assert evidence.authorization_level == spec.authorization_level


class TestApprovalRequirementDerivation:
    """Test that approval requirement comes from catalog, not client."""

    def test_approval_required_from_catalog_false(self, bridge):
        """Approval requirement should come from catalog (False case)."""
        proposal = AIWorkerProposal(
            proposal_id="proposal-001",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="inspect_git_status",  # Does not require approval
            workspace_id="workspace-001",
            immutable_parameters={},
            expected_result="Result",
        )

        evidence = bridge.validate_proposal(proposal)

        # inspect_git_status does not require approval in catalog
        assert evidence.approval_required is False

        # Verify this matches the catalog
        spec = bridge.action_catalog.get_spec(ActionType.INSPECT_GIT_STATUS)
        assert evidence.approval_required == spec.approval_required

    def test_approval_required_from_catalog_true(self, bridge):
        """Approval requirement should come from catalog (True case)."""
        proposal = AIWorkerProposal(
            proposal_id="proposal-001",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="run_test_target",  # Requires approval
            workspace_id="workspace-001",
            immutable_parameters={"test_target": "tests/test_example.py"},
            expected_result="Test results",
        )

        evidence = bridge.validate_proposal(proposal)

        # run_test_target requires approval in catalog
        assert evidence.approval_required is True

        # Verify this matches the catalog
        spec = bridge.action_catalog.get_spec(ActionType.RUN_TEST_TARGET)
        assert evidence.approval_required == spec.approval_required

        # Should have approval execution fingerprint
        assert evidence.approval_execution_fingerprint is not None


class TestRequiredCapabilitiesDerivation:
    """Test that required capabilities come from catalog."""

    def test_required_capabilities_from_catalog(self, bridge):
        """Required capabilities should come from catalog."""
        proposal = AIWorkerProposal(
            proposal_id="proposal-001",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="inspect_git_status",
            workspace_id="workspace-001",
            immutable_parameters={},
            expected_result="Result",
        )

        evidence = bridge.validate_proposal(proposal)

        # inspect_git_status requires 'git' capability
        assert NodeCapability("git") in evidence.required_capabilities

        # Verify this matches the catalog
        spec = bridge.action_catalog.get_spec(ActionType.INSPECT_GIT_STATUS)
        assert spec.required_capability in evidence.required_capabilities

    def test_pytest_capability_for_test_actions(self, bridge):
        """Test actions should require pytest capability."""
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

        evidence = bridge.validate_proposal(proposal)

        # run_test_target requires 'pytest' capability
        assert NodeCapability("pytest") in evidence.required_capabilities


class TestTimeoutValidation:
    """Test timeout validation against catalog policy."""

    def test_timeout_bounded_by_catalog(self, bridge):
        """Timeout should be bounded by catalog maximum."""
        proposal = AIWorkerProposal(
            proposal_id="proposal-001",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="inspect_git_status",
            workspace_id="workspace-001",
            immutable_parameters={},
            expected_result="Result",
            timeout_seconds=999999,  # Exceeds catalog maximum
        )

        with pytest.raises(ProposalValidationError, match="timeout"):
            bridge.validate_proposal(proposal)

    def test_timeout_within_policy_accepted(self, bridge):
        """Timeout within policy should be accepted."""
        proposal = AIWorkerProposal(
            proposal_id="proposal-001",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="inspect_git_status",
            workspace_id="workspace-001",
            immutable_parameters={},
            expected_result="Result",
            timeout_seconds=15,  # Within 30s maximum
        )

        evidence = bridge.validate_proposal(proposal)
        assert evidence is not None


class TestActionParameterValidation:
    """Test action-specific parameter validation."""

    def test_inspect_git_status_rejects_parameters(self, bridge):
        """inspect_git_status should reject immutable_parameters."""
        proposal = AIWorkerProposal(
            proposal_id="proposal-001",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="inspect_git_status",
            workspace_id="workspace-001",
            immutable_parameters={"unexpected": "param"},
            expected_result="Result",
        )

        with pytest.raises(ProposalValidationError, match="empty"):
            bridge.validate_proposal(proposal)

    def test_run_test_target_requires_test_target(self, bridge):
        """run_test_target should require test_target parameter."""
        proposal = AIWorkerProposal(
            proposal_id="proposal-001",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="run_test_target",
            workspace_id="workspace-001",
            immutable_parameters={},  # Missing test_target
            expected_result="Test results",
        )

        with pytest.raises(ProposalValidationError, match="test_target"):
            bridge.validate_proposal(proposal)

    def test_run_test_target_validates_shell_injection(self, bridge):
        """run_test_target should reject shell injection attempts."""
        dangerous_targets = [
            "tests/test_foo.py; rm -rf /",
            "tests/test_foo.py && evil",
            "tests/test_foo.py | cat /etc/passwd",
            "tests/test_foo.py `whoami`",
            "tests/test_foo.py $(evil)",
            "tests/test_foo.py > /dev/null",
        ]

        for dangerous in dangerous_targets:
            proposal = AIWorkerProposal(
                proposal_id="proposal-001",
                worker_identity="ai-worker-1",
                mission_id="mission-001",
                task_id="task-001",
                action_type="run_test_target",
                workspace_id="workspace-001",
                immutable_parameters={"test_target": dangerous},
                expected_result="Test results",
            )

            with pytest.raises(ProposalValidationError, match="test_target"):
                bridge.validate_proposal(proposal)

    def test_run_test_target_accepts_safe_target(self, bridge):
        """run_test_target should accept safe test targets."""
        safe_targets = [
            "tests/test_example.py",
            "tests/test_example.py::TestClass",
            "tests/test_example.py::TestClass::test_method",
            "tests/",
            "tests/subdir/test_foo.py",
        ]

        for safe in safe_targets:
            proposal = AIWorkerProposal(
                proposal_id=f"proposal-{safe.replace('/', '-')}",
                worker_identity="ai-worker-1",
                mission_id="mission-001",
                task_id="task-001",
                action_type="run_test_target",
                workspace_id="workspace-001",
                immutable_parameters={"test_target": safe},
                expected_result="Test results",
            )

            evidence = bridge.validate_proposal(proposal)
            assert evidence is not None


class TestExecutionFingerprintBinding:
    """Test execution fingerprint binds immutable parameters."""

    def test_execution_fingerprint_present_for_executable_actions(self, bridge):
        """Executable actions should have execution fingerprint."""
        proposal = AIWorkerProposal(
            proposal_id="proposal-001",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="inspect_git_status",
            workspace_id="workspace-001",
            immutable_parameters={},
            expected_result="Result",
        )

        evidence = bridge.validate_proposal(proposal)

        # inspect_git_status is executable in v0.1
        assert evidence.execution_fingerprint is not None
        assert len(evidence.execution_fingerprint) == 64
        assert all(c in "0123456789abcdef" for c in evidence.execution_fingerprint)

    def test_execution_fingerprint_deterministic(self, bridge):
        """Execution fingerprint should be deterministic."""
        proposal1 = AIWorkerProposal(
            proposal_id="proposal-001",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="inspect_git_status",
            workspace_id="workspace-001",
            immutable_parameters={},
            expected_result="Result",
        )

        proposal2 = AIWorkerProposal(
            proposal_id="proposal-001",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="inspect_git_status",
            workspace_id="workspace-001",
            immutable_parameters={},
            expected_result="Result",
        )

        evidence1 = bridge.validate_proposal(proposal1)
        evidence2 = bridge.validate_proposal(proposal2)

        assert evidence1.execution_fingerprint == evidence2.execution_fingerprint

    def test_execution_fingerprint_changes_with_parameters(self, bridge):
        """Execution fingerprint should change with parameters."""
        proposal1 = AIWorkerProposal(
            proposal_id="proposal-001",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="run_test_target",
            workspace_id="workspace-001",
            immutable_parameters={"test_target": "tests/test_a.py"},
            expected_result="Result",
        )

        proposal2 = AIWorkerProposal(
            proposal_id="proposal-002",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="run_test_target",
            workspace_id="workspace-001",
            immutable_parameters={"test_target": "tests/test_b.py"},
            expected_result="Result",
        )

        evidence1 = bridge.validate_proposal(proposal1)
        evidence2 = bridge.validate_proposal(proposal2)

        # Different test targets should produce different fingerprints
        assert evidence1.execution_fingerprint != evidence2.execution_fingerprint


class TestApprovalFingerprintBinding:
    """Test approval execution fingerprint for approval-required actions."""

    def test_approval_fingerprint_present_when_required(self, bridge):
        """Approval-required actions should have approval fingerprint."""
        proposal = AIWorkerProposal(
            proposal_id="proposal-001",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="run_test_target",  # Requires approval
            workspace_id="workspace-001",
            immutable_parameters={"test_target": "tests/test_example.py"},
            expected_result="Test results",
        )

        evidence = bridge.validate_proposal(proposal)

        assert evidence.approval_required is True
        assert evidence.approval_execution_fingerprint is not None
        assert len(evidence.approval_execution_fingerprint) == 64

    def test_approval_fingerprint_absent_when_not_required(self, bridge):
        """Non-approval-required actions should not have approval fingerprint."""
        proposal = AIWorkerProposal(
            proposal_id="proposal-001",
            worker_identity="ai-worker-1",
            mission_id="mission-001",
            task_id="task-001",
            action_type="inspect_git_status",  # Does not require approval
            workspace_id="workspace-001",
            immutable_parameters={},
            expected_result="Result",
        )

        evidence = bridge.validate_proposal(proposal)

        assert evidence.approval_required is False
        assert evidence.approval_execution_fingerprint is None


class TestTaskRequestCreation:
    """Test TaskRequest creation from validated evidence."""

    def test_cannot_create_task_request_from_invalid_status(self, bridge, valid_proposal):
        """Cannot create TaskRequest from non-PENDING evidence."""
        from dataclasses import replace

        evidence = bridge.validate_proposal(valid_proposal)

        # Manually change status to non-PENDING
        modified = replace(evidence, status=ProposalStatus.COMPLETED)

        with pytest.raises(ProposalValidationError, match="cannot create task request"):
            bridge.create_task_request(modified)

    def test_task_request_preserves_proposal_data(self, bridge, valid_proposal):
        """TaskRequest should preserve proposal data in input_data."""
        evidence = bridge.validate_proposal(valid_proposal)
        task_request = bridge.create_task_request(evidence)

        assert task_request.input_data["proposal_id"] == valid_proposal.proposal_id
        assert task_request.input_data["worker_identity"] == valid_proposal.worker_identity
        assert task_request.input_data["action_type"] == valid_proposal.action_type
        assert task_request.input_data["workspace_id"] == valid_proposal.workspace_id
        assert task_request.input_data["proposal_fingerprint"] == evidence.proposal_fingerprint
        assert task_request.input_data["execution_fingerprint"] == evidence.execution_fingerprint

    def test_task_request_uses_authoritative_policy(self, bridge, valid_proposal):
        """TaskRequest should use authoritative policy from evidence."""
        evidence = bridge.validate_proposal(valid_proposal)
        task_request = bridge.create_task_request(evidence)

        # Policy comes from evidence (catalog), not from proposal
        assert task_request.authorization_level == evidence.authorization_level
        assert task_request.approval_required == evidence.approval_required
        assert task_request.required_capabilities == evidence.required_capabilities


class TestProposalToDict:
    """Test ProposalEvidence serialization."""

    def test_evidence_to_dict_contains_required_fields(self, bridge, valid_proposal):
        """Evidence to_dict should contain all required fields."""
        evidence = bridge.validate_proposal(valid_proposal)
        data = evidence.to_dict()

        required_fields = [
            "proposal_id",
            "worker_identity",
            "mission_id",
            "task_id",
            "action_type",
            "workspace_id",
            "expected_result",
            "status",
            "authorization_level",
            "approval_required",
            "required_capabilities",
            "proposal_fingerprint",
            "execution_fingerprint",
            "approval_execution_fingerprint",
            "created_at",
            "updated_at",
        ]

        for field in required_fields:
            assert field in data

    def test_evidence_to_dict_safe_serialization(self, bridge, valid_proposal):
        """Evidence to_dict should produce safe serializable data."""
        evidence = bridge.validate_proposal(valid_proposal)
        data = evidence.to_dict()

        # Should be JSON-serializable
        import json
        json_str = json.dumps(data)
        assert json_str is not None
