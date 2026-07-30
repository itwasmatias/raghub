from __future__ import annotations

import os
from pathlib import Path

from ..models import ProviderResult, Task
from ..remote import LocalRunner
from .base import Provider


def classify(stderr: str, stdout: str, *, timed_out: bool, exit_code: int | None) -> tuple[str, bool]:
    message = f"{stderr}\n{stdout}".lower()
    if timed_out:
        return "timeout", True
    if exit_code is None and ("no such file" in message or "not found" in message):
        return "provider_unavailable", False
    if any(
        term in message
        for term in (
            "could not create path aliases",
            "failed to initialize in-process app-server client",
            "error sending request for url",
            "api.openai.com",
            "stream disconnected before completion",
            "read-only file system",
            "operation not permitted",
        )
    ):
        return "provider_unavailable", True
    if any(term in message for term in ("usage limit", "rate limit", "quota", "429")):
        return "usage_limit", True
    if any(term in message for term in ("unauthorized", "authentication", "invalid api key", "401")):
        return "authentication", False
    if "ssh" in message and any(term in message for term in ("denied", "connect", "host")):
        return "transport", True
    return "provider_failed", True


class CodexProvider(Provider):
    name = "codex"

    def __init__(
        self,
        *,
        runner: LocalRunner,
        executable: str,
        timeout_seconds: float,
        prefix_args: list[str] | None = None,
        local: bool = False,
    ) -> None:
        self.runner = runner
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.prefix_args = list(prefix_args or [])
        self.local = local
        if local:
            self.name = "local-ollama"

    def _argv(self, worktree: Path) -> list[str]:
        if self.prefix_args:
            return [self.executable, *self.prefix_args]
        argv = [
            self.executable,
            "exec",
            "--json",
            "--ephemeral",
            "--sandbox",
            "workspace-write",
            "-C",
            str(worktree),
        ]
        if self.local:
            argv.extend(["--oss", "--local-provider", "ollama"])
        argv.append("-")
        return argv

    def execute(self, *, task: Task, worktree: Path, attempt: int) -> ProviderResult:
        result = self.runner.run(
            self._argv(worktree),
            cwd=worktree,
            input_text=task.prompt,
            timeout_seconds=self.timeout_seconds,
        )
        common = {
            "provider": self.name,
            "attempt": attempt,
            "worktree_path": str(worktree),
            "branch": f"controller/{task.id}",
            "exit_code": result.exit_code,
            "stdout": result.stdout[-100_000:],
            "stderr": result.stderr[-100_000:],
            "timed_out": result.timed_out,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
        }
        if result.exit_code == 0 and not result.timed_out:
            return ProviderResult.success_result(**common)
        category, retryable = classify(
            result.stderr, result.stdout, timed_out=result.timed_out, exit_code=result.exit_code
        )
        return ProviderResult.failure_result(
            category=category, retryable=retryable, **common
        )




class LocalOllamaProvider(CodexProvider):
    # Use Fedora Codex OSS mode with Windows Ollama through an SSH tunnel.

    DEFAULT_MODEL = "qwen2.5-coder:3b"

    def __init__(self, *, model: str | None = None, **kwargs) -> None:
        configured_model = (
            model
            or os.environ.get("RAGHUB_OLLAMA_MODEL")
            or self.DEFAULT_MODEL
        ).strip()

        if not configured_model:
            raise ValueError("The Ollama model name cannot be empty.")

        self.model = configured_model
        super().__init__(local=True, **kwargs)

    def _argv(self, worktree: Path) -> list[str]:
        argv = super()._argv(worktree)

        if self.prefix_args:
            return argv

        try:
            prompt_index = argv.index("-")
        except ValueError:
            prompt_index = len(argv)

        argv[prompt_index:prompt_index] = [
            "--model",
            self.model,
        ]

        return argv
