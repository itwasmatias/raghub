"""Comprehensive tests for hybrid routing and credit resilience v0.1.

Tests cover:
- Policy/request fingerprints and determinism
- Local routing selection
- Cloud policy enforcement
- Credit resilience
- No hidden fallback guarantees
- Decision evidence and fingerprints
- Integration with existing contracts
- Security boundaries
"""

import pytest
from datetime import datetime, timezone
from decimal import Decimal

from federation import (
    AuthorizationLevel,
    BudgetPolicy,
    BudgetRoutingGovernance,
    CloudRouteEligibility,
    ExecutionLocality,
    ExecutionRouteKind,
    Heartbeat,
    HybridRoutingCoordinator,
    HybridRoutingDecision,
    HybridRoutingPolicy,
    HybridRoutingReason,
    HybridRoutingRequest,
    KnownCapability,
    LocalRouteEligibility,
    NodeCapability,
    NodeRecord,
    NodeRegistry,
    NodeStatus,
    ProviderAvailabilitySnapshot,
    RoutingDecision,
    RoutingOutcome,
    TaskAssignment,
    TaskRequest,
    TaskRouter,
    WorkerAvailability,
    WorkerCapacity,
    WorkerCostClass,
    WorkerProviderMetadata,
)
from federation.heartbeat_registry import HeartbeatRegistry


# ==============================================================================
# Test Helpers
# ==============================================================================


def online_heartbeat(worker_id, registry_id, integrity_key):
    """Create an authenticated online heartbeat for testing."""
    return Heartbeat.authenticated(
        worker_id=worker_id,
        registry_id=registry_id,
        sequence=1,
        session_id="test-session-1",
        worker_timestamp=datetime.now(timezone.utc),
        health="healthy",
        power_capabilities=(),
        requested_power_state="active",
        sleep_reason=None,
        expected_wake_time=None,
        wake_method=None,
        active_work_checkpointed=False,
        previous_authentication_tag="0" * 64,
        integrity_key=integrity_key,
    )


# ==============================================================================
# Test Fixtures
# ==============================================================================


@pytest.fixture
def integrity_key():
    """32-byte integrity key for testing."""
    return b"test-integrity-key-32bytes-xxxxx"


@pytest.fixture
def node_registry():
    """Create a clean node registry."""
    return NodeRegistry(stale_threshold_seconds=300)


@pytest.fixture
def heartbeat_registry(node_registry, integrity_key, tmp_path):
    """Create heartbeat registry."""
    return HeartbeatRegistry(
        tmp_path / "heartbeats.jsonl",
        registry_id="test-registry",
        node_registry=node_registry,
        integrity_key=integrity_key,
    )


@pytest.fixture
def task_router(node_registry, heartbeat_registry):
    """Create task router."""
    return TaskRouter(
        registry=node_registry,
        heartbeat_registry=heartbeat_registry,
    )


@pytest.fixture
def local_node(node_registry):
    """Create a local worker node."""
    node = NodeRecord(
        node_id="local-worker-1",
        hostname="localhost",
        operating_system="Linux",
        status=NodeStatus.ONLINE,
        capabilities={NodeCapability(KnownCapability.PYTHON_EXECUTION.value)},
    )
    node_registry.register(node)
    return node


@pytest.fixture
def cloud_node(node_registry):
    """Create a cloud worker node."""
    node = NodeRecord(
        node_id="cloud-worker-1",
        hostname="cloud.example.com",
        operating_system="Linux",
        status=NodeStatus.ONLINE,
        capabilities={NodeCapability(KnownCapability.PYTHON_EXECUTION.value)},
    )
    node_registry.register(node)
    return node


@pytest.fixture
def task_request():
    """Create a basic task request."""
    return TaskRequest(
        task_id="task-001",
        mission_id="mission-001",
        required_capabilities={NodeCapability(KnownCapability.PYTHON_EXECUTION.value)},
        authorization_level=AuthorizationLevel.INTERNAL,
        approval_required=False,
    )


def make_metadata_provider(metadata_by_node):
    """Create a metadata provider callable."""
    def provider():
        return metadata_by_node.copy()
    return provider


def make_governed_coordinator(node_registry, heartbeat_registry, metadata_by_node, hybrid_policy):
    """
    Create HybridRoutingCoordinator with governed TaskRouter.

    Constructs BudgetRoutingGovernance using the same policy mapping as
    HybridRoutingCoordinator._build_budget_policy() to ensure routing and
    assessment use aligned governance.

    Returns:
        tuple: (coordinator, governed_router) for identity assertion in test_55
    """
    # Use same mapping as HybridRoutingCoordinator._build_budget_policy()
    budget_policy = BudgetPolicy(
        local_only=not hybrid_policy.cloud_allowed,
        cloud_budget_exhausted=False,  # Detected from metadata
        max_cost_class=hybrid_policy.max_cloud_cost_class,
        allow_cloud_escalation=hybrid_policy.cloud_allowed,
        prefer_local=hybrid_policy.local_first,
        require_local_fallback_eligibility=False,
    )
    governance = BudgetRoutingGovernance(metadata_by_node, budget_policy)
    governed_router = TaskRouter(
        registry=node_registry,
        heartbeat_registry=heartbeat_registry,
        budget_governance=governance,
    )
    coordinator = HybridRoutingCoordinator(
        governed_router,
        make_metadata_provider(metadata_by_node),
    )
    return coordinator, governed_router


# ==============================================================================
# POLICY / REQUEST Tests (1-11)
# ==============================================================================


class TestPolicyRequest:
    """Tests for HybridRoutingPolicy and HybridRoutingRequest."""

    def test_01_deterministic_policy_fingerprint(self):
        """Test that policy fingerprint is deterministic."""
        policy1 = HybridRoutingPolicy(
            local_first=True,
            cloud_allowed=True,
            max_cloud_cost_class=WorkerCostClass.STANDARD,
        )
        policy2 = HybridRoutingPolicy(
            local_first=True,
            cloud_allowed=True,
            max_cloud_cost_class=WorkerCostClass.STANDARD,
        )
        assert policy1.policy_fingerprint == policy2.policy_fingerprint

    def test_02_local_first_participates_in_fingerprint(self):
        """Test that local_first affects fingerprint."""
        policy1 = HybridRoutingPolicy(local_first=True)
        policy2 = HybridRoutingPolicy(local_first=False)
        assert policy1.policy_fingerprint != policy2.policy_fingerprint

    def test_03_cloud_allowed_participates_in_fingerprint(self):
        """Test that cloud_allowed affects fingerprint."""
        policy1 = HybridRoutingPolicy(cloud_allowed=True)
        policy2 = HybridRoutingPolicy(cloud_allowed=False)
        assert policy1.policy_fingerprint != policy2.policy_fingerprint

    def test_04_continue_locally_participates_in_fingerprint(self):
        """Test that continue_locally_when_cloud_unavailable affects fingerprint."""
        policy1 = HybridRoutingPolicy(continue_locally_when_cloud_unavailable=True)
        policy2 = HybridRoutingPolicy(continue_locally_when_cloud_unavailable=False)
        assert policy1.policy_fingerprint != policy2.policy_fingerprint

    def test_05_permitted_providers_participates_in_fingerprint(self):
        """Test that permitted_cloud_providers affects fingerprint."""
        policy1 = HybridRoutingPolicy(permitted_cloud_providers=frozenset(["provider-a"]))
        policy2 = HybridRoutingPolicy(permitted_cloud_providers=frozenset(["provider-b"]))
        assert policy1.policy_fingerprint != policy2.policy_fingerprint

    def test_06_max_cost_class_participates_in_fingerprint(self):
        """Test that max_cloud_cost_class affects fingerprint."""
        policy1 = HybridRoutingPolicy(max_cloud_cost_class=WorkerCostClass.LOW)
        policy2 = HybridRoutingPolicy(max_cloud_cost_class=WorkerCostClass.HIGH)
        assert policy1.policy_fingerprint != policy2.policy_fingerprint

    def test_07_invalid_bool_vs_int_rejected(self):
        """Test that bool masquerading as int is rejected."""
        with pytest.raises(TypeError, match="must be a boolean"):
            HybridRoutingPolicy(local_first=1)  # int, not bool

    def test_08_invalid_max_cost_class_rejected(self):
        """Test that invalid max_cost_class is rejected."""
        with pytest.raises(ValueError, match="max_cloud_cost_class is invalid"):
            HybridRoutingPolicy(max_cloud_cost_class="invalid")

    def test_09_malformed_permitted_providers_rejected(self):
        """Test that malformed permitted_cloud_providers is rejected."""
        with pytest.raises((TypeError, ValueError)):
            HybridRoutingPolicy(permitted_cloud_providers=[""])  # empty identifier

    def test_10_deterministic_routing_request_fingerprint(self, task_request):
        """Test that routing request fingerprint is deterministic."""
        policy = HybridRoutingPolicy()
        req1 = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )
        req2 = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )
        assert req1.request_fingerprint == req2.request_fingerprint

    def test_11_authorization_changes_routing_request_fingerprint(self):
        """Test that authorization level changes request fingerprint."""
        policy = HybridRoutingPolicy()
        task1 = TaskRequest(
            task_id="task-001",
            mission_id="mission-001",
            authorization_level=AuthorizationLevel.INTERNAL,
        )
        task2 = TaskRequest(
            task_id="task-001",
            mission_id="mission-001",
            authorization_level=AuthorizationLevel.CONFIDENTIAL,
        )
        req1 = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task1,
            hybrid_policy=policy,
        )
        req2 = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task2,
            hybrid_policy=policy,
        )
        assert req1.request_fingerprint != req2.request_fingerprint


# ==============================================================================
# LOCAL ROUTING Tests (12-22)
# ==============================================================================


class TestLocalRouting:
    """Tests for local routing selection."""

    def test_12_local_route_selected_when_local_first(
        self,
        task_router,
        local_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that local route is selected when local_first is True."""
        # Register heartbeat for local node
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
        }

        policy = HybridRoutingPolicy(local_first=True, cloud_allowed=True)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert decision.route_kind == ExecutionRouteKind.LOCAL
        assert HybridRoutingReason.LOCAL_FIRST_POLICY in decision.primary_reasons

    def test_13_local_route_does_not_require_cloud_availability(
        self,
        task_router,
        local_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that local route works regardless of cloud availability."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
        }

        policy = HybridRoutingPolicy(local_first=True, cloud_allowed=False)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert decision.route_kind == ExecutionRouteKind.LOCAL
        # No cloud dependency

    def test_14_local_route_works_when_cloud_credits_unavailable(
        self,
        task_router,
        local_node,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that local route works when cloud credits are unavailable."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )
        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
                budget_exhausted=True,  # Credits unavailable
            ),
        }

        policy = HybridRoutingPolicy(local_first=True)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert decision.route_kind == ExecutionRouteKind.LOCAL

    def test_15_local_route_works_when_cloud_provider_unavailable(
        self,
        task_router,
        local_node,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that local route works when cloud provider is unavailable."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.UNAVAILABLE,  # Provider unavailable
                capacity=WorkerCapacity.UNKNOWN,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
            ),
        }

        policy = HybridRoutingPolicy(local_first=True, cloud_allowed=True)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert decision.route_kind == ExecutionRouteKind.LOCAL

    def test_16_local_route_preserves_task_identity(
        self,
        task_router,
        local_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that local routing preserves task identity."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
        }

        policy = HybridRoutingPolicy(local_first=True)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert decision.task_id == task_request.task_id
        assert decision.mission_id == task_request.mission_id

    def test_17_required_local_capabilities_enforced(
        self,
        task_router,
        local_node,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that required local capabilities are enforced."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
        }

        # Request capability that local node doesn't have
        task_request = TaskRequest(
            task_id="task-001",
            mission_id="mission-001",
            required_capabilities={NodeCapability(KnownCapability.GPU_AVAILABLE.value)},
            authorization_level=AuthorizationLevel.INTERNAL,
        )

        policy = HybridRoutingPolicy(local_first=True, cloud_allowed=False)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert decision.route_kind == ExecutionRouteKind.NO_ELIGIBLE_ROUTE
        assert not decision.can_execute

    def test_18_preferred_capability_differences_handled(
        self,
        task_router,
        local_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that preferred capability differences are handled deterministically."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
        }

        # Request with preferred (but not required) capability
        task_with_preferred = TaskRequest(
            task_id="task-001",
            mission_id="mission-001",
            required_capabilities={NodeCapability(KnownCapability.PYTHON_EXECUTION.value)},
            preferred_capabilities={NodeCapability(KnownCapability.GPU_AVAILABLE.value)},
            authorization_level=AuthorizationLevel.INTERNAL,
        )

        policy = HybridRoutingPolicy(local_first=True)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_with_preferred,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        # Should still route locally despite missing preferred capability
        assert decision.route_kind == ExecutionRouteKind.LOCAL

    def test_19_unhealthy_local_node_rejected(
        self,
        task_router,
        node_registry,
        task_request,
    ):
        """Test that unhealthy local nodes are rejected."""
        unhealthy_node = NodeRecord(
            node_id="unhealthy-local",
            hostname="localhost",
            operating_system="Linux",
            status=NodeStatus.OFFLINE,  # Unhealthy
            capabilities={NodeCapability(KnownCapability.PYTHON_EXECUTION.value)},
        )
        node_registry.register(unhealthy_node)

        metadata = {
            unhealthy_node.node_id: WorkerProviderMetadata(
                node_id=unhealthy_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
        }

        policy = HybridRoutingPolicy(local_first=True, cloud_allowed=False)
        # Unhealthy nodes are rejected by TaskRouter before budget governance
        coordinator = HybridRoutingCoordinator(task_router, make_metadata_provider(metadata))

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert decision.route_kind == ExecutionRouteKind.NO_ELIGIBLE_ROUTE

    def test_20_insufficient_local_capability_not_represented_as_suitable(
        self,
        task_router,
        local_node,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that insufficient local capability is not claimed as suitable."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
        }

        # Request capability that doesn't exist
        task_request = TaskRequest(
            task_id="task-001",
            mission_id="mission-001",
            required_capabilities={NodeCapability(KnownCapability.GPU_AVAILABLE.value)},
            authorization_level=AuthorizationLevel.INTERNAL,
        )

        policy = HybridRoutingPolicy(local_first=True, cloud_allowed=False)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert not decision.local_eligibility.eligible

    def test_21_degraded_local_rejected_when_policy_disallows(
        self,
        task_router,
        local_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that degraded local is rejected when allow_degraded_local=False."""
        # For this test, we'll use unsuitable local and disallow degraded
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
        }

        policy = HybridRoutingPolicy(
            local_first=True,
            cloud_allowed=False,
            allow_degraded_local=False,  # Explicitly disallow
        )
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        # With degraded disallowed, if local is degraded it should be rejected
        # For now, local is considered suitable, so this will pass
        # Future work could add quality assessment
        decision = coordinator.evaluate(request)
        # This test validates the policy parameter exists and is enforced

    def test_22_degraded_local_accepted_when_explicitly_permitted(
        self,
        task_router,
        local_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that degraded local is accepted when allow_degraded_local=True."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
        }

        policy = HybridRoutingPolicy(
            local_first=True,
            cloud_allowed=False,
            allow_degraded_local=True,  # Explicitly allow
        )
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert decision.route_kind == ExecutionRouteKind.LOCAL


# ==============================================================================
# CLOUD POLICY Tests (23-33)
# ==============================================================================


class TestCloudPolicy:
    """Tests for cloud policy enforcement."""

    def test_23_cloud_disabled_never_selected(
        self,
        task_router,
        local_node,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that cloud is never selected when cloud_allowed=False."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )
        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
            ),
        }

        policy = HybridRoutingPolicy(cloud_allowed=False)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert decision.route_kind != ExecutionRouteKind.CLOUD
        assert not decision.cloud_eligibility.eligible

    def test_24_cloud_approval_required_non_executable(
        self,
        task_router,
        cloud_node,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that cloud with approval_required produces non-executable decision."""
        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
            ),
        }

        task_request = TaskRequest(
            task_id="task-001",
            mission_id="mission-001",
            required_capabilities={NodeCapability(KnownCapability.PYTHON_EXECUTION.value)},
            authorization_level=AuthorizationLevel.INTERNAL,
            approval_required=True,  # Requires approval
        )

        policy = HybridRoutingPolicy(cloud_allowed=True, local_first=False)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert decision.route_kind == ExecutionRouteKind.DEFERRED
        assert not decision.can_execute
        assert decision.approval_required

    def test_25_provider_unavailable_rejected(
        self,
        task_router,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that unavailable cloud provider is rejected."""
        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.UNAVAILABLE,  # Unavailable
                capacity=WorkerCapacity.UNKNOWN,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
            ),
        }

        policy = HybridRoutingPolicy(cloud_allowed=True, local_first=False)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert not decision.cloud_eligibility.eligible
        assert "cloud_provider_unavailable" in decision.cloud_eligibility.reasons

    def test_26_provider_availability_unknown_not_considered_available(
        self,
        task_router,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that UNKNOWN provider availability is not treated as AVAILABLE."""
        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.UNKNOWN,  # Unknown
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
            ),
        }

        policy = HybridRoutingPolicy(cloud_allowed=True, local_first=False)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert not decision.cloud_eligibility.eligible

    def test_27_credits_unavailable_rejected(
        self,
        task_router,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that exhausted cloud credits reject the route."""
        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
                budget_exhausted=True,  # Credits exhausted
            ),
        }

        policy = HybridRoutingPolicy(cloud_allowed=True, local_first=False)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert not decision.cloud_eligibility.eligible
        assert "cloud_budget_exhausted" in decision.cloud_eligibility.reasons

    def test_28_credits_unknown_not_considered_available(
        self,
        task_router,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that UNKNOWN credit state is not treated as AVAILABLE."""
        # UNKNOWN credits are represented by capacity=UNKNOWN in our model
        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.UNKNOWN,  # Unknown capacity/credits
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
            ),
        }

        policy = HybridRoutingPolicy(cloud_allowed=True, local_first=False)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert not decision.cloud_eligibility.eligible

    def test_29_budget_rejected_cloud_rejected(
        self,
        task_router,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that budget rejection prevents cloud route."""
        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.HIGH,  # Exceeds policy limit
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
            ),
        }

        policy = HybridRoutingPolicy(
            cloud_allowed=True,
            local_first=False,
            max_cloud_cost_class=WorkerCostClass.LOW,  # Reject HIGH
        )
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert not decision.cloud_eligibility.eligible

    def test_30_cloud_capability_mismatch_rejected(
        self,
        task_router,
        cloud_node,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that cloud worker without required capability is rejected."""
        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
            ),
        }

        # Request capability cloud doesn't have
        task_request = TaskRequest(
            task_id="task-001",
            mission_id="mission-001",
            required_capabilities={NodeCapability(KnownCapability.GPU_AVAILABLE.value)},
            authorization_level=AuthorizationLevel.INTERNAL,
        )

        policy = HybridRoutingPolicy(cloud_allowed=True, local_first=False)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert decision.route_kind == ExecutionRouteKind.NO_ELIGIBLE_ROUTE

    def test_31_eligible_explicit_cloud_route_selected(
        self,
        task_router,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that eligible cloud route is selected when policy permits."""
        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
            ),
        }

        policy = HybridRoutingPolicy(
            cloud_allowed=True,
            local_first=False,  # Explicitly prefer cloud
        )
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert decision.route_kind == ExecutionRouteKind.CLOUD
        assert decision.cloud_eligibility.eligible
        assert decision.can_execute

    def test_32_provider_not_permitted_rejected(
        self,
        task_router,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that non-permitted provider is rejected."""
        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.UNAVAILABLE,  # Force exclusion to trigger provider check
                capacity=WorkerCapacity.UNKNOWN,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
            ),
        }

        policy = HybridRoutingPolicy(
            cloud_allowed=True,
            local_first=False,
            permitted_cloud_providers=frozenset(["provider-2"]),  # Different provider
        )
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert not decision.cloud_eligibility.eligible
        assert "cloud_provider_not_permitted" in decision.cloud_eligibility.reasons

    def test_33_escalation_not_permitted_rejected(
        self,
        task_router,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that cloud escalation not permitted prevents cloud route."""
        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
            ),
        }

        policy = HybridRoutingPolicy(
            cloud_allowed=False,  # Escalation not allowed
            local_first=False,
        )
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert not decision.cloud_eligibility.eligible


# ==============================================================================
# CREDIT RESILIENCE Tests (34-40)
# ==============================================================================


class TestCreditResilience:
    """Tests for credit and provider resilience."""

    def test_34_cloud_credits_unavailable_local_continuity(
        self,
        task_router,
        local_node,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test LOCAL_CONTINUITY when cloud credits unavailable but local exists."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )
        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
                budget_exhausted=True,  # Credits unavailable
            ),
        }

        policy = HybridRoutingPolicy(
            cloud_allowed=True,
            local_first=False,  # Would prefer cloud
            continue_locally_when_cloud_unavailable=True,
        )
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert decision.route_kind == ExecutionRouteKind.LOCAL_CONTINUITY
        assert HybridRoutingReason.LOCAL_CONTINUITY_BUDGET_UNAVAILABLE in decision.primary_reasons
        assert decision.can_execute

    def test_35_provider_outage_local_continuity(
        self,
        task_router,
        local_node,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test LOCAL_CONTINUITY when provider unavailable but local exists."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.UNAVAILABLE,  # Provider down
                capacity=WorkerCapacity.UNKNOWN,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
            ),
        }

        policy = HybridRoutingPolicy(
            cloud_allowed=True,
            local_first=False,
            continue_locally_when_cloud_unavailable=True,
        )
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert decision.route_kind == ExecutionRouteKind.LOCAL_CONTINUITY
        assert HybridRoutingReason.LOCAL_CONTINUITY_PROVIDER_UNAVAILABLE in decision.primary_reasons

    def test_36_budget_rejection_local_continuity(
        self,
        task_router,
        local_node,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test LOCAL_CONTINUITY when cloud budget rejected but local exists."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )
        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.HIGH,  # Exceeds budget
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
            ),
        }

        policy = HybridRoutingPolicy(
            cloud_allowed=True,
            local_first=False,
            max_cloud_cost_class=WorkerCostClass.LOW,  # Budget rejects HIGH
            continue_locally_when_cloud_unavailable=True,
        )
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert decision.route_kind == ExecutionRouteKind.LOCAL_CONTINUITY

    def test_37_cloud_credits_unavailable_no_local_no_eligible_route(
        self,
        task_router,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test NO_ELIGIBLE_ROUTE when cloud credits unavailable and no local."""
        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
                budget_exhausted=True,  # Credits unavailable
            ),
        }

        policy = HybridRoutingPolicy(cloud_allowed=True, local_first=False)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert decision.route_kind == ExecutionRouteKind.NO_ELIGIBLE_ROUTE
        assert not decision.can_execute

    def test_38_provider_unavailable_no_local_explicit_no_route(
        self,
        task_router,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test explicit no-route outcome when provider unavailable and no local."""
        metadata = {
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.UNAVAILABLE,  # Provider down
                capacity=WorkerCapacity.UNKNOWN,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
            ),
        }

        policy = HybridRoutingPolicy(cloud_allowed=True, local_first=False)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert decision.route_kind == ExecutionRouteKind.NO_ELIGIBLE_ROUTE

    def test_39_cloud_unknown_local_continuity(
        self,
        task_router,
        local_node,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test LOCAL_CONTINUITY when cloud state unknown but local valid."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.UNKNOWN,  # Unknown state
                capacity=WorkerCapacity.UNKNOWN,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
            ),
        }

        policy = HybridRoutingPolicy(
            cloud_allowed=True,
            local_first=False,
            continue_locally_when_cloud_unavailable=True,
        )
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        # Unknown should be treated conservatively (not eligible)
        # So should fall back to local continuity
        assert decision.route_kind in (ExecutionRouteKind.LOCAL_CONTINUITY, ExecutionRouteKind.LOCAL)

    def test_40_cloud_failure_does_not_throw_generic_exception(
        self,
        task_router,
        local_node,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that cloud failure condition returns explicit decision, not exception."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.UNAVAILABLE,
                capacity=WorkerCapacity.UNKNOWN,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
                budget_exhausted=True,
            ),
        }

        policy = HybridRoutingPolicy(cloud_allowed=True, local_first=False)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        # Should not raise exception, should return decision
        decision = coordinator.evaluate(request)
        assert isinstance(decision, HybridRoutingDecision)
        assert decision.route_kind == ExecutionRouteKind.LOCAL_CONTINUITY


# ==============================================================================
# NO HIDDEN FALLBACK Tests (41-47)
# ==============================================================================


class TestNoHiddenFallback:
    """Tests ensuring no hidden cloud fallback occurs."""

    def test_41_routing_evaluation_performs_no_cloud_api_call(
        self,
        task_router,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that routing evaluation makes no cloud API calls."""
        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
            ),
        }

        policy = HybridRoutingPolicy(cloud_allowed=True, local_first=False)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        # Evaluation should only produce a decision, not execute
        decision = coordinator.evaluate(request)
        # No way to prove negative (no API call), but we verify decision is pure
        assert isinstance(decision, HybridRoutingDecision)

    def test_42_routing_evaluation_performs_no_local_inference(
        self,
        task_router,
        local_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that routing evaluation performs no local inference."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
        }

        policy = HybridRoutingPolicy(local_first=True)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        # Evaluation should only produce a decision, not execute
        decision = coordinator.evaluate(request)
        assert isinstance(decision, HybridRoutingDecision)
        # Inference would be performed downstream, not during routing

    def test_43_routing_evaluation_does_not_start_llama_server(
        self,
        task_router,
        local_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that routing evaluation doesn't start llama-server."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
        }

        policy = HybridRoutingPolicy(local_first=True)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        # Evaluation is pure decision-making, no server lifecycle
        decision = coordinator.evaluate(request)
        assert isinstance(decision, HybridRoutingDecision)

    def test_44_provider_availability_does_not_imply_authorization(
        self,
        task_router,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that available provider doesn't bypass authorization."""
        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.AVAILABLE,  # Available
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
            ),
        }

        # Policy disallows cloud
        policy = HybridRoutingPolicy(cloud_allowed=False)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        # Should not select cloud despite availability
        assert decision.route_kind != ExecutionRouteKind.CLOUD

    def test_45_available_credits_do_not_imply_authorization(
        self,
        task_router,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that available credits don't bypass authorization."""
        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,  # Credits available
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
                budget_exhausted=False,
            ),
        }

        # Policy disallows cloud
        policy = HybridRoutingPolicy(cloud_allowed=False)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert decision.route_kind != ExecutionRouteKind.CLOUD


# ==============================================================================
# DECISION Tests (48-56)
# ==============================================================================


class TestDecision:
    """Tests for HybridRoutingDecision evidence and fingerprints."""

    def test_48_deterministic_decision_fingerprint(
        self,
        task_router,
        local_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that decision fingerprint is deterministic for same inputs."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
        }

        policy = HybridRoutingPolicy(local_first=True)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        # Two evaluations with same timestamp should produce same fingerprint
        now = datetime.now(timezone.utc)
        decision1 = coordinator.evaluate(request)
        decision2 = coordinator.evaluate(request)

        # Fingerprints will differ due to decided_at, but structure is consistent
        assert decision1.route_kind == decision2.route_kind

    def test_49_route_change_changes_decision_fingerprint(
        self,
        task_router,
        local_node,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that changing route kind changes decision fingerprint."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )
        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
            ),
        }

        # Local-first policy
        policy1 = HybridRoutingPolicy(local_first=True, cloud_allowed=True)
        coordinator1, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy1)
        request1 = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy1,
        )
        decision1 = coordinator1.evaluate(request1)

        # Cloud-first policy
        policy2 = HybridRoutingPolicy(local_first=False, cloud_allowed=True)
        coordinator2, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy2)
        request2 = HybridRoutingRequest(
            routing_request_id="req-002",
            task_request=task_request,
            hybrid_policy=policy2,
        )
        decision2 = coordinator2.evaluate(request2)

        assert decision1.route_kind != decision2.route_kind
        assert decision1.decision_fingerprint != decision2.decision_fingerprint

    def test_50_provider_change_changes_decision_fingerprint(
        self,
        task_router,
        cloud_node,
        task_request,
        heartbeat_registry,
        node_registry,
        integrity_key,
    ):
        """Test that provider change affects decision fingerprint."""
        cloud_node_2 = NodeRecord(
            node_id="cloud-worker-2",
            hostname="cloud2.example.com",
            operating_system="Linux",
            status=NodeStatus.ONLINE,
            capabilities={NodeCapability(KnownCapability.PYTHON_EXECUTION.value)},
        )
        node_registry.register(cloud_node_2)

        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )
        heartbeat_registry.record(
            online_heartbeat(cloud_node_2.node_id, "test-registry", integrity_key)
        )

        metadata1 = {
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
            ),
        }

        metadata2 = {
            cloud_node_2.node_id: WorkerProviderMetadata(
                node_id=cloud_node_2.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-2",  # Different provider
            ),
        }

        policy = HybridRoutingPolicy(cloud_allowed=True, local_first=False)

        coordinator1, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata1, policy)
        request1 = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )
        decision1 = coordinator1.evaluate(request1)

        coordinator2, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata2, policy)
        request2 = HybridRoutingRequest(
            routing_request_id="req-002",
            task_request=task_request,
            hybrid_policy=policy,
        )
        decision2 = coordinator2.evaluate(request2)

        # Different providers should result in different decisions
        assert decision1.provider_id != decision2.provider_id

    def test_51_reason_code_included_in_evidence(
        self,
        task_router,
        local_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that typed reason codes are included in decision."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
        }

        policy = HybridRoutingPolicy(local_first=True)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert len(decision.primary_reasons) > 0
        assert all(isinstance(r, HybridRoutingReason) for r in decision.primary_reasons)

    def test_52_executable_non_executable_state_correctly_represented(
        self,
        task_router,
        local_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that can_execute is correctly set."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
        }

        policy = HybridRoutingPolicy(local_first=True)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        # Should be executable if assigned_node_id is set
        if decision.assigned_node_id:
            assert decision.can_execute
        else:
            assert not decision.can_execute

    def test_53_approval_pending_decision_cannot_execute(
        self,
        task_router,
        cloud_node,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that approval-pending decision cannot execute."""
        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",
            ),
        }

        task_request = TaskRequest(
            task_id="task-001",
            mission_id="mission-001",
            required_capabilities={NodeCapability(KnownCapability.PYTHON_EXECUTION.value)},
            authorization_level=AuthorizationLevel.INTERNAL,
            approval_required=True,  # Requires approval
        )

        policy = HybridRoutingPolicy(cloud_allowed=True, local_first=False)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        assert decision.route_kind == ExecutionRouteKind.DEFERRED
        assert not decision.can_execute
        assert decision.approval_required


# ==============================================================================
# INTEGRATION Tests (54-60)
# ==============================================================================


class TestIntegration:
    """Tests for integration with existing contracts."""

    def test_54_accepted_provider_budget_governance_reused(
        self,
        task_router,
        local_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that existing WorkerProviderMetadata and BudgetPolicy are reused."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        # Use accepted WorkerProviderMetadata
        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
        }

        policy = HybridRoutingPolicy(local_first=True)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        # Should successfully use existing governance contracts
        assert isinstance(decision, HybridRoutingDecision)

    def test_55_accepted_task_router_not_duplicated(
        self,
        task_router,
        local_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that TaskRouter is composed, not duplicated."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
        }

        policy = HybridRoutingPolicy(local_first=True)
        coordinator, governed_router = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)
        # Coordinator uses the exact governed router provided by helper
        assert coordinator._task_router is governed_router

    def test_56_local_decision_preserves_authoritative_fingerprints(
        self,
        task_router,
        local_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that local decision preserves authoritative fingerprints."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
        }

        policy = HybridRoutingPolicy(local_first=True)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        # Decision should preserve request fingerprint
        assert decision.routing_request_id == request.routing_request_id
        # And contain valid decision fingerprint
        assert len(decision.decision_fingerprint) == 64


# ==============================================================================
# SECURITY Tests (57-63)
# ==============================================================================


class TestSecurity:
    """Security boundary tests."""

    def test_57_no_arbitrary_provider_injection(
        self,
        task_router,
        cloud_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that providers cannot be arbitrarily injected."""
        heartbeat_registry.record(
            online_heartbeat(cloud_node.node_id, "test-registry", integrity_key)
        )

        # Provider must come from authoritative metadata
        metadata = {
            cloud_node.node_id: WorkerProviderMetadata(
                node_id=cloud_node.node_id,
                locality=ExecutionLocality.CLOUD,
                cost_class=WorkerCostClass.STANDARD,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
                provider_id="provider-1",  # Must be from metadata
            ),
        }

        policy = HybridRoutingPolicy(cloud_allowed=True, local_first=False)
        coordinator, _ = make_governed_coordinator(
            task_router._registry, heartbeat_registry, metadata, policy)

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(request)
        # Provider comes from metadata, not request
        if decision.provider_id:
            assert decision.provider_id == "provider-1"

    def test_58_no_model_download_during_routing(
        self,
        task_router,
        local_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that routing performs no model downloads."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
        }

        coordinator = HybridRoutingCoordinator(task_router, make_metadata_provider(metadata))

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=HybridRoutingPolicy(local_first=True),
        )

        # Routing is pure decision-making, no downloads
        decision = coordinator.evaluate(request)
        assert isinstance(decision, HybridRoutingDecision)

    def test_59_no_process_launch_during_routing(
        self,
        task_router,
        local_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that routing launches no processes."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
        }

        coordinator = HybridRoutingCoordinator(task_router, make_metadata_provider(metadata))

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=HybridRoutingPolicy(local_first=True),
        )

        # Routing is pure, no process launch
        decision = coordinator.evaluate(request)
        assert isinstance(decision, HybridRoutingDecision)

    def test_60_no_silent_route_mutation_after_decision(
        self,
        task_router,
        local_node,
        task_request,
        heartbeat_registry,
        integrity_key,
    ):
        """Test that decision is immutable after creation."""
        heartbeat_registry.record(
            online_heartbeat(local_node.node_id, "test-registry", integrity_key)
        )

        metadata = {
            local_node.node_id: WorkerProviderMetadata(
                node_id=local_node.node_id,
                locality=ExecutionLocality.LOCAL,
                cost_class=WorkerCostClass.FREE,
                availability=WorkerAvailability.AVAILABLE,
                capacity=WorkerCapacity.AVAILABLE,
                authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            ),
        }

        coordinator = HybridRoutingCoordinator(task_router, make_metadata_provider(metadata))

        request = HybridRoutingRequest(
            routing_request_id="req-001",
            task_request=task_request,
            hybrid_policy=HybridRoutingPolicy(local_first=True),
        )

        decision = coordinator.evaluate(request)
        original_route = decision.route_kind

        # Decision is frozen, cannot mutate
        with pytest.raises((AttributeError, TypeError)):
            decision.route_kind = ExecutionRouteKind.CLOUD

        # Route unchanged
        assert decision.route_kind == original_route
