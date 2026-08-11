from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from federation.subprocess_environment import (
    build_subprocess_environment,
    subprocess_environment_assignments,
    subprocess_environment_fingerprint,
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

CHILD_PROBE = (
    "import json, os, sys\n"
    "names = sys.argv[1:]\n"
    "print(json.dumps({name: os.environ.get(name) for name in names}, sort_keys=True))\n"
)


def test_safe_defaults_expose_only_justified_variables(monkeypatch):
    synthetic = {name: f"synthetic-{index}" for index, name in enumerate(SECRET_NAMES, start=1)}
    for name, value in synthetic.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "/tmp/evil/bin")
    env = build_subprocess_environment()

    result = subprocess.run(
        [sys.executable, "-c", CHILD_PROBE, *SECRET_NAMES, *SAFE_NAMES],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)

    assert all(payload[name] is None for name in SECRET_NAMES)
    assert payload["PATH"] == "/usr/bin:/bin"
    assert payload["HOME"] == "/"
    assert payload["LANG"] == "C.UTF-8"
    assert payload["LC_ALL"] == "C.UTF-8"
    assert payload["TMPDIR"] == "/tmp"
    assert os.environ["PATH"] == "/tmp/evil/bin"
    for name, value in synthetic.items():
        assert os.environ[name] == value


def test_trusted_additions_are_explicit_and_deterministic():
    env = build_subprocess_environment(trusted_additions={"TERM": "dumb", "TZ": "UTC"})
    assert env["TERM"] == "dumb"
    assert env["TZ"] == "UTC"
    assert env["PATH"] == "/usr/bin:/bin"
    assert subprocess_environment_fingerprint(env) == subprocess_environment_fingerprint(env)
    assert len(subprocess_environment_fingerprint(env)) == 64
    assert subprocess_environment_assignments(env) == tuple(
        sorted(subprocess_environment_assignments(env), key=lambda item: item.split("=", 1)[0].casefold())
    )


@pytest.mark.parametrize(
    "name",
    [
        "OPENAI_API_KEY",
        "openai_api_key",
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
        "LD_PRELOAD",
        "PYTHONPATH",
        "PYTHONHOME",
        "SSH_AUTH_SOCK",
    ],
)
def test_dangerous_and_secret_names_are_rejected_without_value_leakage(name):
    with pytest.raises(ValueError) as excinfo:
        build_subprocess_environment(trusted_additions={name: "synthetic-secret"})

    assert "synthetic-secret" not in str(excinfo.value)
    assert name.split("_", 1)[0].casefold() in str(excinfo.value).casefold()


def test_windows_sensitive_case_handling_rejects_colliding_names():
    with pytest.raises(ValueError, match="Path"):
        build_subprocess_environment(platform_name="nt", trusted_additions={"Path": r"C:\evil"})
    with pytest.raises(ValueError, match="openai_api_key"):
        build_subprocess_environment(platform_name="nt", trusted_additions={"openai_api_key": "synthetic"})


def test_missing_required_safe_path_fails_explicitly():
    with pytest.raises(ValueError, match="path"):
        build_subprocess_environment(path="")


def test_concurrent_calls_do_not_mutate_shared_policy_state():
    baseline = build_subprocess_environment(trusted_additions={"TERM": "dumb"})
    expected_fingerprint = subprocess_environment_fingerprint(baseline)

    def build_and_fingerprint() -> tuple[dict[str, str], str]:
        environment = build_subprocess_environment(trusted_additions={"TERM": "dumb"})
        return environment, subprocess_environment_fingerprint(environment)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: build_and_fingerprint(), range(32)))

    for environment, fingerprint in results:
        assert environment == baseline
        assert fingerprint == expected_fingerprint
