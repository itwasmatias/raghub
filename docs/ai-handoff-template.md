# MissionaryX AI Task Handoff Template

This template provides a structured format for handing off MissionaryX development tasks to any AI assistant (Claude, ChatGPT, Codex, local models, or future systems).

This template is **provider-neutral** and works with any capable AI system.

---

## Task Handoff

### TASK_ID
```
<unique-task-identifier>
```

### TASK_NAME
```
<short-descriptive-name>
```

### ASSIGNED_TO
```
AI Assistant: <Claude / ChatGPT / Codex / Local Model / Other>
Human Reviewer: <name-or-identifier>
```

---

## Repository Context

### WORKTREE
```
/path/to/worktree
```

### BRANCH
```
<branch-name>
```

### EXPECTED_HEAD
```
<full-40-character-SHA>
```

**Verification required**: AI must verify HEAD matches before starting.

Use `./tools/verify-checkpoint --require-clean <full-40-character-SHA>` when a
clean first gate is required.

### TREE_STATUS
```
[ ] CLEAN (required at start)
[ ] DIRTY_ALLOWED (document reason)
```

---

## Access Control

### READ_ONLY_MODE
```
[ ] Yes - AI may only read, analyze, and report
[ ] No - AI may modify files
```

### ALLOWED_FILES
```
List of files/directories AI is permitted to modify:
- path/to/file1.py
- path/to/file2.py
- docs/*.md
- tests/test_*.py
```

### PROTECTED_FILES
```
List of files/directories AI must NOT modify:
- federation/durable_effect_store.py
- federation/effect_gateway.py
- pavilionos/
- (see docs/protected-boundaries.md for full list)
```

---

## Task Specification

### OBJECTIVE
```
Clear, concise statement of what needs to be accomplished.

Example: "Add checkpoint verification tool that compares current HEAD
against expected SHA and reports mismatch."
```

### SCOPE
```
Explicitly define what is IN SCOPE and OUT OF SCOPE.

IN SCOPE:
- Item 1
- Item 2

OUT OF SCOPE:
- Item 3
- Item 4
```

### REQUIREMENTS
```
List of specific requirements:
1. Requirement 1
2. Requirement 2
3. Requirement 3
```

### SUCCESS_CRITERIA
```
How to determine if the task is complete:
1. Criterion 1 passes
2. Criterion 2 verified
3. Validation suite passes
```

---

## Required Invariants

### ARCHITECTURAL_INVARIANTS
```
List invariants that must be preserved:
- identity != authority
- dispatch != effect success
- (list other relevant invariants from ARCHITECTURE.md)
```

### SUBSYSTEM_BOUNDARIES
```
List subsystem boundaries that must be respected:
- Must not modify Effect Truth implementation
- Must preserve Mission Truth lifecycle semantics
- (list other relevant boundaries)
```

### TEST_REQUIREMENTS
```
Testing requirements:
[ ] Add unit tests for new code
[ ] Update integration tests if needed
[ ] All tests must pass
[ ] No skip/xfail to hide failures
```

---

## Validation Requirements

### FOCUSED_VALIDATION
```
Subsystem-specific tests to run:
pytest tests/test_<subsystem>.py
```

### FULL_VALIDATION
```
./tools/validate-missionaryx --full --base <full-base-sha>
```

### STATIC_CHECKS
```
[ ] git diff --check (whitespace errors)
[ ] compileall with PYTHONPYCACHEPREFIX set to a temporary external directory
```

### EXPECTED_TEST_DESELECTIONS
```
List of known deselections (if any):
- tests/test_llamacpp_adapter.py::test_timeout_returns_error_response
  (inherited flaky timeout)
```

---

## Commit Policy

### COMMIT_ALLOWED
```
[ ] Yes - AI may create commits
[ ] No - AI drafts changes only, human commits
```

### COMMIT_MESSAGE_FORMAT
```
<short-summary>

<detailed-explanation>

<rationale-if-needed>

Closes: #<issue-number> (if applicable)
```

### AMEND_POLICY
```
[ ] No amend allowed (forward-only commits)
[ ] Amend allowed (document reason)
```

### REBASE_POLICY
```
[ ] No rebase allowed (preserve checkpoint history)
[ ] Rebase allowed (document reason)
```

---

## Handoff Information

### BASE_CHECKPOINT
```
Commit SHA: <full-40-character-SHA>
Description: <what-this-checkpoint-represents>
Verified:   [ ] Yes [ ] No
```

### INHERITED_ISSUES
```
List of known issues inherited from base checkpoint:
1. Issue 1 (non-blocking)
2. Issue 2 (documented in DEVELOPMENT_STATE.md)
```

### DEPENDENCIES
```
List of dependencies or prerequisites:
- Dependency 1
- Dependency 2
```

### REFERENCES
```
Relevant documentation:
- DEVELOPMENT_STATE.md (read first)
- ARCHITECTURE.md (subsystem X)
- docs/accepted-checkpoints.md
- docs/<relevant-design-doc>.md
```

---

## Completion Report

### FINAL_STATUS
```
Upon completion, AI must report:

TASK_COMPLETION_STATUS:
[ ] COMPLETE
[ ] INCOMPLETE (explain why)
[ ] BLOCKED (explain blocker)

FINAL_COMMIT_SHA:
<full-40-character-SHA-if-committed>

TREE_STATUS:
[ ] CLEAN
[ ] DIRTY (list uncommitted changes)
```

### VALIDATION_RESULTS
```
Report exact validation results:

FOCUSED_VALIDATION:
Passed: <count>
Failed: <count>
Skipped: <count>

FULL_VALIDATION:
Passed: <count>
Failed: <count>
Skipped: <count>
Deselected: <count>
Elapsed: <time>

STATIC_CHECKS:
git diff --check: [ ] PASS [ ] FAIL
compileall: [ ] PASS [ ] FAIL
```

### SCOPE_DEVIATIONS
```
Report any scope deviations:
- Deviation 1 (rationale)
- Deviation 2 (rationale)

Or: NO_SCOPE_DEVIATIONS
```

### PROTECTED_BOUNDARY_CHANGES
```
Report any protected file modifications:
- File 1 (rationale)
- File 2 (rationale)

Or: NO_PROTECTED_CHANGES
```

### HANDOFF_TO_REVIEWER
```
Information for independent reviewer:

Base checkpoint: <SHA>
New commit: <SHA>
Branch: <name>
Worktree: <path>

Validation results: <attached or documented above>
Scope: <in spec / deviated (explain)>
Protected boundaries: <respected / modified (explain)>
Known issues: <list any new non-blocking observations>

Ready for independent review: [ ] Yes [ ] No (explain)
```

---

## Example: Filled Template

```markdown
# TASK_ID: DEVCON-001

# TASK_NAME: Add Checkpoint Verification Tool

# ASSIGNED_TO:
AI Assistant: Claude Code
Human Reviewer: Matias

# WORKTREE: /home/matias/missionaryx-development-continuity-v0-1
# BRANCH: feature/development-continuity-v0-1
# EXPECTED_HEAD: 5637f813ce1669cd288b47edc946a71ea53dc63e
# TREE_STATUS: [X] CLEAN

# READ_ONLY_MODE: [ ] Yes [X] No

# ALLOWED_FILES:
- tools/verify-checkpoint (new)
- tests/test_development_continuity.py (new)
- docs/ (documentation updates)

# PROTECTED_FILES:
- federation/ (all files)
- pavilionos/ (all files)

# OBJECTIVE:
Create a checkpoint verification tool that compares current HEAD against
expected SHA and reports match/mismatch with full repository context.

# SCOPE:
IN SCOPE:
- Create tools/verify-checkpoint script
- Add tests for checkpoint verification
- Document tool usage

OUT OF SCOPE:
- Modifying existing tools
- Changing core implementation
- Automated checkpoint designation

# REQUIREMENTS:
1. Tool must accept optional expected SHA argument
2. Tool must report: repo root, branch, HEAD, parent, tree status
3. Tool must verify full 40-character SHAs only
4. Tool must exit 0 on match, 1 on mismatch/clean-tree failure, and 2 on invalid usage or Git environment error

# SUCCESS_CRITERIA:
1. Tool correctly reports repository state
2. Tool correctly verifies checkpoint match
3. Tool correctly detects checkpoint mismatch
4. Tests verify tool behavior
5. Full validation passes

# ARCHITECTURAL_INVARIANTS:
- None (tooling does not affect architecture)

# VALIDATION_REQUIREMENTS:
[X] Add unit tests
[X] Full validation passes
[ ] No skip/xfail

# COMMIT_ALLOWED: [X] Yes
# AMEND_POLICY: [X] No amend (forward-only)
# REBASE_POLICY: [X] No rebase (preserve history)

# BASE_CHECKPOINT: 5637f813ce1669cd288b47edc946a71ea53dc63e
# Verified: [X] Yes
```

---

## Template Usage Notes

1. **Fill out ALL sections** - Do not leave sections blank without explanation
2. **Be specific** - Vague requirements lead to scope creep
3. **Verify checkpoint** - AI must verify HEAD before starting
4. **Document deviations** - Any scope change must be documented
5. **Complete report** - AI must provide full completion report

**This template ensures clear, unambiguous task handoff to any AI system.**
