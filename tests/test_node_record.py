"""Tests for NodeRecord model."""

from datetime import datetime, timedelta, timezone

import pytest

from federation.capability import KnownCapability, NodeCapability
from federation.node_record import NodeRecord, NodeStatus


def test_node_record_creation():
    """Test basic NodeRecord creation."""
    node = NodeRecord(domain_id="test-domain", 
        node_id="fedora-1",
        hostname="hp-pavilion",
        operating_system="Fedora 44",
    )

    assert node.node_id == "fedora-1"
    assert node.hostname == "hp-pavilion"
    assert node.operating_system == "Fedora 44"
    assert node.status == NodeStatus.ONLINE
    assert isinstance(node.last_seen, datetime)
    assert node.last_seen.tzinfo is not None
    assert node.last_seen.utcoffset() == timedelta(0)
    assert isinstance(node.capabilities, set)
    assert len(node.capabilities) == 0


def test_node_record_with_capabilities():
    """Test NodeRecord creation with initial capabilities."""
    caps = {
        NodeCapability.from_known(KnownCapability.CONTAINER_RUNTIME),
        NodeCapability.from_known(KnownCapability.LINUX_SERVICES),
    }

    node = NodeRecord(domain_id="test-domain", 
        node_id="fedora-1",
        hostname="hp-pavilion",
        operating_system="Fedora 44",
        capabilities=caps,
    )

    assert len(node.capabilities) == 2
    assert node.has_capability(KnownCapability.CONTAINER_RUNTIME.value)
    assert node.has_capability(KnownCapability.LINUX_SERVICES.value)


def test_node_record_add_capability():
    """Test adding capabilities to a node."""
    node = NodeRecord(domain_id="test-domain", 
        node_id="fedora-1",
        hostname="hp-pavilion",
        operating_system="Fedora 44",
    )

    cap = NodeCapability.from_known(KnownCapability.PYTHON_EXECUTION)
    node.add_capability(cap)

    assert len(node.capabilities) == 1
    assert node.has_capability(cap)
    assert node.has_capability("python_execution")


def test_node_record_remove_capability():
    """Test removing capabilities from a node."""
    cap = NodeCapability.from_known(KnownCapability.PYTHON_EXECUTION)
    node = NodeRecord(domain_id="test-domain", 
        node_id="fedora-1",
        hostname="hp-pavilion",
        operating_system="Fedora 44",
        capabilities={cap},
    )

    assert node.has_capability(cap)
    node.remove_capability(cap)
    assert not node.has_capability(cap)


def test_node_record_has_capability_by_string():
    """Test checking capability by string name."""
    cap = NodeCapability.from_known(KnownCapability.CONTAINER_RUNTIME)
    node = NodeRecord(domain_id="test-domain", 
        node_id="fedora-1",
        hostname="hp-pavilion",
        operating_system="Fedora 44",
        capabilities={cap},
    )

    assert node.has_capability("container_runtime")
    assert node.has_capability(" CONTAINER_RUNTIME ")
    assert not node.has_capability("gpu_available")


def test_node_record_has_capability_by_object():
    """Test checking capability by NodeCapability object."""
    cap1 = NodeCapability.from_known(KnownCapability.CONTAINER_RUNTIME)
    cap2 = NodeCapability.from_known(KnownCapability.GPU_AVAILABLE)

    node = NodeRecord(domain_id="test-domain", 
        node_id="fedora-1",
        hostname="hp-pavilion",
        operating_system="Fedora 44",
        capabilities={cap1},
    )

    assert node.has_capability(cap1)
    assert not node.has_capability(cap2)


def test_node_record_update_heartbeat():
    """Test updating the heartbeat timestamp."""
    node = NodeRecord(domain_id="test-domain", 
        node_id="fedora-1",
        hostname="hp-pavilion",
        operating_system="Fedora 44",
    )

    original_time = node.last_seen
    # Small delay to ensure time difference
    import time
    time.sleep(0.01)

    node.update_heartbeat()
    assert node.last_seen > original_time


def test_node_record_mark_online():
    """Test marking a node as online."""
    node = NodeRecord(domain_id="test-domain", 
        node_id="fedora-1",
        hostname="hp-pavilion",
        operating_system="Fedora 44",
        status=NodeStatus.OFFLINE,
    )

    assert node.status == NodeStatus.OFFLINE
    node.mark_online()
    assert node.status == NodeStatus.ONLINE


def test_node_record_mark_offline():
    """Test marking a node as offline."""
    node = NodeRecord(domain_id="test-domain", 
        node_id="fedora-1",
        hostname="hp-pavilion",
        operating_system="Fedora 44",
        status=NodeStatus.ONLINE,
    )

    assert node.status == NodeStatus.ONLINE
    node.mark_offline()
    assert node.status == NodeStatus.OFFLINE


def test_node_record_is_available():
    """Test checking if a node is available."""
    node = NodeRecord(domain_id="test-domain", 
        node_id="fedora-1",
        hostname="hp-pavilion",
        operating_system="Fedora 44",
        status=NodeStatus.ONLINE,
    )

    assert node.is_available()

    node.status = NodeStatus.OFFLINE
    assert not node.is_available()

    node.status = NodeStatus.MAINTENANCE
    assert not node.is_available()


def test_node_record_validation():
    """Test that invalid node records raise errors."""
    with pytest.raises(ValueError, match="node_id"):
        NodeRecord(domain_id="test-domain", 
            node_id="",
            hostname="hp-pavilion",
            operating_system="Fedora 44",
        )

    with pytest.raises(ValueError, match="hostname"):
        NodeRecord(domain_id="test-domain", 
            node_id="fedora-1",
            hostname="",
            operating_system="Fedora 44",
        )

    with pytest.raises(ValueError, match="operating_system"):
        NodeRecord(domain_id="test-domain", 
            node_id="fedora-1",
            hostname="hp-pavilion",
            operating_system="",
        )


def test_node_record_status_string_conversion():
    """Test that string status values are converted to NodeStatus enum."""
    node = NodeRecord(domain_id="test-domain", 
        node_id="fedora-1",
        hostname="hp-pavilion",
        operating_system="Fedora 44",
        status="offline",
    )

    assert node.status == NodeStatus.OFFLINE
    assert isinstance(node.status, NodeStatus)


def test_node_record_capabilities_list_conversion():
    """Test that list of capabilities is converted to set."""
    caps = [
        NodeCapability.from_known(KnownCapability.CONTAINER_RUNTIME),
        NodeCapability.from_known(KnownCapability.LINUX_SERVICES),
    ]

    node = NodeRecord(domain_id="test-domain", 
        node_id="fedora-1",
        hostname="hp-pavilion",
        operating_system="Fedora 44",
        capabilities=caps,
    )

    assert isinstance(node.capabilities, set)
    assert len(node.capabilities) == 2


def test_node_record_copies_capability_collection():
    """Mutating the caller's set does not change a node's capabilities."""
    cap = NodeCapability(name="python_execution")
    caps = {cap}
    node = NodeRecord(domain_id="test-domain", 
        node_id="fedora-1",
        hostname="hp-pavilion",
        operating_system="Fedora 44",
        capabilities=caps,
    )

    caps.clear()
    assert node.capabilities == {cap}


def test_node_record_rejects_invalid_capabilities():
    """Capability collections contain only NodeCapability values."""
    with pytest.raises(TypeError, match="NodeCapability"):
        NodeRecord(domain_id="test-domain", 
            node_id="fedora-1",
            hostname="hp-pavilion",
            operating_system="Fedora 44",
            capabilities={"python_execution"},
        )


def test_node_record_normalizes_aware_last_seen_to_utc():
    """Aware timestamps are stored in UTC for reliable stale comparisons."""
    eastern = timezone(timedelta(hours=-5))
    node = NodeRecord(domain_id="test-domain", 
        node_id="fedora-1",
        hostname="hp-pavilion",
        operating_system="Fedora 44",
        last_seen=datetime(2026, 8, 5, 9, 30, tzinfo=eastern),
    )

    assert node.last_seen == datetime(2026, 8, 5, 14, 30, tzinfo=timezone.utc)


def test_node_record_rejects_naive_last_seen():
    """Naive timestamps cannot be mixed with UTC heartbeat timestamps."""
    with pytest.raises(ValueError, match="timezone-aware"):
        NodeRecord(domain_id="test-domain", 
            node_id="fedora-1",
            hostname="hp-pavilion",
            operating_system="Fedora 44",
            last_seen=datetime(2026, 8, 5, 14, 30),
        )


def test_node_record_rejects_invalid_status_type():
    """Statuses must be NodeStatus values or their string values."""
    with pytest.raises(TypeError, match="status"):
        NodeRecord(domain_id="test-domain", 
            node_id="fedora-1",
            hostname="hp-pavilion",
            operating_system="Fedora 44",
            status=1,
        )


def test_node_record_with_all_statuses():
    """Test creating nodes with all possible statuses."""
    for status in NodeStatus:
        node = NodeRecord(domain_id="test-domain", 
            node_id="test-node",
            hostname="test-host",
            operating_system="Test OS",
            status=status,
        )
        assert node.status == status
