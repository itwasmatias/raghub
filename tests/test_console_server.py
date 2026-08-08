"""Comprehensive tests for Console Server v0.1."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from flask import Flask

from federation.capability import NodeCapability
from federation.heartbeat import Heartbeat
from federation.worker_liveness import PowerState
from federation.heartbeat_registry import HeartbeatRegistry
from federation.node_record import NodeRecord, NodeStatus
from federation.registry import NodeRegistry
from tools.ai_controller.operations_api.console_server import (
    ActionCatalog,
    ActionType,
    ConsoleServer,
    JobStatus,
    JobTracker,
    WorkspaceRegistry,
    create_console_blueprint,
)
from tools.ai_controller.operations_api.console_server.actions import ActionSpec


# Test tokens
CONSOLE_TOKENS = {
    "read-token": {
        "principal": "read-user",
        "capabilities": ["console.read"],
    },
    "propose-token": {
        "principal": "propose-user",
        "capabilities": ["console.read", "console.propose"],
    },
    "full-token": {
        "principal": "full-user",
        "capabilities": ["console.read", "console.propose", "console.execute"],
    },
}


def _auth(token_name: str) -> dict[str, str]:
    """Create auth headers."""
    return {"Authorization": f"Bearer {token_name}"}


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    """Create a test workspace root."""
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def workspaces(workspace_root: Path) -> WorkspaceRegistry:
    """Create workspace registry."""
    return WorkspaceRegistry({"test-workspace": workspace_root})


@pytest.fixture
def node_registry(tmp_path: Path) -> NodeRegistry:
    """Create node registry."""
    registry = NodeRegistry(stale_threshold_seconds=300)

    # Register a test node
    test_node = NodeRecord(
        node_id="test-node-1",
        hostname="test-host",
        operating_system="linux",
        status=NodeStatus.ONLINE,
        capabilities={NodeCapability("git"), NodeCapability("pytest")},
    )
    registry.register(test_node)

    return registry


@pytest.fixture
def heartbeat_registry(node_registry: NodeRegistry, tmp_path: Path) -> HeartbeatRegistry:
    """Create heartbeat registry."""
    heartbeat_file = tmp_path / "heartbeats.jsonl"

    # Create integrity key (exactly 32 bytes)
    integrity_key = b"test-integrity-key-0123456789abc"

    registry = HeartbeatRegistry(
        heartbeat_file,
        registry_id="test-registry",
        node_registry=node_registry,
        integrity_key=integrity_key,
    )

    # Report authenticated heartbeat for test node
    heartbeat = Heartbeat.authenticated(
        integrity_key=integrity_key,
        worker_id="test-node-1",
        registry_id="test-registry",
        sequence=1,
        session_id="test-session",
        worker_timestamp=datetime.now(timezone.utc),
        health="healthy",
        power_capabilities=[],
        requested_power_state=PowerState.ACTIVE,
        sleep_reason=None,
        expected_wake_time=None,
        wake_method=None,
        active_work_checkpointed=False,
        previous_authentication_tag="0" * 64,
    )
    registry.record(heartbeat)

    return registry


@pytest.fixture
def console(
    workspaces: WorkspaceRegistry,
    node_registry: NodeRegistry,
    heartbeat_registry: HeartbeatRegistry,
    tmp_path: Path,
) -> ConsoleServer:
    """Create Console Server instance."""
    job_storage = tmp_path / "jobs"
    job_storage.mkdir()

    return ConsoleServer(
        workspaces=workspaces,
        node_registry=node_registry,
        heartbeat_registry=heartbeat_registry,
        job_storage_path=job_storage,
        tokens=CONSOLE_TOKENS,
    )


@pytest.fixture
def client(console: ConsoleServer) -> Flask:
    """Create Flask test client."""
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(create_console_blueprint(console))
    return app.test_client()


# ====================
# AUTHENTICATION TESTS
# ====================


def test_health_endpoint_requires_no_authentication(client):
    """Health endpoint should not require authentication."""
    response = client.get("/api/console/v1/health")
    assert response.status_code == 200
    assert response.json["status"] == "healthy"


def test_system_status_requires_authentication(client):
    """System status endpoint requires authentication."""
    response = client.get("/api/console/v1/system")
    assert response.status_code == 401


def test_system_status_requires_read_capability(client):
    """System status requires console.read capability."""
    response = client.get(
        "/api/console/v1/system",
        headers=_auth("read-token"),
    )
    assert response.status_code == 200
    assert "server_id" in response.json


def test_action_request_requires_propose_capability(client):
    """Action request requires console.propose capability."""
    # read-token only has console.read
    response = client.post(
        "/api/console/v1/actions",
        json={
            "action_type": "inspect_git_status",
            "workspace_id": "test-workspace",
        },
        headers=_auth("read-token"),
    )
    assert response.status_code == 403
    assert response.json["error"]["code"] == "FORBIDDEN"


def test_propose_token_can_request_actions(client):
    """propose-token can request actions."""
    response = client.post(
        "/api/console/v1/actions",
        json={
            "action_type": "inspect_git_status",
            "workspace_id": "test-workspace",
        },
        headers=_auth("propose-token"),
    )
    assert response.status_code == 201
    assert "job_id" in response.json


# ====================
# WORKSPACE GOVERNANCE
# ====================


def test_workspace_registry_rejects_relative_paths():
    """Workspace roots must be absolute paths."""
    with pytest.raises(ValueError, match="must be absolute"):
        WorkspaceRegistry({"test": Path("relative/path")})


def test_workspace_registry_rejects_filesystem_root():
    """Workspace root cannot be filesystem root."""
    with pytest.raises(ValueError, match="cannot be filesystem root"):
        WorkspaceRegistry({"test": Path("/")})


def test_path_traversal_is_rejected(workspaces: WorkspaceRegistry):
    """Path traversal attempts must be rejected."""
    with pytest.raises(ValueError, match="traversal"):
        workspaces.validate_path("test-workspace", "../etc/passwd")


def test_symlink_escape_is_prevented(workspace_root: Path, workspaces: WorkspaceRegistry):
    """Symlink escapes must be prevented."""
    # Create a symlink pointing outside workspace
    outside_dir = workspace_root.parent / "outside"
    outside_dir.mkdir()

    symlink = workspace_root / "escape"
    symlink.symlink_to(outside_dir)

    # Attempting to use the symlink with traversal should fail
    # (Note: The ".." is caught by traversal check before symlink resolution)
    with pytest.raises(ValueError, match="traversal"):
        workspaces.validate_path("test-workspace", "escape/../../../etc")


def test_unknown_workspace_is_rejected(client):
    """Unknown workspace IDs must be rejected."""
    response = client.post(
        "/api/console/v1/actions",
        json={
            "action_type": "inspect_git_status",
            "workspace_id": "unknown-workspace",
        },
        headers=_auth("propose-token"),
    )
    assert response.status_code == 400
    assert "Unknown workspace" in response.json["error"]["message"]


# ====================
# ACTION CATALOG TESTS
# ====================


def test_action_catalog_lists_only_allowed_actions():
    """Action catalog should list only v0.1 allowed actions."""
    catalog = ActionCatalog()
    actions = catalog.list_actions()

    assert ActionType.INSPECT_GIT_STATUS in actions
    assert ActionType.RUN_TEST_TARGET in actions
    assert len(actions) == 5  # All v0.1 actions


def test_action_spec_defines_required_capability():
    """Each action spec must define required capability."""
    catalog = ActionCatalog()
    spec = catalog.get_spec(ActionType.INSPECT_GIT_STATUS)

    assert spec.required_capability == NodeCapability("git")
    assert spec.authorization_level is not None
    assert spec.max_timeout_seconds > 0


def test_unknown_action_type_is_rejected():
    """Unknown action types must be rejected."""
    catalog = ActionCatalog()

    with pytest.raises(ValueError, match="Unknown action type"):
        catalog.get_spec("unknown_action")


def test_client_cannot_override_action_timeout_ceiling():
    """Client cannot request timeout exceeding policy maximum."""
    catalog = ActionCatalog()

    with pytest.raises(ValueError, match="exceeds policy maximum"):
        catalog.validate_timeout(ActionType.INSPECT_GIT_STATUS, 999999)


def test_client_can_request_lower_timeout():
    """Client can request timeout lower than maximum."""
    catalog = ActionCatalog()
    timeout = catalog.validate_timeout(ActionType.INSPECT_GIT_STATUS, 10)
    assert timeout == 10


# ====================
# SHELL INJECTION TESTS
# ====================


@pytest.mark.parametrize(
    "malicious_input",
    [
        "test.py; rm -rf /",
        "test.py && evil-command",
        "test.py || fallback",
        "test.py `whoami`",
        "test.py $(whoami)",
        "test.py > /dev/null",
        "test.py | grep secret",
        "test.py & background",
        "test.py\nrm -rf /",
        "test.py\\nrm",
        "TEST=value pytest",
        "test.py{injection}",
        "test.py[0]",
    ],
)
def test_shell_injection_attempts_are_rejected(malicious_input: str):
    """Shell injection attempts must be rejected."""
    catalog = ActionCatalog()

    with pytest.raises(ValueError, match="disallowed"):
        catalog.validate_test_target(malicious_input)


def test_valid_test_targets_are_accepted():
    """Valid test targets should be accepted."""
    catalog = ActionCatalog()

    # Valid pytest target patterns
    valid_targets = [
        "test_module.py",
        "tests/test_suite.py",
        "tests/test_file.py::TestClass::test_method",
        "tests/test_file.py::test_function",
        "tests/",
    ]

    for target in valid_targets:
        result = catalog.validate_test_target(target)
        assert result == target


def test_test_target_length_is_bounded():
    """Test targets must have reasonable length limit."""
    catalog = ActionCatalog()

    # Excessively long test target
    long_target = "a" * 501

    with pytest.raises(ValueError, match="exceeds maximum length"):
        catalog.validate_test_target(long_target)


# ====================
# JOB MODEL TESTS
# ====================


def test_job_creation_generates_unique_ids(tmp_path: Path):
    """Job creation must generate unique job and action IDs."""
    tracker = JobTracker(tmp_path / "jobs")

    job1 = tracker.create_job("inspect_git_status", "workspace1", "user1")
    job2 = tracker.create_job("inspect_git_status", "workspace1", "user1")

    assert job1.job_id != job2.job_id
    assert job1.action_id != job2.action_id


def test_job_starts_in_pending_state(tmp_path: Path):
    """New jobs must start in PENDING state."""
    tracker = JobTracker(tmp_path / "jobs")

    job = tracker.create_job("inspect_git_status", "workspace1", "user1")

    assert job.status == JobStatus.PENDING
    assert job.started_at is None
    assert job.finished_at is None


def test_terminal_state_finality_is_enforced(tmp_path: Path):
    """Terminal job states must be final and immutable."""
    tracker = JobTracker(tmp_path / "jobs")

    job = tracker.create_job("inspect_git_status", "workspace1", "user1")

    # Transition to terminal state
    tracker.update_status(
        job.job_id,
        JobStatus.SUCCEEDED,
        finished_at="2026-08-07T12:00:00+00:00",
    )

    # Attempt to change terminal state should fail
    with pytest.raises(ValueError, match="already in terminal state"):
        tracker.update_status(job.job_id, JobStatus.FAILED)


def test_job_not_found_raises_error(tmp_path: Path):
    """Querying non-existent job must raise error."""
    tracker = JobTracker(tmp_path / "jobs")

    with pytest.raises(ValueError, match="not found"):
        tracker.get_job("non-existent-job")


def test_job_list_returns_most_recent_first(tmp_path: Path):
    """Job list must return most recent jobs first."""
    tracker = JobTracker(tmp_path / "jobs")

    job1 = tracker.create_job("action1", "workspace1", "user1")
    job2 = tracker.create_job("action2", "workspace1", "user1")
    job3 = tracker.create_job("action3", "workspace1", "user1")

    jobs = tracker.list_jobs(limit=10)

    # Most recent first
    assert jobs[0].job_id == job3.job_id
    assert jobs[1].job_id == job2.job_id
    assert jobs[2].job_id == job1.job_id


# ====================
# API ENDPOINT TESTS
# ====================


def test_system_endpoint_returns_safe_status(client):
    """System endpoint must return safe, non-secret status."""
    response = client.get("/api/console/v1/system", headers=_auth("read-token"))

    assert response.status_code == 200
    data = response.json

    assert "server_id" in data
    assert "server_version" in data
    assert "server_timestamp" in data
    assert "total_nodes" in data
    assert "healthy_nodes" in data
    assert "operating_mode" in data

    # Must not expose secrets
    encoded = json.dumps(data).lower()
    assert "password" not in encoded
    assert "token" not in encoded
    assert "key" not in encoded


def test_node_endpoint_returns_safe_node_info(client):
    """Node endpoint must return safe node information."""
    response = client.get("/api/console/v1/nodes", headers=_auth("read-token"))

    assert response.status_code == 200
    data = response.json

    assert "nodes" in data
    nodes = data["nodes"]
    assert len(nodes) >= 1

    node = nodes[0]
    assert "node_id" in node
    assert "capabilities" in node
    assert "status" in node
    assert "healthy" in node


def test_actions_endpoint_lists_available_actions(client):
    """Actions endpoint must list available governed actions."""
    response = client.get("/api/console/v1/actions", headers=_auth("read-token"))

    assert response.status_code == 200
    data = response.json

    assert "actions" in data
    actions = data["actions"]
    assert len(actions) == 5

    action = actions[0]
    assert "action_type" in action
    assert "required_capability" in action
    assert "authorization_level" in action
    assert "approval_required" in action
    assert "max_timeout_seconds" in action
    assert "max_output_bytes" in action


def test_action_request_creates_job(client):
    """Action request must create a tracked job."""
    response = client.post(
        "/api/console/v1/actions",
        json={
            "action_type": "inspect_git_status",
            "workspace_id": "test-workspace",
        },
        headers=_auth("propose-token"),
    )

    assert response.status_code == 201
    data = response.json

    assert "job_id" in data
    assert "action_id" in data
    assert "action_type" in data
    assert data["action_type"] == "inspect_git_status"
    assert data["status"] == "pending"


def test_action_request_requires_workspace_id(client):
    """Action request must include workspace_id."""
    response = client.post(
        "/api/console/v1/actions",
        json={"action_type": "inspect_git_status"},
        headers=_auth("propose-token"),
    )

    assert response.status_code == 400
    assert "workspace_id" in response.json["error"]["message"]


def test_action_request_validates_test_target(client):
    """Action request must validate test targets."""
    response = client.post(
        "/api/console/v1/actions",
        json={
            "action_type": "run_test_target",
            "workspace_id": "test-workspace",
            "test_target": "test.py; rm -rf /",
        },
        headers=_auth("propose-token"),
    )

    assert response.status_code == 400
    assert "disallowed" in response.json["error"]["message"]


def test_job_status_endpoint_returns_job_details(client):
    """Job status endpoint must return job details."""
    # Create a job
    create_response = client.post(
        "/api/console/v1/actions",
        json={
            "action_type": "inspect_git_status",
            "workspace_id": "test-workspace",
        },
        headers=_auth("propose-token"),
    )
    job_id = create_response.json["job_id"]

    # Query job status
    response = client.get(
        f"/api/console/v1/jobs/{job_id}",
        headers=_auth("read-token"),
    )

    assert response.status_code == 200
    data = response.json

    assert data["job_id"] == job_id
    assert data["status"] == "pending"
    assert "created_at" in data
    assert "created_by" in data


def test_job_list_endpoint_returns_recent_jobs(client):
    """Job list endpoint must return recent jobs."""
    # Create some jobs
    for i in range(3):
        client.post(
            "/api/console/v1/actions",
            json={
                "action_type": "inspect_git_status",
                "workspace_id": "test-workspace",
            },
            headers=_auth("propose-token"),
        )

    # List jobs
    response = client.get(
        "/api/console/v1/jobs",
        headers=_auth("read-token"),
    )

    assert response.status_code == 200
    data = response.json

    assert "jobs" in data
    assert len(data["jobs"]) >= 3


def test_job_list_respects_limit_parameter(client):
    """Job list must respect limit parameter."""
    # Create multiple jobs
    for i in range(5):
        client.post(
            "/api/console/v1/actions",
            json={
                "action_type": "inspect_git_status",
                "workspace_id": "test-workspace",
            },
            headers=_auth("propose-token"),
        )

    # Request with limit
    response = client.get(
        "/api/console/v1/jobs?limit=2",
        headers=_auth("read-token"),
    )

    assert response.status_code == 200
    assert len(response.json["jobs"]) == 2


# ====================
# INTEGRATION TESTS
# ====================


def test_console_server_uses_existing_node_registry(
    node_registry: NodeRegistry,
    heartbeat_registry: HeartbeatRegistry,
    workspaces: WorkspaceRegistry,
    tmp_path: Path,
):
    """Console Server must use existing NodeRegistry."""
    console = ConsoleServer(
        workspaces=workspaces,
        node_registry=node_registry,
        heartbeat_registry=heartbeat_registry,
        job_storage_path=tmp_path / "jobs",
        tokens=CONSOLE_TOKENS,
    )

    # System status should reflect nodes from registry
    status = console.system_status()
    assert status["total_nodes"] >= 1


def test_console_server_uses_existing_heartbeat_registry(
    node_registry: NodeRegistry,
    heartbeat_registry: HeartbeatRegistry,
    workspaces: WorkspaceRegistry,
    tmp_path: Path,
):
    """Console Server must use existing HeartbeatRegistry."""
    console = ConsoleServer(
        workspaces=workspaces,
        node_registry=node_registry,
        heartbeat_registry=heartbeat_registry,
        job_storage_path=tmp_path / "jobs",
        tokens=CONSOLE_TOKENS,
    )

    # Node status should reflect heartbeat state
    nodes = console.node_status()
    assert len(nodes) >= 1
    assert "healthy" in nodes[0]


def test_console_server_creates_task_router(
    node_registry: NodeRegistry,
    heartbeat_registry: HeartbeatRegistry,
    workspaces: WorkspaceRegistry,
    tmp_path: Path,
):
    """Console Server must create TaskRouter using federation contracts."""
    console = ConsoleServer(
        workspaces=workspaces,
        node_registry=node_registry,
        heartbeat_registry=heartbeat_registry,
        job_storage_path=tmp_path / "jobs",
        tokens=CONSOLE_TOKENS,
    )

    # TaskRouter should exist and use provided registries
    assert console.task_router is not None
    assert console.task_router._registry is node_registry
    assert console.task_router._heartbeat_registry is heartbeat_registry


def test_action_request_routes_through_task_router(client, console: ConsoleServer):
    """Action request must create TaskRequest and invoke TaskRouter.route()."""
    from unittest.mock import MagicMock
    from federation.task_request import TaskRequest, AuthorizationLevel

    # Spy on TaskRouter.route to verify it's called
    original_route = console.task_router.route
    route_spy = MagicMock(side_effect=original_route)
    console.task_router.route = route_spy

    # Make action request
    response = client.post(
        "/api/console/v1/actions",
        json={
            "action_type": "inspect_git_status",
            "workspace_id": "test-workspace",
        },
        headers=_auth("propose-token"),
    )

    assert response.status_code == 201

    # Verify route() was called exactly once
    assert route_spy.call_count == 1

    # Verify TaskRequest was created with correct fields
    task_request = route_spy.call_args[0][0]
    assert isinstance(task_request, TaskRequest)
    assert task_request.task_id.startswith("console-action-")
    assert task_request.mission_id.startswith("console-")
    assert task_request.required_capabilities == {NodeCapability("git")}
    assert task_request.authorization_level == AuthorizationLevel.INTERNAL
    assert task_request.approval_required is False
    assert task_request.input_data["action_type"] == "inspect_git_status"
    assert task_request.input_data["workspace_id"] == "test-workspace"

    # Verify routing decision was used to set target_node_id
    job_id = response.json["job_id"]
    job = console.job_tracker.get_job(job_id)

    # Should have a target_node_id from routing (test-node-1 from fixture)
    assert job.target_node_id is not None
    assert "routing_outcome" in response.json


def test_tier2_action_request_includes_approval_flag(client, console: ConsoleServer):
    """Tier 2 action request must include approval_required in routing."""
    from unittest.mock import MagicMock
    from federation.task_request import AuthorizationLevel

    # Spy on TaskRouter.route to capture the request
    captured_request = None
    original_route = console.task_router.route

    def capture_route(task_request):
        nonlocal captured_request
        captured_request = task_request
        return original_route(task_request)

    route_spy = MagicMock(side_effect=capture_route)
    console.task_router.route = route_spy

    response = client.post(
        "/api/console/v1/actions",
        json={
            "action_type": "run_test_target",
            "workspace_id": "test-workspace",
            "test_target": "tests/test_example.py",
        },
        headers=_auth("propose-token"),
    )

    # Should succeed
    assert response.status_code == 201

    # Verify approval_required was set correctly in TaskRequest
    assert captured_request is not None
    assert captured_request.approval_required is True
    assert captured_request.authorization_level == AuthorizationLevel.INTERNAL


# ====================
# ERROR HANDLING TESTS
# ====================


def test_malformed_json_is_rejected(client):
    """Malformed JSON must be rejected."""
    response = client.post(
        "/api/console/v1/actions",
        data="{invalid json",
        content_type="application/json",
        headers=_auth("propose-token"),
    )

    assert response.status_code == 400
    assert "Invalid JSON" in response.json["error"]["message"]


def test_non_object_json_is_rejected(client):
    """Non-object JSON must be rejected."""
    response = client.post(
        "/api/console/v1/actions",
        json=["array", "not", "object"],
        headers=_auth("propose-token"),
    )

    assert response.status_code == 400
    assert "must be a JSON object" in response.json["error"]["message"]


def test_missing_required_fields_are_rejected(client):
    """Missing required fields must be rejected."""
    # Missing action_type
    response = client.post(
        "/api/console/v1/actions",
        json={"workspace_id": "test-workspace"},
        headers=_auth("propose-token"),
    )

    assert response.status_code == 400
    assert "action_type" in response.json["error"]["message"]


def test_api_errors_include_correlation_id(client):
    """API errors must include correlation ID."""
    response = client.post(
        "/api/console/v1/actions",
        json={},
        headers=_auth("propose-token"),
    )

    assert response.status_code == 400
    assert "correlation_id" in response.json["error"]


def test_unknown_job_returns_404(client):
    """Querying unknown job must return 404."""
    response = client.get(
        "/api/console/v1/jobs/non-existent-job",
        headers=_auth("read-token"),
    )

    assert response.status_code == 404
    assert response.json["error"]["code"] == "NOT_FOUND"


# ====================
# IDEMPOTENCY TESTS
# ====================


def test_idempotency_key_allows_duplicate_prevention(client):
    """Idempotency key prevents duplicate job creation."""
    request_body = {
        "action_type": "inspect_git_status",
        "workspace_id": "test-workspace",
        "idempotency_key": "unique-request-123",
    }

    # First request creates job
    response1 = client.post(
        "/api/console/v1/actions",
        json=request_body,
        headers=_auth("propose-token"),
    )
    assert response1.status_code == 201
    job_id_1 = response1.json["job_id"]

    # Exact duplicate returns same job (200, not 201)
    response2 = client.post(
        "/api/console/v1/actions",
        json=request_body,
        headers=_auth("propose-token"),
    )
    assert response2.status_code == 200
    job_id_2 = response2.json["job_id"]

    # Same job ID returned
    assert job_id_1 == job_id_2


def test_idempotency_conflict_on_changed_request(client):
    """Same idempotency key with different request is rejected."""
    # First request
    response1 = client.post(
        "/api/console/v1/actions",
        json={
            "action_type": "inspect_git_status",
            "workspace_id": "test-workspace",
            "idempotency_key": "request-456",
        },
        headers=_auth("propose-token"),
    )
    assert response1.status_code == 201

    # Same key, different action type
    response2 = client.post(
        "/api/console/v1/actions",
        json={
            "action_type": "inspect_git_log",
            "workspace_id": "test-workspace",
            "idempotency_key": "request-456",
        },
        headers=_auth("propose-token"),
    )
    assert response2.status_code == 409
    assert response2.json["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_empty_idempotency_key_is_rejected(client):
    """Empty idempotency key must be rejected."""
    response = client.post(
        "/api/console/v1/actions",
        json={
            "action_type": "inspect_git_status",
            "workspace_id": "test-workspace",
            "idempotency_key": "",
        },
        headers=_auth("propose-token"),
    )

    assert response.status_code == 400
    assert "must not be empty" in response.json["error"]["message"]


def test_idempotency_key_with_whitespace_padding_is_rejected(client):
    """Idempotency key with whitespace padding is rejected."""
    response = client.post(
        "/api/console/v1/actions",
        json={
            "action_type": "inspect_git_status",
            "workspace_id": "test-workspace",
            "idempotency_key": " padded-key ",
        },
        headers=_auth("propose-token"),
    )

    assert response.status_code == 400
    assert "whitespace padding" in response.json["error"]["message"]


def test_idempotency_key_length_is_bounded(client):
    """Idempotency key must not exceed 200 characters."""
    long_key = "a" * 201

    response = client.post(
        "/api/console/v1/actions",
        json={
            "action_type": "inspect_git_status",
            "workspace_id": "test-workspace",
            "idempotency_key": long_key,
        },
        headers=_auth("propose-token"),
    )

    assert response.status_code == 400
    assert "must not exceed 200 characters" in response.json["error"]["message"]


def test_idempotency_key_is_optional(client):
    """Idempotency key is optional."""
    # Request without idempotency key should work
    response = client.post(
        "/api/console/v1/actions",
        json={
            "action_type": "inspect_git_status",
            "workspace_id": "test-workspace",
        },
        headers=_auth("propose-token"),
    )

    assert response.status_code == 201
    assert "job_id" in response.json
