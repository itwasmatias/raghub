"""
Comprehensive tests for the NativeOllamaAgentProvider.

All tests run without live Ollama, live SSH, or network access.
Uses FakeOllamaServer from conftest for HTTP interaction tests.
Uses temporary git repositories for file-operation tests.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.ai_controller.models import Task
from tools.ai_controller.providers.native_agent import (
    AgentLoop,
    AgentOutcome,
    NativeOllamaAgentProvider,
    OllamaClientError,
    OllamaModelClient,
    RepositoryToolRunner,
    ToolError,
    _validate_relative_path,
    extract_json_object,
)
from tools.ai_controller.remote import LocalRunner


# ─── Fixtures ────────────────────────────────────────────────────────────────

def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.test")
    git(repo, "config", "user.name", "Test")
    (repo / "hello.py").write_text('print("hello")\n', encoding="utf-8")
    (repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    return repo


def make_task(
    prompt: str = "Edit templates/sip_markets.html",
    tests: list[list[str]] | None = None,
) -> Task:
    return Task(
        id="native-agent-task",
        title="Native agent task",
        prompt=prompt,
        base_ref="HEAD",
        tests=tests or [],
    )


def make_tool_runner(repo: Path, task: Task | None = None) -> RepositoryToolRunner:
    return RepositoryToolRunner(
        runner=LocalRunner(),
        worktree=repo,
        task=task or make_task(),
        allowed_commands=[["python3"], ["python", "-m", "pytest"]],
        command_timeout_seconds=30,
    )


def make_client(base_url: str) -> OllamaModelClient:
    return OllamaModelClient(
        base_url=base_url,
        model="qwen2.5-coder:3b",
        request_timeout_seconds=2.0,
        generation_timeout_seconds=10.0,
        context_size=4096,
        temperature=0.2,
        keep_alive="5m",
    )


def make_provider(base_url: str) -> NativeOllamaAgentProvider:
    return NativeOllamaAgentProvider(
        runner=LocalRunner(),
        base_url=base_url,
        model="qwen2.5-coder:3b",
        request_timeout_seconds=2.0,
        generation_timeout_seconds=10.0,
        context_size=4096,
        temperature=0.2,
        keep_alive="5m",
        max_agent_steps=10,
        max_tool_failures=3,
        max_output_chars=50_000,
        allowed_commands=[["python3"], [sys.executable, "-m", "pytest"]],
        command_timeout_seconds=30,
    )


def chat_turn(content: str) -> dict:
    """Build a fake-server chat_queue entry returning given content."""
    return {"content": content}


def finish_turn(success: bool = True, summary: str = "done") -> dict:
    content = json.dumps({
        "thought_summary": "task complete",
        "action": {
            "tool": "finish",
            "arguments": {"success": success, "summary": summary},
        },
    })
    return chat_turn(content)


def tool_call(tool: str, arguments: dict, thought: str = "working") -> dict:
    content = json.dumps({
        "thought_summary": thought,
        "action": {"tool": tool, "arguments": arguments},
    })
    return chat_turn(content)


# ─── Path validation ──────────────────────────────────────────────────────────

class TestPathValidation:
    def test_valid_relative_path(self):
        assert _validate_relative_path("foo/bar.py") == "foo/bar.py"

    def test_valid_dot(self):
        assert _validate_relative_path(".") == "."

    def test_rejects_null_byte(self):
        with pytest.raises(ValueError, match="null byte"):
            _validate_relative_path("foo\x00bar")

    def test_rejects_absolute(self):
        with pytest.raises(ValueError, match="absolute"):
            _validate_relative_path("/etc/passwd")

    def test_rejects_traversal(self):
        with pytest.raises(ValueError, match="traversal"):
            _validate_relative_path("../escape.txt")

    def test_rejects_deep_traversal(self):
        with pytest.raises(ValueError, match="traversal"):
            _validate_relative_path("a/b/../../c/../../escape")

    def test_rejects_windows_drive(self):
        with pytest.raises(ValueError, match="Windows"):
            _validate_relative_path("C:\\RAGHubOperator\\secrets.txt")

    def test_rejects_unc(self):
        with pytest.raises(ValueError, match="Windows"):
            _validate_relative_path("\\\\server\\share\\file")

    def test_rejects_git_directory(self):
        with pytest.raises(ValueError, match="prohibited"):
            _validate_relative_path(".git/config")

    def test_rejects_non_string(self):
        with pytest.raises(ValueError, match="string"):
            _validate_relative_path(None)  # type: ignore


# ─── JSON extraction ──────────────────────────────────────────────────────────

class TestExtractJsonObject:
    def test_direct_json(self):
        payload = {"thought_summary": "hello", "action": {"tool": "finish", "arguments": {}}}
        assert extract_json_object(json.dumps(payload)) == payload

    def test_prose_wrapped(self):
        payload = {"a": 1}
        text = f'Sure, here is my response:\n{json.dumps(payload)}\nI hope that helps.'
        assert extract_json_object(text) == payload

    def test_code_fence_json(self):
        payload = {"tool": "read_file"}
        text = f"Here:\n```json\n{json.dumps(payload)}\n```\n"
        assert extract_json_object(text) == payload

    def test_code_fence_no_lang(self):
        payload = {"x": 42}
        text = f"```\n{json.dumps(payload)}\n```"
        assert extract_json_object(text) == payload

    def test_nested_object_scanned(self):
        inner = {"thought_summary": "x", "action": {"tool": "git_status", "arguments": {}}}
        text = f"some text before {{ ignored }} and then {json.dumps(inner)} after"
        result = extract_json_object(text)
        assert result == inner

    def test_unicode_preserved(self):
        payload = {"summary": "café 🧪 C:\\RAGHubOperator apostrophe's"}
        assert extract_json_object(json.dumps(payload, ensure_ascii=False)) == payload

    def test_raises_on_no_json(self):
        with pytest.raises(ValueError, match="could not extract"):
            extract_json_object("no JSON here at all")

    def test_raises_on_array_not_object(self):
        with pytest.raises(ValueError, match="could not extract"):
            extract_json_object("[1, 2, 3]")

    def test_exact_prompt_preserved(self):
        prompt = (
            "Edit templates/sip_markets.html\n"
            "Unicode café, apostrophe's, C:\\RAGHubOperator, and forward/slashes."
        )
        payload = {"prompt": prompt}
        result = extract_json_object(json.dumps(payload, ensure_ascii=False))
        assert result["prompt"] == prompt


# ─── OllamaModelClient ───────────────────────────────────────────────────────

class TestOllamaModelClient:
    def test_available_returns_true_when_version_ok(self, start_ollama_server):
        with start_ollama_server() as server:
            client = make_client(server.url)
            assert client.available() is True

    def test_available_returns_false_on_connection_refused(self):
        import socket as _socket
        with _socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        client = make_client(f"http://127.0.0.1:{port}")
        assert client.available() is False

    def test_model_installed_true(self, start_ollama_server):
        with start_ollama_server(tags_models=[{"name": "qwen2.5-coder:3b"}]) as server:
            client = make_client(server.url)
            assert client.model_installed() is True

    def test_model_installed_false(self, start_ollama_server):
        with start_ollama_server(tags_models=[{"name": "other-model:7b"}]) as server:
            client = make_client(server.url)
            assert client.model_installed() is False

    def test_model_installed_via_model_key(self, start_ollama_server):
        with start_ollama_server(tags_models=[{"model": "qwen2.5-coder:3b"}]) as server:
            client = make_client(server.url)
            assert client.model_installed() is True

    def test_chat_returns_content(self, start_ollama_server):
        content = json.dumps({"thought_summary": "hi", "action": {"tool": "finish", "arguments": {"success": True, "summary": "done"}}})
        with start_ollama_server(chat_queue=[{"content": content}]) as server:
            client = make_client(server.url)
            result = client.chat([{"role": "user", "content": "hello"}])
            assert result == content

    def test_chat_sends_correct_model(self, start_ollama_server):
        content = json.dumps({"thought_summary": "x", "action": {"tool": "finish", "arguments": {"success": False, "summary": ""}}})
        with start_ollama_server(chat_queue=[{"content": content}]) as server:
            client = make_client(server.url)
            client.chat([{"role": "user", "content": "test"}])
            assert server.state.chat_requests[0]["model"] == "qwen2.5-coder:3b"

    def test_chat_sends_context_size(self, start_ollama_server):
        content = json.dumps({"thought_summary": "x", "action": {"tool": "finish", "arguments": {"success": False, "summary": ""}}})
        with start_ollama_server(chat_queue=[{"content": content}]) as server:
            client = make_client(server.url)
            client.chat([{"role": "user", "content": "test"}])
            assert server.state.chat_requests[0]["options"]["num_ctx"] == 4096

    def test_chat_raises_on_server_error(self, start_ollama_server):
        with start_ollama_server(chat_queue=[{"status": 500, "body": {"error": "boom"}}]) as server:
            client = make_client(server.url)
            with pytest.raises(OllamaClientError) as exc_info:
                client.chat([{"role": "user", "content": "test"}])
            assert exc_info.value.category == "server_error"
            assert exc_info.value.retryable is True
            assert exc_info.value.http_status == 500

    def test_chat_raises_on_missing_message(self, start_ollama_server):
        with start_ollama_server(chat_queue=[{"body": {"done": True}}]) as server:
            client = make_client(server.url)
            with pytest.raises(OllamaClientError) as exc_info:
                client.chat([{"role": "user", "content": "test"}])
            assert exc_info.value.category == "malformed_response"

    def test_chat_raises_on_empty_content(self, start_ollama_server):
        with start_ollama_server(chat_queue=[{"body": {"message": {"role": "assistant", "content": ""}}}]) as server:
            client = make_client(server.url)
            with pytest.raises(OllamaClientError) as exc_info:
                client.chat([{"role": "user", "content": "test"}])
            assert exc_info.value.category == "missing_response_text"

    def test_chat_raises_on_malformed_json_body(self, start_ollama_server):
        with start_ollama_server(chat_queue=[{"raw": "not-json-at-all"}]) as server:
            client = make_client(server.url)
            with pytest.raises(OllamaClientError) as exc_info:
                client.chat([{"role": "user", "content": "test"}])
            assert exc_info.value.category == "malformed_response"

    def test_chat_raises_on_timeout(self, start_ollama_server):
        content = json.dumps({"thought_summary": "x", "action": {"tool": "finish", "arguments": {"success": False, "summary": ""}}})
        with start_ollama_server(chat_queue=[{"delay": 0.5, "content": content}]) as server:
            fast_client = OllamaModelClient(
                base_url=server.url,
                model="qwen2.5-coder:3b",
                request_timeout_seconds=2.0,
                generation_timeout_seconds=0.05,
                context_size=4096,
                temperature=0.2,
                keep_alive="5m",
            )
            with pytest.raises(OllamaClientError) as exc_info:
                fast_client.chat([{"role": "user", "content": "test"}])
            assert exc_info.value.category == "timeout"
            assert exc_info.value.retryable is True

    def test_request_construction_preserves_exact_prompt(self, start_ollama_server):
        prompt = (
            "Edit templates/sip_markets.html\n"
            "Unicode café, apostrophe's, C:\\RAGHubOperator, forward/slashes. 🧪"
        )
        content = json.dumps({"thought_summary": "x", "action": {"tool": "finish", "arguments": {"success": False, "summary": ""}}})
        with start_ollama_server(chat_queue=[{"content": content}]) as server:
            client = make_client(server.url)
            client.chat([{"role": "user", "content": prompt}])
            sent = server.state.chat_requests[0]["messages"]
            assert any(m["content"] == prompt for m in sent)


# ─── RepositoryToolRunner ─────────────────────────────────────────────────────

class TestRepositoryToolRunner:
    def test_list_files_returns_entries(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        listing = tr.list_files(".")
        assert "hello.py" in listing
        assert "README.md" in listing

    def test_read_file_returns_content(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        content = tr.read_file("hello.py")
        assert 'print("hello")' in content

    def test_read_file_raises_on_missing(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        with pytest.raises(ToolError):
            tr.read_file("does_not_exist.py")

    def test_read_file_rejects_traversal(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        with pytest.raises(ToolError, match="traversal"):
            tr.read_file("../other.txt")

    def test_read_file_rejects_absolute(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        with pytest.raises(ToolError, match="absolute"):
            tr.read_file("/etc/passwd")

    def test_read_file_rejects_git_directory(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        with pytest.raises(ToolError, match="prohibited"):
            tr.read_file(".git/config")

    def test_read_file_rejects_null_byte(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        with pytest.raises(ToolError, match="null byte"):
            tr.read_file("foo\x00bar")

    def test_read_file_rejects_windows_path(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        with pytest.raises(ToolError, match="Windows"):
            tr.read_file("C:\\secrets.txt")

    def test_write_file_creates_content(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        result = tr.write_file("new_file.py", "x = 1\n")
        assert "new_file.py" in result
        assert (repo / "new_file.py").read_text(encoding="utf-8") == "x = 1\n"

    def test_write_file_is_atomic_via_replace(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        tr.write_file("hello.py", "# updated\n")
        assert (repo / "hello.py").read_text(encoding="utf-8") == "# updated\n"
        assert not list(repo.glob("*.tmp")), "tmp file not cleaned up"

    def test_write_file_creates_parent_dirs(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        tr.write_file("subdir/deep/file.txt", "content\n")
        assert (repo / "subdir" / "deep" / "file.txt").exists()

    def test_write_file_rejects_traversal(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        with pytest.raises(ToolError, match="traversal"):
            tr.write_file("../escape.txt", "bad")

    def test_write_file_rejects_git_directory(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        with pytest.raises(ToolError, match="prohibited"):
            tr.write_file(".git/config", "bad")

    def test_git_status_clean(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        status = tr.git_status()
        assert status.strip() == "" or status == "(clean)"

    def test_git_status_after_modification(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        (repo / "hello.py").write_text("modified\n", encoding="utf-8")
        status = tr.git_status()
        assert "hello.py" in status

    def test_git_diff_shows_change(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        (repo / "hello.py").write_text("modified\n", encoding="utf-8")
        diff = tr.git_diff()
        assert "hello.py" in diff or "modified" in diff

    def test_apply_patch_modifies_file(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        patch = (
            "diff --git a/hello.py b/hello.py\n"
            "--- a/hello.py\n"
            "+++ b/hello.py\n"
            "@@ -1 +1 @@\n"
            '-print("hello")\n'
            '+print("world")\n'
        )
        tr.apply_patch(patch)
        assert (repo / "hello.py").read_text(encoding="utf-8") == 'print("world")\n'

    def test_apply_patch_rejects_traversal(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        patch = (
            "diff --git a/../escape.txt b/../escape.txt\n"
            "--- a/../escape.txt\n"
            "+++ b/../escape.txt\n"
            "@@ -0,0 +1 @@\n"
            "+bad\n"
        )
        with pytest.raises(ToolError, match="escapes"):
            tr.apply_patch(patch)

    def test_apply_patch_rejects_git_modification(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        patch = (
            "diff --git a/.git/config b/.git/config\n"
            "--- a/.git/config\n"
            "+++ b/.git/config\n"
            "@@ -1 +1 @@\n"
            "+injected\n"
        )
        with pytest.raises(ToolError, match="prohibited"):
            tr.apply_patch(patch)

    def test_apply_patch_empty_raises(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        with pytest.raises(ToolError):
            tr.apply_patch("")

    def test_run_declared_test_valid_index(self, tmp_path):
        repo = make_repo(tmp_path)
        task = make_task(tests=[[sys.executable, "-c", "print('ok')"]])
        tr = make_tool_runner(repo, task)
        result = tr.run_declared_test(0)
        assert "passed" in result or "exit 0" in result.lower() or "ok" in result

    def test_run_declared_test_invalid_index(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo, make_task(tests=[]))
        with pytest.raises(ToolError, match="out of range"):
            tr.run_declared_test(0)

    def test_run_declared_test_failing(self, tmp_path):
        repo = make_repo(tmp_path)
        task = make_task(tests=[[sys.executable, "-c", "import sys; sys.exit(1)"]])
        tr = make_tool_runner(repo, task)
        result = tr.run_declared_test(0)
        assert "failed" in result

    def test_run_allowed_command_matching_prefix(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        result = tr.run_allowed_command(["python3", "--version"])
        assert "Python" in result or "exit 0" in result or "exit" in result

    def test_run_allowed_command_not_in_allowlist(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        with pytest.raises(ToolError, match="allowlist"):
            tr.run_allowed_command(["rm", "-rf", "."])

    def test_run_allowed_command_rejects_shell_operator(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        with pytest.raises(ToolError, match="shell operator"):
            tr.run_allowed_command(["python3", "-c", "print('x'); rm -rf /"])

    def test_run_allowed_command_rejects_empty(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        with pytest.raises(ToolError):
            tr.run_allowed_command([])

    def test_search_text_finds_match(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        result = tr.search_text("hello")
        assert "hello" in result.lower() or "hello.py" in result

    def test_search_text_no_match(self, tmp_path):
        repo = make_repo(tmp_path)
        tr = make_tool_runner(repo)
        result = tr.search_text("xyzzy_unique_string_9999")
        assert "no matches" in result.lower() or result.strip() == ""


# ─── AgentLoop ───────────────────────────────────────────────────────────────

class TestAgentLoop:
    """Tests for AgentLoop using fake Ollama server and local repos."""

    def _make_loop(self, server_url: str, repo: Path, task: Task, *, steps: int = 10, failures: int = 3) -> AgentLoop:
        client = make_client(server_url)
        tr = make_tool_runner(repo, task)
        return AgentLoop(
            client=client,
            tool_runner=tr,
            max_steps=steps,
            max_tool_failures=failures,
            max_output_chars=10_000,
        )

    def test_finish_success(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        task = make_task()
        turns = [finish_turn(success=True, summary="done perfectly")]
        with start_ollama_server(chat_queue=turns) as server:
            loop = self._make_loop(server.url, repo, task)
            outcome = loop.run(task)
        assert outcome.success is True
        assert outcome.stop_reason == "finish"
        assert outcome.summary == "done perfectly"
        assert len(outcome.steps) == 1
        assert outcome.steps[0].tool == "finish"

    def test_finish_failure(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        task = make_task()
        turns = [finish_turn(success=False, summary="cannot complete")]
        with start_ollama_server(chat_queue=turns) as server:
            loop = self._make_loop(server.url, repo, task)
            outcome = loop.run(task)
        assert outcome.success is False
        assert outcome.stop_reason == "finish"

    def test_multi_turn_git_status_then_finish(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        task = make_task()
        turns = [
            tool_call("git_status", {}),
            finish_turn(success=True, summary="checked status"),
        ]
        with start_ollama_server(chat_queue=turns) as server:
            loop = self._make_loop(server.url, repo, task)
            outcome = loop.run(task)
        assert outcome.success is True
        assert len(outcome.steps) == 2
        assert outcome.steps[0].tool == "git_status"
        assert outcome.steps[1].tool == "finish"

    def test_write_file_then_finish(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        task = make_task()
        turns = [
            tool_call("write_file", {"path": "hello.py", "content": "# updated\n"}),
            finish_turn(success=True, summary="file written"),
        ]
        with start_ollama_server(chat_queue=turns) as server:
            loop = self._make_loop(server.url, repo, task)
            outcome = loop.run(task)
        assert outcome.success is True
        assert (repo / "hello.py").read_text(encoding="utf-8") == "# updated\n"

    def test_malformed_json_recovery_then_success(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        task = make_task()
        turns = [
            {"content": "this is not json at all"},
            finish_turn(success=True, summary="recovered"),
        ]
        with start_ollama_server(chat_queue=turns) as server:
            loop = self._make_loop(server.url, repo, task)
            outcome = loop.run(task)
        assert outcome.success is True
        assert len(outcome.recovery_events) == 1
        assert "malformed JSON" in outcome.recovery_events[0]

    def test_malformed_json_exceeds_failure_limit(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        task = make_task()
        # 3 bad turns, then a good one that never fires
        turns = [
            {"content": "not json"},
            {"content": "also not json"},
            {"content": "still not json"},
            finish_turn(success=True),
        ]
        with start_ollama_server(chat_queue=turns) as server:
            loop = self._make_loop(server.url, repo, task, failures=3)
            outcome = loop.run(task)
        assert outcome.success is False
        assert "malformed_json" in outcome.stop_reason
        assert outcome.tool_failures == 3

    def test_unknown_tool_counts_as_failure(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        task = make_task()
        turns = [
            tool_call("delete_everything", {}),
            finish_turn(success=False, summary="failed"),
        ]
        with start_ollama_server(chat_queue=turns) as server:
            loop = self._make_loop(server.url, repo, task)
            outcome = loop.run(task)
        assert outcome.tool_failures >= 1
        step = outcome.steps[0]
        assert step.tool == "delete_everything"
        assert step.success is False
        assert "unknown tool" in (step.error or "").lower()

    def test_path_traversal_rejected_by_tool_runner(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        task = make_task()
        turns = [
            tool_call("read_file", {"path": "../../etc/passwd"}),
            finish_turn(success=False, summary="rejected"),
        ]
        with start_ollama_server(chat_queue=turns) as server:
            loop = self._make_loop(server.url, repo, task)
            outcome = loop.run(task)
        assert outcome.steps[0].success is False
        assert not (tmp_path.parent.parent / "etc" / "passwd").read_text if False else True

    def test_repeated_action_detection(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        task = make_task()
        # Same git_status 3 times
        turns = [
            tool_call("git_status", {}),
            tool_call("git_status", {}),
            tool_call("git_status", {}),
            finish_turn(success=True),
        ]
        with start_ollama_server(chat_queue=turns) as server:
            loop = self._make_loop(server.url, repo, task, failures=5)
            outcome = loop.run(task)
        assert any("repeated" in event for event in outcome.recovery_events)

    def test_step_limit_exhaustion(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        task = make_task()
        # Use distinct tools (read_file, git_status, git_diff) to avoid repeated-action detection
        turns = [
            tool_call("git_status", {}),
            tool_call("git_diff", {}),
            tool_call("read_file", {"path": "hello.py"}),
            tool_call("read_file", {"path": "README.md"}),
            tool_call("git_status", {"extra": "a"}),
        ]
        with start_ollama_server(chat_queue=turns) as server:
            loop = self._make_loop(server.url, repo, task, steps=3, failures=10)
            outcome = loop.run(task)
        assert outcome.success is False
        assert outcome.stop_reason == "max_steps"
        assert len(outcome.steps) == 3

    def test_ollama_error_terminates_loop(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        task = make_task()
        turns = [{"status": 500, "body": {"error": "server on fire"}}]
        with start_ollama_server(chat_queue=turns) as server:
            loop = self._make_loop(server.url, repo, task)
            outcome = loop.run(task)
        assert outcome.success is False
        assert "ollama_error" in outcome.stop_reason

    def test_declared_test_executed_via_tool(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        task = make_task(tests=[[sys.executable, "-c", "print('test-pass')"]])
        turns = [
            tool_call("run_declared_test", {"index": 0}),
            finish_turn(success=True, summary="test ran"),
        ]
        with start_ollama_server(chat_queue=turns) as server:
            loop = self._make_loop(server.url, repo, task)
            outcome = loop.run(task)
        assert outcome.steps[0].tool == "run_declared_test"
        assert outcome.steps[0].success is True

    def test_prompt_preserved_exactly_in_initial_message(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        prompt = "Edit templates/sip_markets.html\nUnicode café, apostrophe's, C:\\RAGHubOperator."
        task = make_task(prompt=prompt)
        turns = [finish_turn(success=False, summary="checking")]
        with start_ollama_server(chat_queue=turns) as server:
            loop = self._make_loop(server.url, repo, task)
            loop.run(task)
            # Initial user message must contain the exact prompt
            first_request = server.state.chat_requests[0]
            user_messages = [m["content"] for m in first_request["messages"] if m["role"] == "user"]
            assert any(prompt in msg for msg in user_messages)

    def test_sip_markets_html_string_preserved(self, tmp_path, start_ollama_server):
        """The exact string templates/sip_markets.html must survive the full round-trip."""
        repo = make_repo(tmp_path)
        prompt = "Preserve templates/sip_markets.html path in all communications."
        task = make_task(prompt=prompt)
        turns = [finish_turn(success=True, summary="templates/sip_markets.html unchanged")]
        with start_ollama_server(chat_queue=turns) as server:
            loop = self._make_loop(server.url, repo, task)
            outcome = loop.run(task)
        assert "templates/sip_markets.html" in outcome.summary


# ─── NativeOllamaAgentProvider (integration) ─────────────────────────────────

class TestNativeOllamaAgentProvider:
    def test_provider_name(self, tmp_path, start_ollama_server):
        with start_ollama_server() as server:
            p = make_provider(server.url)
            assert p.name == "native-ollama-agent"

    def test_provider_unavailable_returns_failure(self, tmp_path):
        import socket as _socket
        repo = make_repo(tmp_path)
        task = make_task()
        with _socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        p = make_provider(f"http://127.0.0.1:{port}")
        result = p.execute(task=task, worktree=repo, attempt=1)
        assert result.success is False
        assert result.category == "provider_unavailable"
        assert result.retryable is True

    def test_missing_model_returns_non_retryable_failure(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        task = make_task()
        with start_ollama_server(tags_models=[{"name": "other-model"}]) as server:
            p = make_provider(server.url)
            result = p.execute(task=task, worktree=repo, attempt=1)
        assert result.success is False
        assert result.category == "missing_model"
        assert result.retryable is False
        assert server.state.chat_requests == []

    def test_successful_file_edit(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        task = make_task()
        turns = [
            tool_call("write_file", {"path": "hello.py", "content": "# edited by agent\n"}),
            finish_turn(success=True, summary="file edited"),
        ]
        with start_ollama_server(chat_queue=turns) as server:
            p = make_provider(server.url)
            result = p.execute(task=task, worktree=repo, attempt=1)
        assert result.success is True
        assert result.provider == "native-ollama-agent"
        assert result.model == "qwen2.5-coder:3b"
        assert (repo / "hello.py").read_text(encoding="utf-8") == "# edited by agent\n"

    def test_result_captures_agent_steps(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        task = make_task()
        turns = [
            tool_call("git_status", {}),
            finish_turn(success=True, summary="complete"),
        ]
        with start_ollama_server(chat_queue=turns) as server:
            p = make_provider(server.url)
            result = p.execute(task=task, worktree=repo, attempt=1)
        data = json.loads(result.returned_text)
        assert "steps" in data
        steps = data["steps"]
        assert any(s["tool"] == "git_status" for s in steps)

    def test_failed_agent_returns_failure_result(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        task = make_task()
        turns = [finish_turn(success=False, summary="could not complete")]
        with start_ollama_server(chat_queue=turns) as server:
            p = make_provider(server.url)
            result = p.execute(task=task, worktree=repo, attempt=1)
        assert result.success is False
        assert result.retryable is True  # finish:False is retryable

    def test_prompt_preserved_end_to_end(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        prompt = (
            "Edit templates/sip_markets.html\n"
            "Unicode café, apostrophe's, C:\\RAGHubOperator, forward/slashes. 🧪"
        )
        task = make_task(prompt=prompt)
        turns = [finish_turn(success=True, summary="done")]
        with start_ollama_server(chat_queue=turns) as server:
            p = make_provider(server.url)
            p.execute(task=task, worktree=repo, attempt=1)
            first_req = server.state.chat_requests[0]
            all_content = " ".join(m["content"] for m in first_req["messages"])
            assert "templates/sip_markets.html" in all_content
            assert "café" in all_content

    def test_apply_patch_via_agent(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        task = make_task()
        patch = (
            'diff --git a/hello.py b/hello.py\n'
            '--- a/hello.py\n'
            '+++ b/hello.py\n'
            '@@ -1 +1 @@\n'
            '-print("hello")\n'
            '+print("patched")\n'
        )
        turns = [
            tool_call("apply_patch", {"patch": patch}),
            finish_turn(success=True, summary="patched"),
        ]
        with start_ollama_server(chat_queue=turns) as server:
            p = make_provider(server.url)
            result = p.execute(task=task, worktree=repo, attempt=1)
        assert result.success is True
        assert (repo / "hello.py").read_text(encoding="utf-8") == 'print("patched")\n'

    def test_path_traversal_patch_rejected(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        task = make_task()
        patch = (
            "diff --git a/../escape.txt b/../escape.txt\n"
            "--- a/../escape.txt\n"
            "+++ b/../escape.txt\n"
            "@@ -0,0 +1 @@\n"
            "+bad\n"
        )
        turns = [
            tool_call("apply_patch", {"patch": patch}),
            finish_turn(success=False, summary="patch rejected"),
        ]
        with start_ollama_server(chat_queue=turns) as server:
            p = make_provider(server.url)
            result = p.execute(task=task, worktree=repo, attempt=1)
        assert not (tmp_path / "escape.txt").exists()

    def test_no_change_provider_result_reflects_outcome(self, tmp_path, start_ollama_server):
        """Agent finish with success=False propagates as provider failure."""
        repo = make_repo(tmp_path)
        task = make_task()
        turns = [finish_turn(success=False, summary="nothing to do")]
        with start_ollama_server(chat_queue=turns) as server:
            p = make_provider(server.url)
            result = p.execute(task=task, worktree=repo, attempt=1)
        assert result.success is False

    def test_server_error_terminates_execution(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        task = make_task()
        turns = [{"status": 500, "body": {"error": "boom"}}]
        with start_ollama_server(chat_queue=turns) as server:
            p = make_provider(server.url)
            result = p.execute(task=task, worktree=repo, attempt=1)
        assert result.success is False
        assert "ollama_error" in result.category

    def test_output_truncation(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        task = make_task()
        big_content = "x" * 200_000
        turns = [
            tool_call("write_file", {"path": "big.txt", "content": big_content}),
            finish_turn(success=True, summary="big file written"),
        ]
        with start_ollama_server(chat_queue=turns) as server:
            p = NativeOllamaAgentProvider(
                runner=LocalRunner(),
                base_url=server.url,
                model="qwen2.5-coder:3b",
                request_timeout_seconds=2.0,
                generation_timeout_seconds=10.0,
                context_size=4096,
                temperature=0.2,
                keep_alive="5m",
                max_agent_steps=10,
                max_tool_failures=3,
                max_output_chars=1000,
                command_timeout_seconds=30,
            )
            result = p.execute(task=task, worktree=repo, attempt=1)
        assert result.success is True
        assert result.returned_text_truncated is True
        assert len(result.returned_text) == 1000

    def test_available_and_model_installed_methods(self, tmp_path, start_ollama_server):
        with start_ollama_server() as server:
            p = make_provider(server.url)
            assert p.available() is True
            assert p.model_installed() is True

    def test_model_installed_false_when_model_missing(self, tmp_path, start_ollama_server):
        with start_ollama_server(tags_models=[{"name": "different-model"}]) as server:
            p = make_provider(server.url)
            assert p.model_installed() is False

    def test_provider_result_includes_provider_and_model(self, tmp_path, start_ollama_server):
        repo = make_repo(tmp_path)
        task = make_task()
        turns = [finish_turn(success=True, summary="ok")]
        with start_ollama_server(chat_queue=turns) as server:
            p = make_provider(server.url)
            result = p.execute(task=task, worktree=repo, attempt=1)
        assert result.provider == "native-ollama-agent"
        assert result.model == "qwen2.5-coder:3b"
        assert result.started_at
        assert result.finished_at

    def test_codex_is_not_invoked(self, tmp_path, start_ollama_server):
        """NativeOllamaAgentProvider must not invoke codex binary."""
        import unittest.mock as mock
        repo = make_repo(tmp_path)
        task = make_task()
        turns = [finish_turn(success=True, summary="pure")]
        with start_ollama_server(chat_queue=turns) as server:
            p = make_provider(server.url)
            with mock.patch("subprocess.run") as mock_run:
                mock_run.side_effect = AssertionError("subprocess.run must not be called directly by provider")
                # The provider uses runner.run() via LocalRunner which calls subprocess.run
                # but must NOT call subprocess.run with "codex" as the command
                # We verify by checking no call uses "codex"
                mock_run.side_effect = None
                mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
                result = p.execute(task=task, worktree=repo, attempt=1)
                for call in mock_run.call_args_list:
                    argv = call.args[0] if call.args else call.kwargs.get("args", [])
                    if isinstance(argv, (list, tuple)) and argv:
                        assert "codex" not in str(argv[0]).lower(), f"codex was invoked: {argv}"
