from __future__ import annotations

import os
import re
from dataclasses import asdict, is_dataclass
from typing import Any
from urllib.parse import urlsplit

from tools.ai_controller.mission.models import MissionDefinition, MissionState


_PRIVATE_KEYS = {
    "access_key",
    "api_key",
    "authorization",
    "bearer",
    "client_secret",
    "credential",
    "credentials",
    "env",
    "environment",
    "events_path",
    "passwd",
    "password",
    "private_key",
    "prompt",
    "provider_prompt",
    "report_contents",
    "report_path",
    "repository_path",
    "secret_key",
    "system_prompt",
    "refresh_token",
    "token",
    "worktree_path",
}

_PUBLIC_KEYS = {
    "accepted_queue_identities",
    "actions",
    "active_tasks",
    "action_id",
    "action_type",
    "application",
    "applied_at",
    "applied_by",
    "approval_required",
    "attempt_count",
    "authority",
    "authorization_level",
    "base_ref",
    "blockers",
    "budget_usage",
    "budgets",
    "capabilities",
    "classification",
    "created_at",
    "created_by",
    "decision",
    "decision_time",
    "definition",
    "dependency_graph",
    "depends_on",
    "description",
    "duration_ms",
    "effect_fingerprint",
    "event_revision",
    "event_type",
    "evidence",
    "evidence_fingerprint",
    "exit_code",
    "expected_event_revision",
    "expected_budget_fingerprint",
    "expected_budget_usage",
    "expected_mission_revision",
    "expected_queue_effects",
    "expected_queue_identities",
    "expected_queue_records",
    "expected_queue_revision",
    "expected_report_revision",
    "expected_task_state_fingerprints",
    "expires_at",
    "failure_policy",
    "failure_reason",
    "files_changed",
    "finding",
    "finished_at",
    "healthy",
    "healthy_nodes",
    "id",
    "idempotency_key",
    "job_id",
    "jobs",
    "last_heartbeat",
    "max_attempts",
    "max_active_tasks",
    "max_failed_tasks",
    "max_output_bytes",
    "max_queued_tasks",
    "max_runtime_seconds",
    "max_timeout_seconds",
    "max_total_attempts",
    "max_worktrees",
    "metadata",
    "milestone_id",
    "milestones",
    "mission_id",
    "mission_revision",
    "mission_task_id",
    "node_id",
    "nodes",
    "note",
    "operating_mode",
    "principal",
    "proposal_revision",
    "proposed_effect_fingerprint",
    "provider",
    "provider_fallback",
    "provider_policy",
    "provider_preference",
    "queue_id",
    "queue_identities",
    "required_capability",
    "sha256",
    "queue_task_id",
    "reason",
    "record",
    "request_fingerprint",
    "revision",
    "root_cause_task_ids",
    "routing_outcome",
    "schema_version",
    "server_id",
    "server_timestamp",
    "server_version",
    "started_at",
    "state",
    "status",
    "stderr",
    "stdout",
    "supersedes_action_ids",
    "target_node_id",
    "task_id",
    "tasks",
    "task_states",
    "test_results",
    "tests",
    "timeout_seconds",
    "topological_order",
    "timestamp",
    "title",
    "total_nodes",
    "truncated",
    "unhealthy_nodes",
    "updated_at",
    "version",
    "workspace_id",
}

_PRIVATE_TEXT = (
    "-----begin openssh private key-----",
    "-----begin private key-----",
    "ssh-rsa ",
    "ssh-ed25519 ",
)
_SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(
        r"(?i)\b(?:api|access|private|secret)[_-]?key\s*[:=]\s*\S+"
    ),
    re.compile(r"(?i)\b(?:password|passwd|credential)s?\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(?:provider|system|developer|user)\s+prompt\s*[:=]"),
    re.compile(r"(?i)\breport\s+contents?\s*[:=]"),
    re.compile(r"(?m)^[A-Za-z_][A-Za-z0-9_]*=\S+"),
    re.compile(r"(?i)(?:^|[\s\"'])/(?:home|root|Users|private|srv|opt)/\S*"),
    re.compile(r"(?i)(?:^|[\s\"'])[A-Za-z]:\\(?:Users|Documents|repos?)\\"),
)
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PUBLIC_TASK_STATE_KEYS = {
    "task_id",
    "status",
    "queue_task_id",
    "attempt_count",
    "failure_reason",
    "depends_on",
    "dependencies",
    "blockers",
    "revision",
    "mission_revision",
    "max_attempts",
    "budgets",
    "budget_usage",
    "queued_at",
    "started_at",
    "finished_at",
    "files_changed",
    "test_results",
}


def _private_key(key: str) -> bool:
    lowered = key.lower()
    compact = lowered.replace("_", "").replace("-", "")
    return (
        lowered in _PRIVATE_KEYS
        or "token" in lowered
        or "secret" in lowered
        or lowered.endswith("_path")
        or compact
        in {
            "apikey",
            "accesskey",
            "clientsecret",
            "privatekey",
            "providerprompt",
            "systemprompt",
            "refreshtoken",
            "worktreepath",
            "repositorypath",
            "reportpath",
        }
    )


def _private_path(value: str) -> bool:
    return (
        os.path.isabs(value)
        or value.startswith("~")
        or value.startswith("\\\\")
        or bool(re.match(r"^[A-Za-z]:[\\\\/]", value))
    )


def _private_text(value: str) -> bool:
    if _private_path(value):
        return True
    lowered = value.lower()
    if any(marker in lowered for marker in _PRIVATE_TEXT):
        return True
    if any(pattern.search(value) for pattern in _SENSITIVE_VALUE_PATTERNS):
        return True
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    return bool(
        parsed.scheme
        and parsed.netloc
        and (parsed.username is not None or parsed.password is not None)
    )


def _public_mapping(value: dict[Any, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item_key, item_value in value.items():
        key = str(item_key)
        if key.lower() not in _PUBLIC_KEYS:
            continue
        serialized = public_value(item_value, key=key)
        if serialized is not None:
            result[key] = serialized
    return result


def _public_metadata(value: Any) -> Any:
    """Preserve operator-useful metadata while recursively removing secrets."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for item_key, item_value in value.items():
            key = str(item_key)
            if _private_key(key):
                continue
            serialized = _public_metadata(item_value)
            if serialized is not None:
                result[key] = serialized
        return result
    if isinstance(value, (list, tuple)):
        return [
            serialized
            for item in value
            if (serialized := _public_metadata(item)) is not None
        ]
    if isinstance(value, str):
        if _private_text(value):
            return None
        return value[:2000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return None


def _public_task_states(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for raw_task_id, raw_state in value.items():
        if (
            not isinstance(raw_task_id, str)
            or not _TASK_ID.fullmatch(raw_task_id)
            or not isinstance(raw_state, dict)
        ):
            continue
        state: dict[str, Any] = {}
        for field in _PUBLIC_TASK_STATE_KEYS:
            if field not in raw_state:
                continue
            serialized = public_value(raw_state[field], key=field)
            if serialized is not None:
                state[field] = serialized
        if state.get("task_id") == raw_task_id:
            result[raw_task_id] = state
    return result


def public_value(value: Any, *, key: str = "") -> Any:
    lowered = key.lower()
    if _private_key(key):
        return None
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    elif not isinstance(value, (dict, list, tuple, str, int, float, bool, type(None))):
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            value = to_dict()
        else:
            return None
    if lowered == "metadata":
        return _public_metadata(value)
    if lowered == "task_states":
        return _public_task_states(value)
    if isinstance(value, dict):
        return _public_mapping(value)
    if isinstance(value, (list, tuple)):
        return [
            serialized
            for item in value
            if (serialized := public_value(item)) is not None
        ]
    if isinstance(value, str):
        if _private_text(value):
            return None
        return value[:2000]
    return value

def public_definition(definition: MissionDefinition) -> dict[str, Any]:
    payload = definition.to_dict()
    payload.pop("repository_path", None)
    for task in payload.get("tasks", []):
        task.pop("prompt", None)
    return public_value(payload)


def public_state(state: MissionState) -> dict[str, Any]:
    return public_value(state.to_dict())
