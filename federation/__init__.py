"""
Federation subsystem for RAGHub AI Operating System.

This module implements the Worker/Node Registry, Capability Discovery,
Task Routing, and Task Dispatch systems that enable RAGHub to coordinate
work across federated compute nodes without embedding execution transport.
"""

from federation.capability import KnownCapability, NodeCapability
from federation.assignment_registry import (
    AssignmentConflictError,
    AssignmentCorruptionError,
    AssignmentNotFoundError,
    AuthoritativeAssignment,
    DurableAssignmentRegistry,
)
from federation.dispatch_offer import (
    TERMINAL_DISPATCH_STATUSES,
    DispatchActorType,
    DispatchAuditEvent,
    DispatchEventType,
    DispatchOfferSnapshot,
    DispatchStatus,
)
from federation.node_record import NodeRecord, NodeStatus
from federation.registry import NodeRegistry
from federation.routing_decision import ExcludedNode, RoutingDecision, RoutingOutcome
from federation.task_assignment import TaskAssignment
from federation.task_dispatcher import (
    DispatchError,
    DispatchCorruptionError,
    DispatchIdentityMismatchError,
    DispatchOfferConflictError,
    DispatchOfferExpiredError,
    DispatchOfferNotFoundError,
    DispatchTerminalStateError,
    TaskDispatchCoordinator,
)
from federation.task_request import AuthorizationLevel, TaskRequest
from federation.task_router import TaskRouter

__all__ = [
    # Node Registry and Capabilities
    "KnownCapability",
    "NodeCapability",
    "NodeRecord",
    "NodeStatus",
    "NodeRegistry",
    # Task Routing
    "AuthorizationLevel",
    "TaskRequest",
    "TaskAssignment",
    "RoutingDecision",
    "RoutingOutcome",
    "ExcludedNode",
    "TaskRouter",
    "AssignmentConflictError",
    "AssignmentCorruptionError",
    "AssignmentNotFoundError",
    "AuthoritativeAssignment",
    "DurableAssignmentRegistry",
    # Task Dispatch
    "TERMINAL_DISPATCH_STATUSES",
    "DispatchActorType",
    "DispatchAuditEvent",
    "DispatchEventType",
    "DispatchOfferSnapshot",
    "DispatchStatus",
    "DispatchError",
    "DispatchCorruptionError",
    "DispatchIdentityMismatchError",
    "DispatchOfferConflictError",
    "DispatchOfferExpiredError",
    "DispatchOfferNotFoundError",
    "DispatchTerminalStateError",
    "TaskDispatchCoordinator",
]
