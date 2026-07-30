from __future__ import annotations

from dataclasses import dataclass, field
import os
import json
from pathlib import Path
from typing import Any


@dataclass
class ControllerConfig:
    controller_root: Path
    repository_path: str | Path
    worktree_root: str | Path
    codex_executable: str = "/home/matias/.local/bin/codex"
    codex_prefix_args: list[str] = field(default_factory=list)
    codex_attempts: int = 2
    local_attempts: int = 1
    provider_timeout_seconds: float = 1800
    test_timeout_seconds: float = 1800
    poll_interval_seconds: float = 5
    orphan_after_seconds: float = 7200
    cleanup_successful_worktrees: bool = False
    ssh_executable: str | None = None
    ssh_host: str | None = None
    ssh_key_path: str | None = None
    remote_python: str = "python3"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5-coder:3b"
    ollama_request_timeout_seconds: float = 120
    ollama_generation_timeout_seconds: float = 300
    ollama_context_size: int = 8192
    ollama_temperature: float = 0.2
    ollama_max_retries: int = 2
    ollama_max_output_chars: int = 200_000
    # Native Ollama agent provider settings
    native_agent_attempts: int = 0
    ollama_max_agent_steps: int = 20
    ollama_max_tool_failures: int = 5
    ollama_keep_alive: str = "5m"
    native_agent_allowed_commands: list[list[str]] = field(default_factory=list)
    native_agent_command_timeout_seconds: float = 120
    # Mission orchestrator settings
    mission_poll_interval_seconds: float = 10.0
    mission_max_concurrent_tasks: int = 5

    def __post_init__(self) -> None:
        self.controller_root = Path(self.controller_root)
        self.repository_path = os.fspath(self.repository_path)
        self.worktree_root = os.fspath(self.worktree_root)
        for name in (
            "codex_attempts",
            "local_attempts",
            "native_agent_attempts",
            "ollama_max_retries",
            "ollama_max_agent_steps",
            "ollama_max_tool_failures",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")
        for name in (
            "provider_timeout_seconds",
            "test_timeout_seconds",
            "poll_interval_seconds",
            "orphan_after_seconds",
            "ollama_request_timeout_seconds",
            "ollama_generation_timeout_seconds",
            "native_agent_command_timeout_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.ollama_context_size <= 0:
            raise ValueError("ollama_context_size must be positive")
        if not 0 <= self.ollama_temperature <= 2:
            raise ValueError("ollama_temperature must be between 0 and 2")
        if self.mission_poll_interval_seconds <= 0:
            raise ValueError("mission_poll_interval_seconds must be positive")
        if self.mission_max_concurrent_tasks <= 0:
            raise ValueError("mission_max_concurrent_tasks must be positive")
        if bool(self.ssh_host) != bool(self.ssh_key_path):
            raise ValueError("ssh_host and ssh_key_path must be configured together")

    @property
    def queue_root(self) -> Path:
        return self.controller_root / "queue"

    @property
    def reports_root(self) -> Path:
        return self.controller_root / "reports"

    @property
    def logs_root(self) -> Path:
        return self.controller_root / "logs"

    @property
    def experience_path(self) -> Path:
        return self.controller_root / "experience.jsonl"

    @property
    def process_lock_path(self) -> Path:
        return self.controller_root / ".controller.lock"

    @property
    def missions_root(self) -> Path:
        return self.controller_root / "missions"

    @classmethod
    def from_json(cls, path: Path) -> "ControllerConfig":
        data: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**data)
