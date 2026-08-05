"""
Task request model for task routing in RAGHub federation.

A TaskRequest represents a request to execute a task on a federated node.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from federation.capability import NodeCapability


class AuthorizationLevel(str, Enum):
    """Authorization level for task execution."""

    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"
    CONFIDENTIAL = "confidential"


@dataclass(slots=True)
class TaskRequest:
    """
    Represents a request to execute a task on a federated node.

    This is a dry-run planning structure that describes task requirements
    without executing the task or having any consequential machine actions.
    """

    task_id: str
    mission_id: str
    required_capabilities: set[NodeCapability] = field(default_factory=set)
    preferred_capabilities: set[NodeCapability] = field(default_factory=set)
    authorization_level: AuthorizationLevel = AuthorizationLevel.INTERNAL
    approval_required: bool = False
    input_data: dict[str, Any] = field(default_factory=dict)
    expected_result: str = ""

    def __post_init__(self) -> None:
        """Validate task request fields."""
        if not isinstance(self.task_id, str):
            raise TypeError("task_id must be a string")
        if not self.task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        if not isinstance(self.mission_id, str):
            raise TypeError("mission_id must be a string")
        if not self.mission_id.strip():
            raise ValueError("mission_id must be a non-empty string")

        # Convert required_capabilities to set
        try:
            self.required_capabilities = set(self.required_capabilities)
        except TypeError as exc:
            raise TypeError(
                "required_capabilities must be an iterable of NodeCapability values",
            ) from exc
        if not all(
            isinstance(cap, NodeCapability) for cap in self.required_capabilities
        ):
            raise TypeError(
                "required_capabilities must contain only NodeCapability values",
            )

        # Convert preferred_capabilities to set
        try:
            self.preferred_capabilities = set(self.preferred_capabilities)
        except TypeError as exc:
            raise TypeError(
                "preferred_capabilities must be an iterable of NodeCapability values",
            ) from exc
        if not all(
            isinstance(cap, NodeCapability) for cap in self.preferred_capabilities
        ):
            raise TypeError(
                "preferred_capabilities must contain only NodeCapability values",
            )

        # Validate no overlap between required and preferred
        overlap = self.required_capabilities & self.preferred_capabilities
        if overlap:
            cap_names = ", ".join(sorted(str(c) for c in overlap))
            raise ValueError(
                f"Capabilities cannot be both required and preferred: {cap_names}",
            )

        # Validate authorization_level
        if not isinstance(self.authorization_level, AuthorizationLevel):
            if not isinstance(self.authorization_level, str):
                raise TypeError(
                    "authorization_level must be an AuthorizationLevel or string value",
                )
            try:
                self.authorization_level = AuthorizationLevel(
                    self.authorization_level,
                )
            except ValueError as exc:
                raise ValueError(
                    f"Invalid authorization_level: {self.authorization_level!r}",
                ) from exc

        # Validate approval_required
        if not isinstance(self.approval_required, bool):
            raise TypeError("approval_required must be a boolean")

        # Validate input_data
        if not isinstance(self.input_data, dict):
            raise TypeError("input_data must be a dict")

        # Validate expected_result
        if not isinstance(self.expected_result, str):
            raise TypeError("expected_result must be a string")

    def has_required_capability(self, capability: NodeCapability | str) -> bool:
        """
        Check if this task requires a specific capability.

        Args:
            capability: Either a NodeCapability instance or a capability name string.

        Returns:
            True if this capability is required, False otherwise.
        """
        if isinstance(capability, NodeCapability):
            normalized = capability
        elif isinstance(capability, str):
            normalized = NodeCapability(capability)
        else:
            raise TypeError("capability must be a NodeCapability or string")
        return normalized in self.required_capabilities

    def has_preferred_capability(self, capability: NodeCapability | str) -> bool:
        """
        Check if this task prefers a specific capability.

        Args:
            capability: Either a NodeCapability instance or a capability name string.

        Returns:
            True if this capability is preferred, False otherwise.
        """
        if isinstance(capability, NodeCapability):
            normalized = capability
        elif isinstance(capability, str):
            normalized = NodeCapability(capability)
        else:
            raise TypeError("capability must be a NodeCapability or string")
        return normalized in self.preferred_capabilities

    def all_capabilities(self) -> set[NodeCapability]:
        """
        Get all capabilities (required + preferred).

        Returns:
            Set of all capabilities mentioned in this request.
        """
        return self.required_capabilities | self.preferred_capabilities
