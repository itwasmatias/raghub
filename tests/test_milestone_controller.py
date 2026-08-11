from __future__ import annotations

import json
import os
import shlex
import site
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from tools.ai_controller._locking import FileLock
from tools.ai_controller.config import ControllerConfig
from tools.ai_controller.milestone import MilestoneBaseError, MilestoneController
from tools.ai_controller.remote import LocalRunner


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_repo(tmp_path: Path, *, failing_test: bool = False) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "controller@example.test")
    git(repo, "config", "user.name", "Controller Test")
    (repo / "app.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_smoke.py").write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
    if failing_test:
        (tests_dir / "test_failure.py").write_text(
            "def test_failure():\n    assert False\n",
            encoding="utf-8",
        )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    return repo, git(repo, "rev-parse", "HEAD")


def make_settings(
    tmp_path: Path,
    repo: Path,
    *,
    controller_root_name: str = "controller",
    worktree_root_name: str = "worktrees",
    test_timeout_seconds: float = 2.0,
) -> ControllerConfig:
    return ControllerConfig(
        controller_root=tmp_path / controller_root_name,
        repository_path=repo,
        worktree_root=tmp_path / worktree_root_name,
        codex_attempts=0,
        local_attempts=0,
        native_agent_attempts=0,
        provider_timeout_seconds=30,
        test_timeout_seconds=test_timeout_seconds,
        poll_interval_seconds=0.01,
        cleanup_successful_worktrees=False,
    )


def make_controller(tmp_path: Path, repo: Path, **kwargs: object) -> tuple[MilestoneController, ControllerConfig]:
    settings = make_settings(tmp_path, repo, **kwargs)
    return MilestoneController(config=settings, runner=LocalRunner()), settings


def config_payload(settings: ControllerConfig) -> dict[str, object]:
    payload = asdict(settings)
    payload["controller_root"] = str(payload["controller_root"])
    payload["repository_path"] = str(payload["repository_path"])
    payload["worktree_root"] = str(payload["worktree_root"])
    if payload.get("ssh_key_path") is not None:
        payload["ssh_key_path"] = str(payload["ssh_key_path"])
    return payload


def write_config(path: Path, settings: ControllerConfig) -> Path:
    path.write_text(json.dumps(config_payload(settings), indent=2), encoding="utf-8")
    return path


def cli_env(repo_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root)
    return env


def run_cli(repo_root: Path, config_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tools.ai_controller.cli", "--config", str(config_path), *args],
        cwd=repo_root,
        env=cli_env(repo_root),
        capture_output=True,
        text=True,
    )


def pytest_shim_command(*pytest_args: str) -> list[str]:
    user_site_packages = site.getusersitepackages()
    script = (
        "import site, sys\n"
        f"site.addsitedir({user_site_packages!r})\n"
        "import pytest\n"
        "sys.exit(pytest.main(sys.argv[1:]))\n"
    )
    return [sys.executable, "-c", script, *pytest_args]


def test_create_refuses_dirty_repository_with_symbolic_base(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    (repo / "app.py").write_text("dirty\n", encoding="utf-8")
    controller, _ = make_controller(tmp_path, repo)

    with pytest.raises(MilestoneBaseError, match="dirty repository requires an exact commit base"):
        controller.create(
            name="Dirty base",
            base_ref="main",
            implementation_area="tooling",
            milestone_id="dirty-base",
        )


def test_create_rejects_missing_base(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    controller, _ = make_controller(tmp_path, repo)

    with pytest.raises(MilestoneBaseError, match="unable to resolve base ref"):
        controller.create(
            name="Missing base",
            base_ref="missing-base",
            implementation_area="tooling",
            milestone_id="missing-base",
        )


def test_create_rejects_existing_branch_collision(tmp_path: Path) -> None:
    repo, base = make_repo(tmp_path)
    git(repo, "branch", "controller/branch-collision", base)
    controller, _ = make_controller(tmp_path, repo)

    with pytest.raises(RuntimeError, match="branch collision"):
        controller.create(
            name="Branch collision",
            base_ref=base,
            implementation_area="tooling",
            milestone_id="branch-collision",
        )


def test_create_rejects_existing_worktree_collision(tmp_path: Path) -> None:
    repo, base = make_repo(tmp_path)
    settings = make_settings(tmp_path, repo)
    Path(settings.worktree_root).joinpath("worktree-collision").mkdir(parents=True)
    controller = MilestoneController(config=settings, runner=LocalRunner())

    with pytest.raises(RuntimeError, match="unrelated existing directory"):
        controller.create(
            name="Worktree collision",
            base_ref=base,
            implementation_area="tooling",
            milestone_id="worktree-collision",
        )


def test_create_and_status_leave_canonical_uninvented_when_metadata_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base = make_repo(tmp_path)
    controller, _ = make_controller(tmp_path, repo)
    monkeypatch.setattr(controller, "resolve_canonical_base", lambda: None)

    state = controller.create(
        name="No canonical metadata",
        base_ref=base,
        implementation_area="tooling",
        milestone_id="no-canonical",
    )
    status = controller.status("no-canonical")

    assert state.canonical_base_commit is None
    assert state.canonical_base_ref is None
    assert status["inventory"]["canonical_base"] is None
    assert status["milestone"]["canonical_base_commit"] is None


def test_validate_records_focused_failure_and_generates_handoff_after_failure(
    tmp_path: Path,
) -> None:
    repo, base = make_repo(tmp_path)
    controller, _ = make_controller(tmp_path, repo)
    state = controller.create(
        name="Focused failure",
        base_ref=base,
        implementation_area="tooling",
        milestone_id="focused-failure",
        focused_tests=[[sys.executable, "-c", "import sys; sys.exit(1)"]],
    )

    summary = controller.validate(state.milestone_id)
    handoff = controller.handoff(state.milestone_id)
    refreshed = controller.load_state(state.milestone_id)

    assert not summary.success
    assert summary.phases[0].name == "focused_tests"
    assert summary.phases[0].status == "failed"
    assert summary.completed_phases[-1] == "final_git_status"
    assert refreshed.status == "handoff_ready"
    assert Path(handoff["handoff_path"]).exists()
    assert Path(refreshed.artifacts["report_md"]).exists()
    assert Path(refreshed.artifacts["evidence_json"]).exists()


def test_validate_records_full_suite_failure(tmp_path: Path) -> None:
    repo, base = make_repo(tmp_path, failing_test=True)
    controller, _ = make_controller(tmp_path, repo)
    state = controller.create(
        name="Full suite failure",
        base_ref=base,
        implementation_area="tooling",
        milestone_id="full-suite-failure",
        focused_tests=[[sys.executable, "-c", "print('focused ok')"]],
    )

    summary = controller.validate(state.milestone_id)
    refreshed = controller.load_state(state.milestone_id)

    assert not summary.success
    assert any(phase.name == "full_suite" and phase.status == "failed" for phase in summary.phases)
    assert refreshed.status == "blocked"
    assert refreshed.validation is not None
    assert refreshed.validation.remaining_phases == ()
    assert refreshed.failures


def test_validate_handles_timeout_without_raising(tmp_path: Path) -> None:
    repo, base = make_repo(tmp_path)
    controller, _ = make_controller(tmp_path, repo, test_timeout_seconds=0.1)
    state = controller.create(
        name="Timeout case",
        base_ref=base,
        implementation_area="tooling",
        milestone_id="timeout-case",
        focused_tests=[[sys.executable, "-c", "import time; time.sleep(1)"]],
    )

    summary = controller.validate(state.milestone_id)

    assert not summary.success
    assert summary.overall_exit_code == 124
    assert summary.phases[0].timed_out is True
    assert summary.phases[0].status == "failed"


def test_status_evidence_and_handoff_repeatably_track_untracked_files_and_quote_spaces(
    tmp_path: Path,
) -> None:
    repo, base = make_repo(tmp_path)
    settings = make_settings(
        tmp_path,
        repo,
        controller_root_name="controller root",
        worktree_root_name="worktrees root",
    )
    config_path = write_config(tmp_path / "controller config.json", settings)

    create = run_cli(
        Path(__file__).resolve().parents[1],
        config_path,
        "milestone",
        "create",
        "Space milestone",
        "--base-ref",
        base,
        "--implementation-area",
        "tooling",
        "--milestone-id",
        "space-case",
        "--focused-test",
        shlex.join(pytest_shim_command("-q", "tests/test_smoke.py")),
        "--json",
    )
    assert create.returncode == 0, create.stderr
    created = json.loads(create.stdout)
    worktree = Path(created["worktree"])
    (worktree / "scratch file.txt").write_text("scratch\n", encoding="utf-8")

    status = run_cli(
        Path(__file__).resolve().parents[1],
        config_path,
        "milestone",
        "status",
        "space-case",
        "--json",
    )
    assert status.returncode == 0, status.stderr
    status_payload = json.loads(status.stdout)
    assert "scratch file.txt" in status_payload["milestone"]["untracked_files"]

    status_text = run_cli(
        Path(__file__).resolve().parents[1],
        config_path,
        "milestone",
        "status",
    )
    assert status_text.returncode == 0, status_text.stderr
    assert "Active worktrees:" in status_text.stdout
    assert "Branches:" in status_text.stdout
    assert "Milestones:" in status_text.stdout
    assert "space-case" in status_text.stdout

    validation = run_cli(
        Path(__file__).resolve().parents[1],
        config_path,
        "milestone",
        "validate",
        "space-case",
        "--json",
    )
    assert validation.returncode == 0, validation.stderr
    validation_payload = json.loads(validation.stdout)
    assert validation_payload["validation"]["overall_exit_code"] == 0

    evidence_1 = run_cli(
        Path(__file__).resolve().parents[1],
        config_path,
        "milestone",
        "evidence",
        "space-case",
        "--json",
    )
    evidence_2 = run_cli(
        Path(__file__).resolve().parents[1],
        config_path,
        "milestone",
        "evidence",
        "space-case",
        "--json",
    )
    assert evidence_1.returncode == 0, evidence_1.stderr
    assert evidence_2.returncode == 0, evidence_2.stderr
    evidence_payload_1 = json.loads(evidence_1.stdout)
    evidence_payload_2 = json.loads(evidence_2.stdout)
    assert evidence_payload_1["report_sha256"] == evidence_payload_2["report_sha256"]
    assert "scratch file.txt" in evidence_payload_2["milestone"]["untracked_files"]

    status_detail = run_cli(
        Path(__file__).resolve().parents[1],
        config_path,
        "milestone",
        "status",
        "space-case",
    )
    assert status_detail.returncode == 0, status_detail.stderr
    assert "Ancestry:" in status_detail.stdout
    assert "Next action:" in status_detail.stdout

    handoff = run_cli(
        Path(__file__).resolve().parents[1],
        config_path,
        "milestone",
        "handoff",
        "space-case",
        "--json",
    )
    assert handoff.returncode == 0, handoff.stderr
    handoff_payload = json.loads(handoff.stdout)
    expected = shlex.quote(str(worktree))
    assert expected in handoff_payload["verification_commands"][0]
    assert Path(handoff_payload["handoff_path"]).exists()


def test_validation_lock_contention_blocks_then_completes(tmp_path: Path) -> None:
    repo, base = make_repo(tmp_path)
    controller, _ = make_controller(tmp_path, repo)
    state = controller.create(
        name="Lock contention",
        base_ref=base,
        implementation_area="tooling",
        milestone_id="lock-contention",
        focused_tests=[[sys.executable, "-c", "print('focused ok')"]],
    )

    acquired = threading.Event()
    release = threading.Event()
    result: dict[str, object] = {}

    def hold_lock() -> None:
        with FileLock(controller.validation_lock_path):
            acquired.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    assert acquired.wait(timeout=5)

    def run_validation() -> None:
        result["summary"] = controller.validate(state.milestone_id)

    worker = threading.Thread(target=run_validation, daemon=True)
    worker.start()
    time.sleep(0.2)
    assert worker.is_alive()
    release.set()
    worker.join(timeout=10)
    holder.join(timeout=10)

    assert not worker.is_alive()
    summary = result["summary"]
    assert getattr(summary, "success", False) is True
