# MissionaryX Protected Boundaries

This document describes how to verify that a development milestone did not unexpectedly modify protected subsystem boundaries.

## Important Principle

**Different milestones have different protected scopes**. There is no single universal list that applies to all work.

A milestone focused on documentation/tooling has a broader protected scope (most implementation files) than a milestone specifically tasked with fixing a subsystem boundary.

## Protected Files by Subsystem

### Effect Truth

Files implementing core effect durability and governance:

```
federation/durable_effect_store.py
federation/effect_gateway.py
federation/effect_boundary.py
federation/canonical_digest.py
pavilionos/canonical_adapter.py
pavilionos/canonical_coordinator.py
```

### Authority Truth

Files implementing core authority evaluation and delegation:

```
federation/authority_evaluator.py
federation/delegation_grant.py
federation/delegation_grant_registry.py
federation/agent_authority_store.py
```

### Access/Credential Truth

Files implementing credential broker and access control:

```
federation/access_credential_broker.py
federation/access_connection.py
federation/access_credential_store.py
federation/access_requirement.py
federation/authentication_session.py
federation/credential_backend.py
federation/credential_broker.py
```

### Mission Truth

Files implementing mission runtime and lifecycle:

```
federation/mission_state.py
federation/mission_runtime.py
federation/mission_runtime_store.py
```

---

## Verifying Protected Boundaries

### Manual Verification

To check if any protected files changed between two commits:

```bash
# General pattern
git diff <base-commit> <new-commit> -- <file-path>

# Example: Check if durable_effect_store.py changed
git diff 5637f813ce1669cd288b47edc946a71ea53dc63e HEAD \
  -- federation/durable_effect_store.py
```

If the command produces no output, the file was not modified.

### Checking Multiple Files

Create a shell script or use a loop:

```bash
#!/bin/bash
# check-boundaries.sh

BASE_COMMIT="5637f813ce1669cd288b47edc946a71ea53dc63e"
NEW_COMMIT="HEAD"

PROTECTED_FILES=(
  "federation/durable_effect_store.py"
  "federation/effect_gateway.py"
  "federation/effect_boundary.py"
  "federation/canonical_digest.py"
  "pavilionos/canonical_adapter.py"
  "pavilionos/canonical_coordinator.py"
  "federation/authority_evaluator.py"
  "federation/delegation_grant.py"
  "federation/delegation_grant_registry.py"
  "federation/agent_authority_store.py"
  "federation/access_credential_broker.py"
  "federation/access_connection.py"
  "federation/access_credential_store.py"
  "federation/access_requirement.py"
  "federation/authentication_session.py"
  "federation/credential_backend.py"
  "federation/credential_broker.py"
  "federation/mission_state.py"
  "federation/mission_runtime.py"
  "federation/mission_runtime_store.py"
)

echo "Checking protected boundaries..."
echo "Base: $BASE_COMMIT"
echo "New:  $NEW_COMMIT"
echo ""

MODIFIED=()

for file in "${PROTECTED_FILES[@]}"; do
  if git diff --quiet "$BASE_COMMIT" "$NEW_COMMIT" -- "$file"; then
    : # No changes
  else
    MODIFIED+=("$file")
  fi
done

if [ ${#MODIFIED[@]} -eq 0 ]; then
  echo "✓ No protected boundaries were modified"
  exit 0
else
  echo "✗ The following protected files were modified:"
  for file in "${MODIFIED[@]}"; do
    echo "  - $file"
  done
  echo ""
  echo "If this is expected, document the rationale in the milestone report."
  exit 1
fi
```

---

## Milestone-Specific Protected Lists

### Documentation/Tooling Milestones

For milestones focused on documentation, testing, or tooling (like Development Continuity v0.1), the protected scope includes:

**All core implementation files** (federation/, pavilionos/) UNLESS:
- Adding new tests
- Adding documentation
- Adding tooling in tools/
- Fixing clearly identified bugs with explicit approval

### Subsystem Enhancement Milestones

For milestones specifically tasked with enhancing a subsystem:

**Protected scope**: All subsystems EXCEPT the one being enhanced

**Example**: A milestone to enhance DurableEffectStore may modify `federation/durable_effect_store.py` but should NOT modify Authority, Access/Credential, or Mission files without explicit justification.

### Bug Fix Milestones

For milestones fixing a specific bug:

**Protected scope**: All files EXCEPT those directly related to the bug

**Verification**: The diff should be minimal and focused only on the bug fix, not refactoring or scope creep.

---

## When Protected Files Must Be Modified

If a milestone legitimately requires modifying a protected file:

1. **Document the rationale** before making changes
2. **Verify architectural invariants** are preserved
3. **Obtain explicit approval** from reviewer
4. **Keep changes minimal** and focused
5. **Add tests** to verify invariants are preserved
6. **Document the change** in the milestone report

---

## Using Git to Verify Boundaries

### List all changed files between commits

```bash
git diff --name-only <base-commit> <new-commit>
```

### Show summary of changes

```bash
git diff --stat <base-commit> <new-commit>
```

### Check specific subsystem

```bash
# Effect Truth
git diff --name-only <base-commit> <new-commit> -- \
  federation/durable_effect_store.py \
  federation/effect_gateway.py \
  federation/effect_boundary.py \
  pavilionos/

# Authority Truth
git diff --name-only <base-commit> <new-commit> -- \
  federation/authority_evaluator.py \
  federation/delegation_grant.py \
  federation/agent_authority_store.py
```

### Exclude expected changes

If your milestone is expected to modify specific files, exclude them:

```bash
# Check if ANY protected files changed EXCEPT the ones we intended to modify
git diff --name-only <base-commit> <new-commit> -- \
  federation/ \
  pavilionos/ \
  ':!federation/expected_change.py'
```

---

## Protected Boundary Violation Report

If protected boundaries were modified unexpectedly:

1. **Identify which files changed**:
   ```bash
   git diff --name-only <base-commit> <new-commit> -- federation/ pavilionos/
   ```

2. **Review the actual changes**:
   ```bash
   git diff <base-commit> <new-commit> -- <modified-file>
   ```

3. **Determine if justified**:
   - Was this file supposed to be modified per the milestone spec?
   - Does the change preserve architectural invariants?
   - Is the change minimal and focused?

4. **Report**:
   ```
   PROTECTED_BOUNDARY_VIOLATION

   Milestone: <name>
   Base: <base-commit>
   New:  <new-commit>

   Unexpected modifications:
   - <file1>: <reason>
   - <file2>: <reason>

   Justification: <explain why this occurred>

   Recommended action: <revert/accept with documentation/other>
   ```

---

## Example: Development Continuity v0.1

For the Development Continuity v0.1 milestone:

**Base commit**: `5637f813ce1669cd288b47edc946a71ea53dc63e`

**Protected scope**: All `federation/` and `pavilionos/` implementation files

**Allowed changes**:
- New files in `docs/`
- New files in `tools/`
- New test files in `tests/`
- Updates to `README.md`, `ARCHITECTURE.md`, etc.
- No changes to core federation/pavilionos implementation

**Verification command**:
```bash
# Should produce no output if boundaries are protected
git diff --name-only 5637f813ce1669cd288b47edc946a71ea53dc63e HEAD -- \
  federation/ \
  pavilionos/
```

If any files are listed, investigate whether they were supposed to be modified.

---

## Tooling Integration

The `./tools/validate-missionaryx` script can be extended to include boundary checking:

```python
def check_protected_boundaries(base_commit, protected_files):
    """Check if protected files were modified."""
    for file_path in protected_files:
        result = subprocess.run(
            ['git', 'diff', '--quiet', base_commit, 'HEAD', '--', file_path],
            capture_output=True
        )
        if result.returncode != 0:
            # File was modified
            return False, file_path
    return True, None
```

---

## Summary

- Different milestones have different protected scopes
- Use `git diff` to verify boundaries were not crossed
- Document any necessary protected file modifications
- Verify architectural invariants are preserved
- Keep changes minimal and focused
- Obtain explicit approval before modifying protected files
