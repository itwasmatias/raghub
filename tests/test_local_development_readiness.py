"""Deterministic tests for MissionaryX local development readiness v0.1."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import runpy
import stat
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from tools.ai_controller.local_capability_baseline import LocalityType
from tools.ai_controller.local_model_runtime import (
    AdapterInferenceResponse,
    LocalInferenceStatus,
    LocalInferenceUsage,
)


ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "check-local-development-readiness"
EXPECTED_MODEL_SHA256 = (
    "74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db"
)


def run(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def git(repo: Path, *args: str) -> str:
    result = run(["git", *args], cwd=repo)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class FakeProfile:
    binary_path: str
    binary_sha256: str
    source_commit: str
    model_alias: str = "raghub-qwen2.5-0.5b-q4km"
    endpoint: str = "http://127.0.0.1:18080"
    context_size: int = 1024

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "binary_path": self.binary_path,
                    "binary_sha256": self.binary_sha256,
                    "source_commit": self.source_commit,
                    "model_alias": self.model_alias,
                    "endpoint": self.endpoint,
                    "context_size": self.context_size,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class StaticFixture:
    repository: Path
    wheelhouse: Path
    llama_source: Path
    llama_server: Path
    model: Path
    model_sha256: str
    python: Path
    profile: FakeProfile

    def arguments(self, *extra: str) -> list[str]:
        return [
            "--repository",
            str(self.repository),
            "--python",
            str(self.python),
            "--wheelhouse",
            str(self.wheelhouse),
            "--llama-cpp-source",
            str(self.llama_source),
            "--llama-server",
            str(self.llama_server),
            "--model",
            str(self.model),
            "--model-sha256",
            self.model_sha256,
            *extra,
        ]


def initialize_repository(path: Path, files: dict[str, str | bytes]) -> str:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "readiness-tests@example.invalid")
    git(path, "config", "user.name", "Readiness Tests")
    for relative, content in files.items():
        destination = path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            destination.write_bytes(content)
        else:
            destination.write_text(content, encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "fixture")
    return git(path, "rev-parse", "HEAD")


def write_fake_python(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        """#!/usr/bin/env python3
import json
from pathlib import Path
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("Python 3.14.6")
    raise SystemExit(0)
if len(args) >= 2 and args[0] == "-c":
    requested = json.loads(args[-1])
    print(json.dumps(requested, sort_keys=True))
    raise SystemExit(0)
if len(args) >= 2 and args[:2] == ["-m", "pip"]:
    if (Path(__file__).parent / ".pip-fail").exists():
        print("No matching distribution found", file=sys.stderr)
        raise SystemExit(1)
    if "--no-index" not in args or "--dry-run" not in args:
        print("unsafe pip invocation", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(0)
print("unexpected fake Python invocation", file=sys.stderr)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


@pytest.fixture
def static_environment(tmp_path: Path) -> StaticFixture:
    repository = tmp_path / "missionaryx"
    initialize_repository(
        repository,
        {
            "missionaryx-state.json": json.dumps(
                {"schema_version": "1.0.0", "project": "MissionaryX"}
            ),
            "requirements-dev.txt": "-r requirements-fedora-core.txt\npytest==9.1.1\n",
            "requirements-fedora-core.txt": "requests==2.34.2\n",
        },
    )
    python = repository / ".venv" / "bin" / "python"
    write_fake_python(python)

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = wheelhouse / "fixture_dependency-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"deterministic fake wheel")
    (wheelhouse / "SHA256SUMS").write_text(
        f"{sha256(wheel)}  {wheel.name}\n", encoding="utf-8"
    )

    llama_source = tmp_path / "llama.cpp"
    llama_server = llama_source / "build" / "bin" / "llama-server"
    source_commit = initialize_repository(
        llama_source,
        {"build/bin/llama-server": b"deterministic fake llama-server"},
    )
    llama_server.chmod(0o755)

    model = tmp_path / "qwen.gguf"
    model.write_bytes(b"deterministic fake GGUF")
    model_sha256 = sha256(model)
    profile = FakeProfile(
        binary_path=str(llama_server),
        binary_sha256=sha256(llama_server),
        source_commit=source_commit,
    )
    return StaticFixture(
        repository=repository,
        wheelhouse=wheelhouse,
        llama_source=llama_source,
        llama_server=llama_server,
        model=model,
        model_sha256=model_sha256,
        python=python,
        profile=profile,
    )


def load_contract() -> dict:
    return runpy.run_path(str(TOOL))


def invoke(
    environment: StaticFixture,
    *extra: str,
    profile: FakeProfile | None = None,
    adapter_factory=None,
) -> tuple[int, str]:
    contract = load_contract()
    output = io.StringIO()
    code = contract["main"](
        environment.arguments(*extra),
        profile=profile or environment.profile,
        adapter_factory=adapter_factory,
        stdout=output,
    )
    return code, output.getvalue()


def test_help_and_argument_contract():
    result = run([str(TOOL), "--help"], cwd=ROOT)
    assert result.returncode == 0, result.stderr
    for option in (
        "--live",
        "--repository",
        "--python",
        "--wheelhouse",
        "--llama-cpp-source",
        "--llama-server",
        "--model",
        "--model-sha256",
        "--endpoint",
    ):
        assert option in result.stdout

    invalid = run([str(TOOL), "--not-a-readiness-option"], cwd=ROOT)
    assert invalid.returncode == 2


def test_clean_valid_static_environment(static_environment: StaticFixture):
    code, output = invoke(static_environment)
    assert code == 0, output
    for dimension in (
        "git_checkout",
        "python_environment",
        "wheelhouse_integrity",
        "llama_cpp_source",
        "llama_server_binary",
        "model_integrity",
    ):
        assert f"PASS {dimension}" in output
    assert "SKIP live_adapter_inference" in output
    assert output.rstrip().endswith("LOCAL_DEVELOPMENT_STATIC_READY")


def test_missing_wheelhouse_fails_closed(static_environment: StaticFixture):
    missing = static_environment.wheelhouse.parent / "missing-wheelhouse"
    arguments = static_environment.arguments()
    arguments[arguments.index("--wheelhouse") + 1] = str(missing)
    output = io.StringIO()
    code = load_contract()["main"](
        arguments, profile=static_environment.profile, stdout=output
    )
    assert code != 0
    assert "FAIL wheelhouse_integrity" in output.getvalue()


def test_missing_checksum_manifest_fails_closed(static_environment: StaticFixture):
    (static_environment.wheelhouse / "SHA256SUMS").unlink()
    code, output = invoke(static_environment)
    assert code != 0
    assert "SHA256SUMS" in output


def test_corrupt_wheel_checksum_fails_closed(static_environment: StaticFixture):
    wheel = next(static_environment.wheelhouse.glob("*.whl"))
    wheel.write_bytes(wheel.read_bytes() + b"corrupt")
    code, output = invoke(static_environment)
    assert code != 0
    assert "checksum" in output.lower()


def test_no_index_resolution_failure_fails_closed(static_environment: StaticFixture):
    (static_environment.python.parent / ".pip-fail").touch()
    code, output = invoke(static_environment)
    assert code != 0
    assert "no-index" in output.lower()


def test_missing_llama_server_fails_closed(static_environment: StaticFixture):
    static_environment.llama_server.unlink()
    code, output = invoke(static_environment)
    assert code != 0
    assert "FAIL llama_server_binary" in output


def test_non_executable_llama_server_fails_closed(static_environment: StaticFixture):
    static_environment.llama_server.chmod(0o644)
    code, output = invoke(static_environment)
    assert code != 0
    assert "not executable" in output.lower()


def test_wrong_llama_cpp_source_checkpoint_fails_closed(
    static_environment: StaticFixture,
):
    profile = replace(static_environment.profile, source_commit="0" * 40)
    code, output = invoke(static_environment, profile=profile)
    assert code != 0
    assert "FAIL llama_cpp_source" in output
    assert "checkpoint" in output.lower()


def test_binary_identity_mismatch_fails_closed(static_environment: StaticFixture):
    profile = replace(static_environment.profile, binary_sha256="0" * 64)
    code, output = invoke(static_environment, profile=profile)
    assert code != 0
    assert "FAIL llama_server_binary" in output
    assert "sha-256" in output.lower()


def test_missing_model_fails_closed(static_environment: StaticFixture):
    static_environment.model.unlink()
    code, output = invoke(static_environment)
    assert code != 0
    assert "FAIL model_integrity" in output


def test_wrong_model_sha256_fails_closed(static_environment: StaticFixture):
    arguments = static_environment.arguments()
    arguments[arguments.index("--model-sha256") + 1] = "0" * 64
    output = io.StringIO()
    code = load_contract()["main"](
        arguments, profile=static_environment.profile, stdout=output
    )
    assert code != 0
    assert "FAIL model_integrity" in output.getvalue()


def test_malformed_expected_model_sha_fails_closed(static_environment: StaticFixture):
    arguments = static_environment.arguments()
    arguments[arguments.index("--model-sha256") + 1] = "not-a-sha"
    output = io.StringIO()
    code = load_contract()["main"](
        arguments, profile=static_environment.profile, stdout=output
    )
    assert code != 0
    assert "lowercase SHA-256" in output.getvalue()


class ReadinessHTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.get_count += 1
        if self.path != "/v1/models":
            self.send_error(404)
            return
        payload = self.server.models_payload
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.server.post_count += 1
        if self.path != "/completion":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        self.server.last_request = json.loads(self.rfile.read(length).decode("utf-8"))
        body = json.dumps(self.server.inference_payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@contextmanager
def fake_llama_server(
    profile: FakeProfile,
    *,
    models_payload=None,
    inference_payload=None,
) -> Iterator[HTTPServer]:
    server = HTTPServer(("127.0.0.1", 0), ReadinessHTTPHandler)
    server.models_payload = (
        {
            "object": "list",
            "data": [{"id": profile.model_alias, "object": "model"}],
        }
        if models_payload is None
        else models_payload
    )
    server.inference_payload = (
        {
            "content": "LOCAL_OK",
            "stopped_eos": True,
            "stopped_word": False,
            "stopped_limit": False,
            "timings": {"prompt_n": 5, "predicted_n": 1},
        }
        if inference_payload is None
        else inference_payload
    )
    server.get_count = 0
    server.post_count = 0
    server.last_request = None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def endpoint(server: HTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def test_live_mode_server_absent_fails_nonzero(static_environment: StaticFixture):
    code, output = invoke(
        static_environment, "--live", "--endpoint", "http://127.0.0.1:1"
    )
    assert code != 0
    assert "FAIL live_adapter_inference" in output
    assert "already-running" in output.lower()


def test_live_wrong_model_identity_fails_closed(static_environment: StaticFixture):
    with fake_llama_server(
        static_environment.profile,
        models_payload={"data": [{"id": "wrong-model"}]},
    ) as server:
        code, output = invoke(
            static_environment, "--live", "--endpoint", endpoint(server)
        )
        assert code != 0
        assert "FAIL live_adapter_inference" in output
        assert server.post_count == 0


@pytest.mark.parametrize(
    "metadata",
    [
        [],
        {},
        {"data": "not-a-list"},
        {"data": []},
        {"data": [{"id": "a"}, {"id": "b"}]},
        {"data": [{}]},
    ],
)
def test_live_malformed_or_ambiguous_metadata_fails_closed(
    static_environment: StaticFixture, metadata,
):
    with fake_llama_server(
        static_environment.profile, models_payload=metadata
    ) as server:
        code, output = invoke(
            static_environment, "--live", "--endpoint", endpoint(server)
        )
        assert code != 0
        assert "FAIL live_adapter_inference" in output
        assert server.post_count == 0


def test_live_successful_local_inference(static_environment: StaticFixture):
    with fake_llama_server(static_environment.profile) as server:
        code, output = invoke(
            static_environment, "--live", "--endpoint", endpoint(server)
        )
        assert code == 0, output
        assert "PASS live_adapter_inference adapter-level live readiness" in output
        assert output.rstrip().endswith("LOCAL_DEVELOPMENT_LIVE_READY")
        assert server.get_count == 1
        assert server.post_count == 1
        assert server.last_request["n_predict"] <= 8
        assert len(server.last_request["prompt"]) <= 64


def adapter_response(**changes) -> AdapterInferenceResponse:
    values = dict(
        status=LocalInferenceStatus.SUCCEEDED,
        model_id="raghub-qwen2.5-0.5b-q4km",
        runtime_id="llama.cpp-readiness",
        node_id="local-development-readiness",
        locality=LocalityType.LOCAL,
        remote_execution=False,
        output_text="LOCAL_OK",
        structured_output=None,
        started_at=datetime(2026, 8, 17, tzinfo=timezone.utc).isoformat(),
        completed_at=datetime(2026, 8, 17, tzinfo=timezone.utc).isoformat(),
        elapsed_seconds=0.0,
        usage=LocalInferenceUsage(prompt_tokens=2, generated_tokens=1),
        resource_measurements={},
        termination_reason="eos",
        error_code=None,
        error_message=None,
        cloud_escalation_count=0,
        cloud_cost_usd=0.0,
    )
    values.update(changes)
    return AdapterInferenceResponse(**values)


def returning_adapter(response: AdapterInferenceResponse):
    class FakeAdapter:
        def __init__(self, *, config):
            self.config = config

        def infer(self, model, inference_request, runtime_config):
            return response

    return FakeAdapter


def test_live_remote_execution_result_is_rejected(static_environment: StaticFixture):
    response = adapter_response(remote_execution=True)
    code, output = invoke(
        static_environment,
        "--live",
        adapter_factory=returning_adapter(response),
    )
    assert code != 0
    assert "remote_execution" in output


def test_live_nonzero_cloud_escalation_is_rejected(
    static_environment: StaticFixture,
):
    response = adapter_response(cloud_escalation_count=1)
    code, output = invoke(
        static_environment,
        "--live",
        adapter_factory=returning_adapter(response),
    )
    assert code != 0
    assert "cloud_escalation_count" in output


def test_live_nonzero_cloud_cost_is_rejected(static_environment: StaticFixture):
    response = adapter_response(cloud_cost_usd=0.01)
    code, output = invoke(
        static_environment,
        "--live",
        adapter_factory=returning_adapter(response),
    )
    assert code != 0
    assert "cloud_cost_usd" in output


def test_live_failed_adapter_result_is_rejected(static_environment: StaticFixture):
    response = adapter_response(
        status=LocalInferenceStatus.FAILED,
        output_text=None,
        termination_reason="http_request_failed",
        error_code="http_request_failed",
        error_message="server failure",
    )
    code, output = invoke(
        static_environment,
        "--live",
        adapter_factory=returning_adapter(response),
    )
    assert code != 0
    assert "status" in output


def test_static_mode_performs_no_http_or_live_inference(
    static_environment: StaticFixture,
):
    class ForbiddenAdapter:
        def __init__(self, *, config):
            raise AssertionError("static mode instantiated the live adapter")

    with fake_llama_server(static_environment.profile) as server:
        code, output = invoke(
            static_environment,
            "--endpoint",
            endpoint(server),
            adapter_factory=ForbiddenAdapter,
        )
        assert code == 0, output
        assert server.get_count == 0
        assert server.post_count == 0


def test_tool_has_no_server_process_start_or_stop_path(
    static_environment: StaticFixture,
):
    source = TOOL.read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Attribute):
            qualified = (
                f"{function.value.id}.{function.attr}"
                if isinstance(function.value, ast.Name)
                else function.attr
            )
            if qualified in {
                "subprocess.Popen",
                "os.kill",
                "os.killpg",
                "start",
                "stop",
                "terminate",
            }:
                forbidden_calls.append(qualified)
    assert forbidden_calls == []
    assert "LocalModelServerLifecycleCoordinator" not in source

    code, output = invoke(static_environment)
    assert code == 0, output


def test_non_loopback_live_endpoint_is_rejected_before_adapter_use(
    static_environment: StaticFixture,
):
    class ForbiddenAdapter:
        def __init__(self, *, config):
            raise AssertionError("non-loopback endpoint reached adapter")

    code, output = invoke(
        static_environment,
        "--live",
        "--endpoint",
        "http://192.0.2.10:18080",
        adapter_factory=ForbiddenAdapter,
    )
    assert code != 0
    assert "loopback" in output.lower()


def git_snapshot(repository: Path) -> tuple[str, ...]:
    return (
        git(repository, "rev-parse", "HEAD"),
        git(repository, "write-tree"),
        git(repository, "status", "--porcelain=v1", "--untracked-files=all"),
        git(repository, "diff", "--binary"),
        git(repository, "diff", "--cached", "--binary"),
    )


def file_snapshot(paths: list[Path]) -> dict[Path, tuple[str, int, int]]:
    return {
        path: (sha256(path), stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns)
        for path in paths
    }


def test_repository_and_local_assets_remain_unmodified(
    static_environment: StaticFixture,
):
    tracked = [
        path
        for path in static_environment.repository.rglob("*")
        if path.is_file() and ".git" not in path.parts
    ]
    assets = [
        static_environment.llama_server,
        static_environment.model,
        static_environment.wheelhouse / "SHA256SUMS",
        *static_environment.wheelhouse.glob("*.whl"),
    ]
    before_git = git_snapshot(static_environment.repository)
    before_files = file_snapshot(tracked + assets)

    code, output = invoke(static_environment)

    assert code == 0, output
    assert git_snapshot(static_environment.repository) == before_git
    assert file_snapshot(tracked + assets) == before_files


def test_documentation_records_precise_local_readiness_evidence():
    document = (ROOT / "LOCAL_DEVELOPMENT_READINESS.md").read_text(encoding="utf-8")
    assert EXPECTED_MODEL_SHA256 in document
    assert "876a4321163249c43ca4e986818fab5ab081f282" in document
    assert "CPython 3.14" in document
    assert "x86_64" in document
    assert "did not physically air-gap" in document
    assert "adapter-level" in document
    assert "full durable" in document
    assert "non-guaranteed decision support" in document
    assert "Hosted models remain optional accelerators" in document
    assert "model-generated answer is never sufficient" in document
