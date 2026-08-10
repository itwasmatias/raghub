#  Hybrid Routing + Credit Resilience v0.1

**Mission**: Ensure RAGHub remains useful when cloud credits, paid-agent capacity, or specific providers are unavailable.

**Status**: v0.1 prototype - Architectural contracts and decision logic implemented

## Principles

### LOCAL CONTINUITY FIRST
When cloud execution is unavailable (credits exhausted, provider down, budget rejected), RAGHub continues operating locally rather than becoming globally unusable.

### EXPLICIT GOVERNED CLOUD ESCALATION
Cloud execution is selected only when:
- Task authorization permits it
- Policy explicitly allows cloud
- Provider is available
- Budget/credits permit use
- Required capabilities are satisfied

### NO HIDDEN FALLBACK
Every routing decision produces explicit evidence about WHY a route was selected. There are no silent cloud calls when local fails, or vice versa.

## Architecture

### Design Philosophy: Composition Over Duplication

The hybrid routing system is a **thin decision layer** that composes existing RAGHub federation contracts:

- **Does NOT duplicate**: TaskRouter, WorkerBudgetGovernor, provider metadata, dispatch
- **Does compose**: Existing provider/budget governance with explicit hybrid routing policy
- **Does add**: LOCAL vs CLOUD vs LOCAL_CONTINUITY classification

### Key Concepts

#### ExecutionRouteKind
```python
LOCAL             # Local execution selected (local-first or only option)
CLOUD             # Explicit cloud escalation with all gates passed
LOCAL_CONTINUITY  # Cloud unavailable, continuing locally
NO_ELIGIBLE_ROUTE # Neither local nor cloud can satisfy requirements
DEFERRED          # Approval required before execution
```

#### HybridRoutingPolicy
Immutable policy controlling routing behavior:
- `local_first`: Prefer local when both routes eligible (default: True)
- `cloud_allowed`: Whether cloud escalation is permitted (default: False)
- `continue_locally_when_cloud_unavailable`: Fall back to local when cloud unavailable (default: True)
- `allow_degraded_local`: Accept degraded local capability (default: False)
- `permitted_cloud_providers`: Allowlist of cloud providers (default: empty)
- `max_cloud_cost_class`: Maximum permitted cloud cost (default: HIGH)

All fields participate in deterministic policy fingerprint.

#### HybridRoutingRequest
Binds a TaskRequest with HybridRoutingPolicy. Includes:
- Routing request ID
- Task request reference
- Hybrid policy
- Deterministic request fingerprint

#### HybridRoutingDecision
Immutable decision with full evidence trail:
- Route kind (LOCAL/CLOUD/LOCAL_CONTINUITY/etc.)
- Assigned node ID and provider ID
- Execution locality
- Local eligibility evidence
- Cloud eligibility evidence
- Underlying routing decision from TaskRouter
- Budget evidence from provider governance
- Primary reason codes (typed enum, not free-form strings)
- Explanation
- Can execute flag
- Approval required flag
- Decision timestamp and fingerprint

## Relationship to Existing Contracts

### Provider/Budget Governance (Reused)
The hybrid routing system fully reuses existing `federation.worker_governance`:
- `WorkerProviderMetadata`: locality, cost, availability, capacity, budget state
- `WorkerAvailability`: AVAILABLE, UNAVAILABLE, UNKNOWN
- `WorkerCapacity`: AVAILABLE, SATURATED, UNKNOWN
- `BudgetPolicy`: local_only, allow_cloud_escalation, prefer_local, max_cost_class
- `BudgetRoutingGovernance`: Composition of metadata + policy
- `WorkerBudgetGovernor`: Deterministic eligibility evaluation

**No duplication**. Hybrid routing composes these contracts.

### TaskRouter (Composed)
HybridRoutingCoordinator receives a TaskRouter instance and uses it for capability-based routing. The coordinator adds hybrid classification on top of TaskRouter's output.

**Not duplicated**. TaskRouter remains authoritative for node selection.

### Dispatch (Separate)
Routing produces decisions. Dispatch consumes assignments and creates execution offers. Hybrid routing does not modify dispatch.

**Boundary preserved**: Routing is pure decision-making. Execution is downstream.

## Credit Resilience

### Provider Availability States
Provider availability uses existing `WorkerAvailability`:
- `AVAILABLE`: Provider is operational
- `UNAVAILABLE`: Provider is down/unreachable
- `UNKNOWN`: Availability cannot be determined

**UNKNOWN is NOT treated as AVAILABLE**. Conservative routing.

### Credit Availability
Credit state uses existing `WorkerProviderMetadata`:
- `budget_exhausted: bool`: Whether credits are exhausted
- `capacity: WorkerCapacity`: AVAILABLE/SATURATED/UNKNOWN

**Credit exhaustion is a routing condition, not a crash**.

### Resilience Scenarios

**Scenario A: Cloud Credits Unavailable**
- Policy: cloud_allowed=True, local_first=False (would prefer cloud)
- State: cloud budget_exhausted=True, local available
- Decision: `LOCAL_CONTINUITY` with reason `LOCAL_CONTINUITY_BUDGET_UNAVAILABLE`
- Outcome: Continues locally, no exception

**Scenario B: Cloud Provider Unavailable**
- Policy: cloud_allowed=True, local_first=False
- State: cloud availability=UNAVAILABLE, local available
- Decision: `LOCAL_CONTINUITY` with reason `LOCAL_CONTINUITY_PROVIDER_UNAVAILABLE`
- Outcome: Continues locally, preserves task

**Scenario C: No Eligible Route**
- Policy: cloud_allowed=True, local_first=False
- State: cloud unavailable, local insufficient capability
- Decision: `NO_ELIGIBLE_ROUTE`
- Outcome: Explicit no-route decision with evidence, not hidden failure

## Decision Priority

Deterministic decision order (v0.1):

1. **Approval required**: If task.approval_required → DEFERRED
2. **Local-first with local eligible**: If policy.local_first AND local suitable → LOCAL
3. **Local-only policy**: If NOT policy.cloud_allowed → LOCAL or NO_ELIGIBLE_ROUTE
4. **Cloud eligible and preferred**: If cloud eligible AND NOT local_first → CLOUD
5. **Cloud unavailable, local continuity**: If NOT cloud eligible AND local eligible AND policy.continue_locally_when_cloud_unavailable → LOCAL_CONTINUITY
6. **No route**: Otherwise → NO_ELIGIBLE_ROUTE

No randomness. Fully deterministic.

## Reason Codes

Typed enum values (not free-form strings):

### Local Reasons
- `LOCAL_FIRST_POLICY`: Local-first policy selected local
- `LOCAL_ONLY_POLICY`: Cloud not permitted, local only
- `LOCAL_CAPABILITY_SUFFICIENT`: Local capability matches
- `LOCAL_DEGRADED_ALLOWED`: Degraded local explicitly permitted

### Cloud Rejection Reasons
- `CLOUD_NOT_PERMITTED`: Policy disallows cloud
- `CLOUD_PROVIDER_UNAVAILABLE`: Provider down/unreachable
- `CLOUD_BUDGET_EXHAUSTED`: Credits exhausted
- `CLOUD_ESCALATION_NOT_PERMITTED`: Policy forbids escalation
- `CLOUD_BUDGET_REJECTED`: Budget governance rejected cost

### Continuity Reasons
- `LOCAL_CONTINUITY_PROVIDER_UNAVAILABLE`: Provider outage, fell back to local
- `LOCAL_CONTINUITY_BUDGET_UNAVAILABLE`: Budget exhaustion, fell back to local
- `LOCAL_CONTINUITY_CLOUD_REJECTED`: Cloud rejected for other reason

### No Route Reasons
- `NO_ROUTE_CAPABILITY`: No worker has required capability
- `NO_ROUTE_AUTHORIZATION`: Authorization prevents all routes
- `APPROVAL_REQUIRED`: Approval needed before execution
- `CLOUD_APPROVAL_REQUIRED`: Cloud escalation needs approval

## Fingerprinting

All authoritative contracts use deterministic SHA-256 fingerprints:

### Policy Fingerprint
Includes:
- local_first
- cloud_allowed
- continue_locally_when_cloud_unavailable
- allow_degraded_local
- cloud_escalation_requires_explicit_permission
- permitted_cloud_providers (sorted)
- max_cloud_cost_class

Any policy change produces different fingerprint.

### Request Fingerprint
Includes:
- routing_request_id
- task_id, mission_id
- required_capabilities (sorted)
- preferred_capabilities (sorted)
- authorization_level
- approval_required
- policy_fingerprint

Different requests have different fingerprints.

### Decision Fingerprint
Includes:
- routing_request_id, task_id, mission_id
- route_kind
- assigned_node_id, execution_locality, provider_id
- local_eligibility
- cloud_eligibility
- primary_reasons
- can_execute, approval_required
- decided_at timestamp

Evidence-preserving fingerprint.

## Security Boundaries

### No Hidden Execution
- Routing performs **no cloud API calls**
- Routing performs **no local inference**
- Routing does **not start llama-server**
- Routing is pure decision-making

### No Authorization Bypass
- Provider availability does **not imply authorization**
- Credit availability does **not imply authorization**
- Eligible route is **not execution authority**

### No Arbitrary Injection
- No arbitrary provider injection
- No model downloads during routing
- No process launch during routing
- No shell execution during routing

### Immutability
All decisions are frozen dataclasses. Route mutation after creation is impossible.

## v0.1 Limitations

### No Governed Cloud Runtime
v0.1 implements hybrid routing **decisions** but does not include a governed cloud execution runtime. Cloud routes are explicit escalation decisions that require a downstream cloud executor (not implemented in v0.1).

### Simplified TaskRouter Integration
v0.1 coordinator composes TaskRouter but doesn't fully integrate per-request budget governance due to TaskRouter's current API. This is acceptable for v0.1 architectural validation.

### No Durability Store
v0.1 routing decisions are ephemeral. A durable routing decision store could be added in future milestones if needed.

### No Automatic Post-Failure Escalation
If local execution fails, v0.1 does **not** automatically attempt cloud. Any escalation after execution failure requires a new routing decision with explicit policy/evidence.

## Non-Goals (Explicitly Out of Scope)

v0.1 does **not** implement:
- Cloud provider API clients (OpenAI, Anthropic, etc.)
- Governed cloud execution runtime
- Provider API key management
- Automatic credit purchasing/refilling
- Multi-cloud bidding or cost optimization
- Quality-based capability assessment
- Automatic model downloads
- New dispatch system
- Changes to existing TaskRouter internals
- Benchmark integration for quality scoring

## Testing

v0.1 includes 60+ deterministic tests covering:
- Policy/request fingerprinting (11 tests)
- Local routing selection (11 tests)
- Cloud policy enforcement (11 tests)
- Credit resilience (7 tests)
- No hidden fallback guarantees (5 tests)
- Decision evidence and fingerprints (5 tests)
- Integration with existing contracts (3 tests)
- Security boundaries (4 tests)

## Usage Example

```python
from federation import (
    HybridRoutingCoordinator,
    HybridRoutingPolicy,
    HybridRoutingRequest,
    TaskRequest,
    TaskRouter,
    WorkerProviderMetadata,
    ExecutionLocality,
    WorkerAvailability,
    WorkerCapacity,
    WorkerCostClass,
)

# Define hybrid policy
policy = HybridRoutingPolicy(
    local_first=True,
    cloud_allowed=True,
    continue_locally_when_cloud_unavailable=True,
    max_cloud_cost_class=WorkerCostClass.STANDARD,
)

# Provider metadata (from authoritative source)
def get_worker_metadata():
    return {
        "local-worker-1": WorkerProviderMetadata(
            node_id="local-worker-1",
            locality=ExecutionLocality.LOCAL,
            cost_class=WorkerCostClass.FREE,
            availability=WorkerAvailability.AVAILABLE,
            capacity=WorkerCapacity.AVAILABLE,
            authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
        ),
        "cloud-worker-1": WorkerProviderMetadata(
            node_id="cloud-worker-1",
            locality=ExecutionLocality.CLOUD,
            cost_class=WorkerCostClass.STANDARD,
            availability=WorkerAvailability.UNAVAILABLE,  # Provider down!
            capacity=WorkerCapacity.UNKNOWN,
            authorization_ceiling=AuthorizationLevel.CONFIDENTIAL,
            provider_id="openai",
            budget_exhausted=True,  # Credits exhausted!
        ),
    }

# Create coordinator
coordinator = HybridRoutingCoordinator(
    task_router=my_task_router,
    metadata_provider=get_worker_metadata,
)

# Create routing request
request = HybridRoutingRequest(
    routing_request_id="req-001",
    task_request=my_task_request,
    hybrid_policy=policy,
)

# Evaluate
decision = coordinator.evaluate(request)

# Result: LOCAL_CONTINUITY (cloud unavailable, continuing locally)
assert decision.route_kind == ExecutionRouteKind.LOCAL_CONTINUITY
assert decision.can_execute
assert decision.assigned_node_id == "local-worker-1"
```

## Future Work

Potential v0.2+ enhancements:
- Governed cloud execution runtime integration
- Quality-based local capability assessment
- Durable routing decision store
- Per-request budget governance in TaskRouter
- Automatic budget monitoring and alerts
- Provider health probes
- Multi-provider failover orchestration
- Routing decision audit trail

## Conclusion

v0.1 establishes the architectural contracts and decision logic for hybrid routing with credit resilience. The system ensures RAGHub remains useful when cloud resources are unavailable, with explicit decisions and no hidden fallback.

All contracts compose existing infrastructure without duplication. Local continuity is the default resilience mode.
