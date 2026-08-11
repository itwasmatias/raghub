"""
Node record model for tracking federated compute nodes.

A NodeRecord represents a compute node in the RAGHub federation
with its identity, status, and capabilities.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from federation.capability import NodeCapability


class NodeStatus(str, Enum):
    """Status of a federated node."""

    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    MAINTENANCE = "maintenance"


def normalize_last_seen(value: datetime) -> datetime:
    """Validate a node timestamp and normalize it to UTC."""
    if not isinstance(value, datetime):
        raise TypeError("last_seen must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("last_seen must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(slots=True)
class NodeRecord:
    """
    Represents a compute node in the RAGHub federation.

    Tracks node identity, domain membership, operating environment, status,
    health, and advertised capabilities.
    """

    node_id: str
    domain_id: str
    hostname: str
    operating_system: str
    status: NodeStatus = NodeStatus.ONLINE
    last_seen: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    capabilities: set[NodeCapability] = field(default_factory=set)

    def __post_init__(self) -> None:
        """Validate node record fields."""
        # Maximum field lengths matching ControlDomain validation
        MAX_DOMAIN_ID_LENGTH = 255

        if not self.node_id or not isinstance(self.node_id, str):
            raise ValueError("node_id must be a non-empty string")

        # Validate domain_id
        if not isinstance(self.domain_id, str) or not self.domain_id.strip():
            raise ValueError("domain_id must be a non-empty string")
        if len(self.domain_id) > MAX_DOMAIN_ID_LENGTH:
            raise ValueError(f"domain_id exceeds {MAX_DOMAIN_ID_LENGTH} characters")
        if "\x00" in self.domain_id:
            raise ValueError("domain_id must not contain NULL bytes")

        if not self.hostname or not isinstance(self.hostname, str):
            raise ValueError("hostname must be a non-empty string")
        if not self.operating_system or not isinstance(self.operating_system, str):
            raise ValueError("operating_system must be a non-empty string")

        if not isinstance(self.status, NodeStatus):
            if not isinstance(self.status, str):
                raise TypeError("status must be a NodeStatus or string value")
            try:
                self.status = NodeStatus(self.status)
            except ValueError as exc:
                raise ValueError(f"Invalid status: {self.status!r}") from exc

        self.last_seen = normalize_last_seen(self.last_seen)

        try:
            self.capabilities = set(self.capabilities)
        except TypeError as exc:
            raise TypeError(
                "capabilities must be an iterable of NodeCapability values",
            ) from exc
        if not all(
            isinstance(capability, NodeCapability)
            for capability in self.capabilities
        ):
            raise TypeError("capabilities must contain only NodeCapability values")

    def add_capability(self, capability: NodeCapability) -> None:
        """Add a capability to this node."""
        if not isinstance(capability, NodeCapability):
            raise TypeError("capability must be a NodeCapability")
        self.capabilities.add(capability)

    def remove_capability(self, capability: NodeCapability) -> None:
        """Remove a capability from this node."""
        if not isinstance(capability, NodeCapability):
            raise TypeError("capability must be a NodeCapability")
        self.capabilities.discard(capability)

    def has_capability(self, capability: NodeCapability | str) -> bool:
        """
        Check if this node has a specific capability.

        Args:
            capability: Either a NodeCapability instance or a capability name string.

        Returns:
            True if the node has this capability, False otherwise.
        """
        if isinstance(capability, NodeCapability):
            normalized = capability
        elif isinstance(capability, str):
            normalized = NodeCapability(capability)
        else:
            raise TypeError("capability must be a NodeCapability or string")
        return normalized in self.capabilities

    def update_heartbeat(self) -> None:
        """Update the last_seen timestamp to now."""
        self.last_seen = datetime.now(timezone.utc)

    def mark_online(self) -> None:
        """Mark the node as online and update heartbeat."""
        self.status = NodeStatus.ONLINE
        self.update_heartbeat()

    def mark_offline(self) -> None:
        """Mark the node as offline."""
        self.status = NodeStatus.OFFLINE

    def is_available(self) -> bool:
        """Check if the node is available for work."""
        return self.status == NodeStatus.ONLINE
