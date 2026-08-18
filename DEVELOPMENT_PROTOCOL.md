# MissionaryX Development Protocol

This document defines the standard development lifecycle for MissionaryX work. This protocol is **model-independent** and applies whether development is performed by humans, Claude, Codex, local models, or any future AI assistant.

---

## Core Principles

1. **Start from accepted checkpoints**: Every development cycle begins from a verified, accepted state
2. **Narrow scope**: Each milestone addresses a specific, well-defined objective
3. **Preserve history**: Never rewrite accepted checkpoints
4. **Evidence over claims**: Executable tests outweigh verbal assertions
5. **Independent review**: Acceptance requires verification by a party other than the implementer
6. **Explicit deviations**: Document any departure from standard protocol

---

## Development Lifecycle

### 1. Start from an Accepted Checkpoint

**Before starting any work**:

```bash
# Navigate to repository
cd /path/to/repository

# Verify current commit
git rev-parse HEAD

# Expected for current development:
# 5637f813ce1669cd288b47edc946a71ea53dc63e

# Verify clean tree
git status --short
# Should output nothing (clean tree)
```

**Important**: Use **full 40-character SHA-1 hashes** for checkpoint identity. Abbreviated SHAs are insufficient.

**Checkpoint verification tool**:
```bash
./tools/verify-checkpoint 5637f813ce1669cd288b47edc946a71ea53dc63e
```

**If checkpoint doesn't match**: STOP. Do not proceed until the mismatch is resolved.

---

### 2. Create a Dedicated Worktree and Branch

**Create worktree** for isolated development:

```bash
cd /home/matias/raghub  # Main repository

git worktree add /path/to/new-worktree -b feature/your-feature-name <checkpoint-sha>
```

**Example**:
```bash
git worktree add /home/matias/missionaryx-your-feature-v0-1 \
  -b feature/your-feature-v0-1 \
  5637f813ce1669cd288b47edc946a71ea53dc63e
```

**Branch naming convention**:
- Use `feature/` prefix for new features
- Include version suffix for milestone tracking (e.g., `-v0-1`)
- Be descriptive but concise

---

### 3. Verify Checkpoint Identity and Clean Tree

**After creating worktree**:

```bash
cd /path/to/new-worktree

# Verify exact checkpoint
git rev-parse HEAD
# Must match expected checkpoint

# Verify branch
git branch --show-current
# Should show your new branch name

# Verify clean tree
git status --short --branch
# Should show branch ahead of origin by 0 commits, no modifications
```

**Automated verification**:
```bash
./tools/verify-checkpoint <expected-sha>
```

**If verification fails**: STOP. Report the discrepancy.

---

### 4. Set Up Development Environment

**Create isolated Python environment**:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

See `ENVIRONMENT_SETUP.md` for detailed environment setup.

**Verify environment**:
```bash
.venv/bin/python --version  # Should match project requirements
.venv/bin/pytest --version  # Should be available
```

---

### 5. Perform Narrow Implementation

**Scope discipline**:
- Implement ONLY what is defined in the milestone specification
- Do NOT silently widen scope
- If scope appears to require widening, STOP and report why

**Protected boundaries**:
- Do NOT modify files in protected subsystem boundaries unless explicitly required
- See `DEVELOPMENT_STATE.md` for protected file list
- If modification appears necessary, STOP and justify

**Implementation guidelines**:
- Preserve existing architectural invariants
- Do not amend or rebase accepted checkpoints
- Make incremental commits with clear messages
- Separate refactoring from feature work

---

### 6. Run Focused Validation

**After implementation, run focused tests**:

```bash
# Quick validation (high-value focused tests)
./tools/validate-missionaryx --quick
```

**Or run subsystem-specific tests**:
```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q tests/test_your_subsystem.py
```

**Focused validation must pass** before proceeding to full validation.

**If focused validation fails**:
- Fix failures immediately
- Do NOT use `skip` or `xfail` to conceal required invariants
- Inherited failures must be documented separately

---

### 7. Run Full Validation

**Run complete test suite**:

```bash
./tools/validate-missionaryx --full
```

**Or manually**:
```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q \
  --deselect=tests/test_llamacpp_adapter.py::test_timeout_returns_error_response
```

**Known deselections**:
- `tests/test_llamacpp_adapter.py::test_timeout_returns_error_response` (inherited flaky timeout)

**Record exact results**:
- Total tests run
- Passed count
- Failed count
- Skipped count
- Deselected count
- Elapsed time

**Full validation must pass** (accounting for documented deselections) before commit.

**Report contradictions**: If test counts or status are inconsistent, report the discrepancy instead of smoothing it over.

---

### 8. Run Static Checks

**Before committing**:

```bash
# Check for whitespace errors
git diff --check

# Verify Python compilation
python3 -m compileall .
```

**Both checks must pass** before commit.

---

### 9. Commit Without Rewriting Accepted History

**Create commit**:

```bash
git add <files>

git commit -m "Clear, concise commit message

Detailed explanation if needed.

Closes: #issue-number (if applicable)"
```

**Commit discipline**:
- Do NOT use `git commit --amend` on accepted checkpoints
- Do NOT use `git rebase` to rewrite accepted checkpoint history
- Create forward-only commits
- Keep commits focused and atomic

**After commit**:

```bash
# Record new commit SHA
git rev-parse HEAD

# Verify tree is still clean
git status
```

---

### 10. Independent Fresh Review

**After commit, hand off for independent review**.

**Reviewer responsibilities**:
1. Start from the **same base checkpoint** in a fresh worktree
2. Verify implementer's checkpoint identity
3. Reproduce environment setup
4. Run focused validation independently
5. Run full validation independently
6. Reproduce important invariants directly (not just trust test pass/fail)
7. Verify no protected boundaries were violated
8. Check for scope creep

**Review is NOT complete until**:
- Reviewer reproduces validation results
- Reviewer verifies important invariants through independent testing
- Reviewer confirms architectural contracts are preserved

---

### 11. Designate New Checkpoint (If Accepted)

**Only after successful independent review**:

1. Record full SHA-1 commit hash
2. Update `docs/accepted-checkpoints.md`
3. Update `missionaryx-state.json`
4. Update `DEVELOPMENT_STATE.md` if this becomes the integrated checkpoint
5. Document any new known non-blocking observations

**Checkpoint must include**:
- Full 40-character SHA
- Subsystem name and purpose
- Major invariants established
- Superseded checkpoints (if any)
- Known non-blocking observations
- Validation evidence

**Never self-accept**: Original implementer cannot designate their own work as accepted. Independent verification is required.

---

## Special Cases

### Inherited Failures

**If a test fails that is not related to your work**:

1. Verify the test also fails at the base checkpoint
2. Document the inherited failure separately
3. Do NOT attempt to fix unrelated failures during your milestone
4. Report inherited failures in your completion report

**Separation of concerns**: Your milestone-introduced failures must be distinguished from inherited failures.

---

### Scope Widening

**If work scope appears to widen during implementation**:

1. STOP implementation
2. Document why scope widening appears necessary
3. Report to reviewer/coordinator
4. Wait for explicit approval before continuing
5. Update milestone specification if approved

**Do NOT silently widen scope**.

---

### Protected Boundary Modifications

**If your work appears to require modifying a protected file**:

1. STOP implementation
2. Document which file and why modification appears necessary
3. Verify the change preserves architectural invariants
4. Obtain explicit approval before modifying
5. Document the modification rationale

**Protected boundaries are protected for a reason**.

---

## Validation Standards

### Quick Validation

**Purpose**: Fast feedback during development

**Contents**:
- High-value subsystem-specific tests
- Architectural invariant smoke tests
- Basic integration tests

**Pass criteria**: All quick tests pass

---

### Full Validation

**Purpose**: Comprehensive verification before acceptance

**Contents**:
- Complete test suite
- Known documented deselections only
- Static analysis checks
- Compilation verification

**Pass criteria**:
- All tests pass (except documented deselections)
- No whitespace errors (`git diff --check`)
- No compilation errors (`python3 -m compileall .`)

**Evidence required**:
- Exact test counts (passed/failed/skipped/deselected)
- Elapsed time
- Python version
- Repository state (branch, HEAD, status)

---

## Evidence Standards

### What Counts as Evidence

**Valid evidence**:
- Executable test results
- Reproducible validation runs
- Direct verification of invariants
- Static analysis output
- Compilation results

**NOT valid evidence**:
- Verbal claims without reproduction
- "Trust me, it works"
- Test results from a different checkpoint
- Results that cannot be reproduced

**Evidence must be reproducible** by independent reviewers.

---

## Anti-Patterns

### Do NOT

- Start from an unverified checkpoint
- Use abbreviated SHA-1 hashes for checkpoint identity
- Amend or rebase accepted checkpoints
- Silently widen milestone scope
- Use `skip` or `xfail` to hide required invariants
- Smooth over contradictory test counts
- Modify protected boundaries without justification
- Self-accept your own work
- Trust handoff claims over executable evidence
- Commit without running full validation

---

## Checklist: Before Marking Work Complete

- [ ] Started from documented accepted checkpoint
- [ ] Created dedicated worktree and branch
- [ ] Verified checkpoint identity with full SHA
- [ ] Verified clean tree at start
- [ ] Implemented only defined scope (no scope creep)
- [ ] Did not modify protected boundaries (or documented exceptions)
- [ ] Ran focused validation (passed)
- [ ] Ran full validation (passed)
- [ ] Ran static checks (passed)
- [ ] Created forward-only commits (no amend/rebase of accepted checkpoints)
- [ ] Recorded exact validation results
- [ ] Prepared for independent review
- [ ] Did NOT self-accept

---

## Handoff to Reviewer

**When handing off for review, provide**:

1. Base checkpoint SHA (full 40 characters)
2. New commit SHA (full 40 characters)
3. Branch name and worktree path
4. Milestone specification reference
5. Focused validation results (exact counts)
6. Full validation results (exact counts)
7. Static check results
8. Any scope deviations or protected boundary modifications
9. Known non-blocking observations
10. Any inherited failures

**Reviewer must receive ALL information needed** to reproduce your work independently.

---

## Summary: The Protocol in Brief

1. Start from accepted checkpoint (verify with full SHA)
2. Create dedicated worktree/branch
3. Verify checkpoint and clean tree
4. Implement narrow scope (no silent widening)
5. Run focused validation
6. Run full validation
7. Run static checks
8. Commit (no amend/rebase of accepted history)
9. Independent review reproduces everything
10. Only then designate new checkpoint

**Evidence over claims. Independence over self-certification. Preservation over rewriting.**
