# Control-Plane Reuse Matrix v0.1

## Document Metadata

**Version:** v0.1
**Canonical Commit:** `0e15dcb15bf58f98d2b2b78a4e2b232534ce1fe6`
**Audit Report:** `/home/matias/RAGHub-Reports/control-plane-reuse-audit-canonical-2026-08-11.md`
**Audit SHA-256:** `bc3cb6d3e024fe87983e85988e5e2bb30830fb7cc14720e74b8bb5fcd93dfdc2`
**Frozen:** 2026-08-11
**Status:** **AUTHORITATIVE**

## Purpose

This document freezes the authoritative reuse matrix for M0 ControlDomain implementation. It defines which existing control-plane components will be REUSED, WRAPPED, EXTENDED, or REBUILT.

**Audit Verdict:** **READY_FOR_M0**

---

## Reuse Strategy Definitions

- **REUSE:** Component can be used as-is without modification. Domain-scoping happens at call sites or through composition.
- **WRAP:** Component logic is sound but needs domain-scoping wrapper. Core implementation unchanged.
- **EXTEND:** Component schema or contract must be extended with domain fields. Existing functionality preserved.
- **REBUILD:** Component cannot satisfy M0 invariants and must be replaced. (None required for M0.)

---

## Authoritative Reuse Matrix

| Component | File | Verdict | Domain Gap | M0 Action | Risk | Tests |
|-----------|------|---------|------------|-----------|------|-------|
| **Authentication & Integrity** |
| HMAC Integrity | `federation/integrity.py` | **REUSE** | None | Use `authentication_tag(key, domain, record)` | Low | Indirect |
| Canonical JSON | (pattern) | **REUSE** | None | Use deterministic serialization | Low | Indirect |
| Timestamp Validation | (pattern) | **REUSE** | None | Use UTC normalization, rollback protection | Low | Indirect |
| **Node & Worker Identity** |
| NodeRecord | `federation/node_record.py` | **WRAP** | High | Add `domain_id` field or domain-filtered registry | High | 26+ |
| NodeRegistry | `federation/registry.py` | **WRAP** | High | Introduce domain-scoped registration/query | **CRITICAL** | 26+ |
| Heartbeat | `federation/heartbeat.py` | **WRAP** | Medium | Validate worker belongs to domain | Medium | 27 |
| HeartbeatRegistry | `federation/heartbeat_registry.py` | **WRAP** | Low | Enforce domain-scoped node registration | Medium | 27 |
| **Routing & Assignment** |
| TaskRequest | `federation/task_request.py` | **EXTEND** | High | Associate with domain-owned mission | High | 26 |
| TaskRouter | `federation/task_router.py` | **WRAP** | Medium | Pass domain-filtered NodeRegistry | Medium | 26 |
| AssignmentRegistry | `federation/assignment_registry.py` | **EXTEND** | Medium | Add `domain_id` to schema | **CRITICAL** | 41 |
| **Dispatch & Execution** |
| DispatchOffer | `federation/dispatch_offer.py` | **EXTEND** | Medium | Validate coordinator/worker domain | High | 12 |
| TaskDispatchCoordinator | `federation/task_dispatcher.py` | **EXTEND** | Medium | Enforce domain-scoped coordinator | **CRITICAL** | 41 |
| WorkerExecution | `federation/worker_execution.py` | **EXTEND** | Medium | Validate actor domain membership | High | 21 |
| **Power Management** |
| PowerCoordinator | `federation/power_coordinator.py` | **WRAP** | Low | Map `controller_authority` to domain | Low | Power |
| **Budget & Governance** |
| BudgetGovernance | `federation/worker_governance.py` | **WRAP** | Medium | Domain-level policy composition | Medium | Router |
| **Mission Lifecycle** |
| MissionCheckpoint | `research_mission/checkpoint.py` | **EXTEND** | High | Add mission-to-domain mapping | High | Checkpoint |
| MissionRecovery | `research_mission/recovery.py` | **EXTEND** | High | Inherit from domain-scoped mission | High | Recovery |
| **Approvals** |
| PowerApproval | `tools/ai_controller/operations_api/approvals.py` | **WRAP** | Medium | Validate actor domain (M0), AgentIdentity (M1) | Medium | Power |
| **Environment & Secrets** |
| Environment Variables | `app.py`, `config.py` | **REUSE** | High | Document for M5 Credential Broker | Low | N/A |

---

## Summary Statistics

**Total Components Analyzed:** 18

**By Verdict:**
- **REUSE:** 3 (17%)
- **WRAP:** 8 (44%)
- **EXTEND:** 7 (39%)
- **REBUILD:** 0 (0%)

**By Domain Gap Severity:**
- **Low:** 4 (22%)
- **Medium:** 9 (50%)
- **High:** 5 (28%)

**By Risk:**
- **Low:** 5 (28%)
- **Medium:** 10 (56%)
- **CRITICAL:** 3 (17%)
  - NodeRegistry (global namespace)
  - TaskDispatchCoordinator (coordinator authority)
  - AssignmentRegistry (fingerprint stability)

---

## Critical Path for M0

### Phase 1: Foundation
1. Implement ControlDomain object (domain_id, name, owner, lifecycle, creation timestamp)
2. Implement DurableControlDomainRegistry (authenticated JSONL, explicit registration, lifecycle enforcement)
3. Add domain context boundary seam

### Phase 2: Identity Scoping
4. Add domain_id field to NodeRecord (or introduce DurableNodeRegistry)
5. Wrap NodeRegistry with domain-scoped registration/query methods
6. Validate domain membership in HeartbeatRegistry.record()

### Phase 3: Routing & Dispatch
7. Add domain_id to AssignmentRegistry schema
8. Validate coordinator belongs to domain in TaskDispatchCoordinator.create_offer()
9. Validate worker belongs to domain in WorkerExecutionCoordinator transitions
10. Pass domain-filtered NodeRegistry to TaskRouter

### Phase 4: Mission & Recovery
11. Add domain mapping to ResearchMission
12. Extend MissionCheckpointStore to validate mission belongs to domain
13. MissionRecoveryEvidenceStore inherits domain-scoping from checkpoint

### Phase 5: Power & Approvals
14. Map PowerCoordinator.controller_authority to ControlDomain.domain_id
15. Validate approval actor belongs to domain

---

## Compatibility Strategy

**Principle:** Existing 2,346 tests must pass without modification (or with minimal, explicit domain setup).

**Approach:**
1. **Default Domain:** Tests without explicit domain setup use "default-domain"
2. **Wrapper Pattern:** Wrap components, don't replace
3. **Gradual Migration:** Domain parameter optional initially, required after full migration
4. **Grandfather Clause:** Existing evidence without domain_id remains valid

**Example Migration Pattern:**
```python
# Before M0
node_registry = NodeRegistry()
node_registry.register(node)

# After M0 (backward compatible)
domain = control_domain_registry.get_or_create_default()
node_registry = NodeRegistry(domain=domain)  # or NodeRegistry() uses default
node_registry.register(node)

# After M0 (domain-explicit)
domain = control_domain_registry.get("example-domain-id")
node_registry = NodeRegistry(domain=domain)
node_registry.register(node)
```

---

## Evidence Guarantee Labels

**M0 provides:**
- Authenticated (HMAC-SHA256)
- Tamper-evident (digest chains)
- Append-only (JSONL with file locks)
- Controller-owned (registry_id, controller_authority)
- Timezone-aware timestamps
- Deterministic fingerprints
- Fail-closed domain boundaries

**M0 does NOT provide:**
- Public-key signatures
- Non-repudiation
- Cross-domain delegation (M2)
- Full EvidenceLedger (M3)
- Universal effect boundary (M4)
- Credential brokering (M5)

---

## M0 Non-Goals (Deferred to Future Milestones)

**M1 AgentIdentity:**
- Durable principal objects
- Provider bindings for agents
- Agent-to-domain membership

**M2 DelegationGrant:**
- Cross-domain task delegation
- Appeal workflow
- Revocable grants

**M3 Evidence Spine:**
- Unified EvidenceLedger
- Public auditability
- Provider-independent evidence export

**M4 Universal Effect Boundary:**
- All effects gated through PowerCoordinator-like flow
- Execution adapter refusal preserved
- Effect evidence linked to principals

**M5 Credential Broker:**
- Domain-scoped CredentialLease
- Secret injection without environment variables
- Provider exit package

---

## Acceptance Criteria for M0

**Functional:**
1. ControlDomain can be explicitly registered
2. Duplicate domain registration rejected
3. Domain lifecycle transitions are durable and non-widening
4. NodeRegistry enforces domain membership
5. HeartbeatRegistry rejects cross-domain workers
6. TaskDispatchCoordinator validates coordinator domain
7. WorkerExecutionCoordinator validates actor domain
8. MissionCheckpoint validates mission belongs to domain
9. PowerCoordinator validates approval actor domain
10. Cross-domain access fails closed

**Regression:**
11. All 2,346 existing tests pass (with default domain setup)
12. Focused M0 tests pass (domain creation, lifecycle, isolation)
13. Adjacent tests pass (routing, dispatch, execution, heartbeat, power)
14. Full test suite passes
15. `git diff --check` clean
16. `compileall -q` succeeds

**Evidence:**
17. Domain registration produces authenticated JSONL
18. Domain lifecycle transitions are tamper-evident
19. Deterministic domain fingerprints
20. Clock-rollback protection
21. Concurrent registration conflicts rejected

---

## Migration Risks & Mitigations

### Risk: Assignment Fingerprint Changes
**Impact:** Adding `domain_id` to AssignmentRegistry changes `assignment_fingerprint`
**Mitigation:** Include `domain_id` in `_routing_payload()`. Existing assignments grandfather as "legacy" (no domain). New assignments include domain. Deterministic ID generation remains stable.

### Risk: Coordinator Authority Validation
**Impact:** TaskDispatchCoordinator must validate coordinator belongs to domain
**Mitigation:** Add check in `create_offer()`: validate `coordinator_node_id` registered in domain. Fail with `DispatchIdentityMismatchError` if coordinator unauthorized.

### Risk: Test Suite Breakage
**Impact:** Adding required domain parameters breaks 2,346 tests
**Mitigation:** Use optional domain parameter with fallback to "default-domain". Gradually migrate tests. Remove fallback after full migration.

### Risk: Worker Heartbeat Cross-Domain Replay
**Impact:** Worker registered in Domain A submits heartbeat to Domain B
**Mitigation:** HeartbeatRegistry already validates `node_registry.get(worker_id)` at line 134. Ensure NodeRegistry is domain-scoped. Existing validation prevents cross-domain leakage.

### Risk: Mission Ownership Ambiguity
**Impact:** ResearchMission has no explicit owner, only `mission_id`
**Mitigation:** Add `domain_id` field to ResearchMission. Validate in MissionCheckpointStore.save(). Use deterministic mission_id (UUID) to prevent collisions.

---

## PowerCoordinator as M4 Pattern

PowerCoordinator demonstrates the ideal M4 universal effect boundary pattern:

**Flow:**
1. **Request:** Worker proposes power action
2. **Validate:** Component registered with controller authority
3. **Evaluate:** Policy determines approval requirement
4. **Approve:** Explicit user approval or automatic policy
5. **Authorize:** Time-bound authorization with expiration
6. **Execute:** Adapter attempts action (retains refusal authority)
7. **Evidence:** All transitions authenticated and append-only
8. **Reconcile:** Manual resolution if outcome ambiguous

**M4 will generalize this to all effects, not just power management.**

---

## Document Signature

**Frozen By:** RAGHub Principal Control-Plane Integration Engineer
**Frozen Date:** 2026-08-11
**Canonical Commit:** 0e15dcb15bf58f98d2b2b78a4e2b232534ce1fe6
**Audit Report SHA-256:** bc3cb6d3e024fe87983e85988e5e2bb30830fb7cc14720e74b8bb5fcd93dfdc2

**Status:** **AUTHORITATIVE**

This matrix is now frozen and authoritative for M0 ControlDomain implementation.

---
