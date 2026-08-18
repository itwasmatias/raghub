# MissionaryX Offline Survival Procedure

**What to do if hosted AI services disappear today.**

This document provides a step-by-step procedure for continuing MissionaryX development without Claude, ChatGPT, or any hosted AI service.

---

## Core Truth

MissionaryX does not require AI assistance for human review, Git operations, or
testing on an **existing prepared machine** with the repository, Python
environment, and dependencies already available.

The following development capabilities remain available on such a prepared machine:
- Repository with complete code and history
- Executable test suite
- Deterministic validation tools
- Human-readable documentation
- Git-based version control
- Optional local model support when a compatible executable and model assets are
  already available (not required)

---

## Immediate Assessment

If hosted AI services are unavailable, first determine your situation:

### Scenario A: Total AI Loss (No Local Models)
- No Claude, ChatGPT, or similar services
- No local models available
- Pure human development only

### Scenario B: Local Models Available
- No hosted services
- llama.cpp or similar local models available
- Hybrid human + local AI development

### Scenario C: Partial Service Loss
- Some AI services unavailable
- Others still accessible
- Selective fallback needed

Hosted-AI loss and machine bootstrap are different problems:

- **Existing prepared machine**: human/Git/test development can continue with
  already-installed dependencies.
- **Fresh machine with no network**: bootstrap is not guaranteed because this
  repository does not vendor Python wheels or every required system package.
- **Local AI**: optional; requires a compatible executable, verified model
  artifacts, and governed lifecycle/inference prerequisites.

---

## Step 1: Open Repository

```bash
# Navigate to repository
cd /home/matias/raghub

# Or to existing worktree
cd /path/to/existing-worktree
```

If you need to locate your worktrees:
```bash
git worktree list
```

---

## Step 2: Identify Accepted Checkpoint

```bash
# Read development state
cat DEVELOPMENT_STATE.md

# Or check accepted checkpoints directly
cat docs/accepted-checkpoints.md

# Verify current checkpoint
./tools/verify-checkpoint
```

**Current accepted integrated checkpoint**: `5637f813ce1669cd288b47edc946a71ea53dc63e`

---

## Step 3: Create Worktree (If Needed)

If starting new work:

```bash
# From main repository
cd /home/matias/raghub

# Create new worktree from accepted checkpoint
git worktree add /path/to/new-worktree \
  -b feature/your-feature-name \
  5637f813ce1669cd288b47edc946a71ea53dc63e

# Navigate to worktree
cd /path/to/new-worktree
```

---

## Step 4: Create Python Environment

```bash
# Create virtual environment
python3 -m venv .venv

# Activate it
source .venv/bin/activate

# Install dependencies
pip install -r requirements-dev.txt
```

This installation requires network access, a pre-populated package cache, or a
copied local wheelhouse. The prepared development host has proven a no-index
reconstruction from `$HOME/missionaryx-offline-wheelhouse`: its 41 wheels are
covered by `SHA256SUMS`, and the fresh environment was populated with:

```bash
wheelhouse="$HOME/missionaryx-offline-wheelhouse"
(
  cd "$wheelhouse"
  sha256sum -c SHA256SUMS
)
python3.14 -m venv .venv
.venv/bin/python -m pip install \
  --no-index \
  --find-links "$wheelhouse" \
  --requirement requirements-dev.txt
```

The wheelhouse was populated earlier with `pip download`; it is external to the
repository and must be copied with its checksum manifest. The proof did not
physically air-gap the machine, and native wheels are CPython 3.14 / x86_64
specific where applicable. This is not a claim that every fresh Fedora machine
can bootstrap. Without compatible Python/system prerequisites or a complete
verified wheelhouse, stop and report that the environment is not bootstrap-ready.
`make setup` installs core requirements only, not the complete development/test
requirements. See `LOCAL_DEVELOPMENT_READINESS.md` for the full workflow.

**Alternative using Makefile**:
```bash
make setup
```

---

## Step 5: Run Quick Validation

Verify your environment works:

```bash
./tools/validate-missionaryx --quick \
  --base 5637f813ce1669cd288b47edc946a71ea53dc63e
```

Expected output: Quick tests pass (or report specific failures).

If validation fails, troubleshoot before proceeding.

The non-destructive static readiness check can be run before validation:

```bash
./tools/check-local-development-readiness
```

It does not start or stop llama-server. Use `--live` only to inspect an
already-running loopback server; that result is explicitly adapter-level and is
not proof of the full durable governed lifecycle/integration chain.

---

## Step 6: Run Full Validation

Verify complete system:

```bash
./tools/validate-missionaryx --full \
  --base 5637f813ce1669cd288b47edc946a71ea53dc63e
```

Expected output: Full suite passes except documented deselections.

**Known deselection**: `tests/test_llamacpp_adapter.py::test_timeout_returns_error_response`

---

## Step 7: Inspect Architecture and State

Read key documentation:

```bash
# Start here
cat DEVELOPMENT_STATE.md

# Understand architecture
cat ARCHITECTURE.md

# Learn development protocol
cat DEVELOPMENT_PROTOCOL.md

# Understand environment setup
cat ENVIRONMENT_SETUP.md
```

**Do not skip documentation reading.** These documents contain essential context for development without AI guidance.

---

## Step 8: Use Local Model (Optional)

If a governed local-model environment has already been prepared and attested:

```bash
# Read local AI development workflow
cat LOCAL_AI_DEVELOPMENT.md

# Verify and use the lifecycle-controlled loopback endpoint:
# http://127.0.0.1:18080
# Follow LOCAL_AI_DEVELOPMENT.md and the governed lifecycle documentation.
```

Do not start or trust an unmanaged server as a shortcut. Local models assist but
do not replace testing and validation.

---

## Step 9: Make Focused Changes

### Without AI Assistance

1. **Read code** to understand what needs to change
2. **Make minimal edits** focused on specific issue
3. **Run tests early and often** to catch breakage
4. **Use Git** to track and undo changes if needed

### With Local Model Assistance

1. **Ask model to explain** unfamiliar code sections
2. **Draft changes** with model help
3. **Verify model output** against actual code
4. **Test immediately** after any change
5. **Trust tests, not model assertions**

### Development Workflow

```bash
# 1. Make changes
vi path/to/file.py

# 2. Run focused tests immediately
pytest tests/test_related.py -v

# 3. Fix failures
# (repeat until tests pass)

# 4. Run quick validation
./tools/validate-missionaryx --quick \
  --base 5637f813ce1669cd288b47edc946a71ea53dc63e

# 5. Run full validation
./tools/validate-missionaryx --full \
  --base 5637f813ce1669cd288b47edc946a71ea53dc63e
```

---

## Step 10: Validate Changes

Before committing:

```bash
# 1. Static checks
git diff --check

# 2. Compilation without source-tree bytecode
bytecode_cache="$(mktemp -d)"
PYTHONPYCACHEPREFIX="$bytecode_cache" python3 -m compileall -q -f .
rm -rf -- "$bytecode_cache"

# 3. Quick validation
./tools/validate-missionaryx --quick \
  --base 5637f813ce1669cd288b47edc946a71ea53dc63e

# 4. Full validation
./tools/validate-missionaryx --full \
  --base 5637f813ce1669cd288b47edc946a71ea53dc63e
```

**All checks must pass before commit.**

---

## Step 11: Commit Safely

```bash
# Review changes
git status
git diff

# Stage changes
git add path/to/modified/files

# Commit with clear message
git commit -m "Clear description of change

Detailed explanation if needed.

Rationale for the change."

# Verify commit
git log -1 --stat
```

**Important**: Do not amend accepted checkpoints. Create forward-only commits.

---

## Step 12: Record Checkpoint

After successful commit:

```bash
# Record new commit SHA
git rev-parse HEAD

# Document for later review
echo "New candidate checkpoint: $(git rev-parse HEAD)" >> WORK_LOG.txt
echo "Purpose: <describe what was accomplished>" >> WORK_LOG.txt
echo "Validation: PASSED" >> WORK_LOG.txt
```

**Do NOT self-designate as accepted.** Checkpoints require independent review.

---

## Pure Human Development (No AI)

### Reading Code

Use these tools to understand code:

```bash
# Search for definitions
grep -r "class DelegationGrant" --include="*.py"

# Find imports
grep -r "from federation import" --include="*.py"

# Search for usage
grep -r "DurableEffectStore" --include="*.py"

# View file with line numbers
cat -n federation/mission_runtime.py | less
```

### Understanding Subsystems

Read these files in order:

1. `ARCHITECTURE.md` - High-level system design
2. `docs/accepted-checkpoints.md` - What has been accepted
3. Source files for relevant subsystem
4. Test files for usage examples

### Making Changes

**Principle**: Make the smallest change that accomplishes the goal.

**Process**:
1. Understand current behavior (read code + tests)
2. Identify what needs to change
3. Make minimal edit
4. Verify with tests
5. Repeat if needed

### Debugging Failures

```bash
# Run single failing test with verbose output
pytest tests/test_subsystem.py::test_specific -vv

# Add print statements (then remove before commit)
# Review stack trace
# Check assumptions

# Use Python debugger if needed
python3 -m pdb script.py
```

---

## Local Model Development (With Local AI)

See `LOCAL_AI_DEVELOPMENT.md` for detailed workflow.

Use only the attested governed inference path described in
`LOCAL_AI_DEVELOPMENT.md`. Drafts may help explain code or suggest tests, but a
developer must review the output, adapt it to repository contracts, and verify
it with deterministic tests.

**Critical**: Always verify model output. Models can be wrong.

---

## Common Tasks Without AI

### Task: Fix a Bug

```bash
# 1. Reproduce bug
pytest tests/test_subsystem.py::test_that_fails -vv

# 2. Understand failure
# Read stack trace
# Examine test code
# Read implementation code

# 3. Identify root cause
# Add print statements if needed
# Trace execution path

# 4. Make minimal fix
vi path/to/buggy/file.py

# 5. Verify fix
pytest tests/test_subsystem.py::test_that_fails -vv

# 6. Run full validation
./tools/validate-missionaryx --full \
  --base 5637f813ce1669cd288b47edc946a71ea53dc63e

# 7. Commit if passed
git add path/to/buggy/file.py
git commit -m "Fix bug in <component>"
```

### Task: Add New Functionality

```bash
# 1. Read architecture
cat ARCHITECTURE.md

# 2. Find related code
grep -r "SimilarComponent" --include="*.py"

# 3. Draft implementation
# Follow existing patterns
# Use similar code as template

# 4. Write tests FIRST
vi tests/test_new_functionality.py
# Write failing test for desired behavior

# 5. Implement until test passes
vi path/to/implementation.py

# 6. Validate
./tools/validate-missionaryx --full \
  --base 5637f813ce1669cd288b47edc946a71ea53dc63e

# 7. Commit
git add tests/ path/to/implementation.py
git commit -m "Add <functionality>"
```

### Task: Refactor Code

```bash
# 1. Ensure tests exist and pass
pytest tests/test_target.py -v

# 2. Make small refactoring
# Change ONE thing at a time

# 3. Run tests immediately
pytest tests/test_target.py -v

# 4. Repeat until refactoring complete

# 5. Full validation
./tools/validate-missionaryx --full \
  --base 5637f813ce1669cd288b47edc946a71ea53dc63e

# 6. Commit
git commit -m "Refactor <component> for <reason>"
```

---

## Troubleshooting

### Tests Fail After Change

1. Inspect `git status --short` and `git diff` to understand every change.
2. Preserve collaborative and uncommitted work before experimenting: record a
   patch or copy affected artifacts to a separate backup location.
3. Reproduce the accepted checkpoint in a **new worktree** and run the same test
   there to distinguish an inherited failure from the current edit.
4. Make a smaller corrective edit in the original worktree and rerun the focused
   test. Do not discard another contributor's changes as a debugging shortcut.

### Don't Understand Code

```bash
# 1. Read tests for usage examples
cat tests/test_<component>.py

# 2. Search for usage in codebase
grep -r "ComponentName" --include="*.py"

# 3. Read related documentation
cat docs/<relevant-doc>.md

# 4. Trace execution path manually
# Follow function calls step by step
```

### Validation Fails

```bash
# 1. Read error output carefully
# Don't skim - every line matters

# 2. Identify which test failed
# Run that specific test

# 3. Understand why it failed
pytest path/to/test.py::test_name -vv

# 4. Fix the issue
# NOT the test (unless test is wrong)

# 5. Revalidate
./tools/validate-missionaryx --full \
  --base 5637f813ce1669cd288b47edc946a71ea53dc63e
```

---

## Emergency Recovery

If repository becomes unstable:

```bash
# 1. Inspect and preserve current work
git status --short --branch
git diff
git diff --cached

# 2. From the main repository, create a separate recovery worktree
git worktree add /path/to/recovery-worktree \
  -b recovery/inspect-accepted-base \
  5637f813ce1669cd288b47edc946a71ea53dc63e

# 3. Validate the accepted base in that new worktree
cd /path/to/recovery-worktree
./tools/verify-checkpoint --require-clean \
  5637f813ce1669cd288b47edc946a71ea53dc63e
./tools/validate-missionaryx --full \
  --base 5637f813ce1669cd288b47edc946a71ea53dc63e
```

Keep the original worktree intact until its changes have been reviewed and
deliberately reconciled. A backup branch records committed state only; copy or
patch uncommitted artifacts separately when they must be preserved.

---

## Summary: Offline Survival Checklist

- [ ] Repository accessible
- [ ] Accepted checkpoint identified
- [ ] Worktree created (if needed)
- [ ] Python environment set up
- [ ] Quick validation passes
- [ ] Full validation passes
- [ ] Documentation read (DEVELOPMENT_STATE.md, ARCHITECTURE.md)
- [ ] Local model running (if available and desired)
- [ ] Ready to make focused changes
- [ ] Validation tools working
- [ ] Commit workflow understood

On a prepared machine, **development can continue without hosted AI**: Git, source
inspection, and deterministic validation remain usable. Local AI is optional and
requires a compatible local executable and model assets. A fresh offline machine is
not guaranteed to be bootstrap-ready, and this continuity claim
does not imply that every MissionaryX runtime capability is available.

---

## Philosophy

**AI is a tool, not a requirement.**

Development fundamentals remain:
- Read code to understand it
- Write tests to verify behavior
- Make minimal changes
- Validate before committing
- Preserve architectural invariants
- Document important decisions

**These practices work with or without AI assistance.**
