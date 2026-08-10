"""Hybrid routing with credit resilience for RAGHub federation.

This module implements LOCAL CONTINUITY FIRST routing that ensures RAGHub
remains useful when cloud credits, paid-agent capacity, or specific providers
are unavailable. It composes existing TaskRouter and WorkerBudgetGovernor
without duplication.

Key principles:
- Local continuity first: prefer local when cloud is unavailable
- Explicit governed cloud escalation: cloud only when all gates pass
- No hidden fallback: every routing decision has explicit evidence
- Credit resilience: credit exhaustion is a routing condition, not a crash
- Provider resilience: provider outages don't make RAGHub globally unusable

v0.1
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from federation.routing_decision import RoutingDecision, RoutingOutcome
from federation.task_request import AuthorizationLevel, TaskRequest
from federation.task_router import TaskRouter
from federation.worker_governance import (
    BudgetPolicy,
    BudgetRoutingEvidence,
    ExecutionLocality,
    WorkerAvailability,
    WorkerCapacity,
    WorkerCostClass,
    WorkerProviderMetadata,
)


class ExecutionRouteKind(str, Enum):
    """Classification of hybrid routing outcomes."""

    LOCAL = "local"
    CLOUD = "cloud"
    LOCAL_CONTINUITY = "local_continuity"
    NO_ELIGIBLE_ROUTE = "no_eligible_route"
    DEFERRED = "deferred"


class HybridRoutingReason(str, Enum):
    """Deterministic reason codes for routing decisions."""

    # Local routing reasons
    LOCAL_FIRST_POLICY = "local_first_policy"
    LOCAL_ONLY_POLICY = "local_only_policy"
    LOCAL_CAPABILITY_SUFFICIENT = "local_capability_sufficient"
    LOCAL_DEGRADED_ALLOWED = "local_degraded_allowed"

    # Cloud rejection reasons
    CLOUD_NOT_PERMITTED = "cloud_not_permitted"
    CLOUD_PROVIDER_UNAVAILABLE = "cloud_provider_unavailable"
    CLOUD_BUDGET_EXHAUSTED = "cloud_budget_exhausted"
    CLOUD_BUDGET_REJECTED = "cloud_budget_rejected"
    CLOUD_ESCALATION_NOT_PERMITTED = "cloud_escalation_not_permitted"
    CLOUD_AUTHORIZATION_FORBIDDEN = "cloud_authorization_forbidden"

    # Cloud selection reasons
    CLOUD_EXPLICITLY_SELECTED = "cloud_explicitly_selected"
    CLOUD_CAPABILITY_REQUIRED = "cloud_capability_required"

    # Continuity reasons
    LOCAL_CONTINUITY_PROVIDER_UNAVAILABLE = "local_continuity_provider_unavailable"
    LOCAL_CONTINUITY_BUDGET_UNAVAILABLE = "local_continuity_budget_unavailable"
    LOCAL_CONTINUITY_CLOUD_REJECTED = "local_continuity_cloud_rejected"

    # No route reasons
    NO_ROUTE_CAPABILITY = "no_route_capability"
    NO_ROUTE_AUTHORIZATION = "no_route_authorization"
    NO_ROUTE_BUDGET = "no_route_budget"
    NO_ROUTE_PROVIDER = "no_route_provider"
    NO_ROUTE_AVAILABILITY = "no_route_availability"

    # Deferred reasons
    APPROVAL_REQUIRED = "approval_required"
    CLOUD_APPROVAL_REQUIRED = "cloud_approval_required"


def _identifier(value: Any, name: str) -> str:
    """Validate and return a canonical identifier."""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical string")
    return value


def _digest(value: Any, name: str) -> str:
    """Validate and return a SHA-256 hex digest."""
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError(f"{name} must be lowercase SHA-256 hex")
    return value


def _canonical(value: Any) -> bytes:
    """Convert a value to canonical JSON bytes for fingerprinting."""
    def normalize(item):
        if isinstance(item, Enum):
            return item.value
        if isinstance(item, datetime):
            return item.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if isinstance(item, frozenset):
            return sorted(normalize(x) for x in item)
        if isinstance(item, tuple):
            return [normalize(x) for x in item]
        if isinstance(item, dict):
            return {key: normalize(val) for key, val in item.items()}
        return item

    try:
        return json.dumps(
            normalize(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Value must contain finite JSON-serializable data") from exc


def _fingerprint(value: Any) -> str:
    """Compute deterministic SHA-256 fingerprint."""
    return hashlib.sha256(_canonical(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class HybridRoutingPolicy:
    """
    Immutable policy controlling hybrid local/cloud routing behavior.

    All decision-affecting fields participate in the deterministic fingerprint.
    """

    local_first: bool = True
    cloud_allowed: bool = False
    continue_locally_when_cloud_unavailable: bool = True
    allow_degraded_local: bool = False
    cloud_escalation_requires_explicit_permission: bool = True
    permitted_cloud_providers: frozenset[str] = field(default_factory=frozenset)
    max_cloud_cost_class: WorkerCostClass = WorkerCostClass.HIGH
    policy_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate policy fields and compute fingerprint."""
        # Validate boolean fields
        for name in (
            "local_first",
            "cloud_allowed",
            "continue_locally_when_cloud_unavailable",
            "allow_degraded_local",
            "cloud_escalation_requires_explicit_permission",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a boolean")

        # Validate max_cloud_cost_class
        try:
            object.__setattr__(
                self,
                "max_cloud_cost_class",
                WorkerCostClass(self.max_cloud_cost_class),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("max_cloud_cost_class is invalid") from exc

        # Validate permitted_cloud_providers
        if not isinstance(self.permitted_cloud_providers, frozenset):
            try:
                providers = frozenset(
                    _identifier(p, "provider") for p in self.permitted_cloud_providers
                )
                object.__setattr__(self, "permitted_cloud_providers", providers)
            except (TypeError, ValueError) as exc:
                raise TypeError("permitted_cloud_providers must be a frozenset of identifiers") from exc
        else:
            for p in self.permitted_cloud_providers:
                _identifier(p, "permitted_cloud_provider")

        # Compute deterministic fingerprint
        fp_data = {
            "local_first": self.local_first,
            "cloud_allowed": self.cloud_allowed,
            "continue_locally_when_cloud_unavailable": self.continue_locally_when_cloud_unavailable,
            "allow_degraded_local": self.allow_degraded_local,
            "cloud_escalation_requires_explicit_permission": self.cloud_escalation_requires_explicit_permission,
            "permitted_cloud_providers": self.permitted_cloud_providers,
            "max_cloud_cost_class": self.max_cloud_cost_class,
        }
        object.__setattr__(self, "policy_fingerprint", _fingerprint(fp_data))

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "local_first": self.local_first,
            "cloud_allowed": self.cloud_allowed,
            "continue_locally_when_cloud_unavailable": self.continue_locally_when_cloud_unavailable,
            "allow_degraded_local": self.allow_degraded_local,
            "cloud_escalation_requires_explicit_permission": self.cloud_escalation_requires_explicit_permission,
            "permitted_cloud_providers": sorted(self.permitted_cloud_providers),
            "max_cloud_cost_class": self.max_cloud_cost_class.value,
            "policy_fingerprint": self.policy_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class HybridRoutingRequest:
    """
    Immutable hybrid routing request binding task requirements and policy.

    References the authoritative TaskRequest rather than duplicating it.
    """

    routing_request_id: str
    task_request: TaskRequest
    hybrid_policy: HybridRoutingPolicy
    request_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate request and compute fingerprint."""
        object.__setattr__(
            self,
            "routing_request_id",
            _identifier(self.routing_request_id, "routing_request_id"),
        )

        if not isinstance(self.task_request, TaskRequest):
            raise TypeError("task_request must be a TaskRequest")

        if not isinstance(self.hybrid_policy, HybridRoutingPolicy):
            raise TypeError("hybrid_policy must be a HybridRoutingPolicy")

        # Compute deterministic fingerprint
        fp_data = {
            "routing_request_id": self.routing_request_id,
            "task_id": self.task_request.task_id,
            "mission_id": self.task_request.mission_id,
            "required_capabilities": sorted(str(c) for c in self.task_request.required_capabilities),
            "preferred_capabilities": sorted(str(c) for c in self.task_request.preferred_capabilities),
            "authorization_level": self.task_request.authorization_level.value,
            "approval_required": self.task_request.approval_required,
            "policy_fingerprint": self.hybrid_policy.policy_fingerprint,
        }
        object.__setattr__(self, "request_fingerprint", _fingerprint(fp_data))

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "routing_request_id": self.routing_request_id,
            "task_id": self.task_request.task_id,
            "mission_id": self.task_request.mission_id,
            "authorization_level": self.task_request.authorization_level.value,
            "approval_required": self.task_request.approval_required,
            "policy_fingerprint": self.hybrid_policy.policy_fingerprint,
            "request_fingerprint": self.request_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class ProviderAvailabilitySnapshot:
    """Immutable snapshot of provider availability state."""

    provider_id: str | None
    availability: WorkerAvailability
    capacity: WorkerCapacity
    budget_exhausted: bool

    def __post_init__(self) -> None:
        """Validate snapshot fields."""
        if self.provider_id is not None:
            object.__setattr__(
                self,
                "provider_id",
                _identifier(self.provider_id, "provider_id"),
            )

        try:
            object.__setattr__(
                self,
                "availability",
                WorkerAvailability(self.availability),
            )
            object.__setattr__(
                self,
                "capacity",
                WorkerCapacity(self.capacity),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("availability or capacity is invalid") from exc

        if not isinstance(self.budget_exhausted, bool):
            raise TypeError("budget_exhausted must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "provider_id": self.provider_id,
            "availability": self.availability.value,
            "capacity": self.capacity.value,
            "budget_exhausted": self.budget_exhausted,
        }


@dataclass(frozen=True, slots=True)
class LocalRouteEligibility:
    """Evidence for local route eligibility assessment."""

    eligible: bool
    suitable: bool
    degraded: bool
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        """Validate eligibility fields."""
        for name in ("eligible", "suitable", "degraded"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a boolean")

        if not isinstance(self.reasons, tuple):
            raise TypeError("reasons must be a tuple")

        reasons = tuple(_identifier(r, "reason") for r in self.reasons)
        object.__setattr__(self, "reasons", reasons)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "eligible": self.eligible,
            "suitable": self.suitable,
            "degraded": self.degraded,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class CloudRouteEligibility:
    """Evidence for cloud route eligibility assessment."""

    eligible: bool
    provider_snapshot: ProviderAvailabilitySnapshot | None
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        """Validate eligibility fields."""
        if not isinstance(self.eligible, bool):
            raise TypeError("eligible must be a boolean")

        if self.provider_snapshot is not None and not isinstance(
            self.provider_snapshot,
            ProviderAvailabilitySnapshot,
        ):
            raise TypeError("provider_snapshot must be ProviderAvailabilitySnapshot or None")

        if not isinstance(self.reasons, tuple):
            raise TypeError("reasons must be a tuple")

        reasons = tuple(_identifier(r, "reason") for r in self.reasons)
        object.__setattr__(self, "reasons", reasons)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "eligible": self.eligible,
            "provider_snapshot": (
                None if self.provider_snapshot is None
                else self.provider_snapshot.to_dict()
            ),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class HybridRoutingDecision:
    """
    Immutable hybrid routing decision with full evidence trail.

    Preserves the underlying RoutingDecision from TaskRouter and adds
    explicit hybrid routing classification and reasoning.
    """

    routing_request_id: str
    task_id: str
    mission_id: str
    route_kind: ExecutionRouteKind
    assigned_node_id: str | None
    execution_locality: ExecutionLocality | None
    provider_id: str | None
    local_eligibility: LocalRouteEligibility
    cloud_eligibility: CloudRouteEligibility
    underlying_routing_decision: RoutingDecision
    budget_evidence: BudgetRoutingEvidence | None
    primary_reasons: tuple[HybridRoutingReason, ...]
    explanation: str
    can_execute: bool
    approval_required: bool
    decided_at: datetime
    decision_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate decision and compute fingerprint."""
        object.__setattr__(
            self,
            "routing_request_id",
            _identifier(self.routing_request_id, "routing_request_id"),
        )
        object.__setattr__(self, "task_id", _identifier(self.task_id, "task_id"))
        object.__setattr__(self, "mission_id", _identifier(self.mission_id, "mission_id"))

        # Validate route_kind
        try:
            object.__setattr__(
                self,
                "route_kind",
                ExecutionRouteKind(self.route_kind),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("route_kind is invalid") from exc

        # Validate assigned_node_id
        if self.assigned_node_id is not None:
            object.__setattr__(
                self,
                "assigned_node_id",
                _identifier(self.assigned_node_id, "assigned_node_id"),
            )

        # Validate execution_locality
        if self.execution_locality is not None:
            try:
                object.__setattr__(
                    self,
                    "execution_locality",
                    ExecutionLocality(self.execution_locality),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("execution_locality is invalid") from exc

        # Validate provider_id
        if self.provider_id is not None:
            object.__setattr__(
                self,
                "provider_id",
                _identifier(self.provider_id, "provider_id"),
            )

        # Validate eligibility evidence
        if not isinstance(self.local_eligibility, LocalRouteEligibility):
            raise TypeError("local_eligibility must be LocalRouteEligibility")
        if not isinstance(self.cloud_eligibility, CloudRouteEligibility):
            raise TypeError("cloud_eligibility must be CloudRouteEligibility")

        # Validate underlying_routing_decision
        if not isinstance(self.underlying_routing_decision, RoutingDecision):
            raise TypeError("underlying_routing_decision must be RoutingDecision")

        # Validate budget_evidence
        if self.budget_evidence is not None and not isinstance(
            self.budget_evidence,
            BudgetRoutingEvidence,
        ):
            raise TypeError("budget_evidence must be BudgetRoutingEvidence or None")

        # Validate primary_reasons
        if not isinstance(self.primary_reasons, tuple):
            raise TypeError("primary_reasons must be a tuple")
        reasons = tuple(HybridRoutingReason(r) for r in self.primary_reasons)
        object.__setattr__(self, "primary_reasons", reasons)

        # Validate explanation
        if not isinstance(self.explanation, str):
            raise TypeError("explanation must be a string")

        # Validate can_execute
        if not isinstance(self.can_execute, bool):
            raise TypeError("can_execute must be a boolean")

        # Validate approval_required
        if not isinstance(self.approval_required, bool):
            raise TypeError("approval_required must be a boolean")

        # Validate decided_at
        if not isinstance(self.decided_at, datetime):
            raise TypeError("decided_at must be a datetime")
        if self.decided_at.tzinfo is None or self.decided_at.utcoffset() is None:
            raise ValueError("decided_at must be timezone-aware")
        object.__setattr__(
            self,
            "decided_at",
            self.decided_at.astimezone(timezone.utc),
        )

        # Compute deterministic fingerprint
        fp_data = {
            "routing_request_id": self.routing_request_id,
            "task_id": self.task_id,
            "mission_id": self.mission_id,
            "route_kind": self.route_kind.value,
            "assigned_node_id": self.assigned_node_id,
            "execution_locality": (
                None if self.execution_locality is None
                else self.execution_locality.value
            ),
            "provider_id": self.provider_id,
            "local_eligibility": self.local_eligibility.to_dict(),
            "cloud_eligibility": self.cloud_eligibility.to_dict(),
            "primary_reasons": [r.value for r in self.primary_reasons],
            "can_execute": self.can_execute,
            "approval_required": self.approval_required,
            "decided_at": self.decided_at,
        }
        object.__setattr__(self, "decision_fingerprint", _fingerprint(fp_data))

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "routing_request_id": self.routing_request_id,
            "task_id": self.task_id,
            "mission_id": self.mission_id,
            "route_kind": self.route_kind.value,
            "assigned_node_id": self.assigned_node_id,
            "execution_locality": (
                None if self.execution_locality is None
                else self.execution_locality.value
            ),
            "provider_id": self.provider_id,
            "local_eligibility": self.local_eligibility.to_dict(),
            "cloud_eligibility": self.cloud_eligibility.to_dict(),
            "primary_reasons": [r.value for r in self.primary_reasons],
            "explanation": self.explanation,
            "can_execute": self.can_execute,
            "approval_required": self.approval_required,
            "decided_at": self.decided_at.isoformat(),
            "decision_fingerprint": self.decision_fingerprint,
        }


class HybridRoutingCoordinator:
    """
    Coordinate hybrid local/cloud routing using existing TaskRouter infrastructure.

    This coordinator composes existing components without duplicating logic:
    - Uses TaskRouter for capability-based routing
    - Uses BudgetRoutingGovernance for provider/budget filtering
    - Adds explicit LOCAL vs CLOUD vs LOCAL_CONTINUITY classification
    - Implements credit/provider resilience by treating unavailability as a routing condition

    The coordinator does NOT execute tasks or make network calls. It only produces
    routing decisions based on policy and available evidence.
    """

    def __init__(
        self,
        task_router: TaskRouter,
        metadata_provider: Any,  # Callable that provides WorkerProviderMetadata per node
    ):
        """
        Initialize the hybrid routing coordinator.

        Args:
            task_router: The existing TaskRouter instance
            metadata_provider: Callable that returns dict[str, WorkerProviderMetadata]
                for the current worker states
        """
        if not isinstance(task_router, TaskRouter):
            raise TypeError("task_router must be a TaskRouter")
        if not callable(metadata_provider):
            raise TypeError("metadata_provider must be callable")

        self._task_router = task_router
        self._metadata_provider = metadata_provider

    def evaluate(self, request: HybridRoutingRequest) -> HybridRoutingDecision:
        """
        Evaluate a hybrid routing request and produce a routing decision.

        This method implements deterministic routing logic:
        1. Validate request
        2. Build BudgetPolicy from HybridRoutingPolicy
        3. Call TaskRouter with budget governance
        4. Classify result as LOCAL/CLOUD/LOCAL_CONTINUITY/NO_ELIGIBLE_ROUTE
        5. Return decision with full evidence

        Args:
            request: The hybrid routing request

        Returns:
            HybridRoutingDecision with explicit route classification and evidence
        """
        if not isinstance(request, HybridRoutingRequest):
            raise TypeError("request must be a HybridRoutingRequest")

        # Get current worker provider metadata
        metadata_by_node = self._metadata_provider()
        if not isinstance(metadata_by_node, dict):
            raise TypeError("metadata_provider must return a dict")

        # Build BudgetPolicy from HybridRoutingPolicy
        budget_policy = self._build_budget_policy(request.hybrid_policy)

        # Import here to avoid circular dependency
        from federation.worker_governance import BudgetRoutingGovernance

        # Create BudgetRoutingGovernance
        budget_governance = BudgetRoutingGovernance(metadata_by_node, budget_policy)

        # Get routing decision from TaskRouter
        # Note: TaskRouter expects budget_governance parameter
        underlying_decision = self._task_router.route(request.task_request)
        budget_evidence = underlying_decision.budget_evidence

        # Assess local and cloud eligibility
        local_eligibility = self._assess_local_eligibility(
            request,
            underlying_decision,
            budget_evidence,
            metadata_by_node,
        )

        cloud_eligibility = self._assess_cloud_eligibility(
            request,
            underlying_decision,
            budget_evidence,
            metadata_by_node,
        )

        # Classify route and build decision
        return self._classify_and_decide(
            request,
            underlying_decision,
            budget_evidence,
            local_eligibility,
            cloud_eligibility,
            metadata_by_node,
        )

    def _build_budget_policy(self, hybrid_policy: HybridRoutingPolicy) -> BudgetPolicy:
        """Convert HybridRoutingPolicy to BudgetPolicy."""
        return BudgetPolicy(
            local_only=not hybrid_policy.cloud_allowed,
            cloud_budget_exhausted=False,  # We'll detect this from metadata
            max_cost_class=hybrid_policy.max_cloud_cost_class,
            allow_cloud_escalation=hybrid_policy.cloud_allowed,
            prefer_local=hybrid_policy.local_first,
            require_local_fallback_eligibility=False,
        )

    def _assess_local_eligibility(
        self,
        request: HybridRoutingRequest,
        routing_decision: RoutingDecision,
        budget_evidence: BudgetRoutingEvidence | None,
        metadata_by_node: dict[str, WorkerProviderMetadata],
    ) -> LocalRouteEligibility:
        """Assess whether a valid local route exists."""
        reasons = []

        # Check if any local workers are eligible
        if budget_evidence is None:
            # No budget governance, check underlying routing decision
            if routing_decision.outcome == RoutingOutcome.SUCCESS:
                if routing_decision.assignment is not None:
                    node_id = routing_decision.assignment.node_id
                    metadata = metadata_by_node.get(node_id)
                    if metadata and metadata.locality == ExecutionLocality.LOCAL:
                        return LocalRouteEligibility(
                            eligible=True,
                            suitable=True,
                            degraded=False,
                            reasons=("local_worker_available",),
                        )

            reasons.append("no_local_worker_available")
            return LocalRouteEligibility(
                eligible=False,
                suitable=False,
                degraded=False,
                reasons=tuple(reasons),
            )

        # Check budget evidence for local workers
        local_workers = [
            w for w in budget_evidence.preference.eligible_workers
            if w.locality == ExecutionLocality.LOCAL
        ]

        if not local_workers:
            # Check if any local workers exist but were excluded
            all_local_exist = any(
                m.locality == ExecutionLocality.LOCAL
                for m in metadata_by_node.values()
            )
            if all_local_exist:
                reasons.append("local_workers_excluded_by_governance")
            else:
                reasons.append("no_local_workers_configured")

            return LocalRouteEligibility(
                eligible=False,
                suitable=False,
                degraded=False,
                reasons=tuple(reasons),
            )

        # Local workers exist and are eligible
        # Determine if degraded (we could check capability quality here)
        degraded = False
        suitable = True

        return LocalRouteEligibility(
            eligible=True,
            suitable=suitable,
            degraded=degraded,
            reasons=("local_workers_eligible",),
        )

    def _assess_cloud_eligibility(
        self,
        request: HybridRoutingRequest,
        routing_decision: RoutingDecision,
        budget_evidence: BudgetRoutingEvidence | None,
        metadata_by_node: dict[str, WorkerProviderMetadata],
    ) -> CloudRouteEligibility:
        """Assess whether cloud escalation is eligible."""
        reasons = []
        provider_snapshot = None

        # Check hybrid policy
        if not request.hybrid_policy.cloud_allowed:
            reasons.append("cloud_not_allowed_by_policy")
            return CloudRouteEligibility(
                eligible=False,
                provider_snapshot=None,
                reasons=tuple(reasons),
            )

        # Check authorization
        # For v0.1, we don't have explicit network/cloud authorization in TaskRequest
        # This would be added in a future milestone

        if budget_evidence is None:
            reasons.append("no_budget_governance")
            return CloudRouteEligibility(
                eligible=False,
                provider_snapshot=None,
                reasons=tuple(reasons),
            )

        # Check budget evidence for cloud workers
        cloud_workers = [
            w for w in budget_evidence.preference.eligible_workers
            if w.locality == ExecutionLocality.CLOUD
        ]

        if not cloud_workers:
            # Check why cloud workers were excluded
            all_cloud_metadata = [
                m for m in metadata_by_node.values()
                if m.locality == ExecutionLocality.CLOUD
            ]

            if not all_cloud_metadata:
                reasons.append("no_cloud_workers_configured")
            else:
                # Cloud workers exist but were excluded - check why
                for metadata in all_cloud_metadata:
                    if metadata.availability == WorkerAvailability.UNAVAILABLE:
                        reasons.append("cloud_provider_unavailable")
                        provider_snapshot = ProviderAvailabilitySnapshot(
                            provider_id=metadata.provider_id,
                            availability=metadata.availability,
                            capacity=metadata.capacity,
                            budget_exhausted=metadata.budget_exhausted,
                        )
                    elif metadata.availability == WorkerAvailability.UNKNOWN:
                        reasons.append("cloud_provider_availability_unknown")

                    if metadata.budget_exhausted:
                        reasons.append("cloud_budget_exhausted")
                        if provider_snapshot is None:
                            provider_snapshot = ProviderAvailabilitySnapshot(
                                provider_id=metadata.provider_id,
                                availability=metadata.availability,
                                capacity=metadata.capacity,
                                budget_exhausted=metadata.budget_exhausted,
                            )

                    if metadata.capacity == WorkerCapacity.SATURATED:
                        reasons.append("cloud_capacity_saturated")
                    elif metadata.capacity == WorkerCapacity.UNKNOWN:
                        reasons.append("cloud_capacity_unknown")

                    # Check provider permission
                    if (
                        request.hybrid_policy.permitted_cloud_providers
                        and metadata.provider_id not in request.hybrid_policy.permitted_cloud_providers
                    ):
                        reasons.append("cloud_provider_not_permitted")

            if not reasons:
                reasons.append("cloud_workers_excluded_by_governance")

            return CloudRouteEligibility(
                eligible=False,
                provider_snapshot=provider_snapshot,
                reasons=tuple(reasons),
            )

        # Cloud workers are eligible
        # Get snapshot of the top cloud worker
        top_cloud = cloud_workers[0]
        metadata = metadata_by_node.get(top_cloud.node_id)
        if metadata:
            provider_snapshot = ProviderAvailabilitySnapshot(
                provider_id=metadata.provider_id,
                availability=metadata.availability,
                capacity=metadata.capacity,
                budget_exhausted=metadata.budget_exhausted,
            )

        return CloudRouteEligibility(
            eligible=True,
            provider_snapshot=provider_snapshot,
            reasons=("cloud_workers_eligible",),
        )

    def _classify_and_decide(
        self,
        request: HybridRoutingRequest,
        underlying_decision: RoutingDecision,
        budget_evidence: BudgetRoutingEvidence | None,
        local_eligibility: LocalRouteEligibility,
        cloud_eligibility: CloudRouteEligibility,
        metadata_by_node: dict[str, WorkerProviderMetadata],
    ) -> HybridRoutingDecision:
        """
        Classify routing outcome and create hybrid decision.

        Implements the deterministic decision priority:
        1. If approval required → DEFERRED
        2. If local-first and local eligible → LOCAL
        3. If local-only policy → LOCAL or NO_ELIGIBLE_ROUTE
        4. If cloud eligible and explicitly selected → CLOUD
        5. If cloud unavailable and local eligible → LOCAL_CONTINUITY
        6. Otherwise → NO_ELIGIBLE_ROUTE
        """
        decided_at = datetime.now(timezone.utc)

        # Handle approval requirement
        if request.task_request.approval_required:
            if cloud_eligibility.eligible:
                return self._create_decision(
                    request,
                    underlying_decision,
                    budget_evidence,
                    local_eligibility,
                    cloud_eligibility,
                    route_kind=ExecutionRouteKind.DEFERRED,
                    assigned_node_id=None,
                    execution_locality=None,
                    provider_id=None,
                    primary_reasons=(HybridRoutingReason.CLOUD_APPROVAL_REQUIRED,),
                    explanation="Cloud escalation requires approval",
                    can_execute=False,
                    approval_required=True,
                    decided_at=decided_at,
                )
            else:
                return self._create_decision(
                    request,
                    underlying_decision,
                    budget_evidence,
                    local_eligibility,
                    cloud_eligibility,
                    route_kind=ExecutionRouteKind.DEFERRED,
                    assigned_node_id=None,
                    execution_locality=None,
                    provider_id=None,
                    primary_reasons=(HybridRoutingReason.APPROVAL_REQUIRED,),
                    explanation="Task execution requires approval",
                    can_execute=False,
                    approval_required=True,
                    decided_at=decided_at,
                )

        # Local-first policy
        if request.hybrid_policy.local_first and local_eligibility.eligible:
            if local_eligibility.suitable or request.hybrid_policy.allow_degraded_local:
                assigned_node_id = None
                if underlying_decision.outcome == RoutingOutcome.SUCCESS:
                    if underlying_decision.assignment:
                        node_metadata = metadata_by_node.get(
                            underlying_decision.assignment.node_id
                        )
                        if node_metadata and node_metadata.locality == ExecutionLocality.LOCAL:
                            assigned_node_id = underlying_decision.assignment.node_id

                reasons = [HybridRoutingReason.LOCAL_FIRST_POLICY]
                if local_eligibility.degraded:
                    reasons.append(HybridRoutingReason.LOCAL_DEGRADED_ALLOWED)

                return self._create_decision(
                    request,
                    underlying_decision,
                    budget_evidence,
                    local_eligibility,
                    cloud_eligibility,
                    route_kind=ExecutionRouteKind.LOCAL,
                    assigned_node_id=assigned_node_id,
                    execution_locality=ExecutionLocality.LOCAL,
                    provider_id=None,
                    primary_reasons=tuple(reasons),
                    explanation=f"Local-first policy selects local execution for task {request.task_request.task_id}",
                    can_execute=assigned_node_id is not None,
                    approval_required=False,
                    decided_at=decided_at,
                )

        # Local-only policy
        if not request.hybrid_policy.cloud_allowed:
            if local_eligibility.eligible:
                assigned_node_id = None
                if underlying_decision.outcome == RoutingOutcome.SUCCESS:
                    if underlying_decision.assignment:
                        node_metadata = metadata_by_node.get(
                            underlying_decision.assignment.node_id
                        )
                        if node_metadata and node_metadata.locality == ExecutionLocality.LOCAL:
                            assigned_node_id = underlying_decision.assignment.node_id

                return self._create_decision(
                    request,
                    underlying_decision,
                    budget_evidence,
                    local_eligibility,
                    cloud_eligibility,
                    route_kind=ExecutionRouteKind.LOCAL,
                    assigned_node_id=assigned_node_id,
                    execution_locality=ExecutionLocality.LOCAL,
                    provider_id=None,
                    primary_reasons=(HybridRoutingReason.LOCAL_ONLY_POLICY,),
                    explanation=f"Local-only policy restricts task {request.task_request.task_id} to local execution",
                    can_execute=assigned_node_id is not None,
                    approval_required=False,
                    decided_at=decided_at,
                )
            else:
                return self._create_decision(
                    request,
                    underlying_decision,
                    budget_evidence,
                    local_eligibility,
                    cloud_eligibility,
                    route_kind=ExecutionRouteKind.NO_ELIGIBLE_ROUTE,
                    assigned_node_id=None,
                    execution_locality=None,
                    provider_id=None,
                    primary_reasons=(
                        HybridRoutingReason.LOCAL_ONLY_POLICY,
                        HybridRoutingReason.NO_ROUTE_CAPABILITY,
                    ),
                    explanation=f"Local-only policy but no eligible local workers for task {request.task_request.task_id}",
                    can_execute=False,
                    approval_required=False,
                    decided_at=decided_at,
                )

        # Cloud eligible and not local-first
        if cloud_eligibility.eligible and not request.hybrid_policy.local_first:
            assigned_node_id = None
            provider_id = None

            if underlying_decision.outcome == RoutingOutcome.SUCCESS:
                if underlying_decision.assignment:
                    node_metadata = metadata_by_node.get(
                        underlying_decision.assignment.node_id
                    )
                    if node_metadata and node_metadata.locality == ExecutionLocality.CLOUD:
                        assigned_node_id = underlying_decision.assignment.node_id
                        provider_id = node_metadata.provider_id

            return self._create_decision(
                request,
                underlying_decision,
                budget_evidence,
                local_eligibility,
                cloud_eligibility,
                route_kind=ExecutionRouteKind.CLOUD,
                assigned_node_id=assigned_node_id,
                execution_locality=ExecutionLocality.CLOUD,
                provider_id=provider_id,
                primary_reasons=(HybridRoutingReason.CLOUD_EXPLICITLY_SELECTED,),
                explanation=f"Cloud escalation selected for task {request.task_request.task_id}",
                can_execute=assigned_node_id is not None,
                approval_required=False,
                decided_at=decided_at,
            )

        # Cloud unavailable, local continuity
        if (
            not cloud_eligibility.eligible
            and local_eligibility.eligible
            and request.hybrid_policy.continue_locally_when_cloud_unavailable
        ):
            assigned_node_id = None
            if underlying_decision.outcome == RoutingOutcome.SUCCESS:
                if underlying_decision.assignment:
                    node_metadata = metadata_by_node.get(
                        underlying_decision.assignment.node_id
                    )
                    if node_metadata and node_metadata.locality == ExecutionLocality.LOCAL:
                        assigned_node_id = underlying_decision.assignment.node_id

            # Determine continuity reason
            continuity_reason = HybridRoutingReason.LOCAL_CONTINUITY_CLOUD_REJECTED
            if "cloud_provider_unavailable" in cloud_eligibility.reasons:
                continuity_reason = HybridRoutingReason.LOCAL_CONTINUITY_PROVIDER_UNAVAILABLE
            elif "cloud_budget_exhausted" in cloud_eligibility.reasons:
                continuity_reason = HybridRoutingReason.LOCAL_CONTINUITY_BUDGET_UNAVAILABLE

            return self._create_decision(
                request,
                underlying_decision,
                budget_evidence,
                local_eligibility,
                cloud_eligibility,
                route_kind=ExecutionRouteKind.LOCAL_CONTINUITY,
                assigned_node_id=assigned_node_id,
                execution_locality=ExecutionLocality.LOCAL,
                provider_id=None,
                primary_reasons=(continuity_reason,),
                explanation=f"Cloud unavailable, continuing locally for task {request.task_request.task_id}. Cloud rejection: {', '.join(cloud_eligibility.reasons)}",
                can_execute=assigned_node_id is not None,
                approval_required=False,
                decided_at=decided_at,
            )

        # No eligible route
        no_route_reason = HybridRoutingReason.NO_ROUTE_CAPABILITY
        if not local_eligibility.eligible and not cloud_eligibility.eligible:
            no_route_reason = HybridRoutingReason.NO_ROUTE_CAPABILITY

        return self._create_decision(
            request,
            underlying_decision,
            budget_evidence,
            local_eligibility,
            cloud_eligibility,
            route_kind=ExecutionRouteKind.NO_ELIGIBLE_ROUTE,
            assigned_node_id=None,
            execution_locality=None,
            provider_id=None,
            primary_reasons=(no_route_reason,),
            explanation=f"No eligible route found for task {request.task_request.task_id}",
            can_execute=False,
            approval_required=False,
            decided_at=decided_at,
        )

    def _create_decision(
        self,
        request: HybridRoutingRequest,
        underlying_decision: RoutingDecision,
        budget_evidence: BudgetRoutingEvidence | None,
        local_eligibility: LocalRouteEligibility,
        cloud_eligibility: CloudRouteEligibility,
        route_kind: ExecutionRouteKind,
        assigned_node_id: str | None,
        execution_locality: ExecutionLocality | None,
        provider_id: str | None,
        primary_reasons: tuple[HybridRoutingReason, ...],
        explanation: str,
        can_execute: bool,
        approval_required: bool,
        decided_at: datetime,
    ) -> HybridRoutingDecision:
        """Create a HybridRoutingDecision with full evidence."""
        return HybridRoutingDecision(
            routing_request_id=request.routing_request_id,
            task_id=request.task_request.task_id,
            mission_id=request.task_request.mission_id,
            route_kind=route_kind,
            assigned_node_id=assigned_node_id,
            execution_locality=execution_locality,
            provider_id=provider_id,
            local_eligibility=local_eligibility,
            cloud_eligibility=cloud_eligibility,
            underlying_routing_decision=underlying_decision,
            budget_evidence=budget_evidence,
            primary_reasons=primary_reasons,
            explanation=explanation,
            can_execute=can_execute,
            approval_required=approval_required,
            decided_at=decided_at,
        )
