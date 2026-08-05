"""
Task assignment model for task routing in RAGHub federation.

A TaskAssignment represents the result of routing a task to a specific node.
"""

from dataclasses import dataclass

from federation.node_record import NodeRecord
from federation.task_request import TaskRequest


@dataclass(slots=True)
class TaskAssignment:
    """
    Represents the assignment of a task to a specific node.

    This is a dry-run planning result that indicates which node should
    execute the task, without actually executing it.
    """

    task_request: TaskRequest
    assigned_node: NodeRecord

    def __post_init__(self) -> None:
        """Validate task assignment fields."""
        if not isinstance(self.task_request, TaskRequest):
            raise TypeError("task_request must be a TaskRequest")
        if not isinstance(self.assigned_node, NodeRecord):
            raise TypeError("assigned_node must be a NodeRecord")

    @property
    def task_id(self) -> str:
        """Get the task ID from the request."""
        return self.task_request.task_id

    @property
    def mission_id(self) -> str:
        """Get the mission ID from the request."""
        return self.task_request.mission_id

    @property
    def node_id(self) -> str:
        """Get the assigned node ID."""
        return self.assigned_node.node_id
