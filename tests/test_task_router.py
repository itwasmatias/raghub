"""Tests for TaskRouter."""

from datetime import datetime, timedelta, timezone

import pytest

from federation import (
    NodeCapability,
    NodeRecord,
    NodeRegistry,
    NodeStatus,
    RoutingOutcome,
    TaskRequest,
    TaskRouter,
)


@pytest.fixture
def registry():
    """Create a fresh NodeRegistry for each test."""
    return NodeRegistry(stale_threshold_seconds=300)


@pytest.fixture
def router(registry):
    """Create a TaskRouter with the registry."""
    return TaskRouter(registry)


@pytest.fixture
def python_node():
    """Create a node with Python execution capability."""
    return NodeRecord(
        node_id="python-1",
        hostname="python-host",
        operating_system="Linux",
        capabilities={NodeCapability("python_execution")},
    )


@pytest.fixture
def gpu_node():
    """Create a node with GPU capability."""
    return NodeRecord(
        node_id="gpu-1",
        hostname="gpu-host",
        operating_system="Linux",
        capabilities={
            NodeCapability("python_execution"),
            NodeCapability("gpu_available"),
        },
    )


@pytest.fixture
def storage_node():
    """Create a node with storage capability."""
    return NodeRecord(
        node_id="storage-1",
        hostname="storage-host",
        operating_system="Linux",
        capabilities={
            NodeCapability("python_execution"),
            NodeCapability("persistent_storage"),
            NodeCapability("network_access"),
        },
    )


def test_task_router_creation(registry):
    """Test creating a task router."""
    router = TaskRouter(registry)
    assert router._registry is registry


def test_task_router_requires_registry():
    """Test that registry is required."""
    with pytest.raises(TypeError, match="registry"):
        TaskRouter("not a registry")


def test_task_router_simple_routing(router, registry, python_node):
    """Test routing a simple task to a node."""
    registry.register(python_node)

    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={NodeCapability("python_execution")},
    )

    decision = router.route(request)

    assert decision.outcome == RoutingOutcome.SUCCESS
    assert decision.is_success is True
    assert decision.assignment is not None
    assert decision.assigned_node_id == "python-1"
    assert len(decision.required_capabilities_matched) == 1
    assert len(decision.preferred_capabilities_matched) == 0


def test_task_router_no_nodes_available(router):
    """Test routing when no nodes are available."""
    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={NodeCapability("python_execution")},
    )

    decision = router.route(request)

    assert decision.outcome == RoutingOutcome.NO_ELIGIBLE_NODES
    assert decision.is_success is False
    assert decision.assignment is None
    assert decision.assigned_node_id is None


def test_task_router_missing_required_capability(router, registry, python_node):
    """Test routing when node is missing required capability."""
    registry.register(python_node)

    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={NodeCapability("gpu_available")},
    )

    decision = router.route(request)

    assert decision.outcome == RoutingOutcome.NO_ELIGIBLE_NODES
    assert len(decision.excluded_nodes) == 1
    assert decision.excluded_nodes[0].node_id == "python-1"
    assert "Missing required capabilities" in decision.excluded_nodes[0].reason
    assert "gpu_available" in decision.excluded_nodes[0].reason


def test_task_router_excludes_offline_nodes(router, registry, python_node):
    """Test that offline nodes are excluded."""
    registry.register(python_node)
    python_node.mark_offline()

    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={NodeCapability("python_execution")},
    )

    decision = router.route(request)

    assert decision.outcome == RoutingOutcome.NO_ELIGIBLE_NODES
    assert len(decision.excluded_nodes) == 1
    assert decision.excluded_nodes[0].node_id == "python-1"
    assert "offline" in decision.excluded_nodes[0].reason


def test_task_router_excludes_stale_nodes(router, registry, python_node):
    """Test that stale nodes are excluded."""
    registry.register(python_node)
    # Make node stale
    python_node.last_seen = datetime.now(timezone.utc) - timedelta(seconds=400)

    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={NodeCapability("python_execution")},
    )

    decision = router.route(request)

    assert decision.outcome == RoutingOutcome.NO_ELIGIBLE_NODES
    assert len(decision.excluded_nodes) == 1
    assert decision.excluded_nodes[0].node_id == "python-1"
    assert "stale" in decision.excluded_nodes[0].reason


def test_task_router_preferred_capabilities_improve_ranking(
    router,
    registry,
    python_node,
    gpu_node,
):
    """Test that preferred capabilities improve node ranking."""
    registry.register(python_node)
    registry.register(gpu_node)

    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={NodeCapability("python_execution")},
        preferred_capabilities={NodeCapability("gpu_available")},
    )

    decision = router.route(request)

    # Should select gpu_node because it has the preferred capability
    assert decision.outcome == RoutingOutcome.SUCCESS
    assert decision.assigned_node_id == "gpu-1"
    assert len(decision.required_capabilities_matched) == 1
    assert len(decision.preferred_capabilities_matched) == 1
    assert NodeCapability("gpu_available") in decision.preferred_capabilities_matched


def test_task_router_preferred_capabilities_not_mandatory(
    router,
    registry,
    python_node,
):
    """Test that preferred capabilities are not mandatory."""
    registry.register(python_node)

    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={NodeCapability("python_execution")},
        preferred_capabilities={NodeCapability("gpu_available")},
    )

    decision = router.route(request)

    # Should still route to python_node even though it lacks preferred capability
    assert decision.outcome == RoutingOutcome.SUCCESS
    assert decision.assigned_node_id == "python-1"
    assert len(decision.required_capabilities_matched) == 1
    assert len(decision.preferred_capabilities_matched) == 0


def test_task_router_deterministic_ranking_by_node_id(router, registry):
    """Test that routing is deterministic using node_id for tie-breaking."""
    # Create two identical nodes (same capabilities)
    node_a = NodeRecord(
        node_id="a-node",
        hostname="host-a",
        operating_system="Linux",
        capabilities={NodeCapability("python_execution")},
    )
    node_b = NodeRecord(
        node_id="b-node",
        hostname="host-b",
        operating_system="Linux",
        capabilities={NodeCapability("python_execution")},
    )

    registry.register(node_a)
    registry.register(node_b)

    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={NodeCapability("python_execution")},
    )

    # Route multiple times - should always get the same result
    decisions = [router.route(request) for _ in range(5)]

    # All decisions should route to the same node (deterministic)
    node_ids = [d.assigned_node_id for d in decisions]
    assert len(set(node_ids)) == 1

    # Should select "a-node" because it's lexicographically first
    assert decisions[0].assigned_node_id == "a-node"


def test_task_router_is_independent_of_registration_order():
    """Routing and exclusions are stable across registration orders."""
    capabilities_by_node = {
        "a-eligible": {"python_execution"},
        "b-eligible": {"python_execution"},
        "m-missing": {"network_access"},
        "z-offline": {"python_execution"},
    }

    def route_with_order(node_ids):
        registry = NodeRegistry(stale_threshold_seconds=300)
        for node_id in node_ids:
            node = NodeRecord(
                node_id=node_id,
                hostname=f"{node_id}-host",
                operating_system="Linux",
                capabilities={
                    NodeCapability(capability)
                    for capability in capabilities_by_node[node_id]
                },
            )
            registry.register(node)
            if node_id == "z-offline":
                node.mark_offline()

        request = TaskRequest(
            task_id="task-1",
            mission_id="mission-1",
            required_capabilities={NodeCapability("python_execution")},
        )
        return TaskRouter(registry).route(request)

    node_ids = list(capabilities_by_node)
    forward = route_with_order(node_ids)
    reverse = route_with_order(reversed(node_ids))

    assert forward.assigned_node_id == reverse.assigned_node_id == "a-eligible"
    assert [
        (node.node_id, node.reason) for node in forward.excluded_nodes
    ] == [
        (node.node_id, node.reason) for node in reverse.excluded_nodes
    ] == [
        ("m-missing", "Missing required capabilities: python_execution"),
        ("z-offline", "Node status is offline, not online"),
    ]


def test_task_router_multiple_required_capabilities(
    router,
    registry,
    gpu_node,
    storage_node,
):
    """Test routing with multiple required capabilities."""
    registry.register(gpu_node)
    registry.register(storage_node)

    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={
            NodeCapability("python_execution"),
            NodeCapability("network_access"),
            NodeCapability("persistent_storage"),
        },
    )

    decision = router.route(request)

    # Should select storage_node which has all required capabilities
    assert decision.outcome == RoutingOutcome.SUCCESS
    assert decision.assigned_node_id == "storage-1"
    assert len(decision.required_capabilities_matched) == 3


def test_task_router_complex_scenario_with_exclusions(
    router,
    registry,
    python_node,
    gpu_node,
    storage_node,
):
    """Test complex routing scenario with various exclusions."""
    registry.register(python_node)
    registry.register(gpu_node)
    registry.register(storage_node)

    # Mark python_node as offline
    python_node.mark_offline()

    # Make gpu_node stale
    gpu_node.last_seen = datetime.now(timezone.utc) - timedelta(seconds=400)

    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={NodeCapability("python_execution")},
    )

    decision = router.route(request)

    # Should select storage_node (only eligible one)
    assert decision.outcome == RoutingOutcome.SUCCESS
    assert decision.assigned_node_id == "storage-1"

    # Should have 2 excluded nodes
    assert len(decision.excluded_nodes) == 2
    excluded_ids = {n.node_id for n in decision.excluded_nodes}
    assert "python-1" in excluded_ids
    assert "gpu-1" in excluded_ids


def test_task_router_explanation_is_informative(router, registry, python_node):
    """Test that routing decision includes informative explanation."""
    registry.register(python_node)

    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={NodeCapability("python_execution")},
    )

    decision = router.route(request)

    assert decision.explanation != ""
    assert "task-1" in decision.explanation
    assert "python-1" in decision.explanation


def test_task_router_no_eligible_nodes_explanation(router, registry, python_node):
    """Test explanation when no eligible nodes are found."""
    registry.register(python_node)
    python_node.mark_offline()

    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={NodeCapability("python_execution")},
    )

    decision = router.route(request)

    assert "No eligible nodes" in decision.explanation
    assert "task-1" in decision.explanation
    assert "1 nodes were excluded" in decision.explanation


def test_task_router_requires_task_request(router):
    """Test that route requires a TaskRequest."""
    with pytest.raises(TypeError, match="task_request"):
        router.route("not a task request")


def test_task_router_maintenance_status_excluded(router, registry, python_node):
    """Test that nodes in maintenance status are excluded."""
    registry.register(python_node)
    python_node.status = NodeStatus.MAINTENANCE

    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={NodeCapability("python_execution")},
    )

    decision = router.route(request)

    assert decision.outcome == RoutingOutcome.NO_ELIGIBLE_NODES
    assert len(decision.excluded_nodes) == 1
    assert "maintenance" in decision.excluded_nodes[0].reason


def test_task_router_degraded_status_excluded(router, registry, python_node):
    """Test that nodes in degraded status are excluded."""
    registry.register(python_node)
    python_node.status = NodeStatus.DEGRADED

    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={NodeCapability("python_execution")},
    )

    decision = router.route(request)

    assert decision.outcome == RoutingOutcome.NO_ELIGIBLE_NODES
    assert len(decision.excluded_nodes) == 1
    assert "degraded" in decision.excluded_nodes[0].reason


def test_task_router_personal_mission_inbox_cleanup(router, registry):
    """Test routing a personal mission like Inbox Cleanup."""
    # Create a node suitable for inbox cleanup
    inbox_node = NodeRecord(
        node_id="personal-assistant",
        hostname="laptop",
        operating_system="Fedora 44",
        capabilities={
            NodeCapability("python_execution"),
            NodeCapability("network_access"),
            NodeCapability("persistent_storage"),
        },
    )
    registry.register(inbox_node)

    request = TaskRequest(
        task_id="inbox-cleanup-001",
        mission_id="personal-inbox-management",
        required_capabilities={
            NodeCapability("python_execution"),
            NodeCapability("network_access"),
        },
        preferred_capabilities={
            NodeCapability("persistent_storage"),
        },
    )

    decision = router.route(request)

    assert decision.outcome == RoutingOutcome.SUCCESS
    assert decision.assigned_node_id == "personal-assistant"
    assert len(decision.required_capabilities_matched) == 2
    assert len(decision.preferred_capabilities_matched) == 1


def test_task_router_empty_required_capabilities(router, registry, python_node):
    """Test routing with no required capabilities (any node works)."""
    registry.register(python_node)

    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities=set(),
    )

    decision = router.route(request)

    assert decision.outcome == RoutingOutcome.SUCCESS
    assert decision.assigned_node_id == "python-1"
    assert len(decision.required_capabilities_matched) == 0


def test_task_router_multiple_preferred_matches(router, registry, storage_node):
    """Test routing with multiple preferred capability matches."""
    registry.register(storage_node)

    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={NodeCapability("python_execution")},
        preferred_capabilities={
            NodeCapability("network_access"),
            NodeCapability("persistent_storage"),
            NodeCapability("gpu_available"),  # This one is missing
        },
    )

    decision = router.route(request)

    assert decision.outcome == RoutingOutcome.SUCCESS
    assert decision.assigned_node_id == "storage-1"
    assert len(decision.required_capabilities_matched) == 1
    assert len(decision.preferred_capabilities_matched) == 2
    assert NodeCapability("network_access") in decision.preferred_capabilities_matched
    assert (
        NodeCapability("persistent_storage") in decision.preferred_capabilities_matched
    )
    assert NodeCapability("gpu_available") not in decision.preferred_capabilities_matched


def test_task_router_ranking_prefers_more_preferred_matches(router, registry):
    """Test that nodes with more preferred matches rank higher."""
    # Node with 1 preferred match
    node_1_pref = NodeRecord(
        node_id="node-1-pref",
        hostname="host-1",
        operating_system="Linux",
        capabilities={
            NodeCapability("python_execution"),
            NodeCapability("network_access"),
        },
    )

    # Node with 2 preferred matches
    node_2_pref = NodeRecord(
        node_id="node-2-pref",
        hostname="host-2",
        operating_system="Linux",
        capabilities={
            NodeCapability("python_execution"),
            NodeCapability("network_access"),
            NodeCapability("persistent_storage"),
        },
    )

    registry.register(node_1_pref)
    registry.register(node_2_pref)

    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={NodeCapability("python_execution")},
        preferred_capabilities={
            NodeCapability("network_access"),
            NodeCapability("persistent_storage"),
        },
    )

    decision = router.route(request)

    # Should select node_2_pref because it has 2 preferred matches vs 1
    assert decision.assigned_node_id == "node-2-pref"
    assert len(decision.preferred_capabilities_matched) == 2
