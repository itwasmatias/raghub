from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.ai_controller.models import Task
from tools.ai_controller.providers.codex import classify
from tools.ai_controller.remote import LocalRunner, SSHRunner


def test_task_json_round_trip_preserves_prompt_and_path_characters():
    prompt = (
        "Edit templates/sip_markets.html\n"
        "Keep spaces, apostrophe's, backslashes C:\\RAGHubOperator, "
        "forward/slashes, and Unicode: café 🧪.\n"
        "Do not trim the first character."
    )
    payload = {
        "id": "20260729T213636Z-add-accessible-sip-watchlist-button-state",
        "title": "Accessible watchlist state",
        "prompt": prompt,
        "base_ref": "abc123",
        "tests": [["python", "-m", "pytest", "-q", "tests/test ui.py"]],
        "max_attempts": 3,
        "metadata": {"path": "templates/sip_markets.html", "quote": "it's exact"},
    }

    task = Task.from_dict(json.loads(json.dumps(payload, ensure_ascii=False)))

    assert task.to_dict() == payload
    assert task.prompt == prompt
    assert task.metadata["path"] == "templates/sip_markets.html"


@pytest.mark.parametrize(
    "task_id",
    [
        "../escape",
        "has spaces",
        "slash/name",
        "back\\slash",
        "",
        ".",
        "a" * 129,
    ],
)
def test_task_id_must_be_safe_for_files_branches_logs_and_worktrees(task_id):
    with pytest.raises(ValueError, match="task id"):
        Task.from_dict(
            {
                "id": task_id,
                "title": "bad",
                "prompt": "bad",
                "base_ref": "HEAD",
                "tests": [],
            }
        )


def test_local_runner_preserves_stdin_and_argument_boundaries(tmp_path):
    script = tmp_path / "echo_args.py"
    script.write_text(
        "import json,sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read()}, "
        "ensure_ascii=False))\n",
        encoding="utf-8",
    )
    prompt = "templates/sip_markets.html\nspaces ' apostrophe \\ slash / café"

    result = LocalRunner().run(
        [sys.executable, str(script), "templates/sip_markets.html", "two words"],
        cwd=tmp_path,
        input_text=prompt,
        timeout_seconds=5,
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["argv"] == ["templates/sip_markets.html", "two words"]
    assert payload["stdin"] == prompt


def test_ssh_transport_round_trip_does_not_truncate_first_character(tmp_path):
    capture = tmp_path / "capture.json"
    fake_ssh = tmp_path / "fake ssh.py"
    fake_ssh.write_text(
        "import json,subprocess,sys\n"
        "capture=sys.argv[1]\n"
        "script=sys.stdin.read()\n"
        "open(capture,'w',encoding='utf-8').write(json.dumps({"
        "'ssh_argv':sys.argv[2:], 'script':script},ensure_ascii=False))\n"
        "raise SystemExit(subprocess.run([sys.executable,'-'],input=script,text=True).returncode)\n",
        encoding="utf-8",
    )
    ssh_wrapper = tmp_path / "ssh-wrapper"
    ssh_wrapper.write_text(
        f"#!/bin/sh\nexec '{sys.executable}' '{fake_ssh}' '{capture}' \"$@\"\n",
        encoding="utf-8",
    )
    ssh_wrapper.chmod(0o755)
    runner = SSHRunner(
        ssh_executable=str(ssh_wrapper),
        host="matias@100.93.102.93",
        key_path="C:\\Users\\mr\\.ssh\\id_ed25519_raghub_operator",
        remote_python=sys.executable,
    )
    prompt = "templates/sip_markets.html\nUnicode café\napostrophe's\nC:\\path"

    result = runner.run(
        [sys.executable, "-c", "import sys;print(sys.argv[1]);print(sys.stdin.read())",
         "templates/sip_markets.html"],
        cwd=tmp_path,
        input_text=prompt,
        timeout_seconds=10,
    )

    assert result.exit_code == 0
    assert result.stdout == f"templates/sip_markets.html\n{prompt}\n"
    assert json.loads(capture.read_text(encoding="utf-8"))["script"].startswith(
        "import base64"
    )


def test_runner_timeout_is_explicit(tmp_path):
    result = LocalRunner().run(
        [sys.executable, "-c", "import time;time.sleep(5)"],
        cwd=tmp_path,
        timeout_seconds=0.05,
    )

    assert result.timed_out is True
    assert result.exit_code is None
    assert result.finished_at >= result.started_at


@pytest.mark.parametrize(
    "stderr,stdout",
    [
        (
            "WARNING: proceeding, even though we could not create PATH aliases: Read-only file system (os error 30)\n"
            "Error: failed to initialize in-process app-server client: Read-only file system (os error 30)\n",
            "",
        ),
        (
            "stream disconnected before completion: error sending request for url (https://api.openai.com/v1/responses)\n",
            "",
        ),
    ],
)
def test_codex_classify_marks_startup_and_transport_errors_retryable(stderr, stdout):
    category, retryable = classify(stderr, stdout, timed_out=False, exit_code=1)

    assert category == "provider_unavailable"
    assert retryable is True
