from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import time
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from ..models import ProviderResult, Task, utc_now
from ..remote import LocalRunner
from .base import Provider


_PATH_TOKEN = re.compile(r"(?:[\w.-]+/)+[\w.-]+(?:\.[\w.-]+)?")


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


@dataclass
class OllamaFailure(Exception):
    category: str
    message: str
    retryable: bool
    http_status: int | None = None
    raw_text: str = ""
    malformed_response: bool = False

    def __str__(self) -> str:  # pragma: no cover - trivial
        details = self.message
        if self.http_status is not None:
            details = f"{details} (http_status={self.http_status})"
        return details


class OllamaProvider(Provider):
    name = "local-ollama"

    _SCHEMA: dict[str, Any] = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "patch": {"type": "string"},
            "changed_files": {
                "type": "array",
                "items": {"type": "string"},
            },
            "tests": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "argv": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["argv"],
                    "additionalProperties": True,
                },
            },
            "notes": {"type": "string"},
        },
        "required": ["summary", "patch", "changed_files"],
        "additionalProperties": False,
    }

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
        max_retries: int,
        max_output_chars: int,
    ) -> None:
        self.runner = runner
        self.base_url = base_url.rstrip("/") + "/"
        self.model = model
        self.request_timeout_seconds = request_timeout_seconds
        self.generation_timeout_seconds = generation_timeout_seconds
        self.context_size = context_size
        self.temperature = temperature
        self.max_retries = max_retries
        self.max_output_chars = max_output_chars

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    def _python(self) -> str:
        return getattr(self.runner, "remote_python", sys.executable)

    def _request_raw(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> tuple[int, str]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self._url(path), data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout or self.request_timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return response.getcode() or 200, raw
        except HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace") if error.fp else ""
            raise OllamaFailure(
                category="server_error" if error.code >= 500 else "provider_failed",
                message=f"HTTP {error.code}",
                retryable=error.code >= 500,
                http_status=error.code,
                raw_text=raw,
            ) from error
        except URLError as error:
            reason = error.reason
            message = str(reason)
            lowered = message.lower()
            if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in lowered:
                raise OllamaFailure(
                    category="timeout",
                    message=message or "request timed out",
                    retryable=True,
                    raw_text="",
                ) from error
            raise OllamaFailure(
                category="provider_unavailable",
                message=message or "ollama endpoint unavailable",
                retryable=True,
                raw_text="",
            ) from error
        except (TimeoutError, socket.timeout) as error:
            raise OllamaFailure(
                category="timeout",
                message=str(error) or "request timed out",
                retryable=True,
                raw_text="",
            ) from error

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> tuple[int, dict[str, Any], str]:
        status, raw = self._request_raw(method, path, payload, timeout=timeout)
        try:
            decoded = json.loads(raw) if raw else {}
        except json.JSONDecodeError as error:
            raise OllamaFailure(
                category="malformed_response",
                message="Ollama returned invalid JSON",
                retryable=True,
                http_status=status,
                raw_text=raw,
                malformed_response=True,
            ) from error
        if not isinstance(decoded, dict):
            raise OllamaFailure(
                category="malformed_response",
                message="Ollama returned a non-object JSON payload",
                retryable=True,
                http_status=status,
                raw_text=raw,
                malformed_response=True,
            )
        return status, decoded, raw

    def available(self) -> bool:
        try:
            self._request_json("GET", "/api/version", timeout=self.request_timeout_seconds)
        except OllamaFailure:
            return False
        return True

    def validate_model(self) -> bool:
        try:
            _, payload, _ = self._request_json(
                "GET", "/api/tags", timeout=self.request_timeout_seconds
            )
        except OllamaFailure:
            return False
        models = payload.get("models", [])
        if not isinstance(models, list):
            return False
        for item in models:
            if not isinstance(item, dict):
                continue
            if item.get("name") == self.model or item.get("model") == self.model:
                return True
        return False

    def _discover_paths(self, task: Task) -> list[str]:
        paths: list[str] = []
        metadata_paths = task.metadata.get("inspect_paths", [])
        if isinstance(metadata_paths, str):
            paths.append(metadata_paths)
        elif isinstance(metadata_paths, list):
            paths.extend(item for item in metadata_paths if isinstance(item, str))
        paths.extend(_PATH_TOKEN.findall(task.prompt))
        deduped: list[str] = []
        for path in paths:
            if path not in deduped:
                deduped.append(path)
            if len(deduped) >= 5:
                break
        return deduped

    def _read_file_snippet(self, worktree: Path, path: str) -> str:
        if ".." in PurePosixPath(path).parts or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
            return ""
        result = self.runner.run(
            [
                self._python(),
                "-c",
                (
                    "from pathlib import Path; import sys\n"
                    "target = Path(sys.argv[1])\n"
                    "print(target.read_text(encoding='utf-8'))\n"
                ),
                path,
            ],
            cwd=worktree,
            timeout_seconds=30,
        )
        if result.exit_code != 0:
            return ""
        snippet, truncated = _clip(result.stdout, 20_000)
        if truncated:
            return f"{snippet}\n[truncated]"
        return snippet

    def _build_system_prompt(self, task: Task, worktree: Path) -> str:
        lines = [
            "You are a coding agent operating inside a controller-managed Git worktree.",
            "Return only JSON that matches the supplied schema.",
            "The JSON must contain a unified diff patch under the `patch` key.",
            "Do not include shell commands, secrets, or prose outside the JSON object.",
            "Do not modify files outside the provided worktree.",
            "Do not invent paths. Use only repository-relative paths.",
        ]
        controller_context = task.metadata.get("controller_context")
        if controller_context:
            lines.append("Controller context:")
            lines.append(json.dumps(controller_context, ensure_ascii=False, sort_keys=True, default=str))
        inspect_paths = self._discover_paths(task)
        if inspect_paths:
            lines.append("Repository snippets:")
        for path in inspect_paths:
            snippet = self._read_file_snippet(worktree, path)
            if snippet:
                lines.append(f"### {path}")
                lines.append(snippet)
        return "\n".join(lines)

    def _request_payload(self, task: Task, worktree: Path) -> dict[str, Any]:
        return {
            "model": self.model,
            "prompt": task.prompt,
            "system": self._build_system_prompt(task, worktree),
            "stream": False,
            "format": self._SCHEMA,
            "options": {
                "num_ctx": self.context_size,
                "temperature": self.temperature,
            },
        }

    def _parse_model_response(self, response_text: str) -> dict[str, Any]:
        try:
            payload = json.loads(response_text)
        except json.JSONDecodeError as error:
            raise OllamaFailure(
                category="malformed_response",
                message="Model response was not valid JSON",
                retryable=True,
                raw_text=response_text,
                malformed_response=True,
            ) from error
        if not isinstance(payload, dict):
            raise OllamaFailure(
                category="malformed_response",
                message="Model response must be a JSON object",
                retryable=True,
                raw_text=response_text,
                malformed_response=True,
            )
        return payload

    def _validate_patch_paths(self, patch: str) -> list[str]:
        paths: list[str] = []
        for line in patch.splitlines():
            if line.startswith("diff --git "):
                parts = line.split()
                for token in parts[2:4]:
                    if token == "/dev/null":
                        continue
                    candidate = token[2:] if token.startswith(("a/", "b/")) else token
                    pure = PurePosixPath(candidate)
                    if pure.is_absolute() or ".." in pure.parts:
                        raise OllamaFailure(
                            category="invalid_patch",
                            message=f"Patch path escapes the worktree: {candidate}",
                            retryable=True,
                        )
                    paths.append(candidate)
            elif line.startswith("+++ ") or line.startswith("--- "):
                candidate = line[4:].strip()
                if candidate == "/dev/null":
                    continue
                if candidate.startswith(("a/", "b/")):
                    candidate = candidate[2:]
                pure = PurePosixPath(candidate)
                if pure.is_absolute() or ".." in pure.parts:
                    raise OllamaFailure(
                        category="invalid_patch",
                        message=f"Patch path escapes the worktree: {candidate}",
                        retryable=True,
                    )
                paths.append(candidate)
        return paths

    def _apply_patch(self, worktree: Path, patch: str) -> None:
        check = self.runner.run(
            ["git", "apply", "--check", "--whitespace=nowarn", "-"],
            cwd=worktree,
            input_text=patch,
            timeout_seconds=self.generation_timeout_seconds,
        )
        if check.exit_code != 0:
            raise OllamaFailure(
                category="invalid_patch",
                message=check.stderr or check.stdout or "git apply --check failed",
                retryable=True,
                raw_text=check.stdout + check.stderr,
            )
        result = self.runner.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=worktree,
            input_text=patch,
            timeout_seconds=self.generation_timeout_seconds,
        )
        if result.exit_code != 0:
            raise OllamaFailure(
                category="invalid_patch",
                message=result.stderr or result.stdout or "git apply failed",
                retryable=True,
                raw_text=result.stdout + result.stderr,
            )

    def _changed_files(self, task: Task, worktree: Path) -> list[str]:
        result = self.runner.run(
            ["git", "diff", "--name-only", "--find-renames", task.base_ref, "--"],
            cwd=worktree,
            timeout_seconds=60,
        )
        if result.exit_code != 0:
            raise OllamaFailure(
                category="provider_failed",
                message=result.stderr or result.stdout or "git diff failed",
                retryable=True,
                raw_text=result.stdout + result.stderr,
            )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _generate_once(self, task: Task, worktree: Path) -> tuple[int, dict[str, Any], str]:
        payload = self._request_payload(task, worktree)
        return self._request_json(
            "POST",
            "/api/generate",
            payload,
            timeout=self.generation_timeout_seconds,
        )

    def execute(self, *, task: Task, worktree: Path, attempt: int) -> ProviderResult:
        started = utc_now()
        try:
            self._request_json("GET", "/api/version", timeout=self.request_timeout_seconds)
        except OllamaFailure as failure:
            return ProviderResult.failure_result(
                provider=self.name,
                model=self.model,
                attempt=attempt,
                worktree_path=os.fspath(worktree),
                branch=f"controller/{task.id}",
                category=failure.category,
                retryable=failure.retryable,
                http_status=failure.http_status,
                stdout=failure.raw_text[: self.max_output_chars],
                stderr=failure.message,
                returned_text=failure.raw_text[: self.max_output_chars],
                returned_text_truncated=len(failure.raw_text) > self.max_output_chars,
                malformed_response=failure.malformed_response,
                timed_out=failure.category == "timeout",
                changed_files=[],
                tests=[],
                started_at=started,
                finished_at=utc_now(),
            )
        try:
            status, payload, raw = self._request_json(
                "GET", "/api/tags", timeout=self.request_timeout_seconds
            )
        except OllamaFailure as failure:
            return ProviderResult.failure_result(
                provider=self.name,
                model=self.model,
                attempt=attempt,
                worktree_path=os.fspath(worktree),
                branch=f"controller/{task.id}",
                category=failure.category,
                retryable=failure.retryable,
                http_status=failure.http_status,
                stdout=failure.raw_text[: self.max_output_chars],
                stderr=failure.message,
                returned_text=failure.raw_text[: self.max_output_chars],
                returned_text_truncated=len(failure.raw_text) > self.max_output_chars,
                malformed_response=failure.malformed_response,
                timed_out=failure.category == "timeout",
                changed_files=[],
                tests=[],
                started_at=started,
                finished_at=utc_now(),
            )
        if not isinstance(payload.get("models", []), list):
            return ProviderResult.failure_result(
                provider=self.name,
                model=self.model,
                attempt=attempt,
                worktree_path=os.fspath(worktree),
                branch=f"controller/{task.id}",
                category="malformed_response",
                retryable=True,
                http_status=status,
                stdout=raw[: self.max_output_chars],
                stderr="Ollama tags response was malformed",
                returned_text=raw[: self.max_output_chars],
                returned_text_truncated=len(raw) > self.max_output_chars,
                malformed_response=True,
                timed_out=False,
                changed_files=[],
                tests=[],
                started_at=started,
                finished_at=utc_now(),
            )
        present = any(
            isinstance(item, dict)
            and (item.get("name") == self.model or item.get("model") == self.model)
            for item in payload.get("models", [])
        )
        if not present:
            return ProviderResult.failure_result(
                provider=self.name,
                model=self.model,
                attempt=attempt,
                worktree_path=os.fspath(worktree),
                branch=f"controller/{task.id}",
                category="missing_model",
                retryable=False,
                http_status=status,
                stdout="",
                stderr=f"Configured model not found: {self.model}",
                returned_text="",
                returned_text_truncated=False,
                malformed_response=False,
                timed_out=False,
                changed_files=[],
                tests=[],
                started_at=started,
                finished_at=utc_now(),
            )
        last_failure: OllamaFailure | None = None
        for request_attempt in range(1, self.max_retries + 2):
            try:
                status, payload, raw = self._generate_once(task, worktree)
                response_text = payload.get("response")
                if not isinstance(response_text, str) or not response_text.strip():
                    raise OllamaFailure(
                        category="missing_response_text",
                        message="Ollama response did not include text",
                        retryable=True,
                        http_status=status,
                        raw_text=raw,
                    )
                parsed = self._parse_model_response(response_text)
                patch = parsed.get("patch")
                if not isinstance(patch, str) or not patch.strip():
                    raise OllamaFailure(
                        category="no_changes",
                        message="Model returned an empty patch",
                        retryable=True,
                        http_status=status,
                        raw_text=response_text,
                    )
                self._validate_patch_paths(patch)
                self._apply_patch(worktree, patch)
                changed_files = self._changed_files(task, worktree)
                if not changed_files:
                    raise OllamaFailure(
                        category="no_changes",
                        message="Patch did not change any tracked files",
                        retryable=True,
                        http_status=status,
                        raw_text=response_text,
                    )
                returned_text, truncated = _clip(response_text, self.max_output_chars)
                return ProviderResult.success_result(
                    provider=self.name,
                    model=self.model,
                    attempt=attempt,
                    worktree_path=os.fspath(worktree),
                    branch=f"controller/{task.id}",
                    http_status=status,
                    stdout=raw[: self.max_output_chars],
                    stderr="",
                    returned_text=returned_text,
                    returned_text_truncated=truncated,
                    malformed_response=False,
                    timed_out=False,
                    changed_files=changed_files,
                    tests=[],
                    started_at=started,
                    finished_at=utc_now(),
                )
            except OllamaFailure as failure:
                last_failure = failure
                if request_attempt <= self.max_retries and failure.retryable:
                    time.sleep(min(0.5 * request_attempt, 2.0))
                    continue
                returned_text = failure.raw_text or ""
                clipped_text, truncated = _clip(returned_text, self.max_output_chars)
                return ProviderResult.failure_result(
                    provider=self.name,
                    model=self.model,
                    attempt=attempt,
                    worktree_path=os.fspath(worktree),
                    branch=f"controller/{task.id}",
                    category=failure.category,
                    retryable=failure.retryable,
                    http_status=failure.http_status,
                    stdout=clipped_text if failure.category in {"malformed_response", "missing_response_text"} else "",
                    stderr=failure.message,
                    returned_text=clipped_text,
                    returned_text_truncated=truncated,
                    malformed_response=failure.malformed_response,
                    timed_out=failure.category == "timeout",
                    changed_files=[],
                    tests=[],
                    started_at=started,
                    finished_at=utc_now(),
                )
        assert last_failure is not None
        return ProviderResult.failure_result(
            provider=self.name,
            model=self.model,
            attempt=attempt,
            worktree_path=os.fspath(worktree),
            branch=f"controller/{task.id}",
            category=last_failure.category,
            retryable=last_failure.retryable,
            http_status=last_failure.http_status,
            stdout=last_failure.raw_text[: self.max_output_chars],
            stderr=last_failure.message,
            returned_text=last_failure.raw_text[: self.max_output_chars],
            returned_text_truncated=len(last_failure.raw_text) > self.max_output_chars,
            malformed_response=last_failure.malformed_response,
            timed_out=last_failure.category == "timeout",
            changed_files=[],
            tests=[],
            started_at=started,
            finished_at=utc_now(),
        )


class LocalOllamaProvider(OllamaProvider):
    pass
