"""
Federation subsystem for RAGHub AI Operating System.

This module implements the Worker/Node Registry and Capability Discovery
system that enables RAGHub to coordinate work across federated compute nodes.
"""

from federation.capability import KnownCapability, NodeCapability
from federation.node_record import NodeRecord, NodeStatus
from federation.registry import NodeRegistry

__all__ = [
    "KnownCapability",
    "NodeCapability",
    "NodeRecord",
    "NodeStatus",
    "NodeRegistry",
]
