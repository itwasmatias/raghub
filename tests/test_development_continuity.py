"""Deterministic contract tests for Development Continuity v0.1."""

from __future__ import annotations

import json
import os
import re
import runpy
import shutil
import subprocess
from pathlib import Path


BASE_CHECKPOINT = "5637f813ce1669cd288b47edc946a71ea53dc63e"
AUTHORITY_ACCEPTED_CHECKPOINT = "840d7645045a02174509559637ab9af1ad215e89"
AUTHORITY_INTEGRATED_COMMIT = "5ef84562cffb2e3227e75e6b66d740bf39650927"
LLAMA_TIMEOUT_NODE = (
    "tests/test_llamacpp_adapter.py::test_timeout_returns_error_response"
)
EXPECTED_CHECKPOINTS = {
    "DurableEffectStore Concurrent Migration Correction": BASE_CHECKPOINT,
    "Access & Credential Broker v0.1": "dba334668608034c19efc3e2d9c791b7b8f74583",
    "Agent Identity & Delegation v0.1": AUTHORITY_ACCEPTED_CHECKPOINT,
    "Mission Runtime v0.1": "721f75683fd53ae0549645c70653bf981c713c6d",
}
REQUIRED_DOCS = (
    "DEVELOPMENT_STATE.md",
    "DEVELOPMENT_PROTOCOL.md",
    "ENVIRONMENT_SETUP.md",
    "LOCAL_AI_DEVELOPMENT.md",
    "OFFLINE_SURVIVAL.md",
    "ARCHITECTURE.md",
    "docs/accepted-checkpoints.md",
    "docs/protected-boundaries.md",
    "docs/ai-handoff-template.md",
)
INVENTORY_DOCS = (
    "ARCHITECTURE.md",
    "DEVELOPMENT_STATE.md",
    "docs/protected-boundaries.md",
)


def repository_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_state() -> dict:
    with (repository_root() / "missionaryx-state.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def run(args, *, cwd: Path, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)


def git(repo: Path, *args: str) -> str:
    result = run(["git", *args], cwd=repo)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def initialize_repository(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.email", "continuity-tests@example.invalid")
    git(path, "config", "user.name", "Continuity Tests")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(path, "add", "seed.txt")
    git(path, "commit", "-q", "-m", "seed")
    return path


def install_tool(repo: Path, tool_name: str) -> Path:
    destination = repo / "tools" / tool_name
    destination.parent.mkdir(exist_ok=True)
    shutil.copy2(repository_root() / "tools" / tool_name, destination)
    destination.chmod(0o755)
    git(repo, "add", str(destination.relative_to(repo)))
    git(repo, "commit", "-q", "-m", f"install {tool_name}")
    return destination


def repository_with_tool(tmp_path: Path, tool_name: str) -> tuple[Path, Path]:
    repo = initialize_repository(tmp_path / "repo")
    return repo, install_tool(repo, tool_name)


def validation_contract() -> dict:
    return runpy.run_path(str(repository_root() / "tools" / "validate-missionaryx"))


def prepare_validation_repository(
    tmp_path: Path,
    *,
    omit_test: str | None = None,
) -> tuple[Path, Path]:
    repo, tool = repository_with_tool(tmp_path, "validate-missionaryx")
    tests = repo / "tests"
    tests.mkdir()
    for relative_path in validation_contract()["QUICK_TESTS"]:
        if relative_path == omit_test:
            continue
        path = repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_placeholder():\n    assert True\n", encoding="utf-8")
    (repo / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "validation fixtures")
    return repo, tool


def fake_pytest(tmp_path: Path, body: str) -> tuple[Path, dict[str, str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "pytest"
    executable.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    executable.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    return executable, env


def git_snapshot(repo: Path) -> tuple[str, ...]:
    return (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "write-tree"),
        git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
        git(repo, "diff", "--binary"),
        git(repo, "diff", "--cached", "--binary"),
        git(repo, "for-each-ref", "--format=%(refname):%(objectname)"),
    )


def protected_inventory(document: Path) -> set[str]:
    text = document.read_text(encoding="utf-8")
    start = "<!-- PROTECTED_INVENTORY_START -->"
    end = "<!-- PROTECTED_INVENTORY_END -->"
    assert start in text and end in text
    section = text.split(start, 1)[1].split(end, 1)[0]
    return set(re.findall(r"`((?:federation|pavilionos)/[^`]+\.py)`", section))


def test_machine_readable_state_is_valid_json_with_required_fields():
    state = load_state()
    assert isinstance(state, dict)
    required = {
        "schema_version",
        "project",
        "current_integrated_checkpoint",
        "accepted_subsystem_checkpoints",
        "known_non_blocking_issues",
        "inherited_architectural_debt",
        "python_version",
        "standard_validation_command",
        "validation_modes",
        "subsystems",
        "architectural_invariants",
        "documentation",
        "candidate_review_state",
    }
    assert required <= state.keys()


def test_state_checkpoint_shas_are_full_hexadecimal_values():
    state = load_state()
    values = [state["current_integrated_checkpoint"]["commit_sha"]]
    for checkpoint in state["accepted_subsystem_checkpoints"]:
        values.append(checkpoint["commit_sha"])
        if "integrated_commit_sha" in checkpoint:
            values.append(checkpoint["integrated_commit_sha"])
    assert all(re.fullmatch(r"[0-9a-f]{40}", value) for value in values)


def test_state_is_candidate_awaiting_fresh_independent_review():
    state = load_state()
    assert state["next_milestone"]["status"] == "candidate_awaiting_independent_review"
    review = state["candidate_review_state"]
    assert review == {
        "status": "candidate",
        "independent_review": "awaiting_fresh_review",
        "checkpoint_designated": False,
    }


def test_required_documentation_and_tools_exist():
    root = repository_root()
    for relative_path in REQUIRED_DOCS:
        path = root / relative_path
        assert path.is_file() and path.stat().st_size > 0
    for relative_path in ("tools/verify-checkpoint", "tools/validate-missionaryx"):
        path = root / relative_path
        assert path.is_file() and os.access(path, os.X_OK)


def test_checkpoint_records_agree_between_json_and_prose():
    state = load_state()
    recorded = {
        item["name"]: item["commit_sha"]
        for item in state["accepted_subsystem_checkpoints"]
    }
    assert recorded == EXPECTED_CHECKPOINTS
    for relative_path in ("DEVELOPMENT_STATE.md", "docs/accepted-checkpoints.md"):
        text = (repository_root() / relative_path).read_text(encoding="utf-8")
        for sha in EXPECTED_CHECKPOINTS.values():
            assert sha in text
        assert AUTHORITY_INTEGRATED_COMMIT in text
        assert "not an ancestor" in text.lower()
        assert "patch-equivalent" in text.lower()


def test_authority_checkpoint_provenance_is_explicit_in_state():
    authority = next(
        item
        for item in load_state()["accepted_subsystem_checkpoints"]
        if item["name"] == "Agent Identity & Delegation v0.1"
    )
    assert authority["commit_sha"] == AUTHORITY_ACCEPTED_CHECKPOINT
    assert authority["integrated_commit_sha"] == AUTHORITY_INTEGRATED_COMMIT
    assert authority["integration_relationship"] == "patch_equivalent"
    assert authority["accepted_commit_is_ancestor"] is False


def test_validation_commands_use_explicit_integrated_base():
    state = load_state()
    commands = [state["standard_validation_command"], *state["validation_modes"].values()]
    for command in commands:
        assert f"--base {BASE_CHECKPOINT}" in command


def test_known_llama_deselection_is_exactly_one_documented_test():
    state = load_state()
    documented = [
        item["test_path"]
        for item in state["known_non_blocking_issues"]
        if item.get("deselection_required")
    ]
    assert documented == [LLAMA_TIMEOUT_NODE]
    assert validation_contract()["KNOWN_DESELECTIONS"] == (LLAMA_TIMEOUT_NODE,)


def test_protected_implementation_inventory_is_consistent():
    state = load_state()
    expected = {
        path
        for subsystem in state["subsystems"]
        for path in subsystem["key_files"]
    }
    assert {
        "federation/agent_identity.py",
        "federation/agent_identity_registry.py",
        "federation/effect_safety.py",
    } <= expected
    for relative_path in INVENTORY_DOCS:
        assert protected_inventory(repository_root() / relative_path) == expected


def test_development_continuity_does_not_modify_core_implementation():
    result = run(
        ["git", "diff", "--name-only", BASE_CHECKPOINT, "--", "federation/", "pavilionos/"],
        cwd=repository_root(),
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_architecture_distinguishes_legacy_current_and_future_material():
    text = (repository_root() / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "# Legacy/Historical RAGHub Architecture" in text
    assert "# Legacy/Historical RAGHub/SIP v1.0 Architecture" in text
    assert "# Current MissionaryX Architecture" in text
    assert "Future/Target" in text
    assert "does not prove that every repository entry point" in text


def test_legacy_secret_boundary_is_described_without_global_no_secret_claim():
    documents = ("ARCHITECTURE.md", "DEVELOPMENT_STATE.md", "docs/accepted-checkpoints.md")
    for relative_path in documents:
        text = (repository_root() / relative_path).read_text(encoding="utf-8")
        assert "AccessCredentialAuthorization" in text
        assert "CredentialLease" in text
        assert "secret_bytes()" in text
        assert "secret_text()" in text
    combined = "\n".join(
        (repository_root() / path).read_text(encoding="utf-8") for path in REQUIRED_DOCS
    ).lower()
    assert "secret leases are not exposed in public interfaces" not in combined


def test_local_ai_document_uses_real_adapter_interface_and_governed_endpoint():
    text = (repository_root() / "LOCAL_AI_DEVELOPMENT.md").read_text(encoding="utf-8")
    assert "LlamaCppLocalAdapter" in text
    assert ".infer(" in text
    assert ".output_text" in text
    assert "LlamaCppAdapter(" not in text
    assert ".chat(" not in text
    assert "response.content" not in text
    assert "127.0.0.1:18080" in text
    assert "127.0.0.1:8080" not in text
    assert "production-ready" not in text.lower()


def test_offline_recovery_avoids_destructive_git_commands():
    text = (repository_root() / "OFFLINE_SURVIVAL.md").read_text(encoding="utf-8")
    assert "git reset --hard" not in text
    assert "git clean" not in text
    assert "git checkout path/to/file.py" not in text
    assert "fresh machine with no network" in text.lower()
    assert "existing prepared machine" in text.lower()


def test_verify_checkpoint_reports_state(tmp_path):
    repo, tool = repository_with_tool(tmp_path, "verify-checkpoint")
    result = run([str(tool)], cwd=repo)
    assert result.returncode == 0, result.stderr
    assert "REPOSITORY STATE" in result.stdout
    assert "Tree status:     clean" in result.stdout


def test_verify_checkpoint_matching_sha_succeeds(tmp_path):
    repo, tool = repository_with_tool(tmp_path, "verify-checkpoint")
    head = git(repo, "rev-parse", "HEAD")
    result = run([str(tool), head], cwd=repo)
    assert result.returncode == 0, result.stderr
    assert "CHECKPOINT MATCH" in result.stdout


def test_verify_checkpoint_mismatch_fails_nonzero(tmp_path):
    repo, tool = repository_with_tool(tmp_path, "verify-checkpoint")
    result = run([str(tool), "0" * 40], cwd=repo)
    assert result.returncode != 0
    assert "CHECKPOINT MISMATCH" in result.stdout


def test_verify_checkpoint_require_clean_enforces_tree_state(tmp_path):
    repo, tool = repository_with_tool(tmp_path, "verify-checkpoint")
    head = git(repo, "rev-parse", "HEAD")
    clean = run([str(tool), "--require-clean", head], cwd=repo)
    assert clean.returncode == 0, clean.stderr
    (repo / "seed.txt").write_text("dirty\n", encoding="utf-8")
    dirty = run([str(tool), "--require-clean", head], cwd=repo)
    assert dirty.returncode != 0
    assert "Tree status:     dirty" in dirty.stdout
    assert "CLEAN TREE REQUIRED" in dirty.stdout


def test_verify_checkpoint_does_not_mutate_git_state(tmp_path):
    repo, tool = repository_with_tool(tmp_path, "verify-checkpoint")
    head = git(repo, "rev-parse", "HEAD")
    before = git_snapshot(repo)
    for arguments in ((), (head,), ("0" * 40,)):
        run([str(tool), *arguments], cwd=repo)
    assert git_snapshot(repo) == before


def test_verify_checkpoint_help():
    result = run([str(repository_root() / "tools" / "verify-checkpoint"), "--help"], cwd=repository_root())
    assert result.returncode == 0
    assert "--require-clean" in result.stdout


def test_missing_required_quick_test_fails_nonzero(tmp_path):
    required = validation_contract()["QUICK_TESTS"]
    missing = required[0]
    repo, tool = prepare_validation_repository(tmp_path, omit_test=missing)
    _, env = fake_pytest(tmp_path, "exit 0\n")
    result = run([str(tool), "--quick"], cwd=repo, env=env)
    assert result.returncode != 0
    assert missing in result.stderr
    assert "Required quick-validation test files are missing" in result.stderr


def test_quick_pytest_failure_propagates_nonzero(tmp_path):
    repo, tool = prepare_validation_repository(tmp_path)
    _, env = fake_pytest(tmp_path, "exit 7\n")
    result = run([str(tool), "--quick"], cwd=repo, env=env)
    assert result.returncode != 0
    assert "ONE OR MORE VALIDATIONS FAILED" in result.stdout


def test_full_pytest_failure_propagates_nonzero(tmp_path):
    repo, tool = prepare_validation_repository(tmp_path)
    _, env = fake_pytest(tmp_path, "exit 7\n")
    result = run([str(tool), "--full"], cwd=repo, env=env)
    assert result.returncode != 0
    assert "ONE OR MORE VALIDATIONS FAILED" in result.stdout


def test_full_validation_uses_only_documented_deselection_and_no_source_cache(tmp_path):
    repo, tool = prepare_validation_repository(tmp_path)
    capture = tmp_path / "pytest-arguments.txt"
    _, env = fake_pytest(
        tmp_path,
        'printf "%s\\n" "$@" > "$CAPTURE_PATH"\nexit 0\n',
    )
    env["CAPTURE_PATH"] = str(capture)
    result = run([str(tool), "--full"], cwd=repo, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    arguments = capture.read_text(encoding="utf-8").splitlines()
    deselections = [arg for arg in arguments if arg.startswith("--deselect=")]
    assert deselections == [f"--deselect={LLAMA_TIMEOUT_NODE}"]
    assert not list(repo.rglob("__pycache__"))
    assert not list(repo.rglob("*.pyc"))


def test_committed_base_range_diff_failure_propagates(tmp_path):
    repo, tool = prepare_validation_repository(tmp_path)
    base = git(repo, "rev-parse", "HEAD")
    (repo / "bad_whitespace.py").write_text("VALUE = 1   \n", encoding="utf-8")
    git(repo, "add", "bad_whitespace.py")
    git(repo, "commit", "-q", "-m", "bad whitespace")
    _, env = fake_pytest(tmp_path, "exit 0\n")
    result = run([str(tool), "--quick", "--base", base], cwd=repo, env=env)
    assert result.returncode != 0
    assert "trailing whitespace" in result.stdout


def test_validation_rejects_non_full_base_sha():
    result = run(
        [str(repository_root() / "tools" / "validate-missionaryx"), "--quick", "--base", "5637f813"],
        cwd=repository_root(),
    )
    assert result.returncode != 0
    assert "full 40-character" in result.stderr
