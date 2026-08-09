"""Action catalog and governance for Console Server."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum

from federation.capability import NodeCapability
from federation.task_request import AuthorizationLevel


class ActionType(str, Enum):
    """Governed action types for Console Server."""

    INSPECT_GIT_STATUS = "inspect_git_status"
    INSPECT_GIT_LOG = "inspect_git_log"
    RUN_TEST_TARGET = "run_test_target"
    RUN_REPOSITORY_TESTS = "run_repository_tests"
    INSPECT_RUNTIME_STATUS = "inspect_runtime_status"


# Shell injection patterns to reject
_SHELL_INJECTION_PATTERNS = [
    re.compile(r"[;&|`$(){}[\]<>]"),  # Shell metacharacters
    re.compile(r"\n"),  # Newlines
    re.compile(r"\\"),  # Backslashes (for escaping)
    re.compile(r"^[A-Z_][A-Z0-9_]*="),  # Environment variable assignment
    re.compile(r"\s+[A-Z_][A-Z0-9_]*="),  # Env var anywhere in string
]


@dataclass(frozen=True)
class ActionSpec:
    """
    Specification for a governed action.

    Runtime boundary:
        Console Action Execution Runtime v0.1 executes only the Tier-1
        ``inspect_git_status`` action. Approval-required actions remain PENDING;
        this runtime does not invent or advance approval evidence.
    """

    action_type: ActionType
    required_capability: NodeCapability
    authorization_level: AuthorizationLevel
    approval_required: bool  # Tier 2 classification; enforcement out of scope for v0.1
    max_timeout_seconds: int
    max_output_bytes: int
    allowed_in_v0_1: bool  # Whether action is implemented in this milestone


class ActionCatalog:
    """
    Catalog of governed actions with security policies.

    Each action has fixed governance rules that cannot be overridden by clients.
    """

    # Action specifications
    _SPECS: dict[ActionType, ActionSpec] = {
        ActionType.INSPECT_GIT_STATUS: ActionSpec(
            action_type=ActionType.INSPECT_GIT_STATUS,
            required_capability=NodeCapability("git"),
            authorization_level=AuthorizationLevel.INTERNAL,
            approval_required=False,
            max_timeout_seconds=30,
            max_output_bytes=1024 * 100,  # 100KB
            allowed_in_v0_1=True,
        ),
        ActionType.INSPECT_GIT_LOG: ActionSpec(
            action_type=ActionType.INSPECT_GIT_LOG,
            required_capability=NodeCapability("git"),
            authorization_level=AuthorizationLevel.INTERNAL,
            approval_required=False,
            max_timeout_seconds=30,
            max_output_bytes=1024 * 100,  # 100KB
            allowed_in_v0_1=True,
        ),
        ActionType.RUN_TEST_TARGET: ActionSpec(
            action_type=ActionType.RUN_TEST_TARGET,
            required_capability=NodeCapability("pytest"),
            authorization_level=AuthorizationLevel.INTERNAL,
            approval_required=True,  # Tier 2: requires approval
            max_timeout_seconds=300,  # 5 minutes
            max_output_bytes=1024 * 512,  # 512KB
            allowed_in_v0_1=True,
        ),
        ActionType.RUN_REPOSITORY_TESTS: ActionSpec(
            action_type=ActionType.RUN_REPOSITORY_TESTS,
            required_capability=NodeCapability("pytest"),
            authorization_level=AuthorizationLevel.INTERNAL,
            approval_required=True,  # Tier 2: requires approval
            max_timeout_seconds=600,  # 10 minutes
            max_output_bytes=1024 * 1024,  # 1MB
            allowed_in_v0_1=True,
        ),
        ActionType.INSPECT_RUNTIME_STATUS: ActionSpec(
            action_type=ActionType.INSPECT_RUNTIME_STATUS,
            required_capability=NodeCapability("status"),
            authorization_level=AuthorizationLevel.INTERNAL,
            approval_required=False,
            max_timeout_seconds=30,
            max_output_bytes=1024 * 50,  # 50KB
            allowed_in_v0_1=True,
        ),
    }

    def __init__(self):
        """Initialize action catalog."""
        pass

    def list_actions(self) -> list[ActionType]:
        """List all available action types."""
        return [
            action_type
            for action_type, spec in self._SPECS.items()
            if spec.allowed_in_v0_1
        ]

    def get_spec(self, action_type: ActionType | str) -> ActionSpec:
        """
        Get action specification.

        Args:
            action_type: Action type to look up

        Returns:
            ActionSpec for the action

        Raises:
            ValueError: If action type is unknown or not allowed
        """
        if isinstance(action_type, str):
            try:
                action_type = ActionType(action_type)
            except ValueError as exc:
                raise ValueError(f"Unknown action type: {action_type!r}") from exc

        if action_type not in self._SPECS:
            raise ValueError(f"Unknown action type: {action_type!r}")

        spec = self._SPECS[action_type]
        if not spec.allowed_in_v0_1:
            raise ValueError(f"Action not implemented: {action_type.value}")

        return spec

    def validate_test_target(self, test_target: str) -> str:
        """
        Validate a pytest test target for shell injection.

        Args:
            test_target: Test target string

        Returns:
            Validated test target

        Raises:
            ValueError: If test target contains shell injection attempts
        """
        if not isinstance(test_target, str):
            raise TypeError("test_target must be a string")

        test_target = test_target.strip()
        if not test_target:
            raise ValueError("test_target cannot be empty")

        # Check for shell injection patterns
        for pattern in _SHELL_INJECTION_PATTERNS:
            if pattern.search(test_target):
                raise ValueError(
                    f"test_target contains disallowed characters: {test_target!r}"
                )

        # Additional checks for common injection attempts
        dangerous_tokens = ["&&", "||", "$(", "`", ">>", "<<", "2>", ">&"]
        for token in dangerous_tokens:
            if token in test_target:
                raise ValueError(
                    f"test_target contains disallowed sequence: {token!r}"
                )

        # Ensure reasonable length
        if len(test_target) > 500:
            raise ValueError("test_target exceeds maximum length of 500 characters")

        return test_target

    def validate_timeout(self, action_type: ActionType, requested_timeout: int | None) -> int:
        """
        Validate and bound timeout for an action.

        Args:
            action_type: Action type
            requested_timeout: Requested timeout in seconds (None = use default)

        Returns:
            Validated timeout in seconds

        Raises:
            ValueError: If timeout is invalid or exceeds policy
        """
        spec = self.get_spec(action_type)

        if requested_timeout is None:
            return spec.max_timeout_seconds

        if not isinstance(requested_timeout, int):
            raise TypeError("timeout must be an integer")

        if requested_timeout <= 0:
            raise ValueError("timeout must be positive")

        if requested_timeout > spec.max_timeout_seconds:
            raise ValueError(
                f"timeout {requested_timeout}s exceeds policy maximum "
                f"{spec.max_timeout_seconds}s for {action_type.value}"
            )

        return requested_timeout


def execution_fingerprint(
    *,
    action_type: ActionType | str,
    workspace_id: str,
    timeout_seconds: int,
    immutable_parameters: dict | None = None,
) -> str:
    """Return the canonical identity of one governed execution request."""
    action = ActionType(action_type).value
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise ValueError("workspace_id must be a non-empty string")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
        raise TypeError("timeout_seconds must be an integer")
    parameters = immutable_parameters or {}
    if not isinstance(parameters, dict):
        raise TypeError("immutable_parameters must be a dictionary")
    payload = {
        "schema_version": 1,
        "action_type": action,
        "workspace_id": workspace_id,
        "timeout_seconds": timeout_seconds,
        "immutable_parameters": parameters,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
