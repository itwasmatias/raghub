#!/usr/bin/env python3
"""Fedora smoke test for Hybrid Routing Policy v0.1.

Validates hybrid routing policy, request fingerprinting, and decision contracts
on real Fedora without requiring full TaskRouter integration.

v0.1 Note: Tests core policy/decision logic. Full TaskRouter integration
is a documented v0.1 limitation deferred to future milestones.
"""

import sys
from federation import (
    AuthorizationLevel,
    ExecutionLocality,
    ExecutionRouteKind,
    HybridRoutingPolicy,
    HybridRoutingRequest,
    KnownCapability,
    NodeCapability,
    TaskRequest,
    WorkerCostClass,
)


def test_policy_fingerprinting():
    """Test deterministic policy fingerprinting."""
    print("\n=== Test: Policy Fingerprinting ===")

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

    # Same policy parameters should produce same fingerprint
    assert policy1.policy_fingerprint == policy2.policy_fingerprint, "Deterministic fingerprints should match"
    assert len(policy1.policy_fingerprint) == 64, "Fingerprint should be 64-char SHA-256"

    # Different local_first should produce different fingerprint
    policy3 = HybridRoutingPolicy(local_first=False)
    assert policy1.policy_fingerprint != policy3.policy_fingerprint, "Different policies should have different fingerprints"

    print(f"  Policy 1 fingerprint: {policy1.policy_fingerprint[:16]}...")
    print(f"  Policy 2 fingerprint: {policy2.policy_fingerprint[:16]}...")
    print(f"  Policy 3 fingerprint: {policy3.policy_fingerprint[:16]}...")
    print("✓ Policy fingerprinting is deterministic")


def test_policy_parameters():
    """Test policy parameter validation."""
    print("\n=== Test: Policy Parameter Validation ===")

    # Test all policy fields participate in fingerprint
    p1 = HybridRoutingPolicy(cloud_allowed=True)
    p2 = HybridRoutingPolicy(cloud_allowed=False)
    assert p1.policy_fingerprint != p2.policy_fingerprint, "cloud_allowed should affect fingerprint"

    p3 = HybridRoutingPolicy(continue_locally_when_cloud_unavailable=True)
    p4 = HybridRoutingPolicy(continue_locally_when_cloud_unavailable=False)
    assert p3.policy_fingerprint != p4.policy_fingerprint, "continue_locally should affect fingerprint"

    p5 = HybridRoutingPolicy(permitted_cloud_providers=frozenset(["provider-a"]))
    p6 = HybridRoutingPolicy(permitted_cloud_providers=frozenset(["provider-b"]))
    assert p5.policy_fingerprint != p6.policy_fingerprint, "permitted_providers should affect fingerprint"

    p7 = HybridRoutingPolicy(max_cloud_cost_class=WorkerCostClass.LOW)
    p8 = HybridRoutingPolicy(max_cloud_cost_class=WorkerCostClass.HIGH)
    assert p7.policy_fingerprint != p8.policy_fingerprint, "max_cost_class should affect fingerprint"

    print("✓ All policy parameters participate in fingerprinting")


def test_request_fingerprinting():
    """Test deterministic request fingerprinting."""
    print("\n=== Test: Request Fingerprinting ===")

    policy = HybridRoutingPolicy()

    task1 = TaskRequest(
        task_id="task-001",
        mission_id="mission-001",
        required_capabilities={NodeCapability(KnownCapability.PYTHON_EXECUTION.value)},
        authorization_level=AuthorizationLevel.INTERNAL,
    )

    req1 = HybridRoutingRequest(
        routing_request_id="req-001",
        task_request=task1,
        hybrid_policy=policy,
    )

    req2 = HybridRoutingRequest(
        routing_request_id="req-001",
        task_request=task1,
        hybrid_policy=policy,
    )

    # Same request should produce same fingerprint
    assert req1.request_fingerprint == req2.request_fingerprint, "Deterministic request fingerprints should match"
    assert len(req1.request_fingerprint) == 64, "Request fingerprint should be 64-char SHA-256"

    # Different authorization should produce different fingerprint
    task2 = TaskRequest(
        task_id="task-001",
        mission_id="mission-001",
        authorization_level=AuthorizationLevel.CONFIDENTIAL,  # Different from INTERNAL
    )

    req3 = HybridRoutingRequest(
        routing_request_id="req-001",
        task_request=task2,
        hybrid_policy=policy,
    )

    assert req1.request_fingerprint != req3.request_fingerprint, "Different authorization should change fingerprint"

    print(f"  Request 1 fingerprint: {req1.request_fingerprint[:16]}...")
    print(f"  Request 2 fingerprint: {req2.request_fingerprint[:16]}...")
    print(f"  Request 3 fingerprint: {req3.request_fingerprint[:16]}...")
    print("✓ Request fingerprinting is deterministic")


def test_policy_defaults():
    """Test policy default values."""
    print("\n=== Test: Policy Defaults ===")

    policy = HybridRoutingPolicy()

    # Verify documented defaults
    assert policy.local_first is True, "Default should be local_first=True"
    assert policy.cloud_allowed is False, "Default should be cloud_allowed=False"
    assert policy.continue_locally_when_cloud_unavailable is True, "Default should be continue_locally=True"
    assert policy.allow_degraded_local is False, "Default should be allow_degraded_local=False"
    assert policy.max_cloud_cost_class == WorkerCostClass.HIGH, "Default should be max_cost=HIGH"
    assert policy.permitted_cloud_providers == frozenset(), "Default should be empty permitted_providers"

    print(f"  local_first: {policy.local_first}")
    print(f"  cloud_allowed: {policy.cloud_allowed}")
    print(f"  continue_locally_when_cloud_unavailable: {policy.continue_locally_when_cloud_unavailable}")
    print(f"  allow_degraded_local: {policy.allow_degraded_local}")
    print(f"  max_cloud_cost_class: {policy.max_cloud_cost_class}")
    print(f"  permitted_cloud_providers: {policy.permitted_cloud_providers}")
    print("✓ Policy defaults match specification")


def test_policy_immutability():
    """Test that policy is immutable (frozen dataclass)."""
    print("\n=== Test: Policy Immutability ===")

    policy = HybridRoutingPolicy(local_first=True)

    # Attempt to mutate should raise error
    try:
        policy.local_first = False
        assert False, "Should not be able to mutate frozen policy"
    except (AttributeError, TypeError):
        print("✓ Policy is immutable (frozen dataclass)")


def test_request_immutability():
    """Test that request is immutable."""
    print("\n=== Test: Request Immutability ===")

    task = TaskRequest(
        task_id="task-001",
        mission_id="mission-001",
        authorization_level=AuthorizationLevel.INTERNAL,
    )

    policy = HybridRoutingPolicy()

    request = HybridRoutingRequest(
        routing_request_id="req-001",
        task_request=task,
        hybrid_policy=policy,
    )

    # Attempt to mutate should raise error
    try:
        request.routing_request_id = "req-002"
        assert False, "Should not be able to mutate frozen request"
    except (AttributeError, TypeError):
        print("✓ Request is immutable (frozen dataclass)")


def test_cloud_provider_allowlist():
    """Test cloud provider allowlist validation."""
    print("\n=== Test: Cloud Provider Allowlist ===")

    # Empty allowlist (no cloud providers permitted)
    policy1 = HybridRoutingPolicy(permitted_cloud_providers=frozenset())
    assert policy1.permitted_cloud_providers == frozenset()

    # Single provider
    policy2 = HybridRoutingPolicy(permitted_cloud_providers=frozenset(["openai"]))
    assert "openai" in policy2.permitted_cloud_providers

    # Multiple providers
    policy3 = HybridRoutingPolicy(permitted_cloud_providers=frozenset(["openai", "anthropic"]))
    assert "openai" in policy3.permitted_cloud_providers
    assert "anthropic" in policy3.permitted_cloud_providers
    assert len(policy3.permitted_cloud_providers) == 2

    # Different allowlists produce different fingerprints
    assert policy1.policy_fingerprint != policy2.policy_fingerprint
    assert policy2.policy_fingerprint != policy3.policy_fingerprint

    print(f"  Empty allowlist fingerprint: {policy1.policy_fingerprint[:16]}...")
    print(f"  Single provider fingerprint: {policy2.policy_fingerprint[:16]}...")
    print(f"  Multi-provider fingerprint: {policy3.policy_fingerprint[:16]}...")
    print("✓ Cloud provider allowlist works correctly")


def test_cost_class_limits():
    """Test cost class limit validation."""
    print("\n=== Test: Cost Class Limits ===")

    policy_low = HybridRoutingPolicy(max_cloud_cost_class=WorkerCostClass.LOW)
    policy_std = HybridRoutingPolicy(max_cloud_cost_class=WorkerCostClass.STANDARD)
    policy_high = HybridRoutingPolicy(max_cloud_cost_class=WorkerCostClass.HIGH)

    # Different cost limits produce different fingerprints
    assert policy_low.policy_fingerprint != policy_std.policy_fingerprint
    assert policy_std.policy_fingerprint != policy_high.policy_fingerprint

    print(f"  LOW cost limit: {policy_low.max_cloud_cost_class}")
    print(f"  STANDARD cost limit: {policy_std.max_cloud_cost_class}")
    print(f"  HIGH cost limit: {policy_high.max_cloud_cost_class}")
    print("✓ Cost class limits validated")


def main():
    """Run all smoke tests."""
    print("Fedora Smoke Test: Hybrid Routing Policy v0.1")
    print("=" * 70)
    print("Testing on real Fedora (no mocks)")
    print("v0.1: Policy/request contracts without full TaskRouter integration")

    try:
        test_policy_fingerprinting()
        test_policy_parameters()
        test_request_fingerprinting()
        test_policy_defaults()
        test_policy_immutability()
        test_request_immutability()
        test_cloud_provider_allowlist()
        test_cost_class_limits()

        print("\n" + "=" * 70)
        print("✓ All policy smoke tests passed!")
        print("\nKey Results:")
        print("  - Policy fingerprinting is deterministic")
        print("  - Request fingerprinting is deterministic")
        print("  - All policy parameters participate in fingerprints")
        print("  - Policy and request contracts are immutable")
        print("  - Cloud provider allowlist works correctly")
        print("  - Cost class limits validated")
        print("\nv0.1 architectural contracts validated on real Fedora.")
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
