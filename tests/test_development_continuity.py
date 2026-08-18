"""
Tests for Development Continuity v0.1 Toolkit

Validates:
- Machine-readable state file parses correctly
- Checkpoint verification tool exists and is executable
- Validation tool exists and is executable
- Documentation files exist
- State file schema is valid
"""

import json
import subprocess
from pathlib import Path

import pytest


def get_repository_root():
    """Get repository root directory."""
    # Assuming tests are run from repository root or with proper pythonpath
    return Path(__file__).parent.parent


def test_machine_readable_state_exists():
    """Verify missionaryx-state.json exists."""
    root = get_repository_root()
    state_file = root / "missionaryx-state.json"
    assert state_file.exists(), "missionaryx-state.json not found"


def test_machine_readable_state_valid_json():
    """Verify state file is valid JSON."""
    root = get_repository_root()
    state_file = root / "missionaryx-state.json"

    with open(state_file, 'r') as f:
        data = json.load(f)

    assert isinstance(data, dict), "State file must be a JSON object"


def test_machine_readable_state_has_required_fields():
    """Verify state file has required top-level fields."""
    root = get_repository_root()
    state_file = root / "missionaryx-state.json"

    with open(state_file, 'r') as f:
        data = json.load(f)

    required_fields = [
        "schema_version",
        "project",
        "current_integrated_checkpoint",
        "accepted_subsystem_checkpoints",
        "known_non_blocking_issues",
        "python_version",
        "standard_validation_command",
        "subsystems",
        "architectural_invariants",
        "documentation"
    ]

    for field in required_fields:
        assert field in data, f"State file missing required field: {field}"


def test_machine_readable_state_checkpoint_sha_format():
    """Verify checkpoint SHAs are full 40-character hashes."""
    root = get_repository_root()
    state_file = root / "missionaryx-state.json"

    with open(state_file, 'r') as f:
        data = json.load(f)

    # Check integrated checkpoint
    integrated = data["current_integrated_checkpoint"]
    sha = integrated["commit_sha"]
    assert len(sha) == 40, f"Integrated checkpoint SHA must be 40 chars, got {len(sha)}"
    assert all(c in '0123456789abcdef' for c in sha.lower()), \
        "Checkpoint SHA must be hexadecimal"

    # Check subsystem checkpoints
    for checkpoint in data["accepted_subsystem_checkpoints"]:
        sha = checkpoint["commit_sha"]
        assert len(sha) == 40, \
            f"Checkpoint {checkpoint['name']} SHA must be 40 chars, got {len(sha)}"
        assert all(c in '0123456789abcdef' for c in sha.lower()), \
            f"Checkpoint {checkpoint['name']} SHA must be hexadecimal"


def test_checkpoint_verification_tool_exists():
    """Verify checkpoint verification tool exists and is executable."""
    root = get_repository_root()
    tool = root / "tools" / "verify-checkpoint"

    assert tool.exists(), "tools/verify-checkpoint not found"
    assert tool.is_file(), "tools/verify-checkpoint is not a file"
    # Check executable bit
    import os
    assert os.access(tool, os.X_OK), "tools/verify-checkpoint is not executable"


def test_validation_tool_exists():
    """Verify validation tool exists and is executable."""
    root = get_repository_root()
    tool = root / "tools" / "validate-missionaryx"

    assert tool.exists(), "tools/validate-missionaryx not found"
    assert tool.is_file(), "tools/validate-missionaryx is not a file"
    # Check executable bit
    import os
    assert os.access(tool, os.X_OK), "tools/validate-missionaryx is not executable"


def test_verify_checkpoint_reports_state():
    """Verify checkpoint tool can report current state."""
    root = get_repository_root()
    tool = root / "tools" / "verify-checkpoint"

    result = subprocess.run(
        [str(tool)],
        capture_output=True,
        text=True,
        cwd=root
    )

    # Should succeed (exit code 0) in report mode
    assert result.returncode == 0, f"verify-checkpoint failed: {result.stderr}"

    # Should output repository state
    assert "REPOSITORY STATE" in result.stdout
    assert "Repository root:" in result.stdout
    assert "Current branch:" in result.stdout
    assert "HEAD commit:" in result.stdout


def test_verify_checkpoint_help():
    """Verify checkpoint tool shows help."""
    root = get_repository_root()
    tool = root / "tools" / "verify-checkpoint"

    result = subprocess.run(
        [str(tool), "--help"],
        capture_output=True,
        text=True,
        cwd=root
    )

    assert result.returncode == 0, f"verify-checkpoint --help failed"
    assert "Usage:" in result.stdout or "usage:" in result.stdout.lower()


def test_documentation_files_exist():
    """Verify all required documentation files exist."""
    root = get_repository_root()

    required_docs = [
        "DEVELOPMENT_STATE.md",
        "DEVELOPMENT_PROTOCOL.md",
        "ENVIRONMENT_SETUP.md",
        "LOCAL_AI_DEVELOPMENT.md",
        "OFFLINE_SURVIVAL.md",
        "ARCHITECTURE.md",
        "docs/accepted-checkpoints.md",
        "docs/protected-boundaries.md",
        "docs/ai-handoff-template.md"
    ]

    for doc_path in required_docs:
        doc_file = root / doc_path
        assert doc_file.exists(), f"Required documentation file missing: {doc_path}"
        assert doc_file.stat().st_size > 0, f"Documentation file is empty: {doc_path}"


def test_protected_boundaries_documented():
    """Verify protected boundaries are documented in state file."""
    root = get_repository_root()
    state_file = root / "missionaryx-state.json"

    with open(state_file, 'r') as f:
        data = json.load(f)

    # All subsystems should be marked as protected
    for subsystem in data["subsystems"]:
        assert "protected" in subsystem, \
            f"Subsystem {subsystem['name']} missing 'protected' field"
        assert subsystem["protected"] is True, \
            f"Subsystem {subsystem['name']} should be marked protected"


def test_architectural_invariants_documented():
    """Verify architectural invariants are documented in state file."""
    root = get_repository_root()
    state_file = root / "missionaryx-state.json"

    with open(state_file, 'r') as f:
        data = json.load(f)

    invariants = data["architectural_invariants"]
    assert len(invariants) >= 8, "Should have at least 8 architectural invariants"

    # Verify key invariants are present
    invariant_strings = [inv.lower() for inv in invariants]
    assert any("identity" in inv and "authority" in inv for inv in invariant_strings), \
        "Missing identity != authority invariant"
    assert any("dispatch" in inv and "success" in inv for inv in invariant_strings), \
        "Missing dispatch != effect success invariant"


def test_known_issues_documented():
    """Verify known non-blocking issues are documented."""
    root = get_repository_root()
    state_file = root / "missionaryx-state.json"

    with open(state_file, 'r') as f:
        data = json.load(f)

    issues = data["known_non_blocking_issues"]
    assert len(issues) >= 1, "Should have at least 1 known non-blocking issue"

    # Verify llama timeout test is documented
    issue_tests = [issue.get("test_path", "") for issue in issues]
    assert any("llamacpp" in test and "timeout" in test for test in issue_tests), \
        "Flaky llama timeout test should be documented"


def test_checkpoint_verification_tool_does_not_mutate_repository():
    """Verify checkpoint tool does not modify repository state."""
    root = get_repository_root()
    tool = root / "tools" / "verify-checkpoint"

    # Get git status before
    result_before = subprocess.run(
        ["git", "status", "--short"],
        capture_output=True,
        text=True,
        cwd=root
    )
    status_before = result_before.stdout

    # Run checkpoint tool
    subprocess.run(
        [str(tool)],
        capture_output=True,
        cwd=root
    )

    # Get git status after
    result_after = subprocess.run(
        ["git", "status", "--short"],
        capture_output=True,
        text=True,
        cwd=root
    )
    status_after = result_after.stdout

    # Status should be unchanged
    assert status_before == status_after, \
        "Checkpoint verification tool mutated repository state"


def test_machine_readable_state_subsystems_have_key_files():
    """Verify each subsystem lists its key implementation files."""
    root = get_repository_root()
    state_file = root / "missionaryx-state.json"

    with open(state_file, 'r') as f:
        data = json.load(f)

    for subsystem in data["subsystems"]:
        assert "key_files" in subsystem, \
            f"Subsystem {subsystem['name']} missing key_files list"
        assert len(subsystem["key_files"]) > 0, \
            f"Subsystem {subsystem['name']} has no key files listed"


def test_validation_command_documented():
    """Verify validation commands are documented in state file."""
    root = get_repository_root()
    state_file = root / "missionaryx-state.json"

    with open(state_file, 'r') as f:
        data = json.load(f)

    assert "standard_validation_command" in data
    assert "validate" in data["standard_validation_command"].lower()

    assert "validation_modes" in data
    assert "quick" in data["validation_modes"]
    assert "full" in data["validation_modes"]
