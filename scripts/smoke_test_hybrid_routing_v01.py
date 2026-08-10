#!/usr/bin/env python3
"""Fedora smoke test for Hybrid Routing + Credit Resilience v0.1.

Demonstrates the four key scenarios on real Fedora without live cloud API calls.

Scenario A: Local-first with valid local capability
Scenario B: Cloud credits unavailable → LOCAL_CONTINUITY
Scenario C: Cloud provider unavailable → LOCAL_CONTINUITY
Scenario D: No eligible route (local insufficient, cloud unavailable)

v0.1 Note: Uses simplified routing without full TaskRouter+HeartbeatRegistry
integration, which is documented as a v0.1 limitation.
"""

import sys
from federation import (
    AuthorizationLevel,
    ExecutionLocality,
    ExecutionRouteKind,
    HybridRoutingCoordinator,
    HybridRoutingPolicy,
    HybridRoutingReason,
    HybridRoutingRequest,
    KnownCapability,
    NodeCapability,
    NodeRecord,
    NodeRegistry,
    NodeStatus,
    TaskRequest,
    TaskRouter,
    WorkerAvailability,
    WorkerCapacity,
    WorkerCostClass,
    WorkerProviderMetadata,
)
from federation.heartbeat_registry import HeartbeatRegistry
from pathlib import Path
import tempfile


def scenario_a_local_first():
    """Scenario A: Local-first with valid local capability."""
    print("\n=== Scenario A: Local-First with Valid Local Capability ===")

    # Setup minimal infrastructure
    registry = NodeRegistry(stale_threshold_seconds=300)
    local_node = NodeRecord(
        node_id="fedora-local-worker",
        hostname="localhost",
        operating_system="Linux",
        status=NodeStatus.ONLINE,
        capabilities={NodeCapability(KnownCapability.PYTHON_EXECUTION.value)},
    )
    registry.register(local_node)

    # Create metadata provider showing available local worker
    metadata = {
        "fedora-local-worker": WorkerProviderMetadata(
            node_id="fedora-local-worker",
            locality=ExecutionLocality.LOCAL,
            cost_class=WorkerCostClass.FREE,
            availability=WorkerAvailability.AVAILABLE,
            capacity=WorkerCapacity.AVAILABLE,
            authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
        ),
    }

    # Create task router (minimal for v0.1)
    with tempfile.TemporaryDirectory() as tmpdir:
        heartbeat_registry = HeartbeatRegistry(
            Path(tmpdir) / "heartbeats.jsonl",
            registry_id="smoke-test-registry",
            node_registry=registry,
            integrity_key=b"smoke-test-integrity-key-32bytes",
        )
        task_router = TaskRouter(registry=registry, heartbeat_registry=heartbeat_registry)

        # Create hybrid routing coordinator
        coordinator = HybridRoutingCoordinator(
            task_router=task_router,
            metadata_provider=lambda: metadata.copy(),
        )

        # Create routing policy: local-first, cloud allowed
        policy = HybridRoutingPolicy(
            local_first=True,
            cloud_allowed=True,
        )

        # Create task request
        task_request = TaskRequest(
            task_id="smoke-task-a",
            mission_id="smoke-mission",
            required_capabilities={NodeCapability(KnownCapability.PYTHON_EXECUTION.value)},
            authorization_level=AuthorizationLevel.INTERNAL,
        )

        # Create routing request
        routing_request = HybridRoutingRequest(
            routing_request_id="smoke-req-a",
            task_request=task_request,
            hybrid_policy=policy,
        )

        # Evaluate routing decision
        decision = coordinator.evaluate(routing_request)

        # Verify result
        print(f"Route Kind: {decision.route_kind}")
        print(f"Execution Locality: {decision.execution_locality}")
        print(f"Assigned Node: {decision.assigned_node_id}")
        print(f"Can Execute: {decision.can_execute}")
        print(f"Primary Reasons: {[r.value for r in decision.primary_reasons]}")

        assert decision.route_kind == ExecutionRouteKind.LOCAL, "Expected LOCAL route"
        assert HybridRoutingReason.LOCAL_FIRST_POLICY in decision.primary_reasons
        print("✓ Scenario A passed: Local-first policy selected local route")


def scenario_b_cloud_credits_unavailable():
    """Scenario B: Cloud credits unavailable → LOCAL_CONTINUITY."""
    print("\n=== Scenario B: Cloud Credits Unavailable → LOCAL_CONTINUITY ===")

    registry = NodeRegistry(stale_threshold_seconds=300)
    local_node = NodeRecord(
        node_id="fedora-local-worker",
        hostname="localhost",
        operating_system="Linux",
        status=NodeStatus.ONLINE,
        capabilities={NodeCapability(KnownCapability.PYTHON_EXECUTION.value)},
    )
    cloud_node = NodeRecord(
        node_id="cloud-worker",
        hostname="cloud.example.com",
        operating_system="Linux",
        status=NodeStatus.ONLINE,
        capabilities={NodeCapability(KnownCapability.PYTHON_EXECUTION.value)},
    )
    registry.register(local_node)
    registry.register(cloud_node)

    # Metadata showing cloud with exhausted budget
    metadata = {
        "fedora-local-worker": WorkerProviderMetadata(
            node_id="fedora-local-worker",
            locality=ExecutionLocality.LOCAL,
            cost_class=WorkerCostClass.FREE,
            availability=WorkerAvailability.AVAILABLE,
            capacity=WorkerCapacity.AVAILABLE,
            authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
        ),
        "cloud-worker": WorkerProviderMetadata(
            node_id="cloud-worker",
            locality=ExecutionLocality.CLOUD,
            cost_class=WorkerCostClass.STANDARD,
            availability=WorkerAvailability.AVAILABLE,
            capacity=WorkerCapacity.AVAILABLE,
            authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            provider_id="openai",
            budget_exhausted=True,  # Credits unavailable!
        ),
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        heartbeat_registry = HeartbeatRegistry(
            Path(tmpdir) / "heartbeats.jsonl",
            registry_id="smoke-test-registry",
            node_registry=registry,
            integrity_key=b"smoke-test-integrity-key-32bytes",
        )
        task_router = TaskRouter(registry=registry, heartbeat_registry=heartbeat_registry)

        coordinator = HybridRoutingCoordinator(
            task_router=task_router,
            metadata_provider=lambda: metadata.copy(),
        )

        # Policy: prefer cloud (local_first=False), allow cloud
        policy = HybridRoutingPolicy(
            local_first=False,  # Would prefer cloud
            cloud_allowed=True,
            continue_locally_when_cloud_unavailable=True,
        )

        task_request = TaskRequest(
            task_id="smoke-task-b",
            mission_id="smoke-mission",
            required_capabilities={NodeCapability(KnownCapability.PYTHON_EXECUTION.value)},
            authorization_level=AuthorizationLevel.INTERNAL,
        )

        routing_request = HybridRoutingRequest(
            routing_request_id="smoke-req-b",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(routing_request)

        print(f"Route Kind: {decision.route_kind}")
        print(f"Execution Locality: {decision.execution_locality}")
        print(f"Assigned Node: {decision.assigned_node_id}")
        print(f"Can Execute: {decision.can_execute}")
        print(f"Primary Reasons: {[r.value for r in decision.primary_reasons]}")
        print(f"Cloud Eligible: {decision.cloud_eligibility.eligible}")
        print(f"Cloud Rejection Reasons: {decision.cloud_eligibility.reasons}")

        assert decision.route_kind == ExecutionRouteKind.LOCAL_CONTINUITY, "Expected LOCAL_CONTINUITY"
        assert HybridRoutingReason.LOCAL_CONTINUITY_BUDGET_UNAVAILABLE in decision.primary_reasons
        assert decision.can_execute, "Should be executable via local continuity"
        print("✓ Scenario B passed: Cloud credits unavailable, continued locally")


def scenario_c_cloud_provider_unavailable():
    """Scenario C: Cloud provider unavailable → LOCAL_CONTINUITY."""
    print("\n=== Scenario C: Cloud Provider Unavailable → LOCAL_CONTINUITY ===")

    registry = NodeRegistry(stale_threshold_seconds=300)
    local_node = NodeRecord(
        node_id="fedora-local-worker",
        hostname="localhost",
        operating_system="Linux",
        status=NodeStatus.ONLINE,
        capabilities={NodeCapability(KnownCapability.PYTHON_EXECUTION.value)},
    )
    cloud_node = NodeRecord(
        node_id="cloud-worker",
        hostname="cloud.example.com",
        operating_system="Linux",
        status=NodeStatus.OFFLINE,  # Provider down
        capabilities={NodeCapability(KnownCapability.PYTHON_EXECUTION.value)},
    )
    registry.register(local_node)
    registry.register(cloud_node)

    # Metadata showing cloud provider unavailable
    metadata = {
        "fedora-local-worker": WorkerProviderMetadata(
            node_id="fedora-local-worker",
            locality=ExecutionLocality.LOCAL,
            cost_class=WorkerCostClass.FREE,
            availability=WorkerAvailability.AVAILABLE,
            capacity=WorkerCapacity.AVAILABLE,
            authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
        ),
        "cloud-worker": WorkerProviderMetadata(
            node_id="cloud-worker",
            locality=ExecutionLocality.CLOUD,
            cost_class=WorkerCostClass.STANDARD,
            availability=WorkerAvailability.UNAVAILABLE,  # Provider down!
            capacity=WorkerCapacity.UNKNOWN,
            authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            provider_id="openai",
        ),
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        heartbeat_registry = HeartbeatRegistry(
            Path(tmpdir) / "heartbeats.jsonl",
            registry_id="smoke-test-registry",
            node_registry=registry,
            integrity_key=b"smoke-test-integrity-key-32bytes",
        )
        task_router = TaskRouter(registry=registry, heartbeat_registry=heartbeat_registry)

        coordinator = HybridRoutingCoordinator(
            task_router=task_router,
            metadata_provider=lambda: metadata.copy(),
        )

        policy = HybridRoutingPolicy(
            local_first=False,  # Would prefer cloud
            cloud_allowed=True,
            continue_locally_when_cloud_unavailable=True,
        )

        task_request = TaskRequest(
            task_id="smoke-task-c",
            mission_id="smoke-mission",
            required_capabilities={NodeCapability(KnownCapability.PYTHON_EXECUTION.value)},
            authorization_level=AuthorizationLevel.INTERNAL,
        )

        routing_request = HybridRoutingRequest(
            routing_request_id="smoke-req-c",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(routing_request)

        print(f"Route Kind: {decision.route_kind}")
        print(f"Execution Locality: {decision.execution_locality}")
        print(f"Assigned Node: {decision.assigned_node_id}")
        print(f"Can Execute: {decision.can_execute}")
        print(f"Primary Reasons: {[r.value for r in decision.primary_reasons]}")

        assert decision.route_kind == ExecutionRouteKind.LOCAL_CONTINUITY, "Expected LOCAL_CONTINUITY"
        assert HybridRoutingReason.LOCAL_CONTINUITY_PROVIDER_UNAVAILABLE in decision.primary_reasons
        assert decision.can_execute, "Should be executable via local continuity"
        print("✓ Scenario C passed: Cloud provider unavailable, continued locally")


def scenario_d_no_eligible_route():
    """Scenario D: No eligible route (local insufficient, cloud unavailable)."""
    print("\n=== Scenario D: No Eligible Route (local insufficient, cloud unavailable) ===")

    registry = NodeRegistry(stale_threshold_seconds=300)
    local_node = NodeRecord(
        node_id="fedora-local-worker",
        hostname="localhost",
        operating_system="Linux",
        status=NodeStatus.ONLINE,
        capabilities={NodeCapability(KnownCapability.PYTHON_EXECUTION.value)},  # Only Python, no GPU
    )
    cloud_node = NodeRecord(
        node_id="cloud-worker",
        hostname="cloud.example.com",
        operating_system="Linux",
        status=NodeStatus.OFFLINE,
        capabilities={NodeCapability(KnownCapability.GPU_AVAILABLE.value)},
    )
    registry.register(local_node)
    registry.register(cloud_node)

    # Metadata showing local without GPU, cloud unavailable
    metadata = {
        "fedora-local-worker": WorkerProviderMetadata(
            node_id="fedora-local-worker",
            locality=ExecutionLocality.LOCAL,
            cost_class=WorkerCostClass.FREE,
            availability=WorkerAvailability.AVAILABLE,
            capacity=WorkerCapacity.AVAILABLE,
            authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
        ),
        "cloud-worker": WorkerProviderMetadata(
            node_id="cloud-worker",
            locality=ExecutionLocality.CLOUD,
            cost_class=WorkerCostClass.STANDARD,
            availability=WorkerAvailability.UNAVAILABLE,
            capacity=WorkerCapacity.UNKNOWN,
            authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            provider_id="openai",
        ),
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        heartbeat_registry = HeartbeatRegistry(
            Path(tmpdir) / "heartbeats.jsonl",
            registry_id="smoke-test-registry",
            node_registry=registry,
            integrity_key=b"smoke-test-integrity-key-32bytes",
        )
        task_router = TaskRouter(registry=registry, heartbeat_registry=heartbeat_registry)

        coordinator = HybridRoutingCoordinator(
            task_router=task_router,
            metadata_provider=lambda: metadata.copy(),
        )

        policy = HybridRoutingPolicy(
            local_first=False,
            cloud_allowed=True,
        )

        # Task requires GPU capability
        task_request = TaskRequest(
            task_id="smoke-task-d",
            mission_id="smoke-mission",
            required_capabilities={NodeCapability(KnownCapability.GPU_AVAILABLE.value)},
            authorization_level=AuthorizationLevel.INTERNAL,
        )

        routing_request = HybridRoutingRequest(
            routing_request_id="smoke-req-d",
            task_request=task_request,
            hybrid_policy=policy,
        )

        decision = coordinator.evaluate(routing_request)

        print(f"Route Kind: {decision.route_kind}")
        print(f"Assigned Node: {decision.assigned_node_id}")
        print(f"Can Execute: {decision.can_execute}")
        print(f"Primary Reasons: {[r.value for r in decision.primary_reasons]}")
        print(f"Local Eligible: {decision.local_eligibility.eligible}")
        print(f"Cloud Eligible: {decision.cloud_eligibility.eligible}")

        assert decision.route_kind == ExecutionRouteKind.NO_ELIGIBLE_ROUTE, "Expected NO_ELIGIBLE_ROUTE"
        assert not decision.can_execute, "Should not be executable"
        assert not decision.local_eligibility.eligible, "Local should not be eligible (no GPU)"
        assert not decision.cloud_eligibility.eligible, "Cloud should not be eligible (unavailable)"
        print("✓ Scenario D passed: No eligible route (explicit failure with evidence)")


def main():
    """Run all smoke test scenarios."""
    print("Fedora Smoke Test: Hybrid Routing + Credit Resilience v0.1")
    print("=" * 70)
    print("Running on real Fedora (no live cloud API calls)")
    print("v0.1: Architectural prototype with simplified TaskRouter integration")

    try:
        scenario_a_local_first()
        scenario_b_cloud_credits_unavailable()
        scenario_c_cloud_provider_unavailable()
        scenario_d_no_eligible_route()

        print("\n" + "=" * 70)
        print("✓ All smoke test scenarios passed!")
        print("\nKey Results:")
        print("  - Local-first routing works")
        print("  - Cloud credit exhaustion triggers LOCAL_CONTINUITY")
        print("  - Cloud provider outage triggers LOCAL_CONTINUITY")
        print("  - No eligible route produces explicit decision with evidence")
        print("\nNo hidden fallback. No silent failures. RAGHub remains useful.")
        return 0

    except AssertionError as e:
        print(f"\n✗ Smoke test failed: {e}")
        return 1
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
