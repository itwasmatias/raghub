"""Tests for TaskRequest."""

import pytest

from federation import AuthorizationLevel, NodeCapability, TaskRequest


def test_task_request_creation():
    """Test creating a basic task request."""
    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
    )

    assert request.task_id == "task-1"
    assert request.mission_id == "mission-1"
    assert request.required_capabilities == set()
    assert request.preferred_capabilities == set()
    assert request.authorization_level == AuthorizationLevel.INTERNAL
    assert request.approval_required is False
    assert request.input_data == {}
    assert request.expected_result == ""


def test_task_request_with_all_fields():
    """Test creating a task request with all fields."""
    required = {NodeCapability("python_execution")}
    preferred = {NodeCapability("gpu_available")}
    input_data = {"query": "test"}

    request = TaskRequest(
        task_id="task-2",
        mission_id="mission-2",
        required_capabilities=required,
        preferred_capabilities=preferred,
        authorization_level=AuthorizationLevel.RESTRICTED,
        approval_required=True,
        input_data=input_data,
        expected_result="Expected output",
    )

    assert request.task_id == "task-2"
    assert request.mission_id == "mission-2"
    assert request.required_capabilities == required
    assert request.preferred_capabilities == preferred
    assert request.authorization_level == AuthorizationLevel.RESTRICTED
    assert request.approval_required is True
    assert request.input_data == input_data
    assert request.expected_result == "Expected output"


def test_task_request_requires_task_id():
    """Test that task_id is required."""
    with pytest.raises(ValueError, match="task_id"):
        TaskRequest(task_id="", mission_id="mission-1")


def test_task_request_rejects_whitespace_only_task_id():
    """Test that task_id cannot contain only whitespace."""
    with pytest.raises(ValueError, match="task_id"):
        TaskRequest(task_id=" \t\n", mission_id="mission-1")


def test_task_request_requires_mission_id():
    """Test that mission_id is required."""
    with pytest.raises(ValueError, match="mission_id"):
        TaskRequest(task_id="task-1", mission_id="")


def test_task_request_rejects_whitespace_only_mission_id():
    """Test that mission_id cannot contain only whitespace."""
    with pytest.raises(ValueError, match="mission_id"):
        TaskRequest(task_id="task-1", mission_id=" \t\n")


def test_task_request_requires_string_task_id():
    """Test that task_id must be a string."""
    with pytest.raises(TypeError, match="task_id"):
        TaskRequest(task_id=123, mission_id="mission-1")


def test_task_request_requires_string_mission_id():
    """Test that mission_id must be a string."""
    with pytest.raises(TypeError, match="mission_id"):
        TaskRequest(task_id="task-1", mission_id=123)


def test_task_request_converts_required_capabilities_to_set():
    """Test that required_capabilities is converted to a set."""
    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities=[NodeCapability("python_execution")],
    )

    assert isinstance(request.required_capabilities, set)


def test_task_request_converts_preferred_capabilities_to_set():
    """Test that preferred_capabilities is converted to a set."""
    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        preferred_capabilities=[NodeCapability("gpu_available")],
    )

    assert isinstance(request.preferred_capabilities, set)


def test_task_request_validates_required_capabilities_type():
    """Test that required_capabilities must contain NodeCapability values."""
    with pytest.raises(TypeError, match="NodeCapability"):
        TaskRequest(
            task_id="task-1",
            mission_id="mission-1",
            required_capabilities=["python_execution"],
        )


def test_task_request_validates_preferred_capabilities_type():
    """Test that preferred_capabilities must contain NodeCapability values."""
    with pytest.raises(TypeError, match="NodeCapability"):
        TaskRequest(
            task_id="task-1",
            mission_id="mission-1",
            preferred_capabilities=["gpu_available"],
        )


def test_task_request_rejects_overlap_between_required_and_preferred():
    """Test that capabilities cannot be both required and preferred."""
    cap = NodeCapability("python_execution")

    with pytest.raises(ValueError, match="both required and preferred"):
        TaskRequest(
            task_id="task-1",
            mission_id="mission-1",
            required_capabilities={cap},
            preferred_capabilities={cap},
        )


def test_task_request_validates_authorization_level():
    """Test that authorization_level is validated."""
    with pytest.raises(ValueError, match="authorization_level"):
        TaskRequest(
            task_id="task-1",
            mission_id="mission-1",
            authorization_level="invalid",
        )


def test_task_request_accepts_authorization_level_string():
    """Test that authorization_level can be a string."""
    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        authorization_level="restricted",
    )

    assert request.authorization_level == AuthorizationLevel.RESTRICTED


def test_task_request_validates_approval_required_type():
    """Test that approval_required must be a boolean."""
    with pytest.raises(TypeError, match="approval_required"):
        TaskRequest(
            task_id="task-1",
            mission_id="mission-1",
            approval_required="true",
        )


def test_task_request_validates_input_data_type():
    """Test that input_data must be a dict."""
    with pytest.raises(TypeError, match="input_data"):
        TaskRequest(
            task_id="task-1",
            mission_id="mission-1",
            input_data="not a dict",
        )


def test_task_request_validates_expected_result_type():
    """Test that expected_result must be a string."""
    with pytest.raises(TypeError, match="expected_result"):
        TaskRequest(
            task_id="task-1",
            mission_id="mission-1",
            expected_result=123,
        )


def test_task_request_has_required_capability():
    """Test checking if a capability is required."""
    cap = NodeCapability("python_execution")
    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities={cap},
    )

    assert request.has_required_capability(cap) is True
    assert request.has_required_capability("python_execution") is True
    assert request.has_required_capability("gpu_available") is False


def test_task_request_has_preferred_capability():
    """Test checking if a capability is preferred."""
    cap = NodeCapability("gpu_available")
    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        preferred_capabilities={cap},
    )

    assert request.has_preferred_capability(cap) is True
    assert request.has_preferred_capability("gpu_available") is True
    assert request.has_preferred_capability("python_execution") is False


def test_task_request_all_capabilities():
    """Test getting all capabilities."""
    required = {NodeCapability("python_execution")}
    preferred = {NodeCapability("gpu_available")}

    request = TaskRequest(
        task_id="task-1",
        mission_id="mission-1",
        required_capabilities=required,
        preferred_capabilities=preferred,
    )

    all_caps = request.all_capabilities()
    assert len(all_caps) == 2
    assert NodeCapability("python_execution") in all_caps
    assert NodeCapability("gpu_available") in all_caps


def test_task_request_personal_mission_scenario():
    """Test a task request for a personal mission like Inbox Cleanup."""
    request = TaskRequest(
        task_id="inbox-cleanup-001",
        mission_id="personal-inbox-management",
        required_capabilities={
            NodeCapability("network_access"),
            NodeCapability("python_execution"),
        },
        preferred_capabilities={
            NodeCapability("persistent_storage"),
        },
        authorization_level=AuthorizationLevel.CONFIDENTIAL,
        approval_required=True,
        input_data={
            "email_filter": "unread:true age:>30d",
            "action": "archive",
        },
        expected_result="Dry-run plan showing which emails would be archived",
    )

    assert request.task_id == "inbox-cleanup-001"
    assert request.mission_id == "personal-inbox-management"
    assert len(request.required_capabilities) == 2
    assert len(request.preferred_capabilities) == 1
    assert request.authorization_level == AuthorizationLevel.CONFIDENTIAL
    assert request.approval_required is True
    assert "email_filter" in request.input_data
