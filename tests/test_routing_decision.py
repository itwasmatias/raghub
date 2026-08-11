"""Tests for RoutingDecision."""

import pytest

from federation import (
    ExcludedNode,
    NodeCapability,
    NodeRecord,
    RoutingDecision,
    RoutingOutcome,
    TaskAssignment,
    TaskRequest,
)


def test_excluded_node_creation():
    """Test creating an ExcludedNode."""
    excluded = ExcludedNode(node_id="node-1", reason="Node is offline")

    assert excluded.node_id == "node-1"
    assert excluded.reason == "Node is offline"


def test_excluded_node_requires_node_id():
    """Test that node_id is required."""
    with pytest.raises(ValueError, match="node_id"):
        ExcludedNode(node_id="", reason="test")


def test_excluded_node_rejects_whitespace_only_node_id():
    """Test that node_id cannot contain only whitespace."""
    with pytest.raises(ValueError, match="node_id"):
        ExcludedNode(node_id=" \t\n", reason="test")


def test_excluded_node_requires_string_node_id():
    """Test that node_id must be a string."""
    with pytest.raises(TypeError, match="node_id"):
        ExcludedNode(node_id=123, reason="test")


def test_excluded_node_requires_reason():
    """Test that reason is required."""
    with pytest.raises(ValueError, match="reason"):
        ExcludedNode(node_id="node-1", reason="")


def test_excluded_node_rejects_whitespace_only_reason():
    """Test that reason cannot contain only whitespace."""
    with pytest.raises(ValueError, match="reason"):
        ExcludedNode(node_id="node-1", reason=" \t\n")


def test_excluded_node_requires_string_reason():
    """Test that reason must be a string."""
    with pytest.raises(TypeError, match="reason"):
        ExcludedNode(node_id="node-1", reason=123)


def test_routing_decision_success():
    """Test creating a successful routing decision."""
    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={NodeCapability("python_execution")},
    )
    node = NodeRecord(domain_id="test-domain", 
        node_id="node-1",
        hostname="test-host",
        operating_system="Linux",
        capabilities={NodeCapability("python_execution")},
    )
    assignment = TaskAssignment(task_request=request, assigned_node=node)

    decision = RoutingDecision(
        task_request=request,
        outcome=RoutingOutcome.SUCCESS,
        assignment=assignment,
        required_capabilities_matched={NodeCapability("python_execution")},
        explanation="Task routed successfully",
    )

    assert decision.task_request is request
    assert decision.outcome == RoutingOutcome.SUCCESS
    assert decision.assignment is assignment
    assert decision.is_success is True
    assert decision.assigned_node_id == "node-1"


def test_routing_decision_no_eligible_nodes():
    """Test creating a no-eligible-nodes routing decision."""
    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={NodeCapability("gpu_available")},
    )

    excluded = [
        ExcludedNode(node_id="node-1", reason="Missing required capabilities: gpu_available"),
        ExcludedNode(node_id="node-2", reason="Node is offline"),
    ]

    decision = RoutingDecision(
        task_request=request,
        outcome=RoutingOutcome.NO_ELIGIBLE_NODES,
        assignment=None,
        excluded_nodes=excluded,
        explanation="No eligible nodes found",
    )

    assert decision.task_request is request
    assert decision.outcome == RoutingOutcome.NO_ELIGIBLE_NODES
    assert decision.assignment is None
    assert decision.is_success is False
    assert decision.assigned_node_id is None
    assert len(decision.excluded_nodes) == 2


def test_routing_decision_requires_task_request():
    """Test that task_request is required."""
    with pytest.raises(TypeError, match="task_request"):
        RoutingDecision(
            task_request="not a request",
            outcome=RoutingOutcome.SUCCESS,
        )


def test_routing_decision_validates_outcome():
    """Test that outcome is validated."""
    request = TaskRequest(task_id="task-1", mission_id="mission-1")

    with pytest.raises(ValueError, match="outcome"):
        RoutingDecision(
            task_request=request,
            outcome="invalid",
        )


def test_routing_decision_accepts_outcome_string():
    """Test that outcome can be a string."""
    request = TaskRequest(task_id="task-1", mission_id="mission-1")

    decision = RoutingDecision(
        task_request=request,
        outcome="no_eligible_nodes",
        assignment=None,
    )

    assert decision.outcome == RoutingOutcome.NO_ELIGIBLE_NODES


def test_routing_decision_success_requires_assignment():
    """Test that SUCCESS outcome requires an assignment."""
    request = TaskRequest(task_id="task-1", mission_id="mission-1")

    with pytest.raises(ValueError, match="assignment must be provided"):
        RoutingDecision(
            task_request=request,
            outcome=RoutingOutcome.SUCCESS,
            assignment=None,
        )


def test_routing_decision_no_eligible_nodes_forbids_assignment():
    """Test that NO_ELIGIBLE_NODES outcome forbids an assignment."""
    request = TaskRequest(task_id="task-1", mission_id="mission-1")
    node = NodeRecord(domain_id="test-domain", 
        node_id="node-1",
        hostname="test-host",
        operating_system="Linux",
    )
    assignment = TaskAssignment(task_request=request, assigned_node=node)

    with pytest.raises(ValueError, match="assignment must be None"):
        RoutingDecision(
            task_request=request,
            outcome=RoutingOutcome.NO_ELIGIBLE_NODES,
            assignment=assignment,
        )


def test_routing_decision_validates_assignment_task_request():
    """Test that assignment.task_request must match routing decision task_request."""
    request1 = TaskRequest(task_id="task-1", mission_id="mission-1")
    request2 = TaskRequest(task_id="task-2", mission_id="mission-2")
    node = NodeRecord(domain_id="test-domain", 
        node_id="node-1",
        hostname="test-host",
        operating_system="Linux",
    )
    assignment = TaskAssignment(task_request=request2, assigned_node=node)

    with pytest.raises(ValueError, match="task_request must match"):
        RoutingDecision(
            task_request=request1,
            outcome=RoutingOutcome.SUCCESS,
            assignment=assignment,
        )


def test_routing_decision_validates_required_capabilities_matched():
    """Test that required_capabilities_matched is validated."""
    request = TaskRequest(task_id="task-1", mission_id="mission-1")

    with pytest.raises(TypeError, match="required_capabilities_matched"):
        RoutingDecision(
            task_request=request,
            outcome=RoutingOutcome.NO_ELIGIBLE_NODES,
            required_capabilities_matched=["python_execution"],
        )


def test_routing_decision_validates_preferred_capabilities_matched():
    """Test that preferred_capabilities_matched is validated."""
    request = TaskRequest(task_id="task-1", mission_id="mission-1")

    with pytest.raises(TypeError, match="preferred_capabilities_matched"):
        RoutingDecision(
            task_request=request,
            outcome=RoutingOutcome.NO_ELIGIBLE_NODES,
            preferred_capabilities_matched=["gpu_available"],
        )


def test_routing_decision_validates_excluded_nodes_type():
    """Test that excluded_nodes must be a list."""
    request = TaskRequest(task_id="task-1", mission_id="mission-1")

    with pytest.raises(TypeError, match="excluded_nodes must be a list"):
        RoutingDecision(
            task_request=request,
            outcome=RoutingOutcome.NO_ELIGIBLE_NODES,
            excluded_nodes="not a list",
        )


def test_routing_decision_validates_excluded_nodes_elements():
    """Test that excluded_nodes must contain ExcludedNode values."""
    request = TaskRequest(task_id="task-1", mission_id="mission-1")

    with pytest.raises(TypeError, match="ExcludedNode"):
        RoutingDecision(
            task_request=request,
            outcome=RoutingOutcome.NO_ELIGIBLE_NODES,
            excluded_nodes=["not an ExcludedNode"],
        )


def test_routing_decision_validates_explanation_type():
    """Test that explanation must be a string."""
    request = TaskRequest(task_id="task-1", mission_id="mission-1")

    with pytest.raises(TypeError, match="explanation"):
        RoutingDecision(
            task_request=request,
            outcome=RoutingOutcome.NO_ELIGIBLE_NODES,
            explanation=123,
        )


def test_routing_decision_get_summary_success():
    """Test getting summary for a successful routing decision."""
    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={NodeCapability("python_execution")},
        preferred_capabilities={NodeCapability("gpu_available")},
    )
    node = NodeRecord(domain_id="test-domain", 
        node_id="node-1",
        hostname="test-host",
        operating_system="Linux",
        capabilities={
            NodeCapability("python_execution"),
            NodeCapability("gpu_available"),
        },
    )
    assignment = TaskAssignment(task_request=request, assigned_node=node)

    decision = RoutingDecision(
        task_request=request,
        outcome=RoutingOutcome.SUCCESS,
        assignment=assignment,
        required_capabilities_matched={NodeCapability("python_execution")},
        preferred_capabilities_matched={NodeCapability("gpu_available")},
    )

    summary = decision.get_summary()
    assert "task-1" in summary
    assert "node-1" in summary
    assert "1 required" in summary
    assert "1 preferred" in summary


def test_routing_decision_get_summary_no_eligible_nodes():
    """Test getting summary for a no-eligible-nodes routing decision."""
    request = TaskRequest(task_id="task-1", mission_id="mission-1")

    excluded = [
        ExcludedNode(node_id="node-1", reason="Offline"),
        ExcludedNode(node_id="node-2", reason="Missing capabilities"),
    ]

    decision = RoutingDecision(
        task_request=request,
        outcome=RoutingOutcome.NO_ELIGIBLE_NODES,
        excluded_nodes=excluded,
    )

    summary = decision.get_summary()
    assert "task-1" in summary
    assert "could not be routed" in summary
    assert "2 nodes excluded" in summary


def test_routing_decision_with_multiple_preferred_matches():
    """Test routing decision with multiple preferred capability matches."""
    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={NodeCapability("python_execution")},
        preferred_capabilities={
            NodeCapability("gpu_available"),
            NodeCapability("persistent_storage"),
            NodeCapability("network_access"),
        },
    )
    node = NodeRecord(domain_id="test-domain", 
        node_id="node-1",
        hostname="test-host",
        operating_system="Linux",
        capabilities={
            NodeCapability("python_execution"),
            NodeCapability("gpu_available"),
            NodeCapability("network_access"),
        },
    )
    assignment = TaskAssignment(task_request=request, assigned_node=node)

    decision = RoutingDecision(
        task_request=request,
        outcome=RoutingOutcome.SUCCESS,
        assignment=assignment,
        required_capabilities_matched={NodeCapability("python_execution")},
        preferred_capabilities_matched={
            NodeCapability("gpu_available"),
            NodeCapability("network_access"),
        },
    )

    assert len(decision.required_capabilities_matched) == 1
    assert len(decision.preferred_capabilities_matched) == 2
