"""Provider-independent worker metadata and deterministic budget governance."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from types import MappingProxyType

from federation.node_record import NodeRecord, NodeStatus
from federation.task_request import AuthorizationLevel, TaskRequest


class ExecutionLocality(str, Enum):
    LOCAL = "local"
    CLOUD = "cloud"


class WorkerCostClass(str, Enum):
    FREE = "free"
    LOW = "low"
    STANDARD = "standard"
    HIGH = "high"


class WorkerAvailability(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class WorkerCapacity(str, Enum):
    AVAILABLE = "available"
    SATURATED = "saturated"
    UNKNOWN = "unknown"


_COST_RANK = {value: index for index, value in enumerate(WorkerCostClass)}
_AUTH_RANK = {value: index for index, value in enumerate(AuthorizationLevel)}


def _identifier(value, name, *, optional=False):
    if value is None and optional: return None
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a canonical non-empty string")
    return value


def _enum(value, cls, name):
    try: return cls(value)
    except (TypeError, ValueError) as exc: raise ValueError(f"{name} is invalid") from exc


@dataclass(frozen=True, slots=True)
class WorkerProviderMetadata:
    node_id: str
    locality: ExecutionLocality
    cost_class: WorkerCostClass
    availability: WorkerAvailability
    capacity: WorkerCapacity
    authorization_ceiling: AuthorizationLevel
    provider_id: str | None = None
    remaining_budget: Decimal | None = None
    budget_exhausted: bool = False
    local_fallback_eligible: bool = False

    def __post_init__(self):
        object.__setattr__(self, "node_id", _identifier(self.node_id, "node_id"))
        object.__setattr__(self, "locality", _enum(self.locality, ExecutionLocality, "locality"))
        object.__setattr__(self, "cost_class", _enum(self.cost_class, WorkerCostClass, "cost_class"))
        object.__setattr__(self, "availability", _enum(self.availability, WorkerAvailability, "availability"))
        object.__setattr__(self, "capacity", _enum(self.capacity, WorkerCapacity, "capacity"))
        object.__setattr__(self, "authorization_ceiling", _enum(
            self.authorization_ceiling, AuthorizationLevel, "authorization_ceiling"))
        object.__setattr__(self, "provider_id", _identifier(self.provider_id, "provider_id", optional=True))
        if self.remaining_budget is not None:
            if type(self.remaining_budget) is not Decimal or not self.remaining_budget.is_finite() or self.remaining_budget < 0:
                raise ValueError("remaining_budget must be a finite nonnegative Decimal or None")
        if not isinstance(self.budget_exhausted, bool): raise TypeError("budget_exhausted must be a boolean")
        if not isinstance(self.local_fallback_eligible, bool):
            raise TypeError("local_fallback_eligible must be a boolean")
        if self.remaining_budget == 0 and not self.budget_exhausted:
            raise ValueError("zero remaining_budget must be marked exhausted")


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    local_only: bool = False
    cloud_budget_exhausted: bool = False
    max_cost_class: WorkerCostClass = WorkerCostClass.HIGH
    allow_cloud_escalation: bool = False
    prefer_local: bool = True
    require_local_fallback_eligibility: bool = False

    def __post_init__(self):
        for name in ("local_only", "cloud_budget_exhausted", "allow_cloud_escalation",
                     "prefer_local", "require_local_fallback_eligibility"):
            if not isinstance(getattr(self, name), bool): raise TypeError(f"{name} must be a boolean")
        object.__setattr__(self, "max_cost_class", _enum(
            self.max_cost_class, WorkerCostClass, "max_cost_class"))


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    node_id: str
    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GovernedWorker:
    node_id: str
    locality: ExecutionLocality
    provider_id: str | None
    cost_class: WorkerCostClass
    preferred_capabilities_matched: int


@dataclass(frozen=True, slots=True)
class ExecutionPreference:
    eligible_workers: tuple[GovernedWorker, ...]
    decisions: tuple[BudgetDecision, ...]

    def for_node(self, node_id):
        node_id = _identifier(node_id, "node_id")
        matches = tuple(item for item in self.decisions if item.node_id == node_id)
        if len(matches) != 1: raise KeyError(node_id)
        return matches[0]


class WorkerBudgetGovernor:
    """Evaluate policy after core node status, capability, and authority checks."""

    def evaluate(self, task_request, nodes, metadata_by_node, policy):
        if type(task_request) is not TaskRequest: raise TypeError("task_request must be a TaskRequest")
        if type(policy) is not BudgetPolicy: raise TypeError("policy must be a BudgetPolicy")
        node_items = tuple(nodes)
        if not all(type(item) is NodeRecord for item in node_items):
            raise TypeError("nodes must contain NodeRecord values")
        if len({item.node_id for item in node_items}) != len(node_items):
            raise ValueError("nodes contains duplicate identity")
        if not isinstance(metadata_by_node, dict): raise TypeError("metadata_by_node must be a dict")
        metadata = MappingProxyType(dict(metadata_by_node))
        node_ids = {item.node_id for item in node_items}
        if set(metadata) - node_ids: raise ValueError("metadata contains foreign node identity")
        decisions = []
        eligible = []
        for worker in sorted(node_items, key=lambda item: item.node_id):
            reasons = []
            item = metadata.get(worker.node_id)
            if item is None:
                reasons.append("provider_metadata_missing")
            elif type(item) is not WorkerProviderMetadata:
                raise TypeError("metadata values must be WorkerProviderMetadata")
            elif item.node_id != worker.node_id:
                raise ValueError("metadata identity does not match node")
            if worker.status is not NodeStatus.ONLINE: reasons.append("node_not_online")
            missing = sorted(cap.name for cap in task_request.required_capabilities
                             if not worker.has_capability(cap))
            if missing: reasons.append("missing_required_capability")
            if item is not None:
                if item.availability is WorkerAvailability.UNAVAILABLE: reasons.append("worker_unavailable")
                elif item.availability is WorkerAvailability.UNKNOWN: reasons.append("availability_unknown")
                if item.capacity is WorkerCapacity.SATURATED: reasons.append("capacity_unavailable")
                elif item.capacity is WorkerCapacity.UNKNOWN: reasons.append("capacity_unknown")
                if _AUTH_RANK[task_request.authorization_level] > _AUTH_RANK[item.authorization_ceiling]:
                    reasons.append("authorization_exceeds_worker_ceiling")
                if item.budget_exhausted: reasons.append("worker_budget_exhausted")
                if _COST_RANK[item.cost_class] > _COST_RANK[policy.max_cost_class]:
                    reasons.append("cost_ceiling_exceeded")
                if item.locality is ExecutionLocality.CLOUD:
                    if item.provider_id is None: reasons.append("provider_identity_missing")
                    if policy.local_only: reasons.append("local_only")
                    if policy.cloud_budget_exhausted: reasons.append("cloud_budget_exhausted")
                    if not policy.allow_cloud_escalation: reasons.append("cloud_escalation_not_permitted")
                elif policy.require_local_fallback_eligibility and not item.local_fallback_eligible:
                    reasons.append("local_fallback_not_eligible")
            unique_reasons = tuple(dict.fromkeys(reasons))
            decisions.append(BudgetDecision(worker.node_id, not unique_reasons, unique_reasons))
            if not unique_reasons:
                preferred = sum(worker.has_capability(cap) for cap in task_request.preferred_capabilities)
                eligible.append(GovernedWorker(worker.node_id, item.locality, item.provider_id,
                    item.cost_class, preferred))
        eligible.sort(key=lambda item: (
            -item.preferred_capabilities_matched,
            (0 if item.locality is ExecutionLocality.LOCAL else 1) if policy.prefer_local else 0,
            _COST_RANK[item.cost_class], item.node_id,
        ))
        return ExecutionPreference(tuple(eligible), tuple(decisions))
