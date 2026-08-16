"""
Federation subsystem for RAGHub AI Operating System.

This module implements the Worker/Node Registry, Capability Discovery,
Task Routing, and Task Dispatch systems that enable RAGHub to coordinate
work across federated compute nodes without embedding execution transport.
"""

from federation.capability import KnownCapability, NodeCapability
from federation.agent_identity import (
    AgentIdentity,
    AgentIdentityLifecycle,
    AuthoritativeAgentIdentity,
)
from federation.agent_identity_registry import (
    AgentIdentityConflictError,
    AgentIdentityCorruptionError,
    AgentIdentityDomainError,
    AgentIdentityError,
    AgentIdentityLifecycleError,
    AgentIdentityNotFoundError,
    DurableAgentIdentityRegistry,
)
from federation.delegation_grant import (
    AuthoritativeDelegationGrant,
    DelegationGrant,
    DelegationGrantStatus,
)
from federation.delegation_grant_registry import (
    DelegationGrantConflictError,
    DelegationGrantCorruptionError,
    DelegationGrantDomainError,
    DelegationGrantError,
    DelegationGrantIdentityError,
    DelegationGrantLifecycleError,
    DelegationGrantNotFoundError,
    DelegationGrantRegistry,
    DelegationGrantScopeError,
)
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
from federation.effect_boundary import (
    EffectAuthorityError,
    EffectAttempt,
    EffectBoundary,
    EffectBoundaryError,
    EffectDecision,
    EffectEvidence,
    EffectOutcome,
    EffectRequest,
    EffectRequestError,
)
from federation.credential_broker import (
    CredentialBroker,
    CredentialBrokerAuthorityError,
    CredentialBrokerError,
    CredentialBrokerNotFoundError,
    CredentialLease,
    CredentialRef,
)
from federation.access_requirement import AccessRequirement
from federation.access_connection import AccessConnection, ConnectionLifecycle
from federation.authentication_session import (
    AuthenticationSession,
    AuthenticationState,
    create_authentication_session,
)
from federation.credential_backend import (
    CredentialBackend,  # INTERNAL: Trusted primitive - use AccessCredentialBroker for authority-gated access
    CredentialBackendError,
    CredentialNotFoundError,
    InMemoryCredentialBackend,  # INTERNAL: Test-only - never use in production
)
from federation.access_credential_broker import (
    AccessCredentialBroker,
    AccessCredentialBrokerError,
    AccessCredentialAuthorityError,
    AccessCredentialConnectionError,
    AccessCredentialLease,
    AccessCredentialNotFoundError,
    AccessCredentialRequest,
)
from federation.access_credential_store import (
    AccessCredentialError,
    AccessCredentialStore,
    AuthSessionConflictError,
    AuthSessionNotFoundError,
    ConnectionConflictError,
    ConnectionLifecycleError,
    ConnectionNotFoundError,
)
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
    BudgetRoutingEvidence,
    BudgetRoutingGovernance,
    ExecutionLocality,
    ExecutionPreference,
    GovernedWorker,
    WorkerAvailability,
    WorkerBudgetGovernor,
    WorkerCapacity,
    WorkerCostClass,
    WorkerProviderMetadata,
)
from federation.hybrid_routing import (
    CloudRouteEligibility,
    ExecutionRouteKind,
    HybridRoutingCoordinator,
    HybridRoutingDecision,
    HybridRoutingPolicy,
    HybridRoutingReason,
    HybridRoutingRequest,
    LocalRouteEligibility,
    ProviderAvailabilitySnapshot,
)

__all__ = [
    # Node Registry and Capabilities
    "KnownCapability",
    "NodeCapability",
    "NodeRecord",
    "NodeStatus",
    "NodeRegistry",
    # Agent Identity
    "AgentIdentity",
    "AgentIdentityLifecycle",
    "AuthoritativeAgentIdentity",
    "AgentIdentityConflictError",
    "AgentIdentityCorruptionError",
    "AgentIdentityDomainError",
    "AgentIdentityError",
    "AgentIdentityLifecycleError",
    "AgentIdentityNotFoundError",
    "DurableAgentIdentityRegistry",
    "AuthoritativeDelegationGrant",
    "DelegationGrant",
    "DelegationGrantStatus",
    "DelegationGrantConflictError",
    "DelegationGrantCorruptionError",
    "DelegationGrantDomainError",
    "DelegationGrantError",
    "DelegationGrantIdentityError",
    "DelegationGrantLifecycleError",
    "DelegationGrantNotFoundError",
    "DelegationGrantRegistry",
    "DelegationGrantScopeError",
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
    "EffectBoundary",
    "EffectBoundaryError",
    "EffectRequestError",
    "EffectAuthorityError",
    "EffectRequest",
    "EffectDecision",
    "EffectAttempt",
    "EffectOutcome",
    "EffectEvidence",
    "CredentialBroker",
    "CredentialBrokerAuthorityError",
    "CredentialBrokerError",
    "CredentialBrokerNotFoundError",
    "CredentialLease",
    "CredentialRef",
    # Access & Credential Broker v0.1
    "AccessRequirement",
    "AccessConnection",
    "ConnectionLifecycle",
    "AuthenticationSession",
    "AuthenticationState",
    "create_authentication_session",
    # Credential Backend (INTERNAL - use AccessCredentialBroker for authority-gated access)
    "CredentialBackend",
    "CredentialBackendError",
    "CredentialNotFoundError",
    "InMemoryCredentialBackend",
    # Access Credential Broker (authority-gated)
    "AccessCredentialBroker",
    "AccessCredentialBrokerError",
    "AccessCredentialAuthorityError",
    "AccessCredentialConnectionError",
    "AccessCredentialLease",
    "AccessCredentialNotFoundError",
    "AccessCredentialRequest",
    # Access Credential Store
    "AccessCredentialError",
    "AccessCredentialStore",
    "AuthSessionConflictError",
    "AuthSessionNotFoundError",
    "ConnectionConflictError",
    "ConnectionLifecycleError",
    "ConnectionNotFoundError",
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
    "BudgetRoutingEvidence",
    "BudgetRoutingGovernance",
    "ExecutionLocality",
    "ExecutionPreference",
    "GovernedWorker",
    "WorkerAvailability",
    "WorkerBudgetGovernor",
    "WorkerCapacity",
    "WorkerCostClass",
    "WorkerProviderMetadata",
    # Hybrid Routing + Credit Resilience
    "CloudRouteEligibility",
    "ExecutionRouteKind",
    "HybridRoutingCoordinator",
    "HybridRoutingDecision",
    "HybridRoutingPolicy",
    "HybridRoutingReason",
    "HybridRoutingRequest",
    "LocalRouteEligibility",
    "ProviderAvailabilitySnapshot",
]
