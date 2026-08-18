# Local AI Development with MissionaryX

This document explains how to use local models for MissionaryX development when hosted AI services (Claude, ChatGPT, etc.) are unavailable or undesired.

---

## Important Principles

1. **Local models are not automatically trusted** - Validation tests and Git remain the acceptance authority
2. **Deterministic verification** - All work must be verified through executable tests
3. **Evidence-based acceptance** - Model output is a tool, not proof of correctness
4. **Human-readable code** - Code must remain understandable without AI assistance

---

## Current Local Model Infrastructure

### AVAILABLE CODE SUPPORT

MissionaryX includes local-model contracts and implementations that can support
a governed local environment when their external prerequisites are supplied and
attested:

**Components**:
- `federation/llamacpp_adapter.py`: llama.cpp integration with OpenAI-compatible API
- `federation/local_model_server_lifecycle.py`: Managed llama-server lifecycle
- `federation/local_model_artifact_registry.py`: Model artifact management
- `federation/host_resource_capacity.py`: Resource telemetry and capacity guards
- `tools/ai_controller/governed_local_inference.py`: Governed local inference

**Documentation**:
- `docs/local-only-pilot-v0-1.md`: Small local-only capability pilot
- `docs/local-model-server-lifecycle-v0.1.md`: Server lifecycle management
- `docs/local-model-artifact-registry-v0.1.md`: Artifact registry design
- `docs/governed-local-inference-integration-v0.1.md`: Governed inference integration

**Test Coverage**:
- `tests/test_llamacpp_adapter.py`: Adapter functionality
- `tests/test_local_model_server_lifecycle.py`: Lifecycle management
- `tests/test_local_model_artifact_registry.py`: Registry operations
- `tests/test_governed_local_inference.py`: Governed inference
- `tests/test_local_only_pilot.py`: End-to-end pilot

### PROVEN RUNNABLE LOCAL ENVIRONMENT

**Not established by this checkout alone.** Repository code and fake-backed
tests do not prove that the current machine has:

- a `llama-server` executable
- compatible, verified model weights
- an offline Python wheelhouse or dependency cache
- a live, attested server ready for governed inference

Verify those prerequisites directly before claiming live readiness. Local AI is
optional assistance; Git history and deterministic tests remain acceptance
authority.

### NOT YET IMPLEMENTED

The following are conceptual or planned:

- **Autonomous code development**: Local models do not currently perform autonomous development
- **Multi-model coordination**: No distributed local model orchestration
- **Automatic test generation**: Tests are manually written or AI-assisted, not automated
- **Self-modifying code**: Local models do not modify MissionaryX core implementation autonomously

---

## Local Model Use Cases

### 1. Code Explanation

**Use local models for**:
- Understanding unfamiliar code sections
- Explaining architectural patterns
- Documenting complex logic
- Learning about subsystems

**Example workflow**:
```bash
# Start local model server (if not already running)
# Configure model endpoint and model selection

# Use model to explain code
# Input: federation/delegation_grant.py
# Prompt: "Explain the DelegationGrant lifecycle and key invariants"

# Model provides explanation
# You verify explanation against actual code
```

**Important**: Verify model explanations against source code. Models can hallucinate or misunderstand.

---

### 2. Repository Search and Summarization

**Use local models for**:
- Finding relevant code files for a task
- Summarizing subsystem responsibilities
- Identifying dependencies between components
- Understanding test coverage

**Example workflow**:
```bash
# Prompt: "Find all files related to effect durability"
# Model suggests: federation/durable_effect_store.py, tests/test_durable_effect_store*.py

# Verify suggestions with grep/find
grep -r "DurableEffectStore" --include="*.py"
```

**Important**: Always verify search results. Use Git/grep/find for authoritative answers.

---

### 3. Test Drafting

**Use local models for**:
- Drafting initial test structure
- Generating test cases based on requirements
- Suggesting edge cases
- Creating test fixtures

**Example workflow**:
```bash
# Prompt: "Draft a test for verifying checkpoint mismatch detection in tools/verify-checkpoint"
# Model generates test structure

# You review and adapt:
# - Verify test logic is correct
# - Ensure test follows repository patterns
# - Add to tests/test_development_continuity.py
# - Run test to verify it works
```

**Important**: All tests must pass and verify actual invariants. Do not accept failing tests.

---

### 4. Small Code Changes

**Use local models for**:
- Drafting simple refactorings
- Generating boilerplate code
- Suggesting bug fixes
- Creating utility functions

**Example workflow**:
```bash
# Prompt: "Draft a helper function to parse pytest output and extract pass/fail counts"
# Model generates function

# You review:
# - Check logic correctness
# - Verify error handling
# - Add type hints if needed
# - Write tests for the function
# - Validate function works correctly
```

**Important**: Never commit AI-generated code without review and testing.

---

### 5. Diff Review

**Use local models for**:
- Reviewing changes before commit
- Identifying potential issues
- Suggesting improvements
- Checking for unintended side effects

**Example workflow**:
```bash
# Generate diff
git diff > changes.diff

# Prompt: "Review this diff for potential issues, focusing on architectural invariants"
# Attach changes.diff

# Model identifies concerns
# You evaluate concerns and decide whether to address them
```

**Important**: Model review supplements, not replaces, human/peer review.

---

### 6. Validation Failure Analysis

**Use local models for**:
- Understanding why tests failed
- Suggesting fixes for test failures
- Identifying root causes
- Generating debugging strategies

**Example workflow**:
```bash
# Test fails with error message
# Prompt: "This test is failing with [error]. Suggest debugging approaches."
# Model suggests potential causes and debugging steps

# You investigate using suggested approaches
# Verify actual cause through debugging
# Fix the real issue
```

**Important**: Verify model's diagnosis. Don't blindly apply suggested fixes.

---

## Local Model Workflow

### Setup

1. Verify a compatible `llama-server` binary and model artifact by digest.
2. Follow `docs/local-model-artifact-registry-v0.1.md` and
   `docs/local-model-server-lifecycle-v0.1.md` to register and manage them.
3. Require an eligible, attested lifecycle record at the governed endpoint
   `http://127.0.0.1:18080`.
4. Use `tools/ai_controller/governed_local_inference.py`; do not start an
   unmanaged server or treat a listening loopback port as lifecycle identity.

### Basic Usage

The governed integration requires the exact endpoint, expected lifecycle and
attestation fingerprints, process identity, artifact identity, and inference
execution authority. Consult `tests/test_governed_local_inference.py` for a
deterministic construction example. A direct unauthenticated HTTP request is not
the governed workflow.

### Integration with Python

The adapter interface shape is:

```python
from federation.llamacpp_adapter import LlamaCppLocalAdapter

# adapter_config, model, inference_request, and runtime_config must come from
# the validated runtime/lifecycle flow; this is not a standalone bootstrap.
adapter = LlamaCppLocalAdapter(config=adapter_config)
response = adapter.infer(model, inference_request, runtime_config)
print(response.output_text)
```

The governed integration additionally binds that request to the attested
`http://127.0.0.1:18080` lifecycle. Do not replace it with an unmanaged adapter
or server invocation.

---

## Local Model Limitations

### What Local Models Cannot Do

1. **Replace deterministic tests**: Tests must verify actual behavior, not model assertions
2. **Self-certify correctness**: Model saying code is correct does not make it correct
3. **Bypass validation**: All code must pass full validation regardless of source
4. **Guarantee architectural compliance**: Model may not understand all invariants
5. **Replace human judgment**: Final decisions remain with human developers/reviewers

### Quality Considerations

- **Small models**: Lower capability than hosted models (e.g., GPT-4, Claude Sonnet)
- **Context limits**: Smaller context windows limit large-file analysis
- **Hallucination risk**: Models may confidently assert incorrect information
- **No internet access**: Models cannot fetch current documentation or recent updates
- **Training cutoff**: Models may have outdated knowledge

---

## Validation Authority Hierarchy

**In order of authority**:

1. **Executable tests**: Tests either pass or fail - no ambiguity
2. **Git history**: Commits are immutable facts
3. **Source code**: Code behavior is deterministic
4. **Documentation**: Written by humans, reviewed by humans
5. **Model explanations**: Useful but not authoritative

**Always prefer higher authority over lower when they conflict.**

---

## Offline Development Scenario

### If Hosted AI Services Disappear

On an existing prepared machine with dependencies already installed:

1. **Repository remains usable**: All code, tests, docs are in Git
2. **Tests remain runnable**: installed pytest/dependencies do not require hosted AI
3. **Validation still works**: `./tools/validate-missionaryx --full --base 5637f813ce1669cd288b47edc946a71ea53dc63e` is deterministic
4. **Local models optional**: Development possible without any AI assistance
5. **Documentation readable**: Architecture and protocol docs are human-readable

A fresh machine with no network is not guaranteed to bootstrap because the
repository does not vendor its complete dependency set. See
`OFFLINE_SURVIVAL.md`.

### Recommended Approach

```bash
# 1. Read documentation first
cat DEVELOPMENT_STATE.md
cat ARCHITECTURE.md
cat DEVELOPMENT_PROTOCOL.md

# 2. Understand current state
./tools/verify-checkpoint
git log --oneline -20

# 3. Run validation to verify environment
./tools/validate-missionaryx --quick \
  --base 5637f813ce1669cd288b47edc946a71ea53dc63e

# 4. If an attested governed local environment is available, use it for exploration
# Do not infer readiness from repository code or a listening port alone

# 5. Make focused changes
# Edit only necessary files

# 6. Validate changes
./tools/validate-missionaryx --full \
  --base 5637f813ce1669cd288b47edc946a71ea53dc63e

# 7. Commit if validation passes
git commit -m "Clear description of change"
```

---

## Future: Local Model Code Development

### Planned (Not Implemented)

Future versions may include:

- **Autonomous refactoring**: Model suggests and executes safe refactorings
- **Test generation**: Automatic test creation for new code
- **Documentation generation**: Auto-generated code documentation
- **Bug detection**: Automated bug finding and fixing
- **Architecture validation**: Automated invariant checking

### Design Principles for Future Work

1. **Governance first**: All model actions go through governed boundaries
2. **Audit trail**: Every model action is logged and reversible
3. **Human approval**: High-risk actions require explicit approval
4. **Test validation**: Model-generated code must pass tests
5. **Bounded autonomy**: Models operate within explicit constraints

See `docs/governed-local-inference-integration-v0.1.md` for governance architecture.

---

## Summary: Local AI Development Checklist

When using local models for development:

- [ ] Verify model output against source code
- [ ] Run validation after any AI-assisted changes
- [ ] Do not trust model assertions without verification
- [ ] Treat models as assistants, not authorities
- [ ] Document AI-assisted changes clearly
- [ ] Ensure tests pass independently
- [ ] Review all AI-generated code before commit
- [ ] Remember: deterministic tests outweigh model output

**Local models are useful tools, not substitutes for rigorous development practices.**
