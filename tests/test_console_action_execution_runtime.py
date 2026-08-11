from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from federation.dispatch_offer import DispatchStatus
from tools.ai_controller.operations_api.console_server import JobStatus
from tools.ai_controller.operations_api.console_server.jobs import JobCorruptionError
from tools.ai_controller.operations_api.console_server.runtime import _run_bounded
from test_console_server import (  # noqa: F401
    client,
    console,
    heartbeat_registry,
    node_registry,
    workspace_root,
    workspaces,
)


SECRET_NAMES = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AZURE_CLIENT_SECRET",
    "DB_PASSWORD",
    "DATABASE_URL",
    "RAGHUB_CONTROLLER_TOKENS",
    "RAGHUB_INTEGRITY_KEY",
)

SAFE_NAMES = ("HOME", "LANG", "LC_ALL", "PATH", "TMPDIR")

CHILD_ENV_PROBE = (
    "import json, os, sys\n"
    "names = sys.argv[1:]\n"
    "print(json.dumps({name: os.environ.get(name) for name in names}, sort_keys=True))\n"
)


class FakeRunner:
    def __init__(self, *, returncode=0, stdout=" M tracked.py\n", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


def test_inspect_git_status_executes_fixed_argv_and_persists_linkage(
    client, console, workspace_root: Path
):
    runner = FakeRunner()
    console.action_runtime.runner = runner

    response = client.post(
        "/api/console/v1/actions",
        json={
            "action_type": "inspect_git_status",
            "workspace_id": "test-workspace",
            "idempotency_key": "execute-once",
            "mission_id": "mission-runtime",
        },
        headers={"Authorization": "Bearer full-token"},
    )

    assert response.status_code == 201
    assert response.json["status"] == "succeeded"
    assert len(runner.calls) == 1
    argv, kwargs = runner.calls[0]
    assert argv == ["git", "status", "--short"]
    # M1: Verify workspace identity is FD-anchored (TOCTOU protection)
    cwd = kwargs["cwd"]
    assert isinstance(cwd, str) and cwd.startswith("/proc/self/fd/"), \
        "cwd must be FD-anchored for workspace TOCTOU protection"
    # Verify the FD-anchored path resolves to the registered workspace
    # The descriptor is closed when the request returns, so its /proc path
    # cannot be resolved here. The dedicated retargeting test verifies that the
    # live FD remains anchored to the registered workspace during execution.
    # Verify other execution parameters are correct
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 30
    assert kwargs["check"] is False

    job = console.job_tracker.get_job(response.json["job_id"])
    offer = console.task_dispatch_coordinator.inspect_offer(job.dispatch_offer_id)
    assert job.status is JobStatus.SUCCEEDED
    assert job.task_id == offer.task_id
    assert job.mission_id == offer.mission_id == "mission-runtime"
    assert job.assignment_id == offer.assignment_id
    assert job.dispatch_offer_id == offer.offer_id
    assert job.action_type == "inspect_git_status"
    assert job.workspace_id == "test-workspace"
    assert offer.status is DispatchStatus.ACCEPTED


def test_exact_idempotent_duplicate_does_not_run_twice(client, console):
    runner = FakeRunner()
    console.action_runtime.runner = runner
    body = {
        "action_type": "inspect_git_status",
        "workspace_id": "test-workspace",
        "idempotency_key": "same-execution",
    }

    first = client.post("/api/console/v1/actions", json=body, headers={"Authorization": "Bearer full-token"})
    second = client.post("/api/console/v1/actions", json=body, headers={"Authorization": "Bearer full-token"})

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json["job_id"] == first.json["job_id"]
    assert second.json["status"] == "succeeded"
    assert len(runner.calls) == 1


@pytest.mark.parametrize("field,value", [
    ("executable", "bash"),
    ("argv", ["git", "status"]),
    ("command", "git status; id"),
    ("shell", True),
    ("env", {"PATH": "/tmp"}),
    ("environment", {"TOKEN": "secret"}),
    ("relative_path", "subdir"),
])
def test_execution_shaping_fields_are_rejected(client, console, field, value):
    runner = FakeRunner()
    console.action_runtime.runner = runner
    response = client.post(
        "/api/console/v1/actions",
        json={"action_type": "inspect_git_status", "workspace_id": "test-workspace", field: value},
        headers={"Authorization": "Bearer full-token"},
    )
    assert response.status_code == 400
    assert runner.calls == []


def test_output_is_independently_bounded_scrubbed_and_marked(
    client, console, monkeypatch
):
    action_type = next(iter(console.action_catalog._SPECS))
    monkeypatch.setitem(
        console.action_catalog._SPECS,
        action_type,
        console.action_catalog.get_spec("inspect_git_status").__class__(
            action_type=action_type,
            required_capability=console.action_catalog.get_spec(
                "inspect_git_status"
            ).required_capability,
            authorization_level=console.action_catalog.get_spec(
                "inspect_git_status"
            ).authorization_level,
            approval_required=False,
            max_timeout_seconds=30,
            max_output_bytes=32,
            allowed_in_v0_1=True,
        ),
    )
    runner = FakeRunner(
        stdout="API_KEY=super-secret\n" + "x" * 100,
        stderr="Bearer very-secret-token\n" + "y" * 100,
    )
    console.action_runtime.runner = runner

    response = client.post(
        "/api/console/v1/actions",
        json={"action_type": "inspect_git_status", "workspace_id": "test-workspace"},
        headers={"Authorization": "Bearer full-token"},
    )
    job = console.job_tracker.get_job(response.json["job_id"])
    persisted = (console.job_tracker._storage_path / f"{job.job_id}.json").read_text()

    assert job.stdout_truncated is True
    assert job.stderr_truncated is True
    assert len(job.stdout.encode()) <= 32
    assert len(job.stderr.encode()) <= 32
    assert "super-secret" not in persisted
    assert "very-secret-token" not in persisted
    assert "[REDACTED]" in job.stdout
    assert "[REDACTED]" in job.stderr


def test_runtime_rejects_missing_or_mismatched_dispatch_identity(console):
    runner = FakeRunner()
    console.action_runtime.runner = runner
    job = console.job_tracker.create_job(
        "inspect_git_status", "test-workspace", "user", dispatch_offer_id="missing"
    )
    with pytest.raises(ValueError, match="dispatch offer"):
        console.action_runtime.execute(job.job_id)
    assert runner.calls == []


def test_non_allowlisted_and_approval_required_action_never_executes(client, console):
    runner = FakeRunner()
    console.action_runtime.runner = runner
    response = client.post(
        "/api/console/v1/actions",
        json={
            "action_type": "run_test_target",
            "workspace_id": "test-workspace",
            "test_target": "tests/test_example.py",
        },
        headers={"Authorization": "Bearer full-token"},
    )
    assert response.status_code == 201
    assert response.json["approval_required"] is True
    assert response.json["status"] == "pending"
    assert runner.calls == []


def test_registered_workspace_must_still_be_a_directory(client, console, workspace_root):
    runner = FakeRunner()
    console.action_runtime.runner = runner
    response = client.post(
        "/api/console/v1/actions",
        json={"action_type": "inspect_git_status", "workspace_id": "test-workspace"},
        headers={"Authorization": "Bearer propose-token"},
    )
    workspace_root.rmdir()
    with pytest.raises(ValueError, match="directory"):
        console.action_runtime.execute(response.json["job_id"])
    assert runner.calls == []


def test_registered_workspace_canonical_path_cannot_be_retargeted(
    client, console, workspace_root, tmp_path
):
    runner = FakeRunner()
    console.action_runtime.runner = runner
    response = client.post(
        "/api/console/v1/actions",
        json={"action_type": "inspect_git_status", "workspace_id": "test-workspace"},
        headers={"Authorization": "Bearer propose-token"},
    )
    escaped = tmp_path / "outside"
    escaped.mkdir()
    workspace_root.rmdir()
    workspace_root.symlink_to(escaped, target_is_directory=True)

    with pytest.raises(ValueError, match="canonical"):
        console.action_runtime.execute(response.json["job_id"])

    assert runner.calls == []


def test_failed_command_is_terminal_and_is_not_retried(client, console):
    runner = FakeRunner(returncode=7, stderr="git failed")
    console.action_runtime.runner = runner
    body = {
        "action_type": "inspect_git_status",
        "workspace_id": "test-workspace",
        "idempotency_key": "failed-once",
    }

    first = client.post(
        "/api/console/v1/actions",
        json=body,
        headers={"Authorization": "Bearer full-token"},
    )
    second = client.post(
        "/api/console/v1/actions",
        json=body,
        headers={"Authorization": "Bearer full-token"},
    )

    assert first.json["status"] == "failed"
    assert first.json["exit_code"] == 7
    assert second.json["status"] == "failed"
    assert len(runner.calls) == 1


def test_interrupted_execution_is_durable_and_requires_reconciliation(client, console):
    class InterruptingRunner:
        def __init__(self):
            self.calls = 0

        def __call__(self, argv, **kwargs):
            self.calls += 1
            raise KeyboardInterrupt

    runner = InterruptingRunner()
    console.action_runtime.runner = runner

    with pytest.raises(KeyboardInterrupt):
        client.post(
            "/api/console/v1/actions",
            json={
                "action_type": "inspect_git_status",
                "workspace_id": "test-workspace",
            },
            headers={"Authorization": "Bearer full-token"},
        )

    job = console.job_tracker.list_jobs(limit=1)[0]
    assert job.status is JobStatus.RECONCILIATION_REQUIRED
    assert job.failure_reason == "execution outcome is ambiguous; reconciliation required"
    assert console.action_runtime.execute(job.job_id).status is JobStatus.RECONCILIATION_REQUIRED
    assert runner.calls == 1


def test_preexisting_running_job_is_not_automatically_retried(client, console):
    runner = FakeRunner()
    console.action_runtime.runner = runner
    response = client.post(
        "/api/console/v1/actions",
        json={
            "action_type": "inspect_git_status",
            "workspace_id": "test-workspace",
        },
        headers={"Authorization": "Bearer propose-token"},
    )
    console.job_tracker.update_status(response.json["job_id"], JobStatus.RUNNING)

    job = console.action_runtime.execute(response.json["job_id"])

    assert job.status is JobStatus.RECONCILIATION_REQUIRED
    assert runner.calls == []


def test_concurrent_exact_duplicate_requests_create_and_execute_one_job(client, console):
    class CountingRunner(FakeRunner):
        def __init__(self):
            super().__init__()
            self._lock = threading.Lock()

        def __call__(self, argv, **kwargs):
            with self._lock:
                self.calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "clean\n", "")

    runner = CountingRunner()
    console.action_runtime.runner = runner
    barrier = threading.Barrier(8)
    body = {
        "action_type": "inspect_git_status",
        "workspace_id": "test-workspace",
        "idempotency_key": "concurrent-exact-duplicate",
        "mission_id": "mission-concurrent",
    }

    def submit():
        barrier.wait()
        with client.application.test_client() as thread_client:
            return thread_client.post(
                "/api/console/v1/actions",
                json=body,
                headers={"Authorization": "Bearer full-token"},
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: submit(), range(8)))

    assert {response.status_code for response in responses} <= {200, 201}
    assert len({response.json["job_id"] for response in responses}) == 1
    assert len(console.job_tracker.list_jobs()) == 1
    assert len(runner.calls) == 1


def test_concurrent_execute_calls_claim_pending_job_once(client, console):
    response = client.post(
        "/api/console/v1/actions",
        json={
            "action_type": "inspect_git_status",
            "workspace_id": "test-workspace",
        },
        headers={"Authorization": "Bearer propose-token"},
    )
    job = console.job_tracker.get_job(response.json["job_id"])
    console.task_dispatch_coordinator.accept_offer(
        offer_id=job.dispatch_offer_id,
        actor_node_id=job.target_node_id,
    )

    class BlockingRunner(FakeRunner):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def __call__(self, argv, **kwargs):
            self.calls.append((argv, kwargs))
            self.entered.set()
            self.release.wait(timeout=5)
            return subprocess.CompletedProcess(argv, 0, "clean\n", "")

    runner = BlockingRunner()
    console.action_runtime.runner = runner
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(console.action_runtime.execute, job.job_id)
        assert runner.entered.wait(timeout=5)
        second = pool.submit(console.action_runtime.execute, job.job_id)
        runner.release.set()
        results = (first.result(), second.result())

    assert len(runner.calls) == 1
    assert all(result.status is JobStatus.SUCCEEDED for result in results)


def test_corrupt_job_record_fails_closed_for_idempotency_lookup(console):
    corrupt = console.job_tracker._storage_path / "job-corrupt.json"
    corrupt.write_text('{"job_id":"job-corrupt",', encoding="utf-8")

    with pytest.raises(JobCorruptionError, match="corrupt"):
        console.job_tracker.find_by_idempotency_key("new-key")


def test_failed_atomic_replace_preserves_previous_authoritative_state(
    console, monkeypatch
):
    job = console.job_tracker.create_job(
        "inspect_git_status", "test-workspace", "user"
    )

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(
        "tools.ai_controller.operations_api.console_server.jobs.os.replace",
        fail_replace,
    )
    with pytest.raises(OSError, match="replace failure"):
        console.job_tracker.update_status(job.job_id, JobStatus.RUNNING)

    assert console.job_tracker.get_job(job.job_id).status is JobStatus.PENDING


def test_execution_fingerprint_rejects_workspace_job_tampering(
    client, console, tmp_path
):
    other = tmp_path / "other-workspace"
    other.mkdir()
    console.workspaces._roots["other-workspace"] = other.resolve()
    response = client.post(
        "/api/console/v1/actions",
        json={
            "action_type": "inspect_git_status",
            "workspace_id": "test-workspace",
        },
        headers={"Authorization": "Bearer propose-token"},
    )
    job = console.job_tracker.get_job(response.json["job_id"])
    console.job_tracker._save(replace(job, workspace_id="other-workspace"))

    with pytest.raises(ValueError, match="execution authority"):
        console.action_runtime.execute(job.job_id)


def test_execution_fingerprint_rejects_composed_foreign_offer(
    client, console, tmp_path
):
    other = tmp_path / "other-workspace"
    other.mkdir()
    console.workspaces._roots["other-workspace"] = other.resolve()

    first = client.post(
        "/api/console/v1/actions",
        json={"action_type": "inspect_git_status", "workspace_id": "test-workspace"},
        headers={"Authorization": "Bearer propose-token"},
    )
    second = client.post(
        "/api/console/v1/actions",
        json={"action_type": "inspect_git_status", "workspace_id": "other-workspace"},
        headers={"Authorization": "Bearer propose-token"},
    )
    first_job = console.job_tracker.get_job(first.json["job_id"])
    second_job = console.job_tracker.get_job(second.json["job_id"])
    composed = replace(
        first_job,
        mission_id=second_job.mission_id,
        task_id=second_job.task_id,
        assignment_id=second_job.assignment_id,
        dispatch_offer_id=second_job.dispatch_offer_id,
        target_node_id=second_job.target_node_id,
    )
    console.job_tracker._save(composed)

    with pytest.raises(ValueError, match="execution authority"):
        console.action_runtime.execute(first_job.job_id)


def test_workspace_identity_is_anchored_during_runner_launch(
    client, console, workspace_root, tmp_path
):
    (workspace_root / "identity.txt").write_text("registered", encoding="utf-8")
    moved = tmp_path / "moved-registered"
    escaped = tmp_path / "escaped"
    escaped.mkdir()
    (escaped / "identity.txt").write_text("escaped", encoding="utf-8")

    class RetargetingRunner(FakeRunner):
        def __call__(self, argv, **kwargs):
            workspace_root.rename(moved)
            workspace_root.symlink_to(escaped, target_is_directory=True)
            assert (Path(kwargs["cwd"]) / "identity.txt").read_text() == "registered"
            return super().__call__(argv, **kwargs)

    runner = RetargetingRunner()
    console.action_runtime.runner = runner
    response = client.post(
        "/api/console/v1/actions",
        json={"action_type": "inspect_git_status", "workspace_id": "test-workspace"},
        headers={"Authorization": "Bearer full-token"},
    )

    assert response.status_code == 201
    assert response.json["status"] == "succeeded"
    assert len(runner.calls) == 1


def test_default_runner_drains_large_stdout_and_stderr_with_fixed_memory_bound():
    maximum = 4096
    result = _run_bounded(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x'*200000); sys.stderr.write('y'*200000)",
        ],
        cwd=None,
        timeout=10,
        max_output_bytes=maximum,
        pass_fds=(),
    )

    assert result.returncode == 0
    assert len(result.stdout) <= maximum
    assert len(result.stderr) <= maximum
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True


def test_default_runner_contains_ambient_environment(tmp_path, monkeypatch):
    synthetic = {name: f"synthetic-{index}" for index, name in enumerate(SECRET_NAMES, start=1)}
    for name, value in synthetic.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "/tmp/evil/bin")
    probe = tmp_path / "probe.py"
    probe.write_text(
        CHILD_ENV_PROBE,
        encoding="utf-8",
    )

    result = _run_bounded(
        [sys.executable, str(probe), *SECRET_NAMES, *SAFE_NAMES],
        cwd=tmp_path,
        timeout=10,
        max_output_bytes=4096,
        pass_fds=(),
    )
    payload = json.loads(result.stdout.decode("utf-8"))

    assert result.returncode == 0
    assert all(payload[name] is None for name in SECRET_NAMES)
    assert payload["PATH"] == "/usr/bin:/bin"
    assert payload["HOME"] == "/"
    assert payload["LANG"] == "C.UTF-8"
    assert payload["LC_ALL"] == "C.UTF-8"
    assert payload["TMPDIR"] == "/tmp"
    assert os.environ["PATH"] == "/tmp/evil/bin"
    for name, value in synthetic.items():
        assert os.environ[name] == value


def test_h3_missing_job_execution_fingerprint_rejects_execution(client, console):
    """H3: Execution MUST FAIL CLOSED when job fingerprint is missing."""
    runner = FakeRunner()
    console.action_runtime.runner = runner
    response = client.post(
        "/api/console/v1/actions",
        json={
            "action_type": "inspect_git_status",
            "workspace_id": "test-workspace",
        },
        headers={"Authorization": "Bearer propose-token"},
    )
    job = console.job_tracker.get_job(response.json["job_id"])
    # Remove execution fingerprint to simulate missing authority
    job_mutated = replace(job, execution_fingerprint=None)
    console.job_tracker._save(job_mutated)

    with pytest.raises(ValueError, match="execution authority missing.*fingerprint required"):
        console.action_runtime.execute(job.job_id)

    assert runner.calls == []


def test_h3_malformed_execution_fingerprint_rejects_execution(client, console):
    """H3: Execution MUST FAIL CLOSED when fingerprint is malformed."""
    runner = FakeRunner()
    console.action_runtime.runner = runner

    test_cases = [
        ("too-short", "invalid fingerprint format"),
        ("not-hex-ZZZZ" + "0" * 52, "invalid fingerprint format"),
        ("0" * 63, "invalid fingerprint format"),  # Wrong length
        ("0" * 65, "invalid fingerprint format"),  # Wrong length
        (123, "invalid fingerprint format"),  # Wrong type
    ]

    for malformed_fp, expected_error in test_cases:
        response = client.post(
            "/api/console/v1/actions",
            json={
                "action_type": "inspect_git_status",
                "workspace_id": "test-workspace",
            },
            headers={"Authorization": "Bearer propose-token"},
        )
        job = console.job_tracker.get_job(response.json["job_id"])
        job_mutated = replace(job, execution_fingerprint=malformed_fp)
        console.job_tracker._save(job_mutated)

        with pytest.raises(ValueError, match=expected_error):
            console.action_runtime.execute(job.job_id)

    assert runner.calls == []


def test_h3_missing_offer_fingerprint_rejects_execution(client, console, monkeypatch):
    """H3: Execution MUST FAIL CLOSED when dispatch offer lacks fingerprint."""
    runner = FakeRunner()
    console.action_runtime.runner = runner
    response = client.post(
        "/api/console/v1/actions",
        json={
            "action_type": "inspect_git_status",
            "workspace_id": "test-workspace",
        },
        headers={"Authorization": "Bearer propose-token"},
    )
    job = console.job_tracker.get_job(response.json["job_id"])
    offer = console.task_dispatch_coordinator.inspect_offer(job.dispatch_offer_id)

    # Remove execution_fingerprint while preserving the immutable
    # key/value-pair representation required by DispatchOffer.
    stripped_metadata = tuple(
        (key, value)
        for key, value in offer.authorization_metadata
        if key != "execution_fingerprint"
    )

    # Patch inspect_offer to return authoritative-shaped evidence that is
    # missing only the security-critical execution fingerprint.
    original_inspect_offer = console.task_dispatch_coordinator.inspect_offer

    def patched_inspect_offer(offer_id):
        if offer_id == job.dispatch_offer_id:
            # Bypass DispatchOffer construction-time integrity validation only
            # for this boundary test. We want to prove the execution runtime
            # independently fails closed when authoritative-looking evidence
            # arrives without the required execution fingerprint.
            import copy

            stripped_offer = copy.copy(offer)
            object.__setattr__(
                stripped_offer,
                "authorization_metadata",
                stripped_metadata,
            )
            return stripped_offer
        return original_inspect_offer(offer_id)

    monkeypatch.setattr(
        console.task_dispatch_coordinator,
        "inspect_offer",
        patched_inspect_offer,
    )

    with pytest.raises(ValueError, match="execution authority missing.*offer fingerprint required"):
        console.action_runtime.execute(job.job_id)

    assert runner.calls == []
