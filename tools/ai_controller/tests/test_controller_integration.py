from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tools.ai_controller.config import ControllerConfig
from tools.ai_controller.controller import Controller
from tools.ai_controller.experience import ExperienceLedger
from tools.ai_controller.models import ProviderResult, Task
from tools.ai_controller.providers.base import Provider
from tools.ai_controller.queue import DurableQueue
from tools.ai_controller.remote import LocalRunner
from tools.ai_controller.reports import ReportStore
from tools.ai_controller.worktrees import WorktreeManager


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "controller@example.test")
    git(repo, "config", "user.name", "Controller Test")
    (repo / "target.txt").write_text("before\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    return repo, git(repo, "rev-parse", "HEAD")


class FakeProvider(Provider):
    def __init__(self, name, outcomes, prompts):
        self.name = name
        self.outcomes = list(outcomes)
        self.prompts = prompts

    def execute(self, *, task, worktree, attempt):
        self.prompts.append(task.prompt)
        outcome = self.outcomes.pop(0)
        if outcome == "change":
            (worktree / "target.txt").write_text("after\n", encoding="utf-8")
            return ProviderResult.success_result(
                provider=self.name,
                attempt=attempt,
                worktree_path=str(worktree),
                branch=f"controller/{task.id}",
            )
        return ProviderResult.failure_result(
            provider=self.name,
            attempt=attempt,
            worktree_path=str(worktree),
            branch=f"controller/{task.id}",
            category=outcome,
            retryable=outcome in {"usage_limit", "timeout", "no_changes"},
        )


def config(tmp_path, repo):
    return ControllerConfig(
        controller_root=tmp_path / "controller",
        repository_path=repo,
        worktree_root=tmp_path / "worktrees",
        codex_attempts=2,
        local_attempts=1,
        provider_timeout_seconds=30,
        test_timeout_seconds=30,
        poll_interval_seconds=0.01,
        cleanup_successful_worktrees=False,
    )


def test_retries_codex_with_original_prompt_then_uses_local_fallback(tmp_path):
    repo, base = make_repo(tmp_path)
    settings = config(tmp_path, repo)
    queue = DurableQueue(settings.queue_root)
    prompt = "Edit templates/sip_markets.html\nDo not mutate this prompt: café"
    queue.enqueue(
        Task(
            id="fallback-task",
            title="Fallback",
            prompt=prompt,
            base_ref=base,
            tests=[[sys.executable, "-c", "assert open('target.txt').read()=='after\\n'"]],
        )
    )
    prompts = []
    codex = FakeProvider("codex", ["usage_limit", "timeout"], prompts)
    local = FakeProvider("local-ollama", ["change"], prompts)
    controller = Controller(
        config=settings,
        queue=queue,
        runner=LocalRunner(),
        worktrees=WorktreeManager(LocalRunner(), repo, settings.worktree_root),
        providers=[(codex, 2), (local, 1)],
        reports=ReportStore(settings.reports_root),
        experience=ExperienceLedger(settings.experience_path),
    )

    assert controller.run_once() is True

    assert prompts == [prompt, prompt, prompt]
    assert (queue.succeeded / "fallback-task.json").exists()
    report = json.loads(
        (settings.reports_root / "fallback-task.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "succeeded"
    assert report["files_changed"] == ["target.txt"]
    assert [item["provider"] for item in report["attempts"]] == [
        "codex",
        "codex",
        "local-ollama",
    ]
    ledger = settings.experience_path.read_text(encoding="utf-8")
    assert "fallback-task" in ledger
    assert prompt not in ledger


def test_no_actual_changes_fails_and_still_writes_report_and_experience(tmp_path):
    repo, base = make_repo(tmp_path)
    settings = config(tmp_path, repo)
    queue = DurableQueue(settings.queue_root)
    queue.enqueue(
        Task(
            id="no-change-task",
            title="No change",
            prompt="Inspect only",
            base_ref=base,
            tests=[],
            max_attempts=1,
        )
    )
    provider = FakeProvider("codex", ["no_changes"], [])
    controller = Controller.build_for_tests(
        config=settings,
        queue=queue,
        providers=[(provider, 1)],
    )

    assert controller.run_once() is False
    assert (queue.failed / "no-change-task.json").exists()
    report = json.loads(
        (settings.reports_root / "no-change-task.json").read_text(encoding="utf-8")
    )
    assert report["failure_category"] == "no_changes"
    assert settings.experience_path.exists()


def test_dirty_owned_worktree_is_reset_before_retries(tmp_path):
    repo, base = make_repo(tmp_path)
    settings = config(tmp_path, repo)
    queue = DurableQueue(settings.queue_root)
    task = Task(
        id="dirty-recovery",
        title="Dirty recovery",
        prompt="Restore a dirty owned worktree",
        base_ref=base,
        tests=[[sys.executable, "-c", "assert open('target.txt').read() == 'after\\n'"]],
    )
    queue.enqueue(task)
    manager = WorktreeManager(LocalRunner(), repo, settings.worktree_root)
    info = manager.create(task)
    (info.path / "orphaned.txt").write_text("dirty\n", encoding="utf-8")

    controller = Controller(
        config=settings,
        queue=queue,
        runner=LocalRunner(),
        worktrees=manager,
        providers=[(FakeProvider("codex", ["change"], []), 1)],
        reports=ReportStore(settings.reports_root),
        experience=ExperienceLedger(settings.experience_path),
    )

    assert controller.run_once() is True
    report = json.loads(
        (settings.reports_root / "dirty-recovery.json").read_text(encoding="utf-8")
    )
    assert report["worktree_dirty_initially"] is True
    assert "orphaned.txt" in report["worktree_dirty_paths"]
    assert report["status"] == "succeeded"


def test_end_to_end_real_cli_with_fake_provider(tmp_path):
    repo, base = make_repo(tmp_path)
    controller_root = tmp_path / "controller root"
    fake_provider = tmp_path / "fake provider.py"
    fake_provider.write_text(
        "from pathlib import Path\n"
        "Path('target.txt').write_text('after\\n',encoding='utf-8')\n"
        "print('changed target')\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "controller_root": str(controller_root),
                "repository_path": str(repo),
                "worktree_root": str(tmp_path / "worktrees"),
                "codex_executable": sys.executable,
                "codex_prefix_args": [str(fake_provider)],
                "codex_attempts": 1,
                "local_attempts": 0,
                "cleanup_successful_worktrees": False,
            }
        ),
        encoding="utf-8",
    )
    task_path = tmp_path / "task.json"
    task_path.write_text(
        json.dumps(
            {
                "id": "cli-smoke",
                "title": "CLI smoke",
                "prompt": "Change target.txt",
                "base_ref": base,
                "tests": [
                    [
                        sys.executable,
                        "-c",
                        "assert open('target.txt').read() == 'after\\n'",
                    ]
                ],
            }
        ),
        encoding="utf-8",
    )
    env = {"PYTHONPATH": str(Path(__file__).parents[3])}
    import os

    env = {**os.environ, **env}
    enqueue = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.ai_controller.cli",
            "--config",
            str(config_path),
            "enqueue",
            str(task_path),
        ],
        cwd=Path(__file__).parents[3],
        env=env,
        capture_output=True,
        text=True,
    )
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.ai_controller.cli",
            "--config",
            str(config_path),
            "run",
            "--once",
        ],
        cwd=Path(__file__).parents[3],
        env=env,
        capture_output=True,
        text=True,
    )

    assert enqueue.returncode == 0, enqueue.stderr
    assert run.returncode == 0, run.stderr
    assert (controller_root / "queue" / "succeeded" / "cli-smoke.json").exists()
    report = json.loads(
        (controller_root / "reports" / "cli-smoke.json").read_text(encoding="utf-8")
    )
    assert report["files_changed"] == ["target.txt"]
    assert (controller_root / "experience.jsonl").exists()
