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

        # Store nodes by (domain_id, node_id) composite key
        self._nodes: dict[tuple[str, str], NodeRecord] = {}
        self._stale_threshold = timedelta(seconds=stale_threshold_seconds)

    def _matches_for_node_id(
        self,
        node_id: str,
        *,
        domain_id: str | None = None,
    ) -> list[tuple[tuple[str, str], NodeRecord]]:
        matches = [
            ((domain, nid), node)
            for (domain, nid), node in self._nodes.items()
            if nid == node_id and (domain_id is None or domain == domain_id)
        ]
        if domain_id is None and len(matches) > 1:
            raise ValueError(
                f"node_id {node_id!r} is ambiguous across domains; "
                "provide domain_id",
            )
        return matches

    def register(self, node: NodeRecord) -> None:
        """
        Register a new node.

        Args:
            node: The NodeRecord to register.

        Raises:
            ValueError: If the (domain_id, node_id) pair is already registered.
        """
        if not isinstance(node, NodeRecord):
            raise TypeError("node must be a NodeRecord")

        key = (node.domain_id, node.node_id)
        if key in self._nodes:
            raise ValueError(
                f"Node already registered in domain: {node.node_id!r} "
                f"in domain {node.domain_id!r}"
            )

        node.update_heartbeat()
        self._nodes[key] = node

    def update(
        self,
        node_id: str,
        *,
        domain_id: str | None = None,
        **kwargs,
    ) -> NodeRecord | None:
        """
        Update a node's properties (backward compatibility - updates the
        resolved match).

        DEPRECATED: Use update_by_domain() for domain-scoped updates.
        When domain_id is omitted, the lookup fails closed if node_id is
        ambiguous across domains.

        Args:
            node_id: The ID of the node to update.
            **kwargs: Fields to update: hostname, operating_system, status,
                or capabilities.

        Returns:
            The updated NodeRecord, or None if the node was not found.
        """
        # Resolve the legacy node_id lookup without guessing across domains.
        node = self.get(node_id, domain_id=domain_id)
        if node is None:
            return None

        allowed_fields = {"hostname", "operating_system", "status", "capabilities"}
        unsupported_fields = set(kwargs) - allowed_fields
        if unsupported_fields:
            fields = ", ".join(sorted(unsupported_fields))
            raise ValueError(f"Unsupported node update field(s): {fields}")

        candidate = NodeRecord(
            node_id=node.node_id,
            domain_id=node.domain_id,
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

    def remove(self, node_id: str, *, domain_id: str | None = None) -> bool:
        """
        Remove a node from the registry (backward compatibility - removes the
        resolved match).

        DEPRECATED: Use remove_by_domain() for domain-scoped removal.
        When domain_id is omitted, the lookup fails closed if node_id is
        ambiguous across domains.

        Args:
            node_id: The ID of the node to remove.

        Returns:
            True if the node was removed, False if it was not found.
        """
        # Resolve the legacy node_id lookup without guessing across domains.
        matches = self._matches_for_node_id(node_id, domain_id=domain_id)
        if not matches:
            return False
        (domain, nid), _ = matches[0]
        del self._nodes[(domain, nid)]
        return True

    def remove_by_domain(self, node_id: str, domain_id: str) -> bool:
        """
        Remove a node from a specific domain.

        Args:
            node_id: The ID of the node to remove.
            domain_id: The domain to remove from.

        Returns:
            True if the node was removed, False if it was not found.
        """
        key = (domain_id, node_id)
        if key in self._nodes:
            del self._nodes[key]
            return True
        return False

    def get(
        self,
        node_id: str,
        *,
        domain_id: str | None = None,
    ) -> NodeRecord | None:
        """
        Get a node by its ID (backward compatibility - returns the resolved
        match).

        DEPRECATED: Use get_by_domain() for domain-scoped lookup.
        When domain_id is omitted, the lookup fails closed if node_id is
        ambiguous across domains.

        Args:
            node_id: The ID of the node to retrieve.

        Returns:
            The NodeRecord, or None if not found. If domain_id is omitted and
            more than one domain contains node_id, the lookup fails closed.
        """
        matches = self._matches_for_node_id(node_id, domain_id=domain_id)
        return None if not matches else matches[0][1]

    def get_by_domain(self, node_id: str, domain_id: str) -> NodeRecord | None:
        """
        Get a node by ID within a specific domain.

        Args:
            node_id: The ID of the node to retrieve.
            domain_id: The domain to search within.

        Returns:
            The NodeRecord, or None if not found in the specified domain.
        """
        return self._nodes.get((domain_id, node_id))

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

    def list_by_domain(self, domain_id: str) -> list[NodeRecord]:
        """
        List all nodes in a specific domain.

        Args:
            domain_id: The domain to list nodes from.

        Returns:
            List of NodeRecords in the specified domain.
        """
        return [
            node
            for (domain, nid), node in self._nodes.items()
            if domain == domain_id
        ]

    def search_by_domain(
        self,
        capabilities: set[NodeCapability] | frozenset[NodeCapability],
        domain_id: str,
        status_filter: NodeStatus | None = NodeStatus.ONLINE,
        include_stale: bool = False,
    ) -> list[NodeRecord]:
        """
        Find all nodes with specific capabilities within a domain.

        Args:
            capabilities: The capabilities to search for.
            domain_id: The domain to search within.
            status_filter: If provided, only return nodes with this status.
                Defaults to ONLINE nodes only.
            include_stale: If False, exclude stale nodes. Default is False.

        Returns:
            List of NodeRecords that match the criteria.
        """
        # Get all nodes in the domain
        nodes = self.list_by_domain(domain_id)

        # Filter by capabilities
        capability_set = set(capabilities) if not isinstance(capabilities, (set, frozenset)) else capabilities
        nodes = [n for n in nodes if capability_set.issubset(n.capabilities)]

        # Filter by staleness
        if not include_stale:
            nodes = [n for n in nodes if not self.is_stale(n)]

        # Filter by status
        if status_filter is not None:
            nodes = [n for n in nodes if n.status == status_filter]

        return nodes

    def heartbeat(
        self,
        node_id: str,
        *,
        domain_id: str | None = None,
    ) -> bool:
        """
        Record a heartbeat for a node (backward compatibility - updates the
        resolved match).

        Updates the last_seen timestamp. If the node was offline,
        it will be marked as online.

        When domain_id is omitted, the lookup fails closed if node_id is
        ambiguous across domains.

        Args:
            node_id: The ID of the node sending the heartbeat.

        Returns:
            True if the heartbeat was recorded, False if node not found.
        """
        node = self.get(node_id, domain_id=domain_id)
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
