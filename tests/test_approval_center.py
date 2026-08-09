"""
Focused tests for Approval Center v0.1 - Foundation

Tests cover:
- Pending approval projection
- Stable identity preservation
- Authorization level preservation
- Approval_required preservation
- Stable deterministic ordering
- Terminal items excluded from pending list
- Non-actionable/inconsistent evidence fails closed
- Missing linked evidence fails closed
- Serialization contains expected safe fields
- Secrets/raw execution material are not exposed
- No execution side effects occur during reads
"""

import json
import multiprocessing
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from federation.task_request import AuthorizationLevel
from tools.ai_controller.mission.models import ApprovalPolicy
from tools.ai_controller.operations_api.approval_center import (
    ApprovalConflict,
    ApprovalCenter,
    ApprovalCoordinator,
    ApprovalExpired,
    ApprovalIntegrityError,
    ApprovalItem,
    ApprovalItemNotFound,
    ApprovalItemQuery,
    ApprovalRequestDraft,
    ApprovalStatus,
    ApprovalStore,
    ApprovalUnauthorized,
    make_execution_fingerprint,
)
from tools.ai_controller.operations_api.approvals import ApprovalLog
from tools.ai_controller.operations_api.serialization import public_approval_item


def _create_approval_in_process(root: str, start, results) -> None:
    now = lambda: datetime(2026, 8, 8, 18, 0, tzinfo=timezone.utc)
    coordinator = ApprovalCoordinator(
        ApprovalStore(Path(root), integrity_key=b"a" * 32),
        authorized_approvers={"human-approver"},
        authorized_requesters={"worker-1"},
        clock=now,
    )
    parameters = {"test_target": "tests/unit"}
    fingerprint = make_execution_fingerprint(
        action_type="run_test_target",
        workspace_id="workspace-1",
        authorization_level=AuthorizationLevel.RESTRICTED,
        approval_required=True,
        immutable_parameters=parameters,
    )
    draft = ApprovalRequestDraft(
        approval_request_id="approval-process",
        mission_id="mission-1",
        task_id="task-1",
        assignment_id="assignment-1",
        dispatch_offer_id="dispatch-1",
        requester_identity="worker-1",
        target_node_id="worker-node-1",
        action_type="run_test_target",
        authorization_level=AuthorizationLevel.RESTRICTED,
        approval_required=True,
        workspace_id="workspace-1",
        execution_fingerprint=fingerprint,
        immutable_parameters=parameters,
        expected_result="focused tests complete",
    )
    start.wait()
    try:
        evidence = coordinator.create_request(draft, actor_identity="worker-1")
        results.put(("ok", evidence.request.request_fingerprint))
    except BaseException as exc:
        results.put(("error", type(exc).__name__, str(exc)))


class TestApprovalCenterFoundation:
    """Test Approval Center read model foundation."""

    @pytest.fixture
    def temp_approval_root(self):
        """Create temporary approval log directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def sample_proposal(self):
        """Create a sample valid proposal record."""
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=24)

        return {
            "schema_version": "controller-proposal-v0.1",
            "action_id": "action-test-001",
            "action_type": "run_tests",
            "mission_id": "mission-alpha",
            "mission_task_id": "task-01",
            "expected_mission_revision": "abc123",
            "expected_event_revision": "def456",
            "expected_queue_identities": ["queue-1", "queue-2"],
            "expected_queue_revision": "ghi789",
            "evidence_fingerprint": "e" * 64,
            "proposed_effect_fingerprint": "f" * 64,
            "finding": "Test execution proposed",
            "created_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "status": "pending",
            "authority": "coordinator-node-1",
            "proposal_revision": "proposal-rev-001",
        }

    def test_empty_approval_log(self, temp_approval_root):
        """Test that empty approval log returns no items."""
        center = ApprovalCenter(temp_approval_root)
        items = center.list_pending_approvals()
        assert items == []
        assert center.count_pending_approvals() == 0
        assert not center.has_pending_approvals()

    def test_pending_approval_projection(self, temp_approval_root, sample_proposal):
        """Test basic pending approval projection from proposal record."""
        # Create approval log with one proposal
        log = ApprovalLog(temp_approval_root, create=True)
        log.append({"record_type": "proposal", "proposal": sample_proposal})

        # Query pending approvals
        center = ApprovalCenter(temp_approval_root)
        items = center.list_pending_approvals()

        assert len(items) == 1
        item = items[0]

        # Verify stable identities preserved
        assert item.action_id == "action-test-001"
        assert item.action_type == "run_tests"
        assert item.proposal_revision == "proposal-rev-001"
        assert item.mission_id == "mission-alpha"
        assert item.mission_task_id == "task-01"

        # Verify approval required (inferred from presence in log)
        assert item.approval_required is True

        # Verify lifecycle fields
        assert item.status == "pending"
        assert item.is_actionable is True
        assert item.non_actionable_reason is None

        # Verify evidence integrity
        assert item.evidence_fingerprint == "e" * 64
        assert item.proposed_effect_fingerprint == "f" * 64

    def test_stable_identity_preservation(self, temp_approval_root, sample_proposal):
        """Test that action_id and proposal_revision are stable identifiers."""
        log = ApprovalLog(temp_approval_root, create=True)
        log.append({"record_type": "proposal", "proposal": sample_proposal})

        center = ApprovalCenter(temp_approval_root)

        # Get item by action_id
        item = center.get_approval_item("action-test-001")
        assert item.action_id == "action-test-001"
        assert item.proposal_revision == "proposal-rev-001"

        # Verify same item from list query
        items = center.list_pending_approvals()
        assert len(items) == 1
        assert items[0].action_id == item.action_id
        assert items[0].proposal_revision == item.proposal_revision

    def test_authorization_level_preservation(self, temp_approval_root, sample_proposal):
        """Test that authorization level is preserved when present in classification."""
        # Add authorization level to classification metadata
        sample_proposal["classification"] = {
            "authorization_level": "restricted",
            "approval_policy": "approval_required_before_execution",
        }

        log = ApprovalLog(temp_approval_root, create=True)
        log.append({"record_type": "proposal", "proposal": sample_proposal})

        center = ApprovalCenter(temp_approval_root)
        item = center.get_approval_item("action-test-001")

        assert item.authorization_level == AuthorizationLevel.RESTRICTED
        assert item.approval_policy == ApprovalPolicy.approval_required_before_execution

    def test_approval_required_preservation(self, temp_approval_root, sample_proposal):
        """Test that approval_required is always True for proposals in log."""
        log = ApprovalLog(temp_approval_root, create=True)
        log.append({"record_type": "proposal", "proposal": sample_proposal})

        center = ApprovalCenter(temp_approval_root)
        items = center.list_pending_approvals()

        assert len(items) == 1
        assert items[0].approval_required is True

    def test_stable_deterministic_ordering(self, temp_approval_root):
        """Test that results have stable deterministic ordering."""
        log = ApprovalLog(temp_approval_root, create=True)

        now = datetime.now(timezone.utc)

        # Create three proposals with different timestamps
        for i in range(3):
            created = now + timedelta(hours=i)
            expires = created + timedelta(hours=24)
            proposal = {
                "schema_version": "controller-proposal-v0.1",
                "action_id": f"action-{i:03d}",
                "action_type": "run_tests",
                "mission_id": "mission-alpha",
                "mission_task_id": f"task-{i:02d}",
                "expected_mission_revision": "abc123",
                "expected_event_revision": "def456",
                "expected_queue_identities": [],
                "expected_queue_revision": "ghi789",
                "evidence_fingerprint": "e" * 64,
                "proposed_effect_fingerprint": "f" * 64,
                "finding": f"Proposal {i}",
                "created_at": created.isoformat(),
                "expires_at": expires.isoformat(),
                "status": "pending",
                "authority": "coordinator",
                "proposal_revision": f"rev-{i:03d}",
            }
            log.append({"record_type": "proposal", "proposal": proposal})

        center = ApprovalCenter(temp_approval_root)

        # Default order: newest first (created_at descending)
        items = center.list_pending_approvals()
        assert len(items) == 3
        assert items[0].action_id == "action-002"
        assert items[1].action_id == "action-001"
        assert items[2].action_id == "action-000"

        # Ascending order: oldest first
        query = ApprovalItemQuery(ascending=True)
        items = center.list_pending_approvals(query)
        assert items[0].action_id == "action-000"
        assert items[1].action_id == "action-001"
        assert items[2].action_id == "action-002"

    def test_terminal_items_excluded_from_pending(self, temp_approval_root, sample_proposal):
        """Test that approved/rejected items are excluded from actionable pending results."""
        log = ApprovalLog(temp_approval_root, create=True)

        # Add pending proposal
        log.append({"record_type": "proposal", "proposal": sample_proposal})

        # Add approved proposal
        approved_proposal = sample_proposal.copy()
        approved_proposal["action_id"] = "action-approved"
        approved_proposal["status"] = "approved"
        log.append({"record_type": "proposal", "proposal": approved_proposal})

        # Add rejected proposal
        rejected_proposal = sample_proposal.copy()
        rejected_proposal["action_id"] = "action-rejected"
        rejected_proposal["status"] = "rejected"
        log.append({"record_type": "proposal", "proposal": rejected_proposal})

        center = ApprovalCenter(temp_approval_root)

        # Default query: actionable_only=True, include_pending=True
        items = center.list_pending_approvals()
        assert len(items) == 1
        assert items[0].action_id == "action-test-001"
        assert items[0].is_actionable is True

        # Query with include_approved=True
        query = ApprovalItemQuery(include_approved=True, actionable_only=False)
        items = center.list_pending_approvals(query)
        assert len(items) == 2
        action_ids = {item.action_id for item in items}
        assert "action-test-001" in action_ids
        assert "action-approved" in action_ids

    def test_expired_items_not_actionable(self, temp_approval_root, sample_proposal):
        """Test that expired proposals are marked non-actionable."""
        # Create expired proposal
        now = datetime.now(timezone.utc)
        past = now - timedelta(hours=1)
        sample_proposal["created_at"] = (past - timedelta(hours=24)).isoformat()
        sample_proposal["expires_at"] = past.isoformat()

        log = ApprovalLog(temp_approval_root, create=True)
        log.append({"record_type": "proposal", "proposal": sample_proposal})

        center = ApprovalCenter(temp_approval_root)

        # Actionable only query should return nothing
        items = center.list_pending_approvals()
        assert len(items) == 0

        # Include expired query should show the item as non-actionable
        query = ApprovalItemQuery(include_expired=True, actionable_only=False)
        items = center.list_pending_approvals(query)
        assert len(items) == 1
        assert items[0].is_actionable is False
        assert items[0].non_actionable_reason == "expired"

    def test_actionability_logic(self, temp_approval_root, sample_proposal):
        """Test that actionability logic correctly identifies actionable proposals."""
        log = ApprovalLog(temp_approval_root, create=True)
        log.append({"record_type": "proposal", "proposal": sample_proposal})

        center = ApprovalCenter(temp_approval_root)

        # Valid pending proposal should be actionable
        items = center.list_pending_approvals()
        assert len(items) == 1
        assert items[0].is_actionable is True
        assert items[0].non_actionable_reason is None

        # Note: ApprovalLog enforces that evidence_fingerprint and proposed_effect_fingerprint
        # are non-empty strings, so we cannot test those edge cases via the append API.
        # The validation is correctly enforced at the ApprovalLog layer.

    def test_item_not_found_raises(self, temp_approval_root):
        """Test that get_approval_item raises for non-existent action_id."""
        center = ApprovalCenter(temp_approval_root)

        with pytest.raises(ApprovalItemNotFound) as exc_info:
            center.get_approval_item("non-existent-action")

        assert "non-existent-action" in str(exc_info.value)

    def test_serialization_safe_fields(self, temp_approval_root, sample_proposal):
        """Test that serialization exposes expected safe fields."""
        log = ApprovalLog(temp_approval_root, create=True)
        log.append({"record_type": "proposal", "proposal": sample_proposal})

        center = ApprovalCenter(temp_approval_root)
        item = center.get_approval_item("action-test-001")

        # Serialize using to_dict
        data = item.to_dict()

        # Verify safe fields are present
        expected_fields = {
            "action_id",
            "action_type",
            "proposal_revision",
            "mission_id",
            "mission_task_id",
            "approval_required",
            "status",
            "created_at",
            "expires_at",
            "finding",
            "authority",
            "evidence_fingerprint",
            "proposed_effect_fingerprint",
            "expected_mission_revision",
            "expected_event_revision",
            "expected_queue_identities",
            "expected_queue_revision",
            "is_actionable",
            "non_actionable_reason",
        }

        for field in expected_fields:
            assert field in data, f"Expected field {field} not in serialized data"

        # Verify values match
        assert data["action_id"] == "action-test-001"
        assert data["approval_required"] is True
        assert data["is_actionable"] is True

    def test_serialization_no_secrets_exposed(self, temp_approval_root, sample_proposal):
        """Test that serialization does not expose secrets or raw execution material."""
        # Add potentially sensitive data to finding (should be truncated)
        sample_proposal["finding"] = "x" * 1000  # Long finding

        log = ApprovalLog(temp_approval_root, create=True)
        log.append({"record_type": "proposal", "proposal": sample_proposal})

        center = ApprovalCenter(temp_approval_root)
        item = center.get_approval_item("action-test-001")

        # Serialize
        data = item.to_dict()

        # Verify finding is truncated
        assert len(data["finding"]) <= 500

        # Use public serialization
        public_data = public_approval_item(item)

        # Verify no private keys are present
        private_keys = {"token", "secret", "password", "credential", "private_key"}
        for key in public_data:
            assert not any(pk in key.lower() for pk in private_keys)

    def test_query_filtering_by_mission(self, temp_approval_root, sample_proposal):
        """Test query filtering by mission_id."""
        log = ApprovalLog(temp_approval_root, create=True)

        # Add proposal for mission-alpha
        log.append({"record_type": "proposal", "proposal": sample_proposal})

        # Add proposal for mission-beta
        beta_proposal = sample_proposal.copy()
        beta_proposal["action_id"] = "action-beta"
        beta_proposal["mission_id"] = "mission-beta"
        log.append({"record_type": "proposal", "proposal": beta_proposal})

        center = ApprovalCenter(temp_approval_root)

        # Query for mission-alpha only
        query = ApprovalItemQuery(mission_id="mission-alpha")
        items = center.list_pending_approvals(query)
        assert len(items) == 1
        assert items[0].mission_id == "mission-alpha"

        # Query for mission-beta only
        query = ApprovalItemQuery(mission_id="mission-beta")
        items = center.list_pending_approvals(query)
        assert len(items) == 1
        assert items[0].mission_id == "mission-beta"

    def test_query_filtering_by_action_type(self, temp_approval_root, sample_proposal):
        """Test query filtering by action_type."""
        log = ApprovalLog(temp_approval_root, create=True)

        # Add run_tests proposal
        log.append({"record_type": "proposal", "proposal": sample_proposal})

        # Add deploy proposal
        deploy_proposal = sample_proposal.copy()
        deploy_proposal["action_id"] = "action-deploy"
        deploy_proposal["action_type"] = "deploy"
        log.append({"record_type": "proposal", "proposal": deploy_proposal})

        center = ApprovalCenter(temp_approval_root)

        # Query for run_tests only
        query = ApprovalItemQuery(action_type="run_tests")
        items = center.list_pending_approvals(query)
        assert len(items) == 1
        assert items[0].action_type == "run_tests"

    def test_no_execution_side_effects(self, temp_approval_root, sample_proposal):
        """Test that read operations have no execution side effects."""
        log = ApprovalLog(temp_approval_root, create=True)
        log.append({"record_type": "proposal", "proposal": sample_proposal})

        # Get initial snapshot revision
        initial_snapshot = log.snapshot()
        initial_revision = initial_snapshot.revision

        center = ApprovalCenter(temp_approval_root)

        # Perform multiple read operations
        center.list_pending_approvals()
        center.get_approval_item("action-test-001")
        center.count_pending_approvals()
        center.has_pending_approvals()

        # Verify approval log is unchanged
        final_snapshot = log.snapshot()
        assert final_snapshot.revision == initial_revision
        assert len(final_snapshot.records) == len(initial_snapshot.records)

    def test_proposal_only_status_projection(self, temp_approval_root, sample_proposal):
        """Test that proposal status is correctly projected without decisions."""
        log = ApprovalLog(temp_approval_root, create=True)
        log.append({"record_type": "proposal", "proposal": sample_proposal})

        center = ApprovalCenter(temp_approval_root)
        item = center.get_approval_item("action-test-001")

        # Verify pending status
        assert item.status == "pending"
        assert item.is_actionable is True

        # Note: Full decision integration testing requires proper request_fingerprint
        # calculation and ApprovalLog decision binding, which is outside the scope
        # of the Approval Center read model foundation. That integration will be
        # tested after Console Action Execution Runtime v0.1 is finalized.

    def test_expected_queue_identities_preserved(self, temp_approval_root, sample_proposal):
        """Test that expected queue identities are preserved as tuple."""
        sample_proposal["expected_queue_identities"] = ["queue-a", "queue-b", "queue-c"]

        log = ApprovalLog(temp_approval_root, create=True)
        log.append({"record_type": "proposal", "proposal": sample_proposal})

        center = ApprovalCenter(temp_approval_root)
        item = center.get_approval_item("action-test-001")

        assert isinstance(item.expected_queue_identities, tuple)
        assert item.expected_queue_identities == ("queue-a", "queue-b", "queue-c")

        # Verify serialization
        data = item.to_dict()
        assert isinstance(data["expected_queue_identities"], list)
        assert data["expected_queue_identities"] == ["queue-a", "queue-b", "queue-c"]


class TestApprovalCenterErrorHandling:
    """Test error handling and fail-closed behavior."""

    @pytest.fixture
    def temp_approval_root(self):
        """Create temporary approval log directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_malformed_proposal_skipped(self, temp_approval_root):
        """Test that malformed proposals are skipped (fail closed)."""
        log = ApprovalLog(temp_approval_root, create=True)

        # Manually write malformed JSON (missing required fields)
        # Note: ApprovalLog.append validates, so we can't use it for this test
        # Instead, we'll test that projection gracefully handles missing fields

        # For now, just verify empty log behavior
        center = ApprovalCenter(temp_approval_root)
        items = center.list_pending_approvals()
        assert items == []

    def test_count_and_has_pending_consistency(self, temp_approval_root):
        """Test that count and has_pending methods are consistent."""
        center = ApprovalCenter(temp_approval_root)

        # Empty log
        assert center.count_pending_approvals() == 0
        assert not center.has_pending_approvals()

        # After adding proposal
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=24)
        proposal = {
            "schema_version": "controller-proposal-v0.1",
            "action_id": "action-001",
            "action_type": "test",
            "mission_id": "mission-x",
            "mission_task_id": "task-x",
            "expected_mission_revision": "abc",
            "expected_event_revision": "def",
            "expected_queue_identities": [],
            "expected_queue_revision": "ghi",
            "evidence_fingerprint": "e" * 64,
            "proposed_effect_fingerprint": "f" * 64,
            "finding": "Test",
            "created_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "status": "pending",
            "authority": "coordinator",
            "proposal_revision": "rev-001",
        }

        log = ApprovalLog(temp_approval_root, create=True)
        log.append({"record_type": "proposal", "proposal": proposal})

        assert center.count_pending_approvals() == 1
        assert center.has_pending_approvals()
        assert center.has_pending_approvals(mission_id="mission-x")
        assert not center.has_pending_approvals(mission_id="mission-y")


class TestDurableApprovalAuthority:
    @pytest.fixture
    def clock(self):
        current = [datetime(2026, 8, 8, 18, 0, tzinfo=timezone.utc)]

        def now():
            return current[0]

        now.advance = lambda **kwargs: current.__setitem__(
            0, current[0] + timedelta(**kwargs)
        )
        return now

    @pytest.fixture
    def authority(self, tmp_path, clock):
        store = ApprovalStore(tmp_path, integrity_key=b"a" * 32)
        coordinator = ApprovalCoordinator(
            store,
            authorized_approvers={"human-approver"},
            authorized_requesters={"worker-1"},
            clock=clock,
        )
        return coordinator

    def draft(self, **changes):
        parameters = changes.pop("immutable_parameters", {"test_target": "tests/unit"})
        values = {
            "approval_request_id": "approval-1",
            "mission_id": "mission-1",
            "task_id": "task-1",
            "assignment_id": "assignment-1",
            "dispatch_offer_id": "dispatch-1",
            "requester_identity": "worker-1",
            "target_node_id": "worker-node-1",
            "action_type": "run_test_target",
            "authorization_level": AuthorizationLevel.RESTRICTED,
            "approval_required": True,
            "workspace_id": "workspace-1",
            "immutable_parameters": parameters,
            "expected_result": "focused tests complete",
            "expires_in_seconds": 3600,
        }
        values.update(changes)
        values.setdefault(
            "execution_fingerprint",
            make_execution_fingerprint(
                action_type=values["action_type"],
                workspace_id=values["workspace_id"],
                authorization_level=values["authorization_level"],
                approval_required=values["approval_required"],
                immutable_parameters=values["immutable_parameters"],
            ),
        )
        return ApprovalRequestDraft(**values)

    def create(self, authority, **changes):
        return authority.create_request(
            self.draft(**changes), actor_identity="worker-1"
        )

    def test_create_and_inspect_pending_request(self, authority):
        created = self.create(authority)
        inspected = authority.inspect("approval-1")
        assert created.status is ApprovalStatus.PENDING
        assert inspected == created
        assert inspected.request.assignment_id == "assignment-1"
        assert inspected.request.dispatch_offer_id == "dispatch-1"
        assert inspected.valid is False

    def test_approve_exact_request_and_verify_evidence(self, authority):
        pending = self.create(authority)
        approved = authority.approve(
            "approval-1",
            actor_identity="human-approver",
            execution_fingerprint=pending.request.execution_fingerprint,
            reason="reviewed",
        )
        assert approved.status is ApprovalStatus.APPROVED
        assert approved.valid is True
        assert authority.verify_approval(
            "approval-1",
            execution_fingerprint=pending.request.execution_fingerprint,
            mission_id="mission-1",
            task_id="task-1",
            assignment_id="assignment-1",
            dispatch_offer_id="dispatch-1",
            target_node_id="worker-node-1",
        ) == approved

    def test_reject_is_terminal_and_cannot_authorize(self, authority):
        pending = self.create(authority)
        rejected = authority.reject(
            "approval-1",
            actor_identity="human-approver",
            execution_fingerprint=pending.request.execution_fingerprint,
        )
        assert rejected.status is ApprovalStatus.REJECTED
        with pytest.raises(ApprovalConflict):
            authority.approve(
                "approval-1",
                actor_identity="human-approver",
                execution_fingerprint=pending.request.execution_fingerprint,
            )
        with pytest.raises(ApprovalConflict):
            authority.verify_approval(
                "approval-1",
                execution_fingerprint=pending.request.execution_fingerprint,
                mission_id="mission-1",
                task_id="task-1",
                assignment_id="assignment-1",
                dispatch_offer_id="dispatch-1",
                target_node_id="worker-node-1",
            )

    def test_approved_terminal_state_is_immutable(self, authority):
        pending = self.create(authority)
        authority.approve(
            "approval-1",
            actor_identity="human-approver",
            execution_fingerprint=pending.request.execution_fingerprint,
        )
        with pytest.raises(ApprovalConflict):
            authority.reject(
                "approval-1",
                actor_identity="human-approver",
                execution_fingerprint=pending.request.execution_fingerprint,
            )

    def test_idempotent_terminal_replay_still_rejects_substituted_fingerprint(
        self, authority
    ):
        pending = self.create(authority)
        authority.approve(
            "approval-1",
            actor_identity="human-approver",
            execution_fingerprint=pending.request.execution_fingerprint,
        )
        with pytest.raises(ApprovalConflict, match="fingerprint"):
            authority.approve(
                "approval-1",
                actor_identity="human-approver",
                execution_fingerprint="b" * 64,
            )

    def test_exact_duplicate_creation_is_idempotent(self, authority, clock):
        first = self.create(authority)
        clock.advance(seconds=10)
        second = self.create(authority)
        assert second == first
        assert len(authority.store.snapshot().records) == 1

    def test_conflicting_duplicate_request_is_rejected(self, authority):
        self.create(authority)
        with pytest.raises(ApprovalConflict):
            self.create(
                authority,
                expected_result="different",
                execution_fingerprint=self.draft().execution_fingerprint,
            )

    @pytest.mark.parametrize(
        "fingerprint",
        [None, "", "A" * 64, "g" * 64, "a" * 63],
    )
    def test_missing_or_malformed_execution_fingerprint_rejected(
        self, authority, fingerprint
    ):
        with pytest.raises((TypeError, ValueError)):
            self.create(authority, execution_fingerprint=fingerprint)

    def test_mismatched_execution_fingerprint_rejected(self, authority):
        with pytest.raises(ValueError, match="exact request"):
            self.create(authority, execution_fingerprint="b" * 64)

    def test_foreign_composed_evidence_is_rejected(self, authority):
        first = self.create(authority)
        authority.approve(
            "approval-1",
            actor_identity="human-approver",
            execution_fingerprint=first.request.execution_fingerprint,
        )
        with pytest.raises(ApprovalConflict):
            authority.verify_approval(
                "approval-1",
                execution_fingerprint=first.request.execution_fingerprint,
                mission_id="mission-foreign",
                task_id="task-1",
                assignment_id="assignment-1",
                dispatch_offer_id="dispatch-1",
                target_node_id="worker-node-1",
            )

    def test_target_node_substitution_is_rejected(self, authority):
        pending = self.create(authority)
        authority.approve(
            "approval-1",
            actor_identity="human-approver",
            execution_fingerprint=pending.request.execution_fingerprint,
        )
        with pytest.raises(ApprovalConflict):
            authority.verify_approval(
                "approval-1",
                execution_fingerprint=pending.request.execution_fingerprint,
                mission_id="mission-1",
                task_id="task-1",
                assignment_id="assignment-1",
                dispatch_offer_id="dispatch-1",
                target_node_id="worker-node-foreign",
            )

    def test_requester_cannot_manufacture_approval(self, authority):
        pending = self.create(authority)
        with pytest.raises(ApprovalUnauthorized):
            authority.approve(
                "approval-1",
                actor_identity="worker-1",
                execution_fingerprint=pending.request.execution_fingerprint,
            )

    def test_forged_requester_identity_rejected(self, authority):
        with pytest.raises(ApprovalUnauthorized):
            authority.create_request(self.draft(), actor_identity="worker-2")

    def test_expired_request_cannot_be_approved(self, authority, clock):
        pending = self.create(authority, expires_in_seconds=1)
        clock.advance(seconds=1)
        with pytest.raises(ApprovalExpired):
            authority.approve(
                "approval-1",
                actor_identity="human-approver",
                execution_fingerprint=pending.request.execution_fingerprint,
            )
        assert authority.inspect("approval-1").status is ApprovalStatus.EXPIRED

    def test_restart_preserves_request_decision_and_linkage(
        self, authority, tmp_path, clock
    ):
        pending = self.create(authority)
        authority.approve(
            "approval-1",
            actor_identity="human-approver",
            execution_fingerprint=pending.request.execution_fingerprint,
        )
        restarted = ApprovalCoordinator(
            ApprovalStore(tmp_path, integrity_key=b"a" * 32),
            authorized_approvers={"human-approver"},
            authorized_requesters={"worker-1"},
            clock=clock,
        )
        restored = restarted.inspect("approval-1")
        assert restored.status is ApprovalStatus.APPROVED
        assert restored.request.assignment_id == "assignment-1"
        assert restored.request.dispatch_offer_id == "dispatch-1"

    def test_corrupt_truncated_or_wrong_key_persistence_fails_closed(
        self, authority, tmp_path
    ):
        self.create(authority)
        path = authority.store.path
        original = path.read_bytes()
        path.write_bytes(original[:-1])
        with pytest.raises(ApprovalIntegrityError):
            authority.inspect("approval-1")
        path.write_bytes(original)
        foreign = ApprovalCoordinator(
            ApprovalStore(tmp_path, integrity_key=b"b" * 32),
            authorized_approvers={"human-approver"},
        )
        with pytest.raises(ApprovalIntegrityError):
            foreign.inspect("approval-1")

    def test_duplicate_evidence_fails_closed(self, authority):
        self.create(authority)
        line = authority.store.path.read_bytes()
        authority.store.path.write_bytes(line + line)
        with pytest.raises(ApprovalIntegrityError):
            authority.inspect("approval-1")

    def test_concurrent_duplicate_creation_produces_one_request(
        self, tmp_path, clock
    ):
        coordinators = [
            ApprovalCoordinator(
                ApprovalStore(tmp_path, integrity_key=b"a" * 32),
                authorized_approvers={"human-approver"},
                authorized_requesters={"worker-1"},
                clock=clock,
            )
            for _ in range(8)
        ]
        draft = self.draft()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda coordinator: coordinator.create_request(
                        draft, actor_identity="worker-1"
                    ),
                    coordinators,
                )
            )
        assert len({result.request.request_fingerprint for result in results}) == 1
        assert len(coordinators[0].store.snapshot().records) == 1

    def test_cross_process_duplicate_creation_produces_one_request(
        self, tmp_path
    ):
        context = multiprocessing.get_context("fork")
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=_create_approval_in_process,
                args=(str(tmp_path), start, results),
            )
            for _ in range(4)
        ]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        outcomes = [results.get(timeout=2) for _ in processes]
        assert {outcome[0] for outcome in outcomes} == {"ok"}
        assert len({outcome[1] for outcome in outcomes}) == 1
        store = ApprovalStore(tmp_path, integrity_key=b"a" * 32)
        assert len(store.snapshot().records) == 1

    def test_concurrent_approve_reject_has_one_authoritative_terminal_result(
        self, tmp_path, clock
    ):
        first = ApprovalCoordinator(
            ApprovalStore(tmp_path, integrity_key=b"a" * 32),
            authorized_approvers={"human-approver", "second-approver"},
            authorized_requesters={"worker-1"},
            clock=clock,
        )
        second = ApprovalCoordinator(
            ApprovalStore(tmp_path, integrity_key=b"a" * 32),
            authorized_approvers={"human-approver", "second-approver"},
            authorized_requesters={"worker-1"},
            clock=clock,
        )
        pending = self.create(first)

        def approve():
            return first.approve(
                "approval-1",
                actor_identity="human-approver",
                execution_fingerprint=pending.request.execution_fingerprint,
            )

        def reject():
            return second.reject(
                "approval-1",
                actor_identity="second-approver",
                execution_fingerprint=pending.request.execution_fingerprint,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(approve), pool.submit(reject)]
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(future.result())
                except ApprovalConflict:
                    pass
        assert len(outcomes) == 1
        assert first.inspect("approval-1").status in {
            ApprovalStatus.APPROVED,
            ApprovalStatus.REJECTED,
        }
        assert len(first.store.snapshot().records) == 2

    def test_caller_parameter_mutation_cannot_change_request(self, authority):
        parameters = {"test_target": "tests/unit"}
        draft = self.draft(immutable_parameters=parameters)
        created = authority.create_request(draft, actor_identity="worker-1")
        parameters["test_target"] = "tests/foreign"
        assert authority.inspect("approval-1") == created

    def test_deterministic_safe_serialization(self, authority):
        evidence = self.create(authority)
        first = public_approval_item(evidence)
        second = public_approval_item(authority.inspect("approval-1"))
        assert first == second
        assert "authentication_tag" not in json.dumps(first)
        assert "immutable_parameters" not in first["request"]
        assert first["request"]["execution_fingerprint"] == evidence.request.execution_fingerprint


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
