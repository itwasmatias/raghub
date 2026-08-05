"""Tests for NodeRegistry."""

import time
from datetime import datetime, timedelta, timezone

import pytest

from federation.capability import KnownCapability, NodeCapability
from federation.node_record import NodeRecord, NodeStatus
from federation.registry import NodeRegistry


@pytest.fixture
def registry():
    """Create a fresh NodeRegistry for each test."""
    return NodeRegistry(stale_threshold_seconds=300)


@pytest.fixture
def fedora_node():
    """Create a Fedora node with container runtime capability."""
    caps = {
        NodeCapability.from_known(KnownCapability.CONTAINER_RUNTIME),
        NodeCapability.from_known(KnownCapability.LINUX_SERVICES),
        NodeCapability.from_known(KnownCapability.PYTHON_EXECUTION),
    }
    return NodeRecord(
        node_id="fedora-1",
        hostname="hp-pavilion",
        operating_system="Fedora 44",
        capabilities=caps,
    )


@pytest.fixture
def windows_node():
    """Create a Windows node with desktop capability."""
    caps = {
        NodeCapability.from_known(KnownCapability.WINDOWS_DESKTOP),
        NodeCapability.from_known(KnownCapability.PYTHON_EXECUTION),
    }
    return NodeRecord(
        node_id="windows-1",
        hostname="hp-14",
        operating_system="Windows 11",
        capabilities=caps,
    )


def test_registry_register_node(registry, fedora_node):
    """Test registering a node."""
    registry.register(fedora_node)

    assert registry.count() == 1
    node = registry.get("fedora-1")
    assert node is not None
    assert node.node_id == "fedora-1"


def test_registry_register_multiple_nodes(registry, fedora_node, windows_node):
    """Test registering multiple nodes."""
    registry.register(fedora_node)
    registry.register(windows_node)

    assert registry.count() == 2


def test_registry_duplicate_registration_raises_without_replacing_record(
    registry,
    fedora_node,
):
    """A duplicate ID fails and leaves the registered record unchanged."""
    registry.register(fedora_node)
    original_last_seen = fedora_node.last_seen
    duplicate = NodeRecord(
        node_id=fedora_node.node_id,
        hostname="duplicate-host",
        operating_system="Fedora 45",
        capabilities={NodeCapability(name="gpu_available")},
    )

    with pytest.raises(ValueError, match=r"fedora-1"):
        registry.register(duplicate)

    assert registry.count() == 1
    assert registry.get(fedora_node.node_id) is fedora_node
    assert fedora_node.hostname == "hp-pavilion"
    assert fedora_node.operating_system == "Fedora 44"
    assert fedora_node.last_seen == original_last_seen


def test_registry_get_node(registry, fedora_node):
    """Test retrieving a node by ID."""
    registry.register(fedora_node)

    node = registry.get("fedora-1")
    assert node is not None
    assert node.node_id == "fedora-1"


def test_registry_get_nonexistent_node(registry):
    """Test retrieving a node that doesn't exist."""
    node = registry.get("nonexistent")
    assert node is None


def test_registry_remove_node(registry, fedora_node):
    """Test removing a node."""
    registry.register(fedora_node)
    assert registry.count() == 1

    removed = registry.remove("fedora-1")
    assert removed is True
    assert registry.count() == 0
    assert registry.get("fedora-1") is None


def test_registry_remove_nonexistent_node(registry):
    """Test removing a node that doesn't exist."""
    removed = registry.remove("nonexistent")
    assert removed is False


def test_registry_update_node(registry, fedora_node):
    """Test updating a node's properties."""
    registry.register(fedora_node)

    updated = registry.update("fedora-1", hostname="new-hostname")
    assert updated is not None
    assert updated.hostname == "new-hostname"

    node = registry.get("fedora-1")
    assert node.hostname == "new-hostname"


def test_registry_update_nonexistent_node(registry):
    """Test updating a node that doesn't exist."""
    updated = registry.update("nonexistent", hostname="test")
    assert updated is None


def test_registry_update_validates_and_normalizes_fields(registry, fedora_node):
    """Updates preserve the same invariants as NodeRecord construction."""
    registry.register(fedora_node)

    updated = registry.update(
        "fedora-1",
        status="offline",
        capabilities=[NodeCapability(name=" GPU_AVAILABLE ")],
    )

    assert updated.status is NodeStatus.OFFLINE
    assert updated.capabilities == {NodeCapability(name="gpu_available")}


def test_registry_update_is_atomic_on_invalid_value(registry, fedora_node):
    """A failed update does not partially mutate the registered node."""
    registry.register(fedora_node)
    original_hostname = fedora_node.hostname
    original_last_seen = fedora_node.last_seen

    with pytest.raises(ValueError, match="status"):
        registry.update("fedora-1", hostname="changed", status="invalid")

    assert fedora_node.hostname == original_hostname
    assert fedora_node.status is NodeStatus.ONLINE
    assert fedora_node.last_seen == original_last_seen


@pytest.mark.parametrize("field", ["last_seen", "unknown"])
def test_registry_update_rejects_unsupported_fields(registry, fedora_node, field):
    """Registry identity and heartbeat fields have dedicated operations."""
    registry.register(fedora_node)

    with pytest.raises(ValueError, match="Unsupported node update field"):
        registry.update("fedora-1", **{field: "value"})

    assert registry.get("fedora-1") is fedora_node


def test_registry_list_nodes(registry, fedora_node, windows_node):
    """Test listing all nodes."""
    registry.register(fedora_node)
    registry.register(windows_node)

    nodes = registry.list_nodes()
    assert len(nodes) == 2


def test_registry_list_nodes_by_status(registry, fedora_node, windows_node):
    """Test listing nodes filtered by status."""
    registry.register(fedora_node)
    registry.register(windows_node)

    fedora_node.mark_offline()

    online_nodes = registry.list_nodes(status_filter=NodeStatus.ONLINE)
    assert len(online_nodes) == 1
    assert online_nodes[0].node_id == "windows-1"

    offline_nodes = registry.list_nodes(status_filter=NodeStatus.OFFLINE)
    assert len(offline_nodes) == 1
    assert offline_nodes[0].node_id == "fedora-1"


def test_registry_find_nodes_with_capability_container_runtime(registry, fedora_node, windows_node):
    """Test the primary requirement: finding nodes with container_runtime capability."""
    registry.register(fedora_node)
    registry.register(windows_node)

    # This is the key test case from the requirements
    nodes = registry.find_nodes_with_capability(" CONTAINER_RUNTIME ")

    assert len(nodes) == 1
    assert nodes[0].node_id == "fedora-1"
    assert nodes[0].hostname == "hp-pavilion"


def test_registry_find_nodes_with_capability_by_string(registry, fedora_node, windows_node):
    """Test finding nodes by string capability name."""
    registry.register(fedora_node)
    registry.register(windows_node)

    python_nodes = registry.find_nodes_with_capability("python_execution")
    assert len(python_nodes) == 2  # Both nodes have python

    windows_nodes = registry.find_nodes_with_capability("windows_desktop")
    assert len(windows_nodes) == 1
    assert windows_nodes[0].node_id == "windows-1"


def test_registry_find_nodes_with_capability_by_object(registry, fedora_node, windows_node):
    """Test finding nodes by NodeCapability object."""
    registry.register(fedora_node)
    registry.register(windows_node)

    cap = NodeCapability.from_known(KnownCapability.CONTAINER_RUNTIME)
    nodes = registry.find_nodes_with_capability(cap)

    assert len(nodes) == 1
    assert nodes[0].node_id == "fedora-1"


def test_registry_find_nodes_filters_by_status(registry, fedora_node, windows_node):
    """Test that find_nodes_with_capability respects status filter."""
    registry.register(fedora_node)
    registry.register(windows_node)

    fedora_node.mark_offline()

    # Default status_filter is ONLINE
    online_python_nodes = registry.find_nodes_with_capability("python_execution")
    assert len(online_python_nodes) == 1
    assert online_python_nodes[0].node_id == "windows-1"

    # With status_filter=None, should include all statuses
    all_python_nodes = registry.find_nodes_with_capability(
        "python_execution",
        status_filter=None,
    )
    assert len(all_python_nodes) == 2


def test_registry_heartbeat(registry, fedora_node):
    """Test recording a heartbeat."""
    registry.register(fedora_node)
    original_time = fedora_node.last_seen

    time.sleep(0.01)

    success = registry.heartbeat("fedora-1")
    assert success is True
    assert fedora_node.last_seen > original_time


def test_registry_heartbeat_brings_offline_node_online(registry, fedora_node):
    """Test that heartbeat marks offline nodes as online."""
    registry.register(fedora_node)
    fedora_node.mark_offline()

    assert fedora_node.status == NodeStatus.OFFLINE

    registry.heartbeat("fedora-1")
    assert fedora_node.status == NodeStatus.ONLINE


def test_registry_heartbeat_nonexistent_node(registry):
    """Test heartbeat for a node that doesn't exist."""
    success = registry.heartbeat("nonexistent")
    assert success is False


def test_registry_detect_stale_nodes(fedora_node):
    """Test detecting stale nodes."""
    # Create registry with very short stale threshold
    registry = NodeRegistry(stale_threshold_seconds=1)
    registry.register(fedora_node)

    # Node should not be stale immediately
    stale = registry.detect_stale_nodes()
    assert len(stale) == 0

    # Manually set last_seen to past
    fedora_node.last_seen = datetime.now(timezone.utc) - timedelta(seconds=2)

    # Now it should be stale
    stale = registry.detect_stale_nodes()
    assert len(stale) == 1
    assert stale[0].node_id == "fedora-1"


def test_registry_is_stale_public_method(fedora_node):
    """The public stale-node check reports fresh and stale records."""
    registry = NodeRegistry(stale_threshold_seconds=1)
    registry.register(fedora_node)

    assert registry.is_stale(fedora_node) is False

    fedora_node.last_seen = datetime.now(timezone.utc) - timedelta(seconds=2)

    assert registry.is_stale(fedora_node) is True
    assert registry._is_stale(fedora_node) is True


def test_registry_mark_stale_nodes_offline(fedora_node):
    """Test marking stale nodes as offline."""
    registry = NodeRegistry(stale_threshold_seconds=1)
    registry.register(fedora_node)

    # Make node stale
    fedora_node.last_seen = datetime.now(timezone.utc) - timedelta(seconds=2)

    count = registry.mark_stale_nodes_offline()
    assert count == 1
    assert fedora_node.status == NodeStatus.OFFLINE


def test_registry_find_nodes_excludes_stale_by_default(fedora_node, windows_node):
    """Test that find_nodes_with_capability excludes stale nodes by default."""
    registry = NodeRegistry(stale_threshold_seconds=1)
    registry.register(fedora_node)
    registry.register(windows_node)

    # Make fedora node stale
    fedora_node.last_seen = datetime.now(timezone.utc) - timedelta(seconds=2)

    # Should only find windows node (not stale)
    nodes = registry.find_nodes_with_capability("python_execution")
    assert len(nodes) == 1
    assert nodes[0].node_id == "windows-1"

    # With include_stale=True, should find both
    nodes = registry.find_nodes_with_capability(
        "python_execution",
        include_stale=True,
    )
    assert len(nodes) == 2


def test_registry_list_nodes_excludes_stale_when_requested(fedora_node, windows_node):
    """Test that list_nodes can exclude stale nodes."""
    registry = NodeRegistry(stale_threshold_seconds=1)
    registry.register(fedora_node)
    registry.register(windows_node)

    # Make fedora node stale
    fedora_node.last_seen = datetime.now(timezone.utc) - timedelta(seconds=2)

    # Default includes stale
    all_nodes = registry.list_nodes()
    assert len(all_nodes) == 2

    # With include_stale=False
    fresh_nodes = registry.list_nodes(include_stale=False)
    assert len(fresh_nodes) == 1
    assert fresh_nodes[0].node_id == "windows-1"


def test_registry_clear(registry, fedora_node, windows_node):
    """Test clearing all nodes from the registry."""
    registry.register(fedora_node)
    registry.register(windows_node)

    assert registry.count() == 2

    registry.clear()
    assert registry.count() == 0


def test_registry_empty_initially(registry):
    """Test that a new registry starts empty."""
    assert registry.count() == 0
    assert registry.list_nodes() == []


def test_registry_rejects_negative_stale_threshold():
    """A negative threshold would make every node immediately stale."""
    with pytest.raises(ValueError, match="non-negative"):
        NodeRegistry(stale_threshold_seconds=-1)


def test_registry_rejects_naive_timestamp_after_mutation(registry, fedora_node):
    """Stale detection fails clearly if a live record is mutated to naive time."""
    registry.register(fedora_node)
    fedora_node.last_seen = datetime.now()

    with pytest.raises(ValueError, match="timezone-aware"):
        registry.detect_stale_nodes()


def test_registry_find_nodes_no_matches(registry, fedora_node):
    """Test finding nodes when no nodes match."""
    registry.register(fedora_node)

    nodes = registry.find_nodes_with_capability("gpu_available")
    assert len(nodes) == 0


def test_registry_complex_capability_search(registry):
    """Test complex capability-based node discovery."""
    # Create nodes with different capability combinations
    gpu_node = NodeRecord(
        node_id="gpu-1",
        hostname="gpu-server",
        operating_system="Ubuntu 24.04",
        capabilities={
            NodeCapability.from_known(KnownCapability.PYTHON_EXECUTION),
            NodeCapability.from_known(KnownCapability.GPU_AVAILABLE),
            NodeCapability.from_known(KnownCapability.CONTAINER_RUNTIME),
        },
    )

    storage_node = NodeRecord(
        node_id="storage-1",
        hostname="storage-server",
        operating_system="Ubuntu 24.04",
        capabilities={
            NodeCapability.from_known(KnownCapability.PERSISTENT_STORAGE),
            NodeCapability.from_known(KnownCapability.NETWORK_ACCESS),
        },
    )

    registry.register(gpu_node)
    registry.register(storage_node)

    # Find GPU nodes
    gpu_nodes = registry.find_nodes_with_capability("gpu_available")
    assert len(gpu_nodes) == 1
    assert gpu_nodes[0].node_id == "gpu-1"

    # Find storage nodes
    storage_nodes = registry.find_nodes_with_capability("persistent_storage")
    assert len(storage_nodes) == 1
    assert storage_nodes[0].node_id == "storage-1"

    # Find container nodes
    container_nodes = registry.find_nodes_with_capability("container_runtime")
    assert len(container_nodes) == 1
    assert container_nodes[0].node_id == "gpu-1"


def test_registry_maintenance_status(registry, fedora_node):
    """Test nodes in maintenance status."""
    registry.register(fedora_node)
    fedora_node.status = NodeStatus.MAINTENANCE

    # Should not be returned by default (ONLINE filter)
    nodes = registry.find_nodes_with_capability("container_runtime")
    assert len(nodes) == 0

    # Should be returned with no status filter
    nodes = registry.find_nodes_with_capability(
        "container_runtime",
        status_filter=None,
    )
    assert len(nodes) == 1

    # Should be returned with explicit maintenance filter
    nodes = registry.find_nodes_with_capability(
        "container_runtime",
        status_filter=NodeStatus.MAINTENANCE,
    )
    assert len(nodes) == 1
