"""
Node capability definitions for the RAGHub federation.

Capabilities represent specific functionality that a compute node can provide.
"""

from dataclasses import dataclass
from enum import Enum


class KnownCapability(str, Enum):
    """Well-known capability identifiers for federated nodes."""

    PYTHON_EXECUTION = "python_execution"
    CONTAINER_RUNTIME = "container_runtime"
    WINDOWS_DESKTOP = "windows_desktop"
    LINUX_SERVICES = "linux_services"
    GPU_AVAILABLE = "gpu_available"
    PERSISTENT_STORAGE = "persistent_storage"
    NETWORK_ACCESS = "network_access"


@dataclass(slots=True, frozen=True)
class NodeCapability:
    """
    Represents a capability that a node can provide.

    A capability is an immutable identifier that describes what a node can do.
    Capabilities enable discovery of nodes with specific functionality.
    """

    name: str

    def __post_init__(self) -> None:
        """Validate capability name."""
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Capability name must be a non-empty string")
        object.__setattr__(self, "name", self.name.strip().casefold())

    def __str__(self) -> str:
        return self.name

    @classmethod
    def from_known(cls, capability: KnownCapability) -> "NodeCapability":
        """Create a capability from a well-known capability."""
        return cls(name=capability.value)
