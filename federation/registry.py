"""
Node registry for managing federated compute nodes.

The NodeRegistry is the central coordinator for tracking available
compute nodes and their capabilities in the RAGHub federation.
"""

from datetime import datetime, timedelta, timezone

from federation.capability import NodeCapability
from federation.node_record import NodeRecord, NodeStatus, normalize_last_seen


class NodeRegistry:
    """
    Central registry for managing federated compute nodes.

    Tracks node registration, health, capabilities, and provides
    capability-based node discovery.
    """

    def __init__(self, stale_threshold_seconds: int = 300):
        """
        Initialize the node registry.

        Args:
            stale_threshold_seconds: Number of seconds before a node is
                considered stale/offline. Default is 300 (5 minutes).
        """
        if (
            isinstance(stale_threshold_seconds, bool)
            or not isinstance(stale_threshold_seconds, int)
        ):
            raise TypeError("stale_threshold_seconds must be an integer")
        if stale_threshold_seconds < 0:
            raise ValueError("stale_threshold_seconds must be non-negative")

        self._nodes: dict[str, NodeRecord] = {}
        self._stale_threshold = timedelta(seconds=stale_threshold_seconds)

    def register(self, node: NodeRecord) -> None:
        """
        Register a new node.

        Args:
            node: The NodeRecord to register.

        Raises:
            ValueError: If the node ID is already registered.
        """
        if not isinstance(node, NodeRecord):
            raise TypeError("node must be a NodeRecord")
        if node.node_id in self._nodes:
            raise ValueError(f"Node ID already registered: {node.node_id}")

        node.update_heartbeat()
        self._nodes[node.node_id] = node

    def update(self, node_id: str, **kwargs) -> NodeRecord | None:
        """
        Update a node's properties.

        Args:
            node_id: The ID of the node to update.
            **kwargs: Fields to update: hostname, operating_system, status,
                or capabilities.

        Returns:
            The updated NodeRecord, or None if the node was not found.
        """
        node = self._nodes.get(node_id)
        if node is None:
            return None

        allowed_fields = {"hostname", "operating_system", "status", "capabilities"}
        unsupported_fields = set(kwargs) - allowed_fields
        if unsupported_fields:
            fields = ", ".join(sorted(unsupported_fields))
            raise ValueError(f"Unsupported node update field(s): {fields}")

        candidate = NodeRecord(
            node_id=node.node_id,
            hostname=kwargs.get("hostname", node.hostname),
            operating_system=kwargs.get(
                "operating_system",
                node.operating_system,
            ),
            status=kwargs.get("status", node.status),
            last_seen=node.last_seen,
            capabilities=kwargs.get("capabilities", node.capabilities),
        )
        for field_name in kwargs:
            setattr(node, field_name, getattr(candidate, field_name))

        node.update_heartbeat()
        return node

    def remove(self, node_id: str) -> bool:
        """
        Remove a node from the registry.

        Args:
            node_id: The ID of the node to remove.

        Returns:
            True if the node was removed, False if it was not found.
        """
        if node_id in self._nodes:
            del self._nodes[node_id]
            return True
        return False

    def get(self, node_id: str) -> NodeRecord | None:
        """
        Get a node by its ID.

        Args:
            node_id: The ID of the node to retrieve.

        Returns:
            The NodeRecord, or None if not found.
        """
        return self._nodes.get(node_id)

    def list_nodes(
        self,
        status_filter: NodeStatus | None = None,
        include_stale: bool = True,
    ) -> list[NodeRecord]:
        """
        List all registered nodes.

        Args:
            status_filter: If provided, only return nodes with this status.
            include_stale: If False, exclude stale nodes.

        Returns:
            List of NodeRecords matching the criteria.
        """
        nodes = list(self._nodes.values())

        if not include_stale:
            nodes = [n for n in nodes if not self.is_stale(n)]

        if status_filter is not None:
            nodes = [n for n in nodes if n.status == status_filter]

        return nodes

    def find_nodes_with_capability(
        self,
        capability: NodeCapability | str,
        status_filter: NodeStatus | None = NodeStatus.ONLINE,
        include_stale: bool = False,
    ) -> list[NodeRecord]:
        """
        Find all nodes that have a specific capability.

        Args:
            capability: The capability to search for (NodeCapability or string name).
            status_filter: If provided, only return nodes with this status.
                Defaults to ONLINE nodes only.
            include_stale: If False, exclude stale nodes. Default is False.

        Returns:
            List of NodeRecords that have the requested capability.
        """
        nodes = list(self._nodes.values())

        # Filter by capability
        nodes = [n for n in nodes if n.has_capability(capability)]

        # Filter by staleness
        if not include_stale:
            nodes = [n for n in nodes if not self.is_stale(n)]

        # Filter by status
        if status_filter is not None:
            nodes = [n for n in nodes if n.status == status_filter]

        return nodes

    def heartbeat(self, node_id: str) -> bool:
        """
        Record a heartbeat for a node.

        Updates the last_seen timestamp. If the node was offline,
        it will be marked as online.

        Args:
            node_id: The ID of the node sending the heartbeat.

        Returns:
            True if the heartbeat was recorded, False if node not found.
        """
        node = self._nodes.get(node_id)
        if node is None:
            return False

        # If node was offline, mark it online
        if node.status == NodeStatus.OFFLINE:
            node.mark_online()
        else:
            node.update_heartbeat()

        return True

    def detect_stale_nodes(self) -> list[NodeRecord]:
        """
        Detect nodes that haven't sent a heartbeat recently.

        Returns:
            List of NodeRecords that are considered stale.
        """
        return [node for node in self._nodes.values() if self.is_stale(node)]

    def mark_stale_nodes_offline(self) -> int:
        """
        Mark stale nodes as offline.

        Returns:
            The number of nodes marked offline.
        """
        stale_nodes = self.detect_stale_nodes()
        count = 0

        for node in stale_nodes:
            if node.status != NodeStatus.OFFLINE:
                node.mark_offline()
                count += 1

        return count

    def is_stale(self, node: NodeRecord) -> bool:
        """
        Check if a node is stale based on its last_seen timestamp.

        Args:
            node: The node to check.

        Returns:
            True if the node is stale, False otherwise.
        """
        last_seen = normalize_last_seen(node.last_seen)
        time_since_last_seen = datetime.now(timezone.utc) - last_seen
        return time_since_last_seen > self._stale_threshold

    def _is_stale(self, node: NodeRecord) -> bool:
        """Compatibility wrapper for the public stale-node check."""
        return self.is_stale(node)

    def count(self) -> int:
        """
        Get the total number of registered nodes.

        Returns:
            The number of nodes in the registry.
        """
        return len(self._nodes)

    def clear(self) -> None:
        """Remove all nodes from the registry."""
        self._nodes.clear()
