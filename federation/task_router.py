"""
Task router for routing tasks to federated nodes.

The TaskRouter implements deterministic task routing based on node
capabilities and availability.
"""

from federation.node_record import NodeRecord, NodeStatus
from federation.registry import NodeRegistry
from federation.routing_decision import ExcludedNode, RoutingDecision, RoutingOutcome
from federation.task_assignment import TaskAssignment
from federation.task_request import TaskRequest


class TaskRouter:
    """
    Routes tasks to federated nodes based on capabilities and availability.

    This is a dry-run planning component that determines where tasks should
    be routed without actually executing them.
    """

    def __init__(self, registry: NodeRegistry):
        """
        Initialize the task router.

        Args:
            registry: The NodeRegistry containing available nodes.
        """
        if not isinstance(registry, NodeRegistry):
            raise TypeError("registry must be a NodeRegistry")
        self._registry = registry

    def route(self, task_request: TaskRequest) -> RoutingDecision:
        """
        Route a task to an appropriate node.

        This method implements deterministic routing based on:
        - Node must be online and not stale
        - All required capabilities must be present
        - Preferred capabilities improve ranking but are not mandatory
        - Deterministic tie-breaking using node_id

        Args:
            task_request: The task to route.

        Returns:
            A RoutingDecision with full explanation of the routing outcome.
        """
        if not isinstance(task_request, TaskRequest):
            raise TypeError("task_request must be a TaskRequest")

        # Get all nodes
        all_nodes = sorted(
            self._registry.list_nodes(),
            key=lambda node: node.node_id,
        )

        # Track excluded nodes for explanation
        excluded_nodes = []

        # Filter eligible nodes
        eligible_nodes: list[NodeRecord] = []

        for node in all_nodes:
            # Check if node is stale
            if self._registry.is_stale(node):
                excluded_nodes.append(
                    ExcludedNode(node_id=node.node_id, reason="Node is stale"),
                )
                continue

            # Check if node is online
            if node.status != NodeStatus.ONLINE:
                excluded_nodes.append(
                    ExcludedNode(
                        node_id=node.node_id,
                        reason=f"Node status is {node.status.value}, not online",
                    ),
                )
                continue

            # Check required capabilities
            missing_required = []
            for required_cap in task_request.required_capabilities:
                if not node.has_capability(required_cap):
                    missing_required.append(str(required_cap))

            if missing_required:
                missing = ", ".join(sorted(missing_required))
                excluded_nodes.append(
                    ExcludedNode(
                        node_id=node.node_id,
                        reason=f"Missing required capabilities: {missing}",
                    ),
                )
                continue

            eligible_nodes.append(node)

        # If no eligible nodes, return NO_ELIGIBLE_NODES outcome
        if not eligible_nodes:
            return RoutingDecision(
                task_request=task_request,
                outcome=RoutingOutcome.NO_ELIGIBLE_NODES,
                assignment=None,
                required_capabilities_matched=set(),
                preferred_capabilities_matched=set(),
                excluded_nodes=excluded_nodes,
                explanation=(
                    f"No eligible nodes found for task {task_request.task_id}. "
                    f"{len(excluded_nodes)} nodes were excluded."
                ),
            )

        # Select the best node (deterministically)
        selected_node = min(
            eligible_nodes,
            key=lambda node: (
                -sum(
                    node.has_capability(capability)
                    for capability in task_request.preferred_capabilities
                ),
                node.node_id,
            ),
        )

        # Calculate matched capabilities
        required_matched = set(task_request.required_capabilities)
        preferred_matched = {
            cap
            for cap in task_request.preferred_capabilities
            if selected_node.has_capability(cap)
        }

        # Create assignment
        assignment = TaskAssignment(
            task_request=task_request,
            assigned_node=selected_node,
        )

        # Generate explanation
        explanation = (
            f"Task {task_request.task_id} routed to node {selected_node.node_id}. "
            f"Matched {len(required_matched)} required and "
            f"{len(preferred_matched)} preferred capabilities."
        )

        return RoutingDecision(
            task_request=task_request,
            outcome=RoutingOutcome.SUCCESS,
            assignment=assignment,
            required_capabilities_matched=required_matched,
            preferred_capabilities_matched=preferred_matched,
            excluded_nodes=excluded_nodes,
            explanation=explanation,
        )
