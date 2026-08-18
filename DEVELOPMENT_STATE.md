# MissionaryX Development State

**READ THIS FIRST** when starting new development work on MissionaryX.

This document provides the current state of the project, accepted checkpoints, known issues, and guidance for development continuation.

---

## What is MissionaryX?

MissionaryX is a **federated AI operating system** built on top of RAGHub, providing governed, durable, and auditable AI agent operations across multiple independent truths:

1. **Mission Truth**: Durable mission lifecycle, checkpointing, and recovery
2. **Authority Truth**: Agent identity and delegation-based authority
3. **Access/Credential Truth**: Credential retrieval authorization and secret management
4. **Effect Truth**: Governed effect dispatch with crash-safe persistence

MissionaryX enables:
- Durable AI agent missions that survive failures
- Explicit authority delegation (identity ≠ authority)
- Credential access control separate from authorization
- Crash-safe effect persistence with schema migration
- Hybrid compute across Fedora and Windows nodes
- Local model support via llama.cpp integration

The project also includes **SIP (Sports Intelligence Platform)**, a sports analytics application that demonstrates RAGHub's intelligence lifecycle.

---

## Current Accepted Checkpoint

**Integrated Checkpoint**: `5637f813ce1669cd288b47edc946a71ea53dc63e`

**Branch**: `feature/development-continuity-v0-1`

**Python Version**: 3.14.6

**Commit Message**: "Make concurrent durable-store migration converge"

This checkpoint integrates all four accepted MissionaryX subsystem semantics:

| Subsystem | Independently accepted checkpoint | Current-lineage integration |
|---|---|---|
| Mission Runtime v0.1 | `721f75683fd53ae0549645c70653bf981c713c6d` | Exact ancestor |
| Agent Identity & Delegation v0.1 | `840d7645045a02174509559637ab9af1ad215e89` | Patch-equivalent commit `5ef84562cffb2e3227e75e6b66d740bf39650927` |
| Access & Credential Broker v0.1 | `dba334668608034c19efc3e2d9c791b7b8f74583` | Exact ancestor |
| DurableEffectStore correction | `5637f813ce1669cd288b47edc946a71ea53dc63e` | Exact ancestor and integrated base |

The independently accepted authority checkpoint
`840d7645045a02174509559637ab9af1ad215e89` is not an ancestor of this
lineage. Independent review confirmed that
`5ef84562cffb2e3227e75e6b66d740bf39650927` has matching patch identity and
source content. The accepted authority semantics are integrated through that
patch-equivalent commit; the original accepted SHA itself is not an ancestor.

**Verification**:
```bash
git rev-parse HEAD
# Should output: 5637f813ce1669cd288b47edc946a71ea53dc63e

./tools/verify-checkpoint --require-clean 5637f813ce1669cd288b47edc946a71ea53dc63e
```

See `docs/accepted-checkpoints.md` for full checkpoint history and subsystem details.

---

## Starting New Work

### Recommended Base

New development should ordinarily start from:
```
commit: 5637f813ce1669cd288b47edc946a71ea53dc63e
branch: feature/development-continuity-v0-1
```

**Exception**: If working on an independent subsystem that requires a different base, explicitly document the base checkpoint and rationale.

### Worktree Setup

Create a dedicated worktree for focused development:
```bash
cd /home/matias/raghub
git worktree add /path/to/new-worktree -b feature/your-feature-name \
  5637f813ce1669cd288b47edc946a71ea53dc63e
```

### Environment Setup

```bash
cd /path/to/worktree
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

See `ENVIRONMENT_SETUP.md` for detailed environment setup instructions.

### Verification Before Starting

```bash
# Verify exact checkpoint and clean tree
./tools/verify-checkpoint --require-clean \
  5637f813ce1669cd288b47edc946a71ea53dc63e

# Run quick validation
./tools/validate-missionaryx --quick \
  --base 5637f813ce1669cd288b47edc946a71ea53dc63e
```

---

## Known Non-Blocking Issues

### 1. Flaky Timeout Test

**Test**: `tests/test_llamacpp_adapter.py::test_timeout_returns_error_response`

**Status**: Inherited flaky timeout behavior

**Impact**: Non-blocking; explicitly deselected during full validation

**Deselection**:
```bash
pytest --deselect=tests/test_llamacpp_adapter.py::test_timeout_returns_error_response
```

**Rationale**: This test validates timeout handling in local model infrastructure. The timeout is environmentally sensitive and does not affect core MissionaryX subsystem invariants.

**Action**: Do NOT attempt to "fix" this test unless specifically working on llama.cpp adapter timeout handling. Deselect during validation.

---

### 2. DurableEffectStore Schema Version Exception

**Component**: `federation/durable_effect_store.py`

**Observation**: Malformed non-integer schema-version metadata fails closed through `ValueError` rather than the schema-specific exception.

**Status**: Non-blocking; fail-safe behavior

**Impact**: Does not compromise effect durability; fails safely

**Action**: Document this behavior; do NOT modify without careful review of DurableEffectStore durability guarantees.

---

## Inherited Architectural Debt

The newer `AccessCredentialBroker.authorize()` returns non-secret
`AccessCredentialAuthorization` and neither resolves nor returns reusable
credentials. Separately, the inherited legacy `CredentialBroker` remains a
public raw-secret boundary: its root-exported `CredentialLease` exposes
`secret_bytes()` and `secret_text()`. Access & Credential Broker v0.1 did not
introduce that legacy API and does not establish a repository-wide "no public
raw-secret lease" property.

---

## Architecture Boundaries: DO NOT CROSS

The following implementation files enforce critical MissionaryX invariants and must **NOT** be modified without architectural review and explicit justification:

<!-- PROTECTED_INVENTORY_START -->

**Effect Truth**:
- `federation/durable_effect_store.py`
- `federation/effect_gateway.py`
- `federation/effect_boundary.py`
- `federation/effect_safety.py`
- `federation/canonical_digest.py`
- `pavilionos/canonical_adapter.py`
- `pavilionos/canonical_coordinator.py`

**Authority Truth**:
- `federation/agent_identity.py`
- `federation/agent_identity_registry.py`
- `federation/authority_evaluator.py`
- `federation/delegation_grant.py`
- `federation/delegation_grant_registry.py`
- `federation/agent_authority_store.py`

**Access/Credential Truth**:
- `federation/access_credential_broker.py`
- `federation/access_connection.py`
- `federation/access_credential_store.py`
- `federation/access_requirement.py`
- `federation/authentication_session.py`
- `federation/credential_backend.py`
- `federation/credential_broker.py`

**Mission Truth**:
- `federation/mission_state.py`
- `federation/mission_runtime.py`
- `federation/mission_runtime_store.py`

<!-- PROTECTED_INVENTORY_END -->

**If your work appears to require changing these files, STOP and report why.**

See `ARCHITECTURE.md` for full architectural documentation.

---

## Validation and Testing

### Quick Validation

Runs high-value focused tests:
```bash
./tools/validate-missionaryx --quick \
  --base 5637f813ce1669cd288b47edc946a71ea53dc63e
```

### Full Validation

Runs complete test suite with documented deselections:
```bash
./tools/validate-missionaryx --full \
  --base 5637f813ce1669cd288b47edc946a71ea53dc63e
```

Or manually:
```bash
PYTHONDONTWRITEBYTECODE=1 pytest -p no:cacheprovider -q \
  --deselect=tests/test_llamacpp_adapter.py::test_timeout_returns_error_response
```

### Static Checks

```bash
git diff --check
bytecode_cache="$(mktemp -d)"
PYTHONPYCACHEPREFIX="$bytecode_cache" python3 -m compileall -q -f .
rm -rf -- "$bytecode_cache"
```

---

## Optional AI Assistance

MissionaryX development is model- and provider-independent. No hosted model,
specific model family, or AI coding tool is required. Human development, Git,
and deterministic tests remain sufficient and authoritative.

**Historical environment observation**: On 2026-08-17 the Fedora HP Pavilion
reported Python 3.14.6 and a CPU without AVX support. Tool and model availability
are mutable machine observations and must be rechecked; they are not project
requirements and do not justify pinning a hosted model in this state record.

AI-assisted handoffs must identify the actual tool/environment used and preserve
the same checkpoint, scope, and validation evidence required of human work.

---

## Acceptance Determination

Work is considered accepted when:

1. **Implementation is complete** according to the defined scope
2. **Focused validation passes** (subsystem-specific tests)
3. **Full validation passes** (complete test suite with documented deselections)
4. **Independent review verifies** implementation and invariants
5. **Reviewer reproduces** important invariants directly
6. **Checkpoint is designated** with full SHA and documentation

**Acceptance authority**: Independent reviewer (human or AI), never the original implementer alone.

**Evidence**: Executable tests outweigh handoff claims.

---

## Next Planned Milestone

**Current Milestone**: Development Continuity v0.1

**Purpose**: Ensure MissionaryX remains understandable, testable, and developable without hosted AI services.

**Status**: Candidate awaiting fresh independent review. This milestone has not
been independently accepted or designated as a checkpoint.

**Next After This**: To be determined based on architectural priorities and subsystem needs.

See `DEVELOPMENT_PROTOCOL.md` for the full development lifecycle.

---

## Local Model Support

MissionaryX contains local-model adapter, lifecycle, registry, capacity, and
governed-inference code support.

**Status**: CODE SUPPORT PRESENT; RUNNABLE LOCAL ENVIRONMENT NOT PROVEN BY THIS
CHECKOUT

**Components**:
- `federation/llamacpp_adapter.py`: llama.cpp integration
- `federation/local_model_server_lifecycle.py`: Server lifecycle management
- `federation/local_model_artifact_registry.py`: Model artifact registry
- `federation/host_resource_capacity.py`: Resource telemetry and capacity guards

**Documentation**:
- `docs/local-only-pilot-v0-1.md`
- `docs/local-model-server-lifecycle-v0.1.md`
- `docs/local-model-artifact-registry-v0.1.md`
- `docs/governed-local-inference-integration-v0.1.md`

**Usage**: See `LOCAL_AI_DEVELOPMENT.md` for local model development workflow.

This checkout does not itself prove that `llama-server`, compatible model
weights, an offline wheelhouse, or a live attested inference environment is
present. Local AI is optional assistance.

**Important**: Local models are not automatically trusted. Deterministic tests and Git remain the acceptance authority.

---

## Repository Structure

```
/home/matias/missionaryx-development-continuity-v0-1/
├── ARCHITECTURE.md                    # Full architectural documentation
├── DEVELOPMENT_STATE.md               # This document
├── DEVELOPMENT_PROTOCOL.md            # Development lifecycle
├── ENVIRONMENT_SETUP.md               # Python environment setup
├── LOCAL_AI_DEVELOPMENT.md            # Local model development workflow
├── OFFLINE_SURVIVAL.md                # Offline development procedure
├── README.md                          # SIP application documentation
├── docs/
│   ├── accepted-checkpoints.md        # Checkpoint history
│   └── [40+ design documents]
├── federation/                        # MissionaryX core subsystems
├── pavilionos/                        # Pavilion canonical infrastructure
├── tools/
│   ├── validate-missionaryx           # Validation script
│   ├── verify-checkpoint              # Checkpoint verification
│   └── [other tooling]
├── tests/                             # Comprehensive test suite
├── scripts/                           # Utility scripts
├── Makefile                           # Standard operations
├── requirements-fedora-core.txt       # Core dependencies (no NumPy/pandas)
├── requirements-dev.txt               # Development dependencies
└── missionaryx-state.json             # Machine-readable state
```

---

## Critical Invariants

These separations define the adopted governed control model and are evidenced for
the governed paths covered by current acceptance and validation. That evidence
does not demonstrate universal enforcement across every MissionaryX operation,
repository entry point, or runtime capability:

- `identity != authority`
- `connection != authority`
- `authentication != authority`
- `authorization != credential retrieval`
- `credential retrieval != effect dispatch`
- `dispatch != effect success`
- `task failure != effect failure`
- `mission reference != mission completion`

See `ARCHITECTURE.md` for detailed invariant explanations.

---

## Getting Help

**Documentation**:
1. Read this document first
2. Review `ARCHITECTURE.md` for system design
3. Review `DEVELOPMENT_PROTOCOL.md` for workflow
4. Check `docs/accepted-checkpoints.md` for checkpoint history
5. Review subsystem documentation in `docs/`

**Before Asking Questions**:
- Verify you're starting from the correct checkpoint
- Run validation to ensure your environment is correct
- Check if your question relates to a known non-blocking issue
- Review whether your work would cross a protected boundary

**Scope Concerns**:
If work scope appears to widen beyond the original plan, STOP and report the scope change before continuing.

---

## Summary: Start Here Checklist

Before starting development:

- [ ] Read this document completely
- [ ] Verify checkpoint: `5637f813ce1669cd288b47edc946a71ea53dc63e`
- [ ] Verify clean tree: `git status`
- [ ] Create/activate Python environment
- [ ] Run quick validation with exact base: `./tools/validate-missionaryx --quick --base 5637f813ce1669cd288b47edc946a71ea53dc63e`
- [ ] Review `ARCHITECTURE.md` for relevant subsystems
- [ ] Understand protected boundaries for your work
- [ ] Create focused worktree/branch for your feature
- [ ] Document your base checkpoint if different from integrated

**If anything is unclear or contradictory, ask before proceeding.**
