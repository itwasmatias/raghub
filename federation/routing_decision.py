"""
Routing decision model for task routing in RAGHub federation.

A RoutingDecision represents the result of the task routing process with
full explanation of how and why the decision was made.
"""

from dataclasses import dataclass, field
from enum import Enum

from federation.capability import NodeCapability
from federation.task_assignment import TaskAssignment
from federation.task_request import TaskRequest
from federation.worker_governance import BudgetRoutingEvidence


class RoutingOutcome(str, Enum):
    """Outcome of a routing decision."""

    SUCCESS = "success"
    NO_ELIGIBLE_NODES = "no_eligible_nodes"


@dataclass(slots=True)
class ExcludedNode:
    """Represents a node that was excluded from routing with the reason."""

    node_id: str
    reason: str

    def __post_init__(self) -> None:
        """Validate excluded node fields."""
        if not isinstance(self.node_id, str):
            raise TypeError("node_id must be a string")
        if not self.node_id.strip():
            raise ValueError("node_id must be a non-empty string")
        if not isinstance(self.reason, str):
            raise TypeError("reason must be a string")
        if not self.reason.strip():
            raise ValueError("reason must be a non-empty string")


@dataclass(slots=True)
class RoutingDecision:
    """
    Represents the outcome and explanation of a task routing decision.

    This structure provides full transparency into how routing decisions
    are made, including which node was selected, what capabilities matched,
    and why other nodes were excluded.
    """

    task_request: TaskRequest
    outcome: RoutingOutcome
    assignment: TaskAssignment | None = None
    required_capabilities_matched: set[NodeCapability] = field(default_factory=set)
    preferred_capabilities_matched: set[NodeCapability] = field(default_factory=set)
    excluded_nodes: list[ExcludedNode] = field(default_factory=list)
    explanation: str = ""
    assignment_id: str | None = None
    budget_evidence: BudgetRoutingEvidence | None = None

    def __post_init__(self) -> None:
        """Validate routing decision fields."""
        if not isinstance(self.task_request, TaskRequest):
            raise TypeError("task_request must be a TaskRequest")

        # Validate outcome
        if not isinstance(self.outcome, RoutingOutcome):
            if not isinstance(self.outcome, str):
                raise TypeError("outcome must be a RoutingOutcome or string value")
            try:
                self.outcome = RoutingOutcome(self.outcome)
            except ValueError as exc:
                raise ValueError(f"Invalid outcome: {self.outcome!r}") from exc

        # Validate assignment consistency
        if self.outcome == RoutingOutcome.SUCCESS:
            if self.assignment is None:
                raise ValueError(
                    "assignment must be provided when outcome is SUCCESS",
                )
            if not isinstance(self.assignment, TaskAssignment):
                raise TypeError("assignment must be a TaskAssignment")
            if self.assignment.task_request is not self.task_request:
                raise ValueError(
                    "assignment.task_request must match routing decision task_request",
                )
        elif self.outcome == RoutingOutcome.NO_ELIGIBLE_NODES:
            if self.assignment is not None:
                raise ValueError(
                    "assignment must be None when outcome is NO_ELIGIBLE_NODES",
                )
            if self.assignment_id is not None:
                raise ValueError(
                    "assignment_id must be None when outcome is NO_ELIGIBLE_NODES",
                )

        if self.assignment_id is not None and (
            not isinstance(self.assignment_id, str) or not self.assignment_id.strip()
        ):
            raise ValueError("assignment_id must be a non-empty string or None")
        if self.budget_evidence is not None and type(
            self.budget_evidence
        ) is not BudgetRoutingEvidence:
            raise TypeError("budget_evidence must be BudgetRoutingEvidence or None")

        # Validate required_capabilities_matched
        try:
            self.required_capabilities_matched = set(
                self.required_capabilities_matched,
            )
        except TypeError as exc:
            raise TypeError(
                "required_capabilities_matched must be an iterable of NodeCapability values",
            ) from exc
        if not all(
            isinstance(cap, NodeCapability)
            for cap in self.required_capabilities_matched
        ):
            raise TypeError(
                "required_capabilities_matched must contain only NodeCapability values",
            )

        # Validate preferred_capabilities_matched
        try:
            self.preferred_capabilities_matched = set(
                self.preferred_capabilities_matched,
            )
        except TypeError as exc:
            raise TypeError(
                "preferred_capabilities_matched must be an iterable of NodeCapability values",
            ) from exc
        if not all(
            isinstance(cap, NodeCapability)
            for cap in self.preferred_capabilities_matched
        ):
            raise TypeError(
                "preferred_capabilities_matched must contain only NodeCapability values",
            )

        # Validate excluded_nodes
        if not isinstance(self.excluded_nodes, list):
            raise TypeError("excluded_nodes must be a list")
        if not all(isinstance(node, ExcludedNode) for node in self.excluded_nodes):
            raise TypeError("excluded_nodes must contain only ExcludedNode values")

        # Validate explanation
        if not isinstance(self.explanation, str):
            raise TypeError("explanation must be a string")

    @property
    def is_success(self) -> bool:
        """Check if routing was successful."""
        return self.outcome == RoutingOutcome.SUCCESS

    @property
    def assigned_node_id(self) -> str | None:
        """Get the assigned node ID, or None if no assignment."""
        if self.assignment is None:
            return None
        return self.assignment.node_id

    def get_summary(self) -> str:
        """
        Get a human-readable summary of the routing decision.

        Returns:
            A formatted string explaining the routing decision.
        """
        if self.outcome == RoutingOutcome.SUCCESS:
            node_id = self.assigned_node_id
            req_count = len(self.required_capabilities_matched)
            pref_count = len(self.preferred_capabilities_matched)
            return (
                f"Task {self.task_request.task_id} assigned to {node_id} "
                f"({req_count} required, {pref_count} preferred capabilities matched)"
            )
        else:
            excluded_count = len(self.excluded_nodes)
            return (
                f"Task {self.task_request.task_id} could not be routed: "
                f"no eligible nodes ({excluded_count} nodes excluded)"
            )
