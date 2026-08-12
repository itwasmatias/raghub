# Execution Prompt Engine v0.1 — Design Document

## Overview

The Execution Prompt Engine v0.1 compiles deterministic, validated execution prompts from:
- Approved milestone specifications
- Fresh development-status handoffs
- Versioned policy profiles

It produces canonical, fingerprinted prompt artifacts with structured rejections for invalid inputs.

## Module Structure

### `tools/execution_prompt/`

New module containing:
- `compiler.py` - Core prompt compilation logic
- `models.py` - Prompt domain models
- `policy.py` - Policy profile definitions
- `validation.py` - Input validation
- `__init__.py` - Public API

### `federation/effect_safety.py`

New module extending `effect_boundary.py` with:
- Effect intent/dispatch separation
- Effect state machine (nothing_landed, something_landed, indeterminate)
- Authority reservation tracking
- Reconciliation obligations
- Provider capability contracts
- Mission posture and lifecycle

## Prompt Compilation Flow

```
Milestone Spec + Dev Status + Policy
              ↓
        Input Validation
              ↓
    Deterministic Compilation
              ↓
    Section Assembly (17 sections)
              ↓
   Canonical Serialization
              ↓
    SHA-256 Fingerprinting
              ↓
   Prompt Artifact + Fingerprint
```

## Effect Safety State Machine

```
Effect Request
      ↓
Intent Commitment (write-ahead)
      ↓
[Authority Reserved]
      ↓
Dispatch Attempt
      ↓
      ├─→ Nothing Landed (affirmative evidence) → [Authority Released]
      ├─→ Something Landed (committed) → [Authority Consumed]
      └─→ Indeterminate (uncertain) → [Authority Held] → Reconciliation Obligation
                                                                  ↓
                                                          [Probe until resolved]
                                                                  ↓
                                                          ├─→ Resolved → Release/Consume
                                                          └─→ Unreconcilable → Terminal Disposition
```

## Acceptance Criteria

### Prompt Compiler
- ✓ Deterministic compilation from canonical inputs
- ✓ Identical fingerprints for identical inputs
- ✓ Changed fingerprint when behavior-affecting data changes
- ✓ Rejects malformed/stale/contradictory dev-status
- ✓ Rejects secret-bearing inputs
- ✓ All 17 required sections present
- ✓ No LLM calls required for compilation

### Effect Safety
- ✓ Intent committed before dispatch
- ✓ Intent-without-dispatch distinguishable from possibly-escaped dispatch
- ✓ Valid nothing_landed, something_landed, indeterminate states
- ✓ Indeterminate cannot resolve from task state/timeout/retry
- ✓ Only boundary reconciliation evidence resolves indeterminate
- ✓ Authority reservation held/consumed/released correctly
- ✓ Retry cannot double-spend indeterminate reservation
- ✓ Non-reconcilable provider eligibility guard
- ✓ Terminal disposition requires explicit policy/escalation
- ✓ Mission posture folds over effects and compensation
- ✓ Sealing rejects unresolved indeterminate or unsettled compensation

## REUSE / WRAP / EXTEND Decisions

### REUSE
- `effect_boundary.py` - Request/Decision/Attempt abstractions
- `evidence_spine.py` - Normalized evidence records
- `mission/models.py` - Mission/task definitions
- `integrity.py` - Canonical serialization, authentication
- `power_action.py` - Idempotency patterns

### EXTEND
- Effect Boundary → Effect Safety (add intent/dispatch/reconciliation)
- Mission models → Mission Posture (add effect-state folding)

### NEW
- Execution Prompt Compiler (no existing equivalent)
- Provider Capability Contract (routing eligibility primitive)
