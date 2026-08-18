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

This checkpoint integrates all four accepted MissionaryX subsystems:
- Mission Runtime v0.1
- Agent Identity & Delegation v0.1
- Access & Credential Broker v0.1
- DurableEffectStore concurrent migration correction

**Verification**:
```bash
git rev-parse HEAD
# Should output: 5637f813ce1669cd288b47edc946a71ea53dc63e

./tools/verify-checkpoint 5637f813ce1669cd288b47edc946a71ea53dc63e
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
git worktree add /path/to/new-worktree -b feature/your-feature-name 5637f813
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
# Verify checkpoint
git rev-parse HEAD

# Verify clean tree
git status

# Run quick validation
./tools/validate-missionaryx --quick
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

## Architecture Boundaries: DO NOT CROSS

The following implementation files enforce critical MissionaryX invariants and must **NOT** be modified without architectural review and explicit justification:

**Effect Truth**:
- `federation/durable_effect_store.py`
- `federation/effect_gateway.py`
- `federation/effect_boundary.py`
- `federation/canonical_digest.py`
- `pavilionos/canonical_adapter.py`
- `pavilionos/canonical_coordinator.py`

**Authority Truth**:
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

**If your work appears to require changing these files, STOP and report why.**

See `ARCHITECTURE.md` for full architectural documentation.

---

## Validation and Testing

### Quick Validation

Runs high-value focused tests:
```bash
./tools/validate-missionaryx --quick
```

### Full Validation

Runs complete test suite with documented deselections:
```bash
./tools/validate-missionaryx --full
```

Or manually:
```bash
PYTHONDONTWRITEBYTECODE=1 pytest -p no:cacheprovider -q \
  --deselect=tests/test_llamacpp_adapter.py::test_timeout_returns_error_response
```

### Static Checks

```bash
git diff --check
python3 -m compileall .
```

---

## AI Development Model Usage

MissionaryX development uses multiple AI assistants in specific roles:

### Fedora HP Pavilion (This Machine)

**AI Tool**: Claude Code (pinned v2.0.14, npm-installed, no AVX support)

**Role**: Programming and development

**Constraint**: This CPU has zero AVX support. Native `claude` builds (Bun runtime, 2.0.15+) crash with "Illegal instruction." Do NOT run `claude update`.

**Model**: Sonnet 4.5 (no /effort settings on this version)

### Claude.ai Chat

**Role**: Planning and idea review ONLY — not programming

### Windows HP 14

**AI Tools**: ChatGPT, Codex, GitHub Copilot

**Role**: Programming on Windows-specific features

### Planning Workflow

- Compare approaches using both Claude.ai chat and ChatGPT
- Implementation happens on the appropriate machine
- Handoff between machines requires explicit preparation

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

**Status**: In progress (this document is part of this milestone)

**Next After This**: To be determined based on architectural priorities and subsystem needs.

See `DEVELOPMENT_PROTOCOL.md` for the full development lifecycle.

---

## Local Model Support

MissionaryX includes comprehensive local model infrastructure:

**Status**: AVAILABLE NOW

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

These must hold for ALL MissionaryX operations:

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
- [ ] Run quick validation: `./tools/validate-missionaryx --quick`
- [ ] Review `ARCHITECTURE.md` for relevant subsystems
- [ ] Understand protected boundaries for your work
- [ ] Create focused worktree/branch for your feature
- [ ] Document your base checkpoint if different from integrated

**If anything is unclear or contradictory, ask before proceeding.**
