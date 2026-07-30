"""
Native Ollama agent provider.

Uses Ollama's /api/chat endpoint in a multi-step tool loop.
Does not invoke Codex CLI or any other coding-agent binary.

Architecture:
  OllamaModelClient   - HTTP communication with Ollama only
  RepositoryToolRunner - safe tool execution inside the task worktree
  AgentLoop           - orchestrates steps, enforces limits
  NativeOllamaAgentProvider - implements Provider, owns the full lifecycle
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from ..models import ProviderResult, Task, utc_now
from ..remote import LocalRunner
from .base import Provider


# ─── Path validation ─────────────────────────────────────────────────────────

_PROHIBITED_TOPS = frozenset({".git"})


def _validate_relative_path(raw: str) -> str:
    """
    Validate a model-supplied relative path.
    Returns the raw string if safe, raises ValueError otherwise.
    Validation uses static analysis only (no filesystem access required).
    """
    if not isinstance(raw, str):
        raise ValueError("path must be a string")
    if "\x00" in raw:
        raise ValueError(f"null byte in path: {raw!r}")
    # Reject Windows drive and UNC
    if re.match(r"^[A-Za-z]:", raw) or raw.startswith("\\\\"):
        raise ValueError(f"Windows path not allowed: {raw!r}")
    pure = PurePosixPath(raw)
    if pure.is_absolute():
        raise ValueError(f"absolute path not allowed: {raw!r}")
    if ".." in pure.parts:
        raise ValueError(f"path traversal not allowed: {raw!r}")
    if pure.parts and pure.parts[0] in _PROHIBITED_TOPS:
        raise ValueError(f"access to {pure.parts[0]} is prohibited")
    return raw


# ─── Ollama HTTP client ───────────────────────────────────────────────────────

class OllamaClientError(Exception):
    def __init__(
        self,
        category: str,
        message: str,
        *,
        retryable: bool,
        http_status: int | None = None,
        raw_text: str = "",
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.retryable = retryable
        self.http_status = http_status
        self.raw_text = raw_text


class OllamaModelClient:
    """
    Communicates with the Ollama HTTP API.
    Strictly handles model I/O; has no knowledge of repository structure.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        request_timeout_seconds: float,
        generation_timeout_seconds: float,
        context_size: int,
        temperature: float,
        keep_alive: str,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.model = model
        self.request_timeout_seconds = request_timeout_seconds
        self.generation_timeout_seconds = generation_timeout_seconds
        self.context_size = context_size
        self.temperature = temperature
        self.keep_alive = keep_alive

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    def _get_json(self, path: str) -> dict[str, Any]:
        req = Request(self._url(path), headers={"Accept": "application/json"}, method="GET")
        try:
            with urlopen(req, timeout=self.request_timeout_seconds) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
            raise OllamaClientError(
                "server_error" if e.code >= 500 else "provider_failed",
                f"HTTP {e.code}",
                retryable=e.code >= 500,
                http_status=e.code,
                raw_text=raw,
            ) from e
        except URLError as e:
            msg = str(e.reason)
            if isinstance(e.reason, (TimeoutError, socket.timeout)) or "timed out" in msg.lower():
                raise OllamaClientError("timeout", msg or "timed out", retryable=True) from e
            raise OllamaClientError("provider_unavailable", msg or "unavailable", retryable=True) from e
        except (TimeoutError, socket.timeout) as e:
            raise OllamaClientError("timeout", str(e) or "timed out", retryable=True) from e

    def _post_json(self, path: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(
            self._url(path),
            data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    return json.loads(raw) if raw else {}
                except json.JSONDecodeError as e:
                    raise OllamaClientError(
                        "malformed_response",
                        "Ollama returned non-JSON",
                        retryable=True,
                        raw_text=raw,
                    ) from e
        except HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
            raise OllamaClientError(
                "server_error" if e.code >= 500 else "provider_failed",
                f"HTTP {e.code}",
                retryable=e.code >= 500,
                http_status=e.code,
                raw_text=raw,
            ) from e
        except URLError as e:
            msg = str(e.reason)
            if isinstance(e.reason, (TimeoutError, socket.timeout)) or "timed out" in msg.lower():
                raise OllamaClientError("timeout", msg or "timed out", retryable=True) from e
            raise OllamaClientError("provider_unavailable", msg or "unavailable", retryable=True) from e
        except (TimeoutError, socket.timeout) as e:
            raise OllamaClientError("timeout", str(e) or "timed out", retryable=True) from e
        except OllamaClientError:
            raise

    def available(self) -> bool:
        try:
            self._get_json("/api/version")
            return True
        except OllamaClientError:
            return False

    def model_installed(self) -> bool:
        try:
            payload = self._get_json("/api/tags")
        except OllamaClientError:
            return False
        models = payload.get("models", [])
        if not isinstance(models, list):
            return False
        return any(
            isinstance(m, dict) and (m.get("name") == self.model or m.get("model") == self.model)
            for m in models
        )

    def chat(self, messages: list[dict[str, Any]]) -> str:
        """
        Send a multi-turn chat request.
        Returns assistant response content string.
        Raises OllamaClientError on any failure.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "num_ctx": self.context_size,
                "temperature": self.temperature,
            },
            "keep_alive": self.keep_alive,
        }
        response = self._post_json("/api/chat", payload, timeout=self.generation_timeout_seconds)
        msg = response.get("message")
        if not isinstance(msg, dict):
            raise OllamaClientError(
                "malformed_response",
                "Ollama /api/chat response missing 'message' object",
                retryable=True,
                raw_text=json.dumps(response, ensure_ascii=False)[:2000],
            )
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            raise OllamaClientError(
                "missing_response_text",
                "Ollama returned empty assistant content",
                retryable=True,
                raw_text=json.dumps(response, ensure_ascii=False)[:2000],
            )
        return content


# ─── JSON extraction with prose recovery ─────────────────────────────────────

def extract_json_object(text: str) -> dict[str, Any]:
    """
    Parse a JSON object from text.
    Recovers from model wrapping JSON in prose or code fences.
    Raises ValueError if no valid JSON object can be extracted.
    """
    stripped = text.strip()

    # Direct parse
    try:
        result = json.loads(stripped)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Code fence: ```json { ... } ```
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fence:
        try:
            result = json.loads(fence.group(1))
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, ValueError):
            pass

    # Scan all balanced brace candidates left-to-right, try each as JSON
    pos = 0
    while True:
        start = stripped.find("{", pos)
        if start == -1:
            break
        depth = 0
        for i, ch in enumerate(stripped[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        result = json.loads(stripped[start : i + 1])
                        if isinstance(result, dict):
                            return result
                    except (json.JSONDecodeError, ValueError):
                        pass
                    break
        pos = start + 1

    raise ValueError(f"could not extract JSON object from: {text[:300]!r}")


# ─── Repository tool runner ───────────────────────────────────────────────────

_MAX_READ_BYTES = 100_000
_MAX_SEARCH_BYTES = 20_000
_MAX_CMD_BYTES = 50_000

# Python scripts executed by the runner (local or remote via SSH)
_SCRIPT_LIST_FILES = (
    "import os, sys\n"
    "p = sys.argv[1]\n"
    "entries = sorted(os.listdir(p))\n"
    "for e in entries:\n"
    "    full = os.path.join(p, e)\n"
    "    print(e + ('/' if os.path.isdir(full) else ''))\n"
)

_SCRIPT_READ_FILE = (
    "import sys\n"
    "p = sys.argv[1]\n"
    "limit = int(sys.argv[2])\n"
    "with open(p, 'rb') as f:\n"
    "    data = f.read(limit + 1)\n"
    "trunc = len(data) > limit\n"
    "text = data[:limit].decode('utf-8', errors='replace')\n"
    "sys.stdout.write(text)\n"
    "if trunc:\n"
    "    sys.stdout.write('\\n[... file truncated at 100KB]')\n"
)

_SCRIPT_WRITE_FILE = (
    "import pathlib, sys\n"
    "p = pathlib.Path(sys.argv[1])\n"
    "content = sys.stdin.read()\n"
    "p.parent.mkdir(parents=True, exist_ok=True)\n"
    "tmp = p.with_suffix(p.suffix + '.tmp')\n"
    "tmp.write_text(content, encoding='utf-8')\n"
    "tmp.replace(p)\n"
    "print(f'wrote {len(content)} chars to {p}')\n"
)

_SCRIPT_SEARCH = (
    "import re, sys, os\n"
    "pattern = sys.argv[1]\n"
    "root = sys.argv[2]\n"
    "limit = int(sys.argv[3])\n"
    "total = 0\n"
    "for dirpath, dirnames, filenames in os.walk(root):\n"
    "    dirnames[:] = [d for d in sorted(dirnames) if not d.startswith('.')]\n"
    "    for fn in sorted(filenames):\n"
    "        fp = os.path.join(dirpath, fn)\n"
    "        try:\n"
    "            with open(fp, encoding='utf-8', errors='replace') as f:\n"
    "                for lineno, line in enumerate(f, 1):\n"
    "                    if re.search(pattern, line):\n"
    "                        rel = os.path.relpath(fp, root)\n"
    "                        out = f'{rel}:{lineno}:{line.rstrip()}\\n'\n"
    "                        sys.stdout.write(out)\n"
    "                        total += len(out)\n"
    "                        if total >= limit:\n"
    "                            sys.stdout.write('[... truncated]\\n')\n"
    "                            sys.exit(0)\n"
    "        except (PermissionError, IsADirectoryError):\n"
    "            pass\n"
)


class ToolError(Exception):
    """Raised when a repository tool call is invalid or fails."""


class RepositoryToolRunner:
    """
    Executes coding-agent tools safely inside a single task worktree.

    All paths are statically validated before dispatch.
    File operations are performed via runner.run() so this class works
    with both LocalRunner (tests) and SSHRunner (remote Fedora).
    """

    def __init__(
        self,
        *,
        runner: LocalRunner,
        worktree: Path,
        task: Task,
        allowed_commands: list[list[str]],
        command_timeout_seconds: float,
    ) -> None:
        self.runner = runner
        self.worktree = worktree
        self.task = task
        self.allowed_commands = allowed_commands
        self.command_timeout_seconds = command_timeout_seconds

    def _python(self) -> str:
        return getattr(self.runner, "remote_python", sys.executable)

    def _validate(self, raw: str) -> str:
        try:
            return _validate_relative_path(raw)
        except ValueError as e:
            raise ToolError(str(e))

    def _clip(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[:limit] + "\n[... truncated]"

    def list_files(self, path: str = ".") -> str:
        self._validate(path if path and path != "." else "README.md")  # validate non-trivial path
        result = self.runner.run(
            [self._python(), "-c", _SCRIPT_LIST_FILES, path or "."],
            cwd=self.worktree,
            timeout_seconds=30,
        )
        if result.exit_code != 0:
            raise ToolError(result.stderr.strip() or f"list_files failed for {path!r}")
        return result.stdout.strip() or "(empty directory)"

    def read_file(self, path: str) -> str:
        self._validate(path)
        result = self.runner.run(
            [self._python(), "-c", _SCRIPT_READ_FILE, path, str(_MAX_READ_BYTES)],
            cwd=self.worktree,
            timeout_seconds=30,
        )
        if result.exit_code != 0:
            raise ToolError(result.stderr.strip() or f"file not found or not readable: {path!r}")
        return result.stdout

    def search_text(self, pattern: str, path: str = ".") -> str:
        if path and path != ".":
            self._validate(path)
        result = self.runner.run(
            [
                self._python(),
                "-c",
                _SCRIPT_SEARCH,
                pattern,
                path or ".",
                str(_MAX_SEARCH_BYTES),
            ],
            cwd=self.worktree,
            timeout_seconds=30,
        )
        output = result.stdout or "(no matches)"
        return self._clip(output, _MAX_SEARCH_BYTES)

    def git_status(self) -> str:
        result = self.runner.run(
            ["git", "status", "--porcelain"],
            cwd=self.worktree,
            timeout_seconds=30,
        )
        return result.stdout or "(clean)"

    def git_diff(self) -> str:
        result = self.runner.run(
            ["git", "diff"],
            cwd=self.worktree,
            timeout_seconds=30,
        )
        return self._clip(result.stdout or "(no diff)", _MAX_SEARCH_BYTES)

    def write_file(self, path: str, content: str) -> str:
        self._validate(path)
        if not isinstance(content, str):
            raise ToolError("content must be a string")
        result = self.runner.run(
            [self._python(), "-c", _SCRIPT_WRITE_FILE, path],
            cwd=self.worktree,
            input_text=content,
            timeout_seconds=60,
        )
        if result.exit_code != 0:
            raise ToolError(result.stderr.strip() or f"could not write {path!r}")
        return result.stdout.strip() or f"wrote to {path}"

    def _validate_patch_paths(self, patch: str) -> None:
        for line in patch.splitlines():
            for prefix in ("--- ", "+++ "):
                if not line.startswith(prefix):
                    continue
                candidate = line[4:].strip()
                if candidate in ("/dev/null", ""):
                    continue
                if candidate.startswith(("a/", "b/")):
                    candidate = candidate[2:]
                pure = PurePosixPath(candidate)
                if pure.is_absolute() or ".." in pure.parts:
                    raise ToolError(f"patch path escapes worktree: {candidate!r}")
                if pure.parts and pure.parts[0] in _PROHIBITED_TOPS:
                    raise ToolError(f"patch modifies prohibited path: {candidate!r}")

    def apply_patch(self, patch: str) -> str:
        if not isinstance(patch, str) or not patch.strip():
            raise ToolError("patch must be a non-empty string")
        self._validate_patch_paths(patch)
        check = self.runner.run(
            ["git", "apply", "--check", "--whitespace=nowarn", "-"],
            cwd=self.worktree,
            input_text=patch,
            timeout_seconds=60,
        )
        if check.exit_code != 0:
            raise ToolError(f"patch rejected: {(check.stderr or check.stdout).strip()}")
        result = self.runner.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=self.worktree,
            input_text=patch,
            timeout_seconds=60,
        )
        if result.exit_code != 0:
            raise ToolError(f"patch failed: {(result.stderr or result.stdout).strip()}")
        return "patch applied successfully"

    def run_declared_test(self, index: int) -> str:
        if not isinstance(index, int) or not (0 <= index < len(self.task.tests)):
            available = len(self.task.tests)
            raise ToolError(
                f"test index {index!r} out of range; task has {available} test(s)"
            )
        argv = self.task.tests[index]
        result = self.runner.run(
            argv,
            cwd=self.worktree,
            timeout_seconds=self.command_timeout_seconds,
        )
        tail = (result.stdout + result.stderr)[-_MAX_CMD_BYTES:]
        if result.timed_out:
            return f"test {index} timed out\n{tail}"
        status = "passed" if result.exit_code == 0 else f"failed (exit {result.exit_code})"
        return f"test {index} {status}\n{tail}"

    def run_allowed_command(self, argv: list[str]) -> str:
        if not isinstance(argv, list) or not argv:
            raise ToolError("argv must be a non-empty list of strings")
        for token in argv:
            if not isinstance(token, str):
                raise ToolError("all argv elements must be strings")
            if any(op in token for op in ("|", ";", "&", "`", "$", ">", "<", "\n")):
                raise ToolError(f"shell operator in argument: {token!r}")
        matched = any(argv[: len(allowed)] == allowed for allowed in self.allowed_commands)
        if not matched:
            raise ToolError(
                f"command not in allowlist: {argv!r}. Allowed prefixes: {self.allowed_commands!r}"
            )
        result = self.runner.run(argv, cwd=self.worktree, timeout_seconds=self.command_timeout_seconds)
        tail = (result.stdout + result.stderr)[-_MAX_CMD_BYTES:]
        status = "timed out" if result.timed_out else f"exit {result.exit_code}"
        return f"[{status}]\n{tail}"


# ─── Agent step records ───────────────────────────────────────────────────────

@dataclass
class AgentStep:
    step: int
    thought_summary: str
    tool: str
    arguments: dict[str, Any]
    result: str
    success: bool
    error: str | None = None


@dataclass
class AgentOutcome:
    steps: list[AgentStep]
    success: bool
    summary: str
    stop_reason: str
    tool_failures: int
    recovery_events: list[str]


# ─── System prompt ────────────────────────────────────────────────────────────

_TOOL_DEFINITIONS = """\
Available tools — use exactly one per response:

  list_files          {"path": "<relative_dir>"}
  read_file           {"path": "<relative_file>"}
  search_text         {"pattern": "<regex>", "path": "<relative_dir_or_dot>"}
  git_status          {}
  git_diff            {}
  write_file          {"path": "<relative_file>", "content": "<full_file_content>"}
  apply_patch         {"patch": "<unified_diff>"}
  run_declared_test   {"index": <int>}
  run_allowed_command {"argv": ["<cmd>", "<arg1>", ...]}
  finish              {"summary": "<what_was_done>", "success": true|false}
"""

_SYSTEM_PROMPT = (
    "You are a coding agent operating inside a controller-managed Git worktree.\n"
    "Each response must be exactly one JSON object. No text before or after it.\n"
    "\n"
    "Response schema:\n"
    "{\n"
    '  "thought_summary": "<one sentence about current intent>",\n'
    '  "action": {\n'
    '    "tool": "<tool_name>",\n'
    '    "arguments": { ... }\n'
    "  }\n"
    "}\n"
    "\n"
    + _TOOL_DEFINITIONS
    + "\n"
    "Safety rules:\n"
    "- Respond ONLY with the JSON object.\n"
    "- Use repository-relative paths only (no leading /, no ..).\n"
    "- Never modify .git.\n"
    "- Do not read or write outside the worktree.\n"
    "- When complete or unable to proceed safely, call finish.\n"
    "- If a tool fails, adapt. Do not repeat the exact same failing action.\n"
)


# ─── Agent loop ───────────────────────────────────────────────────────────────

class AgentLoop:
    def __init__(
        self,
        *,
        client: OllamaModelClient,
        tool_runner: RepositoryToolRunner,
        max_steps: int,
        max_tool_failures: int,
        max_output_chars: int,
    ) -> None:
        self.client = client
        self.tool_runner = tool_runner
        self.max_steps = max_steps
        self.max_tool_failures = max_tool_failures
        self.max_output_chars = max_output_chars

    def _initial_user_message(self, task: Task) -> str:
        lines = [f"Task: {task.prompt}", f"\nWorktree: {self.tool_runner.worktree}"]
        if task.tests:
            lines.append("\nDeclared tests (use run_declared_test with index):")
            for i, argv in enumerate(task.tests):
                lines.append(f"  {i}: {' '.join(argv)}")
        lines.append("\nStart by inspecting the repository.")
        return "\n".join(lines)

    def _dispatch(self, tool: str, arguments: dict[str, Any]) -> str:
        tr = self.tool_runner
        if tool == "list_files":
            return tr.list_files(str(arguments.get("path", ".")))
        if tool == "read_file":
            path = arguments.get("path")
            if not isinstance(path, str) or not path:
                raise ToolError("read_file requires a non-empty 'path'")
            return tr.read_file(path)
        if tool == "search_text":
            pattern = arguments.get("pattern")
            if not isinstance(pattern, str) or not pattern:
                raise ToolError("search_text requires a non-empty 'pattern'")
            return tr.search_text(pattern, str(arguments.get("path", ".")))
        if tool == "git_status":
            return tr.git_status()
        if tool == "git_diff":
            return tr.git_diff()
        if tool == "write_file":
            path = arguments.get("path")
            content = arguments.get("content")
            if not isinstance(path, str) or not path:
                raise ToolError("write_file requires a non-empty 'path'")
            if not isinstance(content, str):
                raise ToolError("write_file requires a string 'content'")
            return tr.write_file(path, content)
        if tool == "apply_patch":
            patch = arguments.get("patch")
            if not isinstance(patch, str):
                raise ToolError("apply_patch requires a string 'patch'")
            return tr.apply_patch(patch)
        if tool == "run_declared_test":
            index = arguments.get("index")
            if not isinstance(index, int):
                raise ToolError("run_declared_test requires an integer 'index'")
            return tr.run_declared_test(index)
        if tool == "run_allowed_command":
            argv = arguments.get("argv")
            if not isinstance(argv, list):
                raise ToolError("run_allowed_command requires an 'argv' list")
            return tr.run_allowed_command(argv)
        if tool == "finish":
            return ""  # handled by caller
        raise ToolError(f"unknown tool: {tool!r}")

    def run(self, task: Task) -> AgentOutcome:
        steps: list[AgentStep] = []
        recovery_events: list[str] = []
        tool_failures = 0
        seen_actions: list[tuple[str, str]] = []

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": self._initial_user_message(task)},
        ]

        for step_num in range(1, self.max_steps + 1):
            # Get model response
            try:
                raw_content = self.client.chat(messages)
            except OllamaClientError as e:
                return AgentOutcome(
                    steps=steps,
                    success=False,
                    summary=f"Ollama error at step {step_num}: {e.message}",
                    stop_reason=f"ollama_error:{e.category}",
                    tool_failures=tool_failures,
                    recovery_events=recovery_events,
                )

            # Parse JSON with prose recovery
            try:
                parsed = extract_json_object(raw_content)
            except ValueError as exc:
                tool_failures += 1
                note = f"step {step_num}: malformed JSON ({exc})"
                recovery_events.append(note)
                messages.append({"role": "assistant", "content": raw_content})
                messages.append({
                    "role": "user",
                    "content": json.dumps({
                        "error": "response was not valid JSON",
                        "instruction": "Respond with only a JSON object matching the schema.",
                        "raw_preview": raw_content[:300],
                    }),
                })
                if tool_failures >= self.max_tool_failures:
                    return AgentOutcome(
                        steps=steps,
                        success=False,
                        summary=f"Too many malformed responses ({tool_failures})",
                        stop_reason="max_tool_failures:malformed_json",
                        tool_failures=tool_failures,
                        recovery_events=recovery_events,
                    )
                continue

            thought = str(parsed.get("thought_summary", ""))
            action = parsed.get("action")
            if not isinstance(action, dict):
                tool_failures += 1
                recovery_events.append(f"step {step_num}: missing action field")
                messages.append({"role": "assistant", "content": raw_content})
                messages.append({
                    "role": "user",
                    "content": json.dumps({"error": "response missing 'action' field"}),
                })
                if tool_failures >= self.max_tool_failures:
                    return AgentOutcome(
                        steps=steps,
                        success=False,
                        summary=f"Too many invalid responses ({tool_failures})",
                        stop_reason="max_tool_failures:missing_action",
                        tool_failures=tool_failures,
                        recovery_events=recovery_events,
                    )
                continue

            tool = str(action.get("tool", ""))
            arguments: dict[str, Any] = action.get("arguments") or {}
            if not isinstance(arguments, dict):
                arguments = {}

            # Repeated action detection
            action_sig = (tool, json.dumps(arguments, sort_keys=True))
            repeat_count = seen_actions.count(action_sig)
            if repeat_count >= 2:
                tool_failures += 1
                recovery_events.append(f"step {step_num}: repeated action '{tool}' ({repeat_count+1}x)")
                messages.append({"role": "assistant", "content": raw_content})
                messages.append({
                    "role": "user",
                    "content": json.dumps({
                        "error": f"repeated identical action '{tool}'. Try something different or call finish.",
                    }),
                })
                if tool_failures >= self.max_tool_failures:
                    return AgentOutcome(
                        steps=steps,
                        success=False,
                        summary=f"Stuck in repeated action '{tool}'",
                        stop_reason="max_tool_failures:repeated_action",
                        tool_failures=tool_failures,
                        recovery_events=recovery_events,
                    )
                continue
            seen_actions.append(action_sig)

            # Handle finish
            if tool == "finish":
                success = bool(arguments.get("success", False))
                summary = str(arguments.get("summary", ""))
                steps.append(AgentStep(
                    step=step_num,
                    thought_summary=thought,
                    tool="finish",
                    arguments=arguments,
                    result="agent loop complete",
                    success=True,
                ))
                return AgentOutcome(
                    steps=steps,
                    success=success,
                    summary=summary,
                    stop_reason="finish",
                    tool_failures=tool_failures,
                    recovery_events=recovery_events,
                )

            # Execute tool
            try:
                result_text = self._dispatch(tool, arguments)
                result_preview = result_text[: self.max_output_chars]
                steps.append(AgentStep(
                    step=step_num,
                    thought_summary=thought,
                    tool=tool,
                    arguments=arguments,
                    result=result_preview,
                    success=True,
                ))
                messages.append({"role": "assistant", "content": raw_content})
                messages.append({
                    "role": "user",
                    "content": json.dumps({
                        "tool": tool,
                        "success": True,
                        "result": result_preview,
                    }, ensure_ascii=False),
                })
            except ToolError as e:
                tool_failures += 1
                err = str(e)
                steps.append(AgentStep(
                    step=step_num,
                    thought_summary=thought,
                    tool=tool,
                    arguments=arguments,
                    result="",
                    success=False,
                    error=err,
                ))
                recovery_events.append(f"step {step_num}: {tool!r} failed: {err}")
                messages.append({"role": "assistant", "content": raw_content})
                messages.append({
                    "role": "user",
                    "content": json.dumps({"tool": tool, "success": False, "error": err}),
                })
                if tool_failures >= self.max_tool_failures:
                    return AgentOutcome(
                        steps=steps,
                        success=False,
                        summary=f"Too many tool failures ({tool_failures}). Last: {err}",
                        stop_reason="max_tool_failures",
                        tool_failures=tool_failures,
                        recovery_events=recovery_events,
                    )

        return AgentOutcome(
            steps=steps,
            success=False,
            summary=f"Step limit reached ({self.max_steps})",
            stop_reason="max_steps",
            tool_failures=tool_failures,
            recovery_events=recovery_events,
        )


# ─── Native Ollama Agent Provider ─────────────────────────────────────────────

class NativeOllamaAgentProvider(Provider):
    """
    Multi-step coding agent using Ollama's /api/chat endpoint.

    Does not invoke Codex CLI or any other coding-agent binary.
    Model access (OllamaModelClient) and repository access (RepositoryToolRunner)
    are separate, allowing them to use different runners in production.
    """

    name = "native-ollama-agent"

    def __init__(
        self,
        *,
        runner: LocalRunner,
        base_url: str,
        model: str,
        request_timeout_seconds: float,
        generation_timeout_seconds: float,
        context_size: int,
        temperature: float,
        keep_alive: str,
        max_agent_steps: int,
        max_tool_failures: int,
        max_output_chars: int,
        allowed_commands: list[list[str]] | None = None,
        command_timeout_seconds: float,
    ) -> None:
        self._runner = runner
        self._max_output_chars = max_output_chars
        self._max_agent_steps = max_agent_steps
        self._max_tool_failures = max_tool_failures
        self._allowed_commands = allowed_commands or []
        self._command_timeout_seconds = command_timeout_seconds
        self._client = OllamaModelClient(
            base_url=base_url,
            model=model,
            request_timeout_seconds=request_timeout_seconds,
            generation_timeout_seconds=generation_timeout_seconds,
            context_size=context_size,
            temperature=temperature,
            keep_alive=keep_alive,
        )

    @property
    def model(self) -> str:
        return self._client.model

    @property
    def base_url(self) -> str:
        return self._client.base_url

    def available(self) -> bool:
        return self._client.available()

    def model_installed(self) -> bool:
        return self._client.model_installed()

    def _build_result(
        self,
        outcome: AgentOutcome | None,
        task: Task,
        worktree: Path,
        attempt: int,
        started: str,
        *,
        category: str | None = None,
        retryable: bool = True,
        stderr: str = "",
        http_status: int | None = None,
        raw_text: str = "",
    ) -> ProviderResult:
        """Build a ProviderResult from agent outcome or early failure."""
        if outcome is not None:
            steps_data = [
                {
                    "step": s.step,
                    "thought": s.thought_summary,
                    "tool": s.tool,
                    "arguments": s.arguments,
                    "success": s.success,
                    "error": s.error,
                    "result_preview": s.result[:500] if s.result else "",
                }
                for s in outcome.steps
            ]
            returned_text = json.dumps(
                {"steps": steps_data, "stop_reason": outcome.stop_reason},
                ensure_ascii=False,
            )
            truncated = len(returned_text) > self._max_output_chars
            clipped = returned_text[: self._max_output_chars]
            finished = utc_now()

            if outcome.success:
                return ProviderResult.success_result(
                    provider=self.name,
                    model=self.model,
                    attempt=attempt,
                    worktree_path=os.fspath(worktree),
                    branch=f"controller/{task.id}",
                    http_status=200,
                    stdout="",
                    stderr="",
                    returned_text=clipped,
                    returned_text_truncated=truncated,
                    malformed_response=False,
                    timed_out=False,
                    changed_files=[],
                    tests=[],
                    started_at=started,
                    finished_at=finished,
                )
            return ProviderResult.failure_result(
                provider=self.name,
                model=self.model,
                attempt=attempt,
                worktree_path=os.fspath(worktree),
                branch=f"controller/{task.id}",
                category=outcome.stop_reason,
                retryable=outcome.stop_reason not in {"missing_model"},
                http_status=None,
                stdout="",
                stderr=outcome.summary,
                returned_text=clipped,
                returned_text_truncated=truncated,
                malformed_response="malformed_json" in outcome.stop_reason,
                timed_out=False,
                changed_files=[],
                tests=[],
                started_at=started,
                finished_at=finished,
            )

        # Early failure (Ollama unavailable etc.)
        clipped, truncated = (
            (raw_text[: self._max_output_chars], len(raw_text) > self._max_output_chars)
            if raw_text
            else ("", False)
        )
        return ProviderResult.failure_result(
            provider=self.name,
            model=self.model,
            attempt=attempt,
            worktree_path=os.fspath(worktree),
            branch=f"controller/{task.id}",
            category=category or "provider_failed",
            retryable=retryable,
            http_status=http_status,
            stdout="",
            stderr=stderr,
            returned_text=clipped,
            returned_text_truncated=truncated,
            malformed_response=False,
            timed_out=category == "timeout",
            changed_files=[],
            tests=[],
            started_at=started,
            finished_at=utc_now(),
        )

    def execute(self, *, task: Task, worktree: Path, attempt: int) -> ProviderResult:
        started = utc_now()

        # Availability checks (controller-side, not model-side)
        if not self._client.available():
            return self._build_result(
                None, task, worktree, attempt, started,
                category="provider_unavailable",
                retryable=True,
                stderr=f"Ollama not reachable at {self.base_url}",
            )

        if not self._client.model_installed():
            return self._build_result(
                None, task, worktree, attempt, started,
                category="missing_model",
                retryable=False,
                stderr=f"Model {self.model!r} not installed at {self.base_url}",
            )

        # Build tool runner and agent loop
        tool_runner = RepositoryToolRunner(
            runner=self._runner,
            worktree=worktree,
            task=task,
            allowed_commands=self._allowed_commands,
            command_timeout_seconds=self._command_timeout_seconds,
        )
        loop = AgentLoop(
            client=self._client,
            tool_runner=tool_runner,
            max_steps=self._max_agent_steps,
            max_tool_failures=self._max_tool_failures,
            max_output_chars=self._max_output_chars,
        )

        try:
            outcome = loop.run(task)
        except OllamaClientError as e:
            return self._build_result(
                None, task, worktree, attempt, started,
                category=e.category,
                retryable=e.retryable,
                http_status=e.http_status,
                stderr=e.message,
                raw_text=e.raw_text,
            )

        return self._build_result(outcome, task, worktree, attempt, started)
