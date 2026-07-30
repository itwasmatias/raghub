from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import re
from typing import Any


_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    prompt: str
    base_ref: str
    tests: list[list[str]]
    max_attempts: int = 3
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _TASK_ID.fullmatch(self.id) or self.id in {".", ".."} or ".." in self.id:
            raise ValueError("task id must be path-safe")
        if not self.title or not self.prompt or not self.base_ref:
            raise ValueError("task title, prompt, and base_ref are required")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if any(not command or any(not isinstance(arg, str) for arg in command) for command in self.tests):
            raise ValueError("tests must be non-empty argument arrays")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Task":
        return cls(
            id=value.get("id", ""),
            title=value.get("title", ""),
            prompt=value.get("prompt", ""),
            base_ref=value.get("base_ref", ""),
            tests=[list(command) for command in value.get("tests", [])],
            max_attempts=value.get("max_attempts", 3),
            metadata=dict(value.get("metadata", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProcessResult:
    argv: list[str]
    cwd: str
    exit_code: int | None
    stdout: str
    stderr: str
    started_at: str
    finished_at: str
    timed_out: bool = False


@dataclass
class ProviderResult:
    provider: str
    attempt: int
    success: bool
    worktree_path: str
    branch: str
    model: str | None = None
    category: str | None = None
    retryable: bool = False
    exit_code: int | None = None
    http_status: int | None = None
    stdout: str = ""
    stderr: str = ""
    returned_text: str = ""
    returned_text_truncated: bool = False
    malformed_response: bool = False
    timed_out: bool = False
    changed_files: list[str] = field(default_factory=list)
    tests: list[dict[str, Any]] = field(default_factory=list)
    started_at: str = field(default_factory=utc_now)
    finished_at: str = field(default_factory=utc_now)

    @classmethod
    def success_result(cls, **kwargs: Any) -> "ProviderResult":
        return cls(success=True, **kwargs)

    @classmethod
    def failure_result(cls, *, category: str, retryable: bool, **kwargs: Any) -> "ProviderResult":
        return cls(success=False, category=category, retryable=retryable, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
