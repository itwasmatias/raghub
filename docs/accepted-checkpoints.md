# MissionaryX Accepted Checkpoints

This document records the accepted MissionaryX checkpoints and their significance. Each checkpoint represents an independently verified, functional integration point for a major subsystem.

## Important Principles

1. **Full SHA Required**: Checkpoint identity requires the full 40-character SHA-1 commit hash. Abbreviated SHAs are never sufficient for checkpoint verification.

2. **Independent Acceptance**: A checkpoint identifies when a subsystem was independently accepted. A later commit does not automatically invalidate an earlier subsystem checkpoint; the SHA identifies the point at which that subsystem was verified.

3. **Subsystem Independence**: Different subsystems may have different accepted checkpoints. The most recent integrated checkpoint combines all accepted subsystems.

4. **Non-Blocking Observations**: Known observations that do not violate required invariants are documented but do not prevent checkpoint acceptance.

## Accepted Subsystem Checkpoints

### DurableEffectStore Concurrent Migration Correction

**Checkpoint SHA**: `5637f813ce1669cd288b47edc946a71ea53dc63e`

**Date**: 2026-08-17 (approximate)

**Subsystem**: Effect Durability and Migration

**Purpose**: Corrected concurrent migration handling in DurableEffectStore to ensure migration operations converge correctly when multiple processes attempt migration simultaneously.

**Major Invariants Established**:
- Concurrent migrations converge to consistent schema state
- Migration operations are idempotent and safe for retry
- File-based locking prevents migration conflicts
- Schema version transitions are atomic

**Supersedes**: None (corrective enhancement)

**Known Non-Blocking Observations**:
- Malformed non-integer schema-version metadata currently fails closed through `ValueError` rather than the schema-specific exception. This is a fail-safe behavior and does not compromise effect durability.

**Validation Evidence**: Comprehensive durability and attack test suites pass.

**Related Subsystems**: Integrates with Effect Gateway, Mission Runtime, and Access/Credential Broker.

---

### Access & Credential Broker v0.1

**Checkpoint SHA**: `dba334668608034c19efc3e2d9c791b7b8f74583`

**Date**: 2026-08-17 (approximate)

**Subsystem**: Access and Credential Management

**Purpose**: Established the Access & Credential Broker foundation that separates credential retrieval authorization from credential use, and binds broker requests to provider and canonical scopes.

**Major Invariants Established**:
- Authentication ≠ authorization
- Authorization ≠ credential retrieval
- Credential retrieval ≠ effect dispatch
- Access requirements are evaluated independently
- Credential backend selection respects provider scope
- Canonical digest binds credentials to specific contexts
- Secret leases are not exposed in public interfaces

**Supersedes**: None (new subsystem)

**Key Commits in Foundation**:
- `c29ad67`: Remove public secret lease and complete broker proofs
- `5ef8456`: Bind authority evaluation to grant grantee identity
- `20558fa`: Separate credential authorization from secret use
- `f55f92f`: Gate credential use on MissionaryX authority
- `451e32f`: Add Access and Credential Broker v0.1 foundation

**Validation Evidence**: Comprehensive broker foundation tests covering authorization, scope binding, and secret handling.

**Related Subsystems**: Integrates with Agent Identity & Delegation, Mission Runtime, and Effect Gateway.

---

### Agent Identity & Delegation v0.1

**Checkpoint SHA**: `840d7645045a02174509559637ab9af1ad215e89`

**Date**: 2026-08-17 (approximate)

**Subsystem**: Agent Identity and Authority Delegation

**Purpose**: Established agent identity model and delegation grant system that separates identity from authority and enables controlled capability delegation.

**Major Invariants Established**:
- Identity ≠ authority
- Connection ≠ authority
- Delegation grants are explicit and scoped
- Authority evaluation is bound to grant grantee identity
- AgentIdentity is immutable once created
- DelegationGrant lifecycle is deterministic and auditable

**Supersedes**: None (foundational subsystem)

**Key Related Commits**:
- `cfc2164`: Extend MissionaryX Agent Identity & Delegation to v0.1 authority-contract compliance
- `c1382ac`: Correct authority enforcement and durability semantics
- `686457e`: Complete legacy authority migration proof

**Validation Evidence**: Comprehensive authority foundation and correction test suites pass, including durability and enforcement tests.

**Related Subsystems**: Foundation for Access & Credential Broker and Mission Runtime authority model.

---

### Mission Runtime v0.1

**Checkpoint SHA**: `721f75683fd53ae0549645c70653bf981c713c6d`

**Date**: 2026-08 (approximate)

**Subsystem**: Mission Lifecycle and Execution

**Purpose**: Established the foundational Mission Runtime that provides durable mission state management, checkpoint/resume capability, and recovery evidence.

**Major Invariants Established**:
- Mission state transitions are durable and auditable
- Mission lifecycle is independent of task lifecycle
- Checkpoint and resume operations preserve mission continuity
- Task failure ≠ effect failure
- Mission reference ≠ mission completion
- Recovery evidence is preserved

**Supersedes**: None (foundational subsystem)

**Validation Evidence**: Mission runtime foundation, checkpoint/resume, and recovery evidence test suites pass.

**Related Subsystems**: Integrates with Agent Identity & Delegation for authority, Access & Credential Broker for credentials, and Effect Gateway for effect dispatch.

---

## Foundational Work: Pavilion and Governed Effect

The accepted checkpoints build upon earlier foundational work in Pavilion OS and Governed Effect architecture, present in the repository history:

**Pavilion Canonical Adapter and Coordinator**:
- Provides canonical effect binding and coordination
- Establishes crash atomicity and concurrency safety
- Located in `pavilionos/canonical_adapter.py` and `pavilionos/canonical_coordinator.py`

**Governed Effect Gateway**:
- Provides effect boundary enforcement
- Separates dispatch from effect success
- Located in `federation/effect_gateway.py` and `federation/effect_boundary.py`

**DurableEffectStore**:
- Provides append-only, crash-safe effect persistence
- Supports schema migration and concurrent access
- Located in `federation/durable_effect_store.py`

These foundational components were established through earlier development cycles and are referenced by the subsystem checkpoints above.

---

## Integrated Checkpoint

**Current Integrated Checkpoint**: `5637f813ce1669cd288b47edc946a71ea53dc63e`

**Integration Status**: This checkpoint represents the most recent integration of all accepted subsystems:
- Mission Runtime v0.1
- Agent Identity & Delegation v0.1
- Access & Credential Broker v0.1
- DurableEffectStore concurrent migration correction

**Development Continuity**: Future development should ordinarily start from this integrated checkpoint unless working on an independent subsystem that requires a different base.

---

## Known Non-Blocking Issues

### Inherited Test Flakiness

**Test**: `tests/test_llamacpp_adapter.py::test_timeout_returns_error_response`

**Status**: Inherited flaky timeout behavior

**Impact**: Non-blocking; test may be explicitly deselected during full-suite validation

**Deselection**: When running full validation, this test should be deselected using:
```bash
pytest --deselect=tests/test_llamacpp_adapter.py::test_timeout_returns_error_response
```

**Rationale**: This is a timeout-sensitive test for local model infrastructure. The timeout behavior is inherited and does not affect core MissionaryX subsystem invariants.

---

## Verification Procedure

To verify a checkpoint matches your current repository state:

```bash
# Show current commit
git rev-parse HEAD

# Compare with checkpoint (example)
test "$(git rev-parse HEAD)" = "5637f813ce1669cd288b47edc946a71ea53dc63e" && \
  echo "CHECKPOINT_MATCH" || echo "CHECKPOINT_MISMATCH"
```

For automated verification, use:
```bash
./tools/verify-checkpoint 5637f813ce1669cd288b47edc946a71ea53dc63e
```

---

## Adding New Checkpoints

When a new subsystem or integration is independently verified:

1. Record the full SHA-1 commit hash
2. Document the subsystem name and purpose
3. List major invariants established
4. Note any known non-blocking observations
5. Reference validation evidence (test suites, manual verification, etc.)
6. Identify whether this checkpoint supersedes an earlier one
7. Update the integrated checkpoint if this represents full integration

Never designate a checkpoint as accepted without independent verification by a separate reviewer (human or AI).
