"""Tests for TaskAssignment."""

import pytest

from federation import (
    NodeCapability,
    NodeRecord,
    TaskAssignment,
    TaskRequest,
)


def test_task_assignment_creation():
    """Test creating a task assignment."""
    request = TaskRequest(task_id="task-1", mission_id="mission-1")
    node = NodeRecord(
        node_id="node-1",
        hostname="test-host",
        operating_system="Linux",
    )

    assignment = TaskAssignment(task_request=request, assigned_node=node)

    assert assignment.task_request is request
    assert assignment.assigned_node is node


def test_task_assignment_requires_task_request():
    """Test that task_request is required."""
    node = NodeRecord(
        node_id="node-1",
        hostname="test-host",
        operating_system="Linux",
    )

    with pytest.raises(TypeError, match="task_request"):
        TaskAssignment(task_request="not a request", assigned_node=node)


def test_task_assignment_requires_assigned_node():
    """Test that assigned_node is required."""
    request = TaskRequest(task_id="task-1", mission_id="mission-1")

    with pytest.raises(TypeError, match="assigned_node"):
        TaskAssignment(task_request=request, assigned_node="not a node")


def test_task_assignment_task_id_property():
    """Test getting task_id from the assignment."""
    request = TaskRequest(task_id="task-1", mission_id="mission-1")
    node = NodeRecord(
        node_id="node-1",
        hostname="test-host",
        operating_system="Linux",
    )

    assignment = TaskAssignment(task_request=request, assigned_node=node)

    assert assignment.task_id == "task-1"


def test_task_assignment_mission_id_property():
    """Test getting mission_id from the assignment."""
    request = TaskRequest(task_id="task-1", mission_id="mission-1")
    node = NodeRecord(
        node_id="node-1",
        hostname="test-host",
        operating_system="Linux",
    )

    assignment = TaskAssignment(task_request=request, assigned_node=node)

    assert assignment.mission_id == "mission-1"


def test_task_assignment_node_id_property():
    """Test getting node_id from the assignment."""
    request = TaskRequest(task_id="task-1", mission_id="mission-1")
    node = NodeRecord(
        node_id="node-1",
        hostname="test-host",
        operating_system="Linux",
    )

    assignment = TaskAssignment(task_request=request, assigned_node=node)

    assert assignment.node_id == "node-1"


def test_task_assignment_with_capabilities():
    """Test assignment with a node that has capabilities."""
    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={
            NodeCapability("python_execution"),
            NodeCapability("network_access"),
        },
    )
    node = NodeRecord(
        node_id="node-1",
        hostname="test-host",
        operating_system="Linux",
        capabilities={
            NodeCapability("python_execution"),
            NodeCapability("network_access"),
            NodeCapability("gpu_available"),
        },
    )

    assignment = TaskAssignment(task_request=request, assigned_node=node)

    # Verify the assignment captures the full context
    assert len(assignment.task_request.required_capabilities) == 2
    assert len(assignment.assigned_node.capabilities) == 3


def test_task_assignment_immutability_of_slots():
    """Test that TaskAssignment uses slots (for memory efficiency)."""
    request = TaskRequest(task_id="task-1", mission_id="mission-1")
    node = NodeRecord(
        node_id="node-1",
        hostname="test-host",
        operating_system="Linux",
    )

    assignment = TaskAssignment(task_request=request, assigned_node=node)

    # Verify slots are used
    assert hasattr(TaskAssignment, "__slots__")

    # Cannot add new attributes with slots
    with pytest.raises(AttributeError):
        assignment.new_attribute = "value"
