"""Tests for NodeCapability model."""

from dataclasses import FrozenInstanceError

import pytest

from federation import KnownCapability as ExportedKnownCapability
from federation.capability import KnownCapability, NodeCapability


def test_node_capability_creation():
    """Test basic NodeCapability creation."""
    cap = NodeCapability(name="python_execution")
    assert cap.name == "python_execution"
    assert str(cap) == "python_execution"


def test_node_capability_from_known():
    """Test creating capability from KnownCapability enum."""
    cap = NodeCapability.from_known(KnownCapability.CONTAINER_RUNTIME)
    assert cap.name == "container_runtime"
    assert ExportedKnownCapability is KnownCapability


def test_node_capability_immutable():
    """Test that NodeCapability is immutable (frozen)."""
    cap = NodeCapability(name="test")
    with pytest.raises(FrozenInstanceError):
        cap.name = "modified"


def test_node_capability_hashable():
    """Test that NodeCapability can be used in sets and as dict keys."""
    cap1 = NodeCapability(name="python_execution")
    cap2 = NodeCapability(name="python_execution")
    cap3 = NodeCapability(name="container_runtime")

    # Should be able to use in a set
    cap_set = {cap1, cap2, cap3}
    assert len(cap_set) == 2  # cap1 and cap2 are equal

    # Should be able to use as dict key
    cap_dict = {cap1: "value1", cap3: "value2"}
    assert cap_dict[cap2] == "value1"  # cap2 equals cap1


def test_node_capability_equality():
    """Test NodeCapability equality."""
    cap1 = NodeCapability(name="python_execution")
    cap2 = NodeCapability(name="python_execution")
    cap3 = NodeCapability(name="container_runtime")

    assert cap1 == cap2
    assert cap1 != cap3


def test_node_capability_validation():
    """Test that empty or invalid capability names raise errors."""
    with pytest.raises(ValueError, match="non-empty string"):
        NodeCapability(name="")

    with pytest.raises(ValueError, match="non-empty string"):
        NodeCapability(name=None)

    with pytest.raises(ValueError, match="non-empty string"):
        NodeCapability(name=" \t ")


def test_node_capability_normalizes_name():
    """Capability identifiers use a canonical case-insensitive form."""
    cap = NodeCapability(name="  Custom_ML_Runtime  ")

    assert cap.name == "custom_ml_runtime"
    assert cap == NodeCapability(name="CUSTOM_ML_RUNTIME")


def test_all_known_capabilities():
    """Test that all KnownCapability values can be converted."""
    known_caps = [
        KnownCapability.PYTHON_EXECUTION,
        KnownCapability.CONTAINER_RUNTIME,
        KnownCapability.WINDOWS_DESKTOP,
        KnownCapability.LINUX_SERVICES,
        KnownCapability.GPU_AVAILABLE,
        KnownCapability.PERSISTENT_STORAGE,
        KnownCapability.NETWORK_ACCESS,
    ]

    for known in known_caps:
        cap = NodeCapability.from_known(known)
        assert cap.name == known.value


def test_custom_capability():
    """Test that custom (non-standard) capabilities can be created."""
    custom_cap = NodeCapability(name="custom_ml_runtime")
    assert custom_cap.name == "custom_ml_runtime"
