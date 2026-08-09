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
from federation.heartbeat import Heartbeat
from federation.heartbeat_registry import (
    HeartbeatAuthenticationError,
    HeartbeatConflictError,
    HeartbeatCorruptionError,
    HeartbeatError,
    HeartbeatRegistry,
)
from federation.worker_lease import HEARTBEAT_INTERVAL, WORKER_LEASE_DURATION
from federation.worker_liveness import (
    LivenessState,
    PowerCapability,
    PowerState,
    WorkerLease,
)
from federation.power_action import (
    ApprovalType,
    ComponentKind,
    GovernedPowerComponent,
    OperationsPowerRecord,
    PowerAction,
    PowerApproval,
    PowerAuditEvent,
    PowerPolicy,
    PowerProposal,
    PowerSnapshot,
    PowerStatus,
)
from federation.power_adapter import (
    DisabledFedoraPowerAdapter,
    DisabledWindowsPowerAdapter,
    PowerExecutionAuthorization,
    PowerExecutionAuthorizationAuthority,
    PowerConflictError,
    PowerCorruptionError,
    PowerRefusalError,
    RecordingPowerAdapter,
)
from federation.windows_display_adapter import (
    HeartbeatWorkerStateProbe,
    WindowsDisplayAdapter,
    WindowsDisplayDeployment,
    WindowsDisplayFailureCode,
    WindowsDisplayResult,
)
from federation.power_coordinator import PowerCoordinator
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
    DispatchWorkerUnavailableError,
    TaskDispatchCoordinator,
)
from federation.task_request import AuthorizationLevel, TaskRequest
from federation.task_router import TaskRouter
from federation.worker_execution import (
    TERMINAL_WORKER_EXECUTION_STATUSES,
    WorkerExecutionAttempt,
    WorkerExecutionConflictError,
    WorkerExecutionCoordinator,
    WorkerExecutionCorruptionError,
    WorkerExecutionError,
    WorkerExecutionIdentityError,
    WorkerExecutionRequest,
    WorkerExecutionResultEnvelope,
    WorkerExecutionStateError,
    WorkerExecutionStatus,
)
from federation.worker_governance import (
    BudgetDecision,
    BudgetPolicy,
    ExecutionLocality,
    ExecutionPreference,
    GovernedWorker,
    WorkerAvailability,
    WorkerBudgetGovernor,
    WorkerCapacity,
    WorkerCostClass,
    WorkerProviderMetadata,
)

__all__ = [
    # Node Registry and Capabilities
    "KnownCapability",
    "NodeCapability",
    "NodeRecord",
    "NodeStatus",
    "NodeRegistry",
    "Heartbeat",
    "HeartbeatError",
    "HeartbeatAuthenticationError",
    "HeartbeatConflictError",
    "HeartbeatCorruptionError",
    "HeartbeatRegistry",
    "HEARTBEAT_INTERVAL",
    "WORKER_LEASE_DURATION",
    "LivenessState",
    "PowerCapability",
    "PowerState",
    "WorkerLease",
    "ApprovalType",
    "ComponentKind",
    "GovernedPowerComponent",
    "OperationsPowerRecord",
    "PowerAction",
    "PowerApproval",
    "PowerAuditEvent",
    "PowerPolicy",
    "PowerProposal",
    "PowerSnapshot",
    "PowerStatus",
    "DisabledFedoraPowerAdapter",
    "DisabledWindowsPowerAdapter",
    "PowerExecutionAuthorization",
    "PowerExecutionAuthorizationAuthority",
    "PowerConflictError",
    "PowerCorruptionError",
    "PowerRefusalError",
    "RecordingPowerAdapter",
    "WindowsDisplayAdapter",
    "HeartbeatWorkerStateProbe",
    "WindowsDisplayDeployment",
    "WindowsDisplayFailureCode",
    "WindowsDisplayResult",
    "PowerCoordinator",
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
    "DispatchWorkerUnavailableError",
    "TaskDispatchCoordinator",
    # Worker execution protocol (transport-neutral; performs no execution)
    "TERMINAL_WORKER_EXECUTION_STATUSES",
    "WorkerExecutionAttempt",
    "WorkerExecutionConflictError",
    "WorkerExecutionCoordinator",
    "WorkerExecutionCorruptionError",
    "WorkerExecutionError",
    "WorkerExecutionIdentityError",
    "WorkerExecutionRequest",
    "WorkerExecutionResultEnvelope",
    "WorkerExecutionStateError",
    "WorkerExecutionStatus",
    "BudgetDecision",
    "BudgetPolicy",
    "ExecutionLocality",
    "ExecutionPreference",
    "GovernedWorker",
    "WorkerAvailability",
    "WorkerBudgetGovernor",
    "WorkerCapacity",
    "WorkerCostClass",
    "WorkerProviderMetadata",
]
