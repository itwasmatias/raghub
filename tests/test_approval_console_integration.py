"""
Approval-to-Console Integration Tests v0.1

Tests cover the governed approval-to-execution authority path.
"""

from __future__ import annotations

import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from federation.task_request import AuthorizationLevel
from tools.ai_controller.operations_api.approval_center import (
    ApprovalCoordinator,
    ApprovalRequestDraft,
    ApprovalStatus,
    ApprovalStore,
    make_execution_fingerprint as make_approval_fingerprint,
)
from tools.ai_controller.operations_api.console_server import JobStatus
from tools.ai_controller.operations_api.console_server.actions import ActionType
from test_console_server import (  # noqa: F401
    console,
    client,
    workspace_root,
    workspaces,
    node_registry,
    heartbeat_registry,
)


@pytest.fixture
def approval_coordinator(tmp_path):
    """Create an ApprovalCoordinator for testing."""
    now = lambda: datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    store = ApprovalStore(tmp_path / "approvals", integrity_key=b"test-key-32-bytes-xxxxxxxxxxxxxx")
    coordinator = ApprovalCoordinator(
        store,
        authorized_approvers={"human-approver"},
        authorized_requesters={"full-principal"},
        clock=now,
    )
    return coordinator


@pytest.fixture
def console_with_approval(console, approval_coordinator):
    """Add approval coordinator to console."""
    console.approval_coordinator = approval_coordinator
    console.action_runtime.approval_coordinator = approval_coordinator
    return console


class TestApprovalRequiredActionsRemainPending:
    """Test that approval-required actions stay pending without approval."""

    def test_approval_required_action_remains_pending_without_approval(
        self, client, console_with_approval
    ):
        """Approval-required action should remain PENDING without approval."""
        # Temporarily mark inspect_git_status as approval-required for testing
        original_spec = console_with_approval.action_catalog._SPECS[ActionType.INSPECT_GIT_STATUS]
        test_spec = replace(original_spec, approval_required=True)
        console_with_approval.action_catalog._SPECS[ActionType.INSPECT_GIT_STATUS] = test_spec

        try:
            response = client.post(
                "/api/console/v1/actions",
                json={
                    "action_type": "inspect_git_status",
                    "workspace_id": "test-workspace",
                },
                headers={"Authorization": "Bearer full-token"},
            )

            assert response.status_code == 201
            job = console_with_approval.job_tracker.get_job(response.json["job_id"])

            # Job should be pending (not executed)
            assert job.status is JobStatus.PENDING
            assert job.approval_request_id is not None
            assert job.approval_execution_fingerprint is not None

        finally:
            # Restore original spec
            console_with_approval.action_catalog._SPECS[ActionType.INSPECT_GIT_STATUS] = original_spec


class TestApprovalPermitsExecution:
    """Test that valid approval permits execution."""

    def test_exact_valid_approval_permits_execution(
        self, client, console_with_approval, approval_coordinator
    ):
        """Exact valid approval should permit execution."""
        # Temporarily mark inspect_git_status as approval-required
        original_spec = console_with_approval.action_catalog._SPECS[ActionType.INSPECT_GIT_STATUS]
        test_spec = replace(original_spec, approval_required=True, authorization_level=AuthorizationLevel.RESTRICTED)
        console_with_approval.action_catalog._SPECS[ActionType.INSPECT_GIT_STATUS] = test_spec

        try:
            # Create job
            response = client.post(
                "/api/console/v1/actions",
                json={
                    "action_type": "inspect_git_status",
                    "workspace_id": "test-workspace",
                },
                headers={"Authorization": "Bearer full-token"},
            )
            assert response.status_code == 201
            job = console_with_approval.job_tracker.get_job(response.json["job_id"])

            # Approve the request
            approval_coordinator.approve(
                job.approval_request_id,
                actor_identity="human-approver",
                execution_fingerprint=job.approval_execution_fingerprint,
                reason="Approved for testing",
            )

            # Now execute
            result = console_with_approval.action_runtime.execute(job.job_id)

            # Should succeed
            assert result.status is JobStatus.SUCCEEDED

        finally:
            console_with_approval.action_catalog._SPECS[ActionType.INSPECT_GIT_STATUS] = original_spec


class TestApprovalFailClosed:
    """Test that approval verification fails closed."""

    def test_missing_approval_request_id_cannot_execute(
        self, console_with_approval
    ):
        """Missing approval_request_id should fail execution."""
        original_spec = console_with_approval.action_catalog._SPECS[ActionType.INSPECT_GIT_STATUS]
        test_spec = replace(original_spec, approval_required=True)
        console_with_approval.action_catalog._SPECS[ActionType.INSPECT_GIT_STATUS] = test_spec

        try:
            # Create job manually without approval_request_id
            job = console_with_approval.job_tracker.create_job(
                action_type="inspect_git_status",
                workspace_id="test-workspace",
                created_by="user",
            )

            # Create dispatch offer manually with approval_required
            from datetime import timedelta
            from federation.dispatch_offer import DispatchOffer
            offer = console_with_approval.task_dispatch_coordinator.create_offer(
                assignment_id="test-assignment",
                actor_node_id="test-node",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )

            # Update job with offer info
            job = replace(
                job,
                dispatch_offer_id=offer.offer_id,
                assignment_id="test-assignment",
                target_node_id="test-node",
                task_id="test-task",
                mission_id="test-mission",
            )
            console_with_approval.job_tracker._save(job)

            # Should fail with missing approval_request_id
            with pytest.raises(ValueError, match="approval_request_id"):
                console_with_approval.action_runtime.execute(job.job_id)

        finally:
            console_with_approval.action_catalog._SPECS[ActionType.INSPECT_GIT_STATUS] = original_spec

    def test_missing_approval_execution_fingerprint_cannot_execute(
        self, client, console_with_approval
    ):
        """Missing approval_execution_fingerprint should fail execution."""
        original_spec = console_with_approval.action_catalog._SPECS[ActionType.INSPECT_GIT_STATUS]
        test_spec = replace(original_spec, approval_required=True)
        console_with_approval.action_catalog._SPECS[ActionType.INSPECT_GIT_STATUS] = test_spec

        try:
            response = client.post(
                "/api/console/v1/actions",
                json={
                    "action_type": "inspect_git_status",
                    "workspace_id": "test-workspace",
                },
                headers={"Authorization": "Bearer full-token"},
            )
            job = console_with_approval.job_tracker.get_job(response.json["job_id"])

            # Remove approval_execution_fingerprint
            mutated = replace(job, approval_execution_fingerprint=None)
            console_with_approval.job_tracker._save(mutated)

            with pytest.raises(ValueError, match="approval_execution_fingerprint"):
                console_with_approval.action_runtime.execute(job.job_id)

        finally:
            console_with_approval.action_catalog._SPECS[ActionType.INSPECT_GIT_STATUS] = original_spec

    def test_rejected_approval_cannot_execute(
        self, client, console_with_approval, approval_coordinator
    ):
        """Rejected approval should not permit execution."""
        original_spec = console_with_approval.action_catalog._SPECS[ActionType.INSPECT_GIT_STATUS]
        test_spec = replace(original_spec, approval_required=True, authorization_level=AuthorizationLevel.RESTRICTED)
        console_with_approval.action_catalog._SPECS[ActionType.INSPECT_GIT_STATUS] = test_spec

        try:
            response = client.post(
                "/api/console/v1/actions",
                json={
                    "action_type": "inspect_git_status",
                    "workspace_id": "test-workspace",
                },
                headers={"Authorization": "Bearer full-token"},
            )
            job = console_with_approval.job_tracker.get_job(response.json["job_id"])

            # Reject the approval
            approval_coordinator.reject(
                job.approval_request_id,
                actor_identity="human-approver",
                execution_fingerprint=job.approval_execution_fingerprint,
                reason="Rejected for testing",
            )

            # Should fail to execute
            with pytest.raises(ValueError, match="approval verification failed"):
                console_with_approval.action_runtime.execute(job.job_id)

        finally:
            console_with_approval.action_catalog._SPECS[ActionType.INSPECT_GIT_STATUS] = original_spec


class TestNoApprovalRequiredPath:
    """Test that non-approval-required actions work as before."""

    def test_inspect_git_status_without_approval_requirement(
        self, client, console_with_approval
    ):
        """inspect_git_status without approval requirement should execute normally."""
        # Ensure approval_required is False (default for inspect_git_status)
        spec = console_with_approval.action_catalog.get_spec(ActionType.INSPECT_GIT_STATUS)
        assert spec.approval_required is False

        response = client.post(
            "/api/console/v1/actions",
            json={
                "action_type": "inspect_git_status",
                "workspace_id": "test-workspace",
            },
            headers={"Authorization": "Bearer full-token"},
        )

        assert response.status_code == 201
        job = console_with_approval.job_tracker.get_job(response.json["job_id"])

        # Should execute successfully without approval
        assert job.status is JobStatus.SUCCEEDED
        assert job.approval_request_id is None
        assert job.approval_execution_fingerprint is None
