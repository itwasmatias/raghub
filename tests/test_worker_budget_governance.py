"""Provider-independent worker metadata and budget policy tests."""

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from federation import (
    AuthorizationLevel, BudgetPolicy, ExecutionLocality, NodeCapability, NodeRecord,
    NodeStatus, TaskRequest, WorkerAvailability, WorkerBudgetGovernor,
    WorkerCapacity, WorkerCostClass, WorkerProviderMetadata,
)


def node(node_id, *capabilities, status=NodeStatus.ONLINE):
    return NodeRecord(
        domain_id="test-domain",
        node_id=node_id,
        hostname=f"{node_id}.local",
        operating_system="generic",
        status=status,
        capabilities={NodeCapability(item) for item in capabilities})


def metadata(node_id, *, locality=ExecutionLocality.LOCAL, provider=None,
             cost=WorkerCostClass.FREE, availability=WorkerAvailability.AVAILABLE,
             capacity=WorkerCapacity.AVAILABLE, ceiling=AuthorizationLevel.CONFIDENTIAL,
             exhausted=False, fallback=True):
    return WorkerProviderMetadata(node_id=node_id, locality=locality,
        provider_id=provider, cost_class=cost, availability=availability,
        capacity=capacity, authorization_ceiling=ceiling,
        budget_exhausted=exhausted, local_fallback_eligible=fallback)


def task(*, auth=AuthorizationLevel.INTERNAL, required=("python_execution",), preferred=()):
    return TaskRequest("task-1", "mission-1",
        required_capabilities={NodeCapability(item) for item in required},
        preferred_capabilities={NodeCapability(item) for item in preferred},
        authorization_level=auth)


def test_local_and_cloud_metadata_are_immutable_and_provider_is_opaque():
    local = metadata("local")
    cloud = metadata("cloud", locality=ExecutionLocality.CLOUD,
        provider="any opaque provider", cost=WorkerCostClass.STANDARD)
    assert local.provider_id is None
    assert cloud.provider_id == "any opaque provider"
    with pytest.raises(FrozenInstanceError): cloud.provider_id = "changed"


def test_local_only_policy_never_selects_cloud():
    nodes = (node("cloud", "python_execution"), node("local", "python_execution"))
    values = {"cloud": metadata("cloud", locality="cloud", provider="opaque"),
              "local": metadata("local")}
    decision = WorkerBudgetGovernor().evaluate(task(), nodes, values,
        BudgetPolicy(local_only=True))
    assert tuple(item.node_id for item in decision.eligible_workers) == ("local",)
    assert "local_only" in decision.for_node("cloud").reasons


def test_exhausted_cloud_budget_preserves_eligible_local_fallback():
    nodes = (node("cloud", "python_execution"), node("local", "python_execution"))
    values = {"cloud": metadata("cloud", locality="cloud", provider="opaque"),
              "local": metadata("local")}
    decision = WorkerBudgetGovernor().evaluate(task(), nodes, values,
        BudgetPolicy(cloud_budget_exhausted=True, allow_cloud_escalation=True))
    assert tuple(item.node_id for item in decision.eligible_workers) == ("local",)
    assert "cloud_budget_exhausted" in decision.for_node("cloud").reasons


@pytest.mark.parametrize(("availability", "capacity", "reason"), [
    (WorkerAvailability.UNAVAILABLE, WorkerCapacity.AVAILABLE, "worker_unavailable"),
    (WorkerAvailability.UNKNOWN, WorkerCapacity.AVAILABLE, "availability_unknown"),
    (WorkerAvailability.AVAILABLE, WorkerCapacity.SATURATED, "capacity_unavailable"),
    (WorkerAvailability.AVAILABLE, WorkerCapacity.UNKNOWN, "capacity_unknown"),
])
def test_unavailable_or_unknown_capacity_never_selected(availability, capacity, reason):
    worker = node("worker", "python_execution")
    decision = WorkerBudgetGovernor().evaluate(task(), (worker,), {"worker": metadata(
        "worker", availability=availability, capacity=capacity)}, BudgetPolicy())
    assert decision.eligible_workers == ()
    assert reason in decision.for_node("worker").reasons


def test_cost_ceiling_and_worker_exhaustion_are_enforced():
    expensive = node("expensive", "python_execution"); exhausted = node("exhausted", "python_execution")
    decision = WorkerBudgetGovernor().evaluate(task(), (expensive, exhausted), {
        "expensive": metadata("expensive", cost=WorkerCostClass.HIGH),
        "exhausted": metadata("exhausted", exhausted=True),
    }, BudgetPolicy(max_cost_class=WorkerCostClass.STANDARD))
    assert decision.eligible_workers == ()
    assert "cost_ceiling_exceeded" in decision.for_node("expensive").reasons
    assert "worker_budget_exhausted" in decision.for_node("exhausted").reasons


def test_cloud_requires_explicit_escalation_permission():
    cloud = node("cloud", "python_execution")
    decision = WorkerBudgetGovernor().evaluate(task(), (cloud,), {"cloud": metadata(
        "cloud", locality="cloud", provider="opaque")}, BudgetPolicy())
    assert decision.eligible_workers == ()
    assert "cloud_escalation_not_permitted" in decision.for_node("cloud").reasons


def test_required_capabilities_and_authorization_outrank_cost():
    cheap = node("cheap"); authorized = node("authorized", "python_execution")
    values = {
        "cheap": metadata("cheap", cost=WorkerCostClass.FREE),
        "authorized": metadata("authorized", cost=WorkerCostClass.HIGH,
            ceiling=AuthorizationLevel.PUBLIC),
    }
    decision = WorkerBudgetGovernor().evaluate(task(auth=AuthorizationLevel.RESTRICTED),
        (cheap, authorized), values, BudgetPolicy(max_cost_class=WorkerCostClass.HIGH))
    assert decision.eligible_workers == ()
    assert "missing_required_capability" in decision.for_node("cheap").reasons
    assert "authorization_exceeds_worker_ceiling" in decision.for_node("authorized").reasons


def test_preferred_capability_then_locality_cost_and_node_id_are_deterministic():
    nodes = (node("b", "python_execution"), node("a", "python_execution"),
             node("preferred", "python_execution", "gpu"))
    values = {item.node_id: metadata(item.node_id, cost=WorkerCostClass.LOW) for item in nodes}
    first = WorkerBudgetGovernor().evaluate(task(preferred=("gpu",)), nodes, values, BudgetPolicy())
    second = WorkerBudgetGovernor().evaluate(task(preferred=("gpu",)), reversed(nodes), values, BudgetPolicy())
    assert tuple(item.node_id for item in first.eligible_workers) == ("preferred", "a", "b")
    assert first == second


def test_missing_metadata_does_not_fabricate_provider_or_availability():
    legacy = node("legacy", "python_execution")
    decision = WorkerBudgetGovernor().evaluate(task(), (legacy,), {}, BudgetPolicy())
    assert decision.eligible_workers == ()
    assert decision.for_node("legacy").reasons == ("provider_metadata_missing",)
    assert legacy.node_id == "legacy"  # old NodeRecord remains valid and unchanged


def test_local_fallback_must_be_explicitly_eligible_when_policy_requires_it():
    local = node("local", "python_execution")
    decision = WorkerBudgetGovernor().evaluate(task(), (local,), {
        "local": metadata("local", fallback=False)}, BudgetPolicy(require_local_fallback_eligibility=True))
    assert decision.eligible_workers == ()
    assert "local_fallback_not_eligible" in decision.for_node("local").reasons


def test_remaining_budget_is_external_nonnegative_metadata_only():
    item = replace_metadata = WorkerProviderMetadata(node_id="worker", locality="cloud",
        provider_id=None, cost_class="low", availability="available", capacity="available",
        authorization_ceiling="internal", remaining_budget=Decimal("12.50"))
    assert item.remaining_budget == Decimal("12.50")
    assert item.provider_id is None
    with pytest.raises(ValueError):
        WorkerProviderMetadata(node_id="bad", locality="cloud", cost_class="low",
            availability="available", capacity="available", authorization_ceiling="internal",
            remaining_budget=Decimal("-1"))
    with pytest.raises(ValueError):
        WorkerProviderMetadata(node_id="zero", locality="cloud", cost_class="low",
            availability="available", capacity="available", authorization_ceiling="internal",
            remaining_budget=Decimal("0"), budget_exhausted=False)


def test_cloud_provider_identity_is_never_fabricated_for_routing():
    cloud = node("cloud", "python_execution")
    value = metadata("cloud", locality="cloud", provider=None)
    decision = WorkerBudgetGovernor().evaluate(task(), (cloud,), {"cloud": value},
        BudgetPolicy(allow_cloud_escalation=True))
    assert decision.eligible_workers == ()
    assert "provider_identity_missing" in decision.for_node("cloud").reasons


@pytest.mark.parametrize(("field", "value"), [
    ("locality", "vendor-cloud"), ("cost_class", "unlimited"),
    ("availability", "probably"), ("capacity", "infinite"),
])
def test_invalid_enum_metadata_fails_closed(field, value):
    values = dict(node_id="worker", locality="local", cost_class="free",
        availability="available", capacity="available", authorization_ceiling="internal")
    values[field] = value
    with pytest.raises(ValueError): WorkerProviderMetadata(**values)


def test_duplicate_nodes_or_foreign_metadata_identity_are_rejected():
    worker = node("worker", "python_execution")
    with pytest.raises(ValueError):
        WorkerBudgetGovernor().evaluate(task(), (worker, worker), {"worker": metadata("worker")}, BudgetPolicy())
    with pytest.raises(ValueError):
        WorkerBudgetGovernor().evaluate(task(), (worker,), {"worker": metadata("foreign")}, BudgetPolicy())
