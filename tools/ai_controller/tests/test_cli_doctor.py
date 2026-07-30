from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

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
    return repo


def make_fake_ssh(tmp_path):
    script = tmp_path / "fake-ssh.sh"
    script.write_text(
        "#!/bin/sh\n"
        "exec \"$6\" \"$7\"\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def make_fake_codex(tmp_path):
    script = tmp_path / "fake-codex.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '--version' in sys.argv[1:]:\n"
        "    print('codex-cli test-double')\n"
        "    raise SystemExit(0)\n"
        "print('codex invoked')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_init_and_doctor_validate_windows_packaging_surface(tmp_path, start_ollama_server):
    repo = make_repo(tmp_path)
    controller_root = tmp_path / "controller root"
    worktree_root = tmp_path / "worktrees"
    ssh_key = tmp_path / "id_ed25519"
    ssh_key.write_text("dummy", encoding="utf-8")
    fake_ssh = make_fake_ssh(tmp_path)
    fake_codex = make_fake_codex(tmp_path)
    config_path = tmp_path / "config.json"

    with start_ollama_server(tags_models=[{"name": "qwen2.5-coder:3b"}]) as server:
        config_path.write_text(
            json.dumps(
                {
                    "controller_root": str(controller_root),
                    "repository_path": str(repo),
                    "worktree_root": str(worktree_root),
                    "codex_executable": str(fake_codex),
                    "codex_attempts": 1,
                    "local_attempts": 1,
                    "provider_timeout_seconds": 30,
                    "test_timeout_seconds": 30,
                    "poll_interval_seconds": 0.01,
                    "ssh_executable": str(fake_ssh),
                    "ssh_host": "matias@fedora",
                    "ssh_key_path": str(ssh_key),
                    "remote_python": sys.executable,
                    "ollama_base_url": server.url,
                    "ollama_model": "qwen2.5-coder:3b",
                    "ollama_request_timeout_seconds": 5,
                    "ollama_generation_timeout_seconds": 5,
                    "ollama_context_size": 2048,
                    "ollama_temperature": 0.2,
                    "ollama_max_retries": 1,
                    "cleanup_successful_worktrees": False,
                }
            ),
            encoding="utf-8",
        )

        env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[3])}
        init = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.ai_controller.cli",
                "--config",
                str(config_path),
                "init",
            ],
            cwd=Path(__file__).parents[3],
            env=env,
            capture_output=True,
            text=True,
        )
        doctor = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.ai_controller.cli",
                "--config",
                str(config_path),
                "doctor",
            ],
            cwd=Path(__file__).parents[3],
            env=env,
            capture_output=True,
            text=True,
        )

    assert init.returncode == 0, init.stderr
    assert doctor.returncode == 0, doctor.stderr
    assert (controller_root / "queue" / "pending").exists()
    assert (controller_root / "reports").exists()
    assert (controller_root / "logs").exists()
    assert worktree_root.exists()
