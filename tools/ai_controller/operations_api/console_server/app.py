"""Console Server main application and Flask blueprint."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from flask import Blueprint, g, request

from federation.heartbeat_registry import HeartbeatRegistry
from federation.registry import NodeRegistry
from federation.task_request import TaskRequest
from federation.task_router import TaskRouter
from tools.ai_controller.operations_api.auth import Principal, authenticate
from tools.ai_controller.operations_api.errors import APIError, invalid
from tools.ai_controller.operations_api.serialization import public_value

from .actions import ActionCatalog, ActionType
from .jobs import JobStatus, JobTracker
from .workspaces import WorkspaceRegistry


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class ConsoleServer:
    """
    RAGHub Console Server for mobile operations.

    Provides governed remote actions through existing federation contracts.
    """

    def __init__(
        self,
        *,
        workspaces: WorkspaceRegistry,
        node_registry: NodeRegistry,
        heartbeat_registry: HeartbeatRegistry,
        job_storage_path: Path,
        tokens: dict[str, dict[str, Any]],
        server_id: str = "console-server-v0.1",
        now: Callable[[], datetime] = _utc_now,
    ):
        """
        Initialize Console Server.

        Args:
            workspaces: Workspace registry
            node_registry: Federation node registry
            heartbeat_registry: Heartbeat registry for liveness
            job_storage_path: Path for job storage
            tokens: Authentication tokens
            server_id: Server identifier
            now: Clock function for testing
        """
        if not isinstance(workspaces, WorkspaceRegistry):
            raise TypeError("workspaces must be a WorkspaceRegistry")
        if not isinstance(node_registry, NodeRegistry):
            raise TypeError("node_registry must be a NodeRegistry")
        if not isinstance(heartbeat_registry, HeartbeatRegistry):
            raise TypeError("heartbeat_registry must be a HeartbeatRegistry")
        if not isinstance(job_storage_path, Path):
            raise TypeError("job_storage_path must be a Path")
        if not isinstance(tokens, dict):
            raise TypeError("tokens must be a dict")
        if not callable(now):
            raise TypeError("now must be callable")

        self.workspaces = workspaces
        self.node_registry = node_registry
        self.heartbeat_registry = heartbeat_registry
        self.job_tracker = JobTracker(job_storage_path)
        self.tokens = tokens
        self.server_id = server_id
        self.now = now
        self.action_catalog = ActionCatalog()

        # Create task router using existing federation contracts
        self.task_router = TaskRouter(
            node_registry,
            heartbeat_registry=heartbeat_registry,
        )

    def system_status(self) -> dict[str, Any]:
        """
        Get safe system status.

        Returns:
            System status dictionary
        """
        now = self.now()
        nodes = self.node_registry.list_nodes()
        healthy_count = sum(
            1
            for node in nodes
            if self.heartbeat_registry.is_routing_eligible(node.node_id)
        )

        return {
            "server_id": self.server_id,
            "server_version": "0.1.0",
            "server_timestamp": _iso(now),
            "total_nodes": len(nodes),
            "healthy_nodes": healthy_count,
            "unhealthy_nodes": len(nodes) - healthy_count,
            "operating_mode": "production",
        }

    def node_status(self) -> list[dict[str, Any]]:
        """
        Get safe node status.

        Returns:
            List of node status dictionaries
        """
        nodes = self.node_registry.list_nodes()
        result = []

        for node in nodes:
            lease = self.heartbeat_registry.inspect(node.node_id)
            is_healthy = lease is not None and lease.routing_eligible

            node_info = {
                "node_id": node.node_id,
                "capabilities": [str(cap) for cap in node.capabilities],
                "status": node.status.value,
                "healthy": is_healthy,
            }

            if lease is not None:
                node_info["last_heartbeat"] = _iso(lease.worker_timestamp)
                node_info["state"] = lease.state.value

            result.append(node_info)

        return sorted(result, key=lambda x: x["node_id"])


def create_console_blueprint(console: ConsoleServer) -> Blueprint:
    """
    Create Flask blueprint for Console Server API.

    Args:
        console: ConsoleServer instance

    Returns:
        Flask Blueprint
    """
    bp = Blueprint("console", __name__, url_prefix="/api/console/v1")

    @bp.before_request
    def add_request_id():
        g.console_request_id = str(uuid.uuid4())

    @bp.errorhandler(APIError)
    def handle_api_error(error: APIError):
        return error.response()

    @bp.errorhandler(Exception)
    def handle_unexpected_error(error: Exception):
        return APIError(
            "INTERNAL_ERROR",
            "An unexpected error occurred",
            500,
        ).response()

    @bp.route("/health", methods=["GET"])
    def health():
        """Health check endpoint (no auth required)."""
        return {"status": "healthy", "timestamp": _iso(console.now())}

    @bp.route("/system", methods=["GET"])
    def system():
        """Get system status."""
        principal = authenticate(console.tokens, "console.read")
        g.console_principal = principal.identity

        return public_value(console.system_status())

    @bp.route("/nodes", methods=["GET"])
    def nodes():
        """Get node status."""
        principal = authenticate(console.tokens, "console.read")
        g.console_principal = principal.identity

        return {"nodes": public_value(console.node_status())}

    @bp.route("/actions", methods=["GET"])
    def list_actions():
        """List available actions."""
        principal = authenticate(console.tokens, "console.read")
        g.console_principal = principal.identity

        actions = []
        for action_type in console.action_catalog.list_actions():
            spec = console.action_catalog.get_spec(action_type)
            actions.append({
                "action_type": action_type.value,
                "required_capability": str(spec.required_capability),
                "authorization_level": spec.authorization_level.value,
                "approval_required": spec.approval_required,
                "max_timeout_seconds": spec.max_timeout_seconds,
                "max_output_bytes": spec.max_output_bytes,
            })

        return {"actions": actions}

    @bp.route("/actions", methods=["POST"])
    def request_action():
        """Request an action."""
        principal = authenticate(console.tokens, "console.propose")
        g.console_principal = principal.identity

        # Parse request body
        try:
            body = request.get_json(force=True)
        except Exception as exc:
            raise invalid(f"Invalid JSON: {exc}")

        if not isinstance(body, dict):
            raise invalid("Request body must be a JSON object")

        # Extract required fields
        action_type_str = body.get("action_type")
        workspace_id = body.get("workspace_id")

        if not action_type_str or not isinstance(action_type_str, str):
            raise invalid("action_type is required and must be a string")
        if not workspace_id or not isinstance(workspace_id, str):
            raise invalid("workspace_id is required and must be a string")

        # Validate action type
        try:
            action_type = ActionType(action_type_str)
        except ValueError:
            raise invalid(f"Unknown action_type: {action_type_str}")

        # Validate workspace
        try:
            workspace_path = console.workspaces.validate_workspace(workspace_id)
        except ValueError as exc:
            raise invalid(str(exc))

        # Get action spec
        spec = console.action_catalog.get_spec(action_type)

        # Validate timeout if provided
        requested_timeout = body.get("timeout_seconds")
        if requested_timeout is not None:
            try:
                timeout = console.action_catalog.validate_timeout(
                    action_type, requested_timeout
                )
            except (TypeError, ValueError) as exc:
                raise invalid(str(exc))
        else:
            timeout = spec.max_timeout_seconds

        # Validate test target for test actions
        test_target = None
        if action_type == ActionType.RUN_TEST_TARGET:
            test_target = body.get("test_target")
            if not test_target:
                raise invalid("test_target is required for run_test_target")
            try:
                test_target = console.action_catalog.validate_test_target(test_target)
            except (TypeError, ValueError) as exc:
                raise invalid(str(exc))

        # Validate and process idempotency key
        idempotency_key = body.get("idempotency_key")
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str):
                raise invalid("idempotency_key must be a string")
            if not idempotency_key or idempotency_key != idempotency_key.strip():
                raise invalid("idempotency_key must not be empty or have whitespace padding")
            if len(idempotency_key) > 200:
                raise invalid("idempotency_key must not exceed 200 characters")

            # Compute deterministic fingerprint of normalized request
            normalized_request = {
                "action_type": action_type.value,
                "workspace_id": workspace_id,
                "timeout_seconds": timeout,
                "test_target": test_target,
                "mission_id": body.get("mission_id"),
            }
            request_fingerprint = hashlib.sha256(
                json.dumps(normalized_request, sort_keys=True).encode("utf-8")
            ).hexdigest()

            # Check for existing job with same idempotency key
            existing_job = console.job_tracker.find_by_idempotency_key(idempotency_key)
            if existing_job:
                if existing_job.request_fingerprint == request_fingerprint:
                    # Exact duplicate - return existing job
                    return {
                        "job_id": existing_job.job_id,
                        "action_id": existing_job.action_id,
                        "action_type": existing_job.action_type,
                        "workspace_id": existing_job.workspace_id,
                        "created_at": existing_job.created_at,
                        "created_by": existing_job.created_by,
                        "status": existing_job.status.value,
                        "approval_required": spec.approval_required,
                        "authorization_level": spec.authorization_level.value,
                        "target_node_id": existing_job.target_node_id,
                    }, 200
                else:
                    # Same key, different request - reject
                    raise APIError(
                        "IDEMPOTENCY_CONFLICT",
                        f"Idempotency key '{idempotency_key}' already used with different request parameters",
                        409,
                    )
        else:
            request_fingerprint = None

        # Create TaskRequest for routing through existing federation contracts
        task_request = TaskRequest(
            task_id=f"console-action-{uuid.uuid4().hex}",
            mission_id=body.get("mission_id") or f"console-{uuid.uuid4().hex}",
            required_capabilities={spec.required_capability},
            authorization_level=spec.authorization_level,
            approval_required=spec.approval_required,
            input_data={
                "action_type": action_type.value,
                "workspace_id": workspace_id,
                "timeout_seconds": timeout,
                "test_target": test_target if test_target else None,
            },
            expected_result=f"Execute {action_type.value} in workspace {workspace_id}",
        )

        # Route through existing TaskRouter
        routing_decision = console.task_router.route(task_request)

        # Create job with routing information
        job = console.job_tracker.create_job(
            action_type=action_type.value,
            workspace_id=workspace_id,
            created_by=principal.identity,
            mission_id=task_request.mission_id,
            target_node_id=(
                routing_decision.assignment.assigned_node.node_id
                if routing_decision.assignment
                else None
            ),
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )

        return {
            "job_id": job.job_id,
            "action_id": job.action_id,
            "action_type": job.action_type,
            "workspace_id": job.workspace_id,
            "created_at": job.created_at,
            "created_by": job.created_by,
            "status": job.status.value,
            "approval_required": spec.approval_required,
            "authorization_level": spec.authorization_level.value,
            "target_node_id": job.target_node_id,
            "routing_outcome": routing_decision.outcome.value,
        }, 201

    @bp.route("/jobs/<job_id>", methods=["GET"])
    def get_job(job_id: str):
        """Get job status."""
        principal = authenticate(console.tokens, "console.read")
        g.console_principal = principal.identity

        try:
            job = console.job_tracker.get_job(job_id)
        except ValueError:
            raise APIError("NOT_FOUND", f"Job not found: {job_id}", 404)

        return public_value(job.to_dict())

    @bp.route("/jobs", methods=["GET"])
    def list_jobs():
        """List recent jobs."""
        principal = authenticate(console.tokens, "console.read")
        g.console_principal = principal.identity

        limit = request.args.get("limit", type=int, default=20)
        if limit < 1 or limit > 100:
            raise invalid("limit must be between 1 and 100")

        jobs = console.job_tracker.list_jobs(limit=limit)
        return {
            "jobs": public_value([job.to_dict() for job in jobs])
        }

    return bp
