from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

from tools.ai_controller.models import Task
from tools.ai_controller.providers.ollama import OllamaProvider
from tools.ai_controller.remote import LocalRunner


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


def make_provider(base_url, *, max_retries=0, max_output_chars=2000, request_timeout=1.0, generation_timeout=1.0):
    return OllamaProvider(
        runner=LocalRunner(),
        base_url=base_url,
        model="qwen2.5-coder:3b",
        request_timeout_seconds=request_timeout,
        generation_timeout_seconds=generation_timeout,
        context_size=4096,
        temperature=0.2,
        max_retries=max_retries,
        max_output_chars=max_output_chars,
    )


def patch_text(before: str, after: str) -> str:
    return (
        "diff --git a/target.txt b/target.txt\n"
        "--- a/target.txt\n"
        "+++ b/target.txt\n"
        "@@ -1 +1 @@\n"
        f"-{before}\n"
        f"+{after}\n"
    )


def make_task(prompt: str = "Edit templates/sip_markets.html\nUnicode café, apostrophe's, C:\\RAGHubOperator, and forward/slashes.") -> Task:
    return Task(
        id="ollama-task",
        title="Ollama task",
        prompt=prompt,
        base_ref="HEAD",
        tests=[],
    )


def test_ollama_success_preserves_prompt_and_applies_patch(tmp_path, start_ollama_server):
    repo = make_repo(tmp_path)
    prompt = (
        "Edit templates/sip_markets.html\n"
        "Keep spaces, apostrophe's, backslashes C:\\RAGHubOperator, "
        "forward/slashes, and Unicode: café 🧪.\n"
        "Do not trim the first character."
    )
    model_response = {
        "summary": "changed target",
        "patch": patch_text("before", "after"),
        "changed_files": ["target.txt"],
        "tests": [],
    }
    with start_ollama_server(
        tags_models=[{"name": "qwen2.5-coder:3b"}],
        generate_queue=[{"body": {"response": json.dumps(model_response, ensure_ascii=False)}}],
    ) as server:
        provider = make_provider(server.url)

        assert provider.available() is True
        assert provider.validate_model() is True

        result = provider.execute(task=make_task(prompt), worktree=repo, attempt=1)

    assert result.success is True
    assert result.provider == "local-ollama"
    assert result.model == "qwen2.5-coder:3b"
    assert result.http_status == 200
    assert result.changed_files == ["target.txt"]
    assert result.returned_text_truncated is False
    assert (repo / "target.txt").read_text(encoding="utf-8") == "after\n"
    assert result.returned_text == json.dumps(model_response, ensure_ascii=False)
    generate_request = server.state.generate_requests[0]
    assert generate_request["prompt"] == prompt


def test_ollama_rejects_path_traversal_patches(tmp_path, start_ollama_server):
    repo = make_repo(tmp_path)
    model_response = {
        "summary": "bad patch",
        "patch": (
            "diff --git a/../escape.txt b/../escape.txt\n"
            "--- a/../escape.txt\n"
            "+++ b/../escape.txt\n"
            "@@ -0,0 +1 @@\n"
            "+bad\n"
        ),
        "changed_files": ["../escape.txt"],
        "tests": [],
    }
    with start_ollama_server(
        tags_models=[{"name": "qwen2.5-coder:3b"}],
        generate_queue=[{"body": {"response": json.dumps(model_response)}}],
    ) as server:
        provider = make_provider(server.url)
        result = provider.execute(task=make_task(), worktree=repo, attempt=1)

    assert result.success is False
    assert result.category == "invalid_patch"
    assert result.retryable is True
    assert not (repo.parent / "escape.txt").exists()


def test_ollama_request_timeout_is_classified_as_timeout(tmp_path, start_ollama_server):
    repo = make_repo(tmp_path)
    with start_ollama_server(
        version_delay=0.2,
        tags_models=[{"name": "qwen2.5-coder:3b"}],
    ) as server:
        provider = make_provider(server.url, request_timeout=0.05)
        result = provider.execute(task=make_task(), worktree=repo, attempt=1)

    assert result.success is False
    assert result.category == "timeout"
    assert result.timed_out is True


def test_ollama_connection_refusal_is_classified_as_unavailable(tmp_path):
    repo = make_repo(tmp_path)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    provider = make_provider(f"http://127.0.0.1:{port}", request_timeout=0.05)

    result = provider.execute(task=make_task(), worktree=repo, attempt=1)

    assert result.success is False
    assert result.category == "provider_unavailable"
    assert result.retryable is True


def test_ollama_missing_model_is_non_retryable(tmp_path, start_ollama_server):
    repo = make_repo(tmp_path)
    with start_ollama_server(tags_models=[{"name": "other-model"}]) as server:
        provider = make_provider(server.url)
        assert provider.validate_model() is False
        result = provider.execute(task=make_task(), worktree=repo, attempt=1)

    assert result.success is False
    assert result.category == "missing_model"
    assert result.retryable is False
    assert server.state.generate_requests == []


def test_ollama_malformed_json_is_classified(tmp_path, start_ollama_server):
    repo = make_repo(tmp_path)
    with start_ollama_server(
        tags_models=[{"name": "qwen2.5-coder:3b"}],
        generate_queue=[{"raw": "not-json"}],
    ) as server:
        provider = make_provider(server.url)
        result = provider.execute(task=make_task(), worktree=repo, attempt=1)

    assert result.success is False
    assert result.category == "malformed_response"
    assert result.malformed_response is True
    assert result.retryable is True


def test_ollama_missing_response_text_is_classified(tmp_path, start_ollama_server):
    repo = make_repo(tmp_path)
    with start_ollama_server(
        tags_models=[{"name": "qwen2.5-coder:3b"}],
        generate_queue=[{"body": {"response": ""}}],
    ) as server:
        provider = make_provider(server.url)
        result = provider.execute(task=make_task(), worktree=repo, attempt=1)

    assert result.success is False
    assert result.category == "missing_response_text"
    assert result.retryable is True


def test_ollama_server_error_is_classified(tmp_path, start_ollama_server):
    repo = make_repo(tmp_path)
    with start_ollama_server(
        tags_models=[{"name": "qwen2.5-coder:3b"}],
        generate_queue=[{"status": 500, "body": {"error": "boom"}}],
    ) as server:
        provider = make_provider(server.url)
        result = provider.execute(task=make_task(), worktree=repo, attempt=1)

    assert result.success is False
    assert result.category == "server_error"
    assert result.retryable is True
    assert result.http_status == 500


def test_ollama_large_output_is_truncated(tmp_path, start_ollama_server):
    repo = make_repo(tmp_path)
    model_response = {
        "summary": "big",
        "patch": patch_text("before", "after"),
        "changed_files": ["target.txt"],
        "tests": [],
        "notes": "x" * 10_000,
    }
    with start_ollama_server(
        tags_models=[{"name": "qwen2.5-coder:3b"}],
        generate_queue=[{"body": {"response": json.dumps(model_response, ensure_ascii=False)}}],
    ) as server:
        provider = make_provider(server.url, max_output_chars=128)
        result = provider.execute(task=make_task(), worktree=repo, attempt=1)

    assert result.success is True
    assert result.returned_text_truncated is True
    assert len(result.returned_text) == 128


def test_ollama_retries_are_bounded_and_eventually_succeed(tmp_path, start_ollama_server):
    repo = make_repo(tmp_path)
    model_response = {
        "summary": "retry success",
        "patch": patch_text("before", "after"),
        "changed_files": ["target.txt"],
        "tests": [],
    }
    with start_ollama_server(
        tags_models=[{"name": "qwen2.5-coder:3b"}],
        generate_queue=[
            {"raw": "not-json"},
            {"body": {"response": json.dumps(model_response, ensure_ascii=False)}},
        ],
    ) as server:
        provider = make_provider(server.url, max_retries=1)
        result = provider.execute(task=make_task(), worktree=repo, attempt=1)

    assert result.success is True
    assert len(server.state.generate_requests) == 2
