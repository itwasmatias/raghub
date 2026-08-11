"""Composition tests for governed budget policy and authoritative TaskRouter."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from federation import (
    AuthorizationLevel, BudgetPolicy, BudgetRoutingGovernance, DurableAssignmentRegistry,
    ExecutionLocality, Heartbeat, HeartbeatRegistry, NodeCapability, NodeRecord,
    NodeRegistry, RoutingOutcome, TaskRequest, TaskRouter, WorkerAvailability,
    WorkerCapacity, WorkerCostClass, WorkerProviderMetadata,
)

NOW = datetime.now(timezone.utc)
KEY = b"budget-routing-integration-test-key-01"


def metadata(node_id, *, locality="local", provider=None, cost="free",
             availability="available", capacity="available", ceiling="confidential",
             exhausted=False, fallback=True):
    return WorkerProviderMetadata(node_id=node_id, locality=locality, provider_id=provider,
        cost_class=cost, availability=availability, capacity=capacity,
        authorization_ceiling=ceiling, budget_exhausted=exhausted,
        local_fallback_eligible=fallback)


def request(*, required=("python_execution",), preferred=(), auth="internal", approval=False):
    return TaskRequest("task-1", "mission-1",
        required_capabilities={NodeCapability(item) for item in required},
        preferred_capabilities={NodeCapability(item) for item in preferred},
        authorization_level=auth, approval_required=approval,
        expected_result="structured result")


def router_for(tmp_path, node_specs, metadata_by_node=None, policy=None, assignments=False):
    registry = NodeRegistry(stale_threshold_seconds=10**9)
    heartbeats = HeartbeatRegistry(tmp_path / "heartbeats.jsonl",
        registry_id="budget-routing-tests", node_registry=registry,
        integrity_key=KEY, clock=lambda: NOW)
    for node_id, capabilities in node_specs:
        registry.register(NodeRecord(domain_id="test-domain", node_id=node_id, hostname=f"{node_id}.local", operating_system="generic",
            capabilities={NodeCapability(item) for item in capabilities}))
        heartbeats.record(Heartbeat.authenticated(worker_id=node_id,
            registry_id="budget-routing-tests", domain_id="test-domain", sequence=1, session_id="boot-1",
            worker_timestamp=NOW, health="healthy", power_capabilities=(),
            requested_power_state="active", sleep_reason=None, expected_wake_time=None,
            wake_method=None, active_work_checkpointed=False,
            previous_authentication_tag="0" * 64, integrity_key=KEY))
    governance = None if metadata_by_node is None else BudgetRoutingGovernance(
        metadata_by_node, policy or BudgetPolicy())
    assignment_store = None
    if assignments:
        assignment_store = DurableAssignmentRegistry(tmp_path / "assignments.jsonl",
            coordinator_node_id="coordinator-1", integrity_key=KEY)
    return TaskRouter(registry, heartbeat_registry=heartbeats,
        assignment_store=assignment_store, budget_governance=governance)


def test_backward_compatibility_without_governance_uses_existing_router(tmp_path):
    router = router_for(tmp_path, (("worker", ("python_execution",)),))
    decision = router.route(request())
    assert decision.outcome is RoutingOutcome.SUCCESS
    assert decision.assigned_node_id == "worker"
    assert decision.budget_evidence is None


def test_local_only_never_selects_cloud_and_keeps_local_fallback(tmp_path):
    values = {"cloud": metadata("cloud", locality="cloud", provider="opaque"),
              "local": metadata("local")}
    router = router_for(tmp_path, (("cloud", ("python_execution",)),
                                   ("local", ("python_execution",))), values,
        BudgetPolicy(local_only=True, allow_cloud_escalation=True))
    decision = router.route(request())
    assert decision.assigned_node_id == "local"
    assert "local_only" in decision.budget_evidence.preference.for_node("cloud").reasons


def test_cloud_allowed_only_with_explicit_escalation(tmp_path):
    values = {"cloud": metadata("cloud", locality="cloud", provider="opaque")}
    denied = router_for(tmp_path / "denied", (("cloud", ("python_execution",)),), values)
    allowed = router_for(tmp_path / "allowed", (("cloud", ("python_execution",)),), values,
        BudgetPolicy(allow_cloud_escalation=True))
    assert denied.route(request()).outcome is RoutingOutcome.NO_ELIGIBLE_NODES
    assert allowed.route(request()).assigned_node_id == "cloud"


@pytest.mark.parametrize(("changes", "policy", "reason"), [
    ({"availability": WorkerAvailability.UNAVAILABLE}, BudgetPolicy(), "worker_unavailable"),
    ({"capacity": WorkerCapacity.SATURATED}, BudgetPolicy(), "capacity_unavailable"),
    ({"cost": WorkerCostClass.HIGH}, BudgetPolicy(max_cost_class=WorkerCostClass.LOW), "cost_ceiling_exceeded"),
    ({"exhausted": True}, BudgetPolicy(), "worker_budget_exhausted"),
])
def test_policy_removes_core_eligible_worker(tmp_path, changes, policy, reason):
    worker_metadata = metadata("worker", **changes)
    router = router_for(tmp_path, (("worker", ("python_execution",)),),
        {"worker": worker_metadata}, policy)
    decision = router.route(request())
    assert decision.outcome is RoutingOutcome.NO_ELIGIBLE_NODES
    assert reason in decision.budget_evidence.preference.for_node("worker").reasons


def test_exhausted_cloud_budget_blocks_cloud_but_not_local(tmp_path):
    values = {"cloud": metadata("cloud", locality="cloud", provider="opaque"),
              "local": metadata("local")}
    router = router_for(tmp_path, (("cloud", ("python_execution",)),
                                   ("local", ("python_execution",))), values,
        BudgetPolicy(cloud_budget_exhausted=True, allow_cloud_escalation=True))
    assert router.route(request()).assigned_node_id == "local"


def test_provider_missing_fails_closed_for_cloud(tmp_path):
    router = router_for(tmp_path, (("cloud", ("python_execution",)),),
        {"cloud": metadata("cloud", locality="cloud")},
        BudgetPolicy(allow_cloud_escalation=True))
    decision = router.route(request())
    assert decision.outcome is RoutingOutcome.NO_ELIGIBLE_NODES
    assert "provider_identity_missing" in decision.budget_evidence.preference.for_node("cloud").reasons


def test_missing_metadata_fails_closed_only_when_governance_configured(tmp_path):
    governed = router_for(tmp_path / "governed", (("worker", ("python_execution",)),), {})
    legacy = router_for(tmp_path / "legacy", (("worker", ("python_execution",)),))
    assert governed.route(request()).outcome is RoutingOutcome.NO_ELIGIBLE_NODES
    assert legacy.route(request()).outcome is RoutingOutcome.SUCCESS


def test_authorization_ceiling_cannot_be_bypassed_by_low_cost(tmp_path):
    router = router_for(tmp_path, (("cheap", ("python_execution",)),),
        {"cheap": metadata("cheap", ceiling=AuthorizationLevel.PUBLIC)})
    decision = router.route(request(auth=AuthorizationLevel.RESTRICTED))
    assert decision.outcome is RoutingOutcome.NO_ELIGIBLE_NODES
    assert "authorization_exceeds_worker_ceiling" in decision.budget_evidence.preference.for_node("cheap").reasons


def test_required_capability_remains_authoritative_and_budget_cannot_promote(tmp_path):
    router = router_for(tmp_path, (("cheap", ()),), {"cheap": metadata("cheap")})
    decision = router.route(request())
    assert decision.outcome is RoutingOutcome.NO_ELIGIBLE_NODES
    assert "Missing required capabilities" in decision.excluded_nodes[0].reason


def test_preferred_capability_and_deterministic_budget_ranking(tmp_path):
    specs = (("b", ("python_execution",)), ("a", ("python_execution",)),
             ("preferred", ("python_execution", "gpu")))
    values = {node_id: metadata(node_id, cost="low") for node_id, _ in specs}
    router = router_for(tmp_path, specs, values)
    decision = router.route(request(preferred=("gpu",)))
    assert decision.assigned_node_id == "preferred"
    assert tuple(item.node_id for item in decision.budget_evidence.preference.eligible_workers) == (
        "preferred", "a", "b")


def test_approval_and_task_authority_are_not_mutated(tmp_path):
    original = request(auth="restricted", approval=True)
    router = router_for(tmp_path, (("worker", ("python_execution",)),),
        {"worker": metadata("worker")})
    decision = router.route(original)
    assert decision.assignment.task_request is original
    assert original.approval_required is True
    assert original.authorization_level is AuthorizationLevel.RESTRICTED


def test_policy_and_evidence_are_immutable_snapshots(tmp_path):
    policy = BudgetPolicy(local_only=True)
    governance = BudgetRoutingGovernance({"worker": metadata("worker")}, policy)
    with pytest.raises(FrozenInstanceError): policy.local_only = False
    with pytest.raises(FrozenInstanceError): governance.policy = BudgetPolicy()
    router = router_for(tmp_path, (("worker", ("python_execution",)),),
        {"worker": metadata("worker")}, policy)
    evidence = router.route(request()).budget_evidence
    with pytest.raises(FrozenInstanceError): evidence.policy = BudgetPolicy()


def test_exact_replay_is_idempotent_with_durable_assignment(tmp_path):
    router = router_for(tmp_path, (("worker", ("python_execution",)),),
        {"worker": metadata("worker")}, assignments=True)
    original = request()
    first = router.route(original); second = router.route(original)
    assert first.assignment_id == second.assignment_id
    assert first.budget_evidence == second.budget_evidence


def test_malformed_or_foreign_metadata_rejected_at_configuration():
    with pytest.raises(ValueError):
        BudgetRoutingGovernance({"worker": metadata("foreign")}, BudgetPolicy())
    with pytest.raises(TypeError): BudgetRoutingGovernance({"worker": object()}, BudgetPolicy())
