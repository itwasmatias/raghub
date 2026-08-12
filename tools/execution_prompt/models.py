"""Domain models for execution prompt compiler.

M6 Execution Prompt Engine v0.1 — Models
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _canonical(value: Any) -> bytes:
    """Canonical JSON serialization for fingerprinting."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fingerprint(value: Any) -> str:
    """SHA-256 fingerprint of canonical representation."""
    return hashlib.sha256(_canonical(value)).hexdigest()


def _require_text(value: Any, field: str) -> str:
    """Require non-empty trimmed string."""
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _require_timestamp(value: Any, field: str) -> datetime:
    """Require timezone-aware datetime."""
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


class PromptKind(str, Enum):
    """Supported execution prompt kinds."""
    NEW_MILESTONE_IMPLEMENTATION = "new_milestone_implementation"


@dataclass(frozen=True, slots=True)
class MilestoneSpec:
    """Approved milestone specification."""
    milestone_id: str
    title: str
    description: str
    requirements: list[str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "milestone_id",
            _require_text(self.milestone_id, "milestone_id"),
        )
        object.__setattr__(self, "title", _require_text(self.title, "title"))
        object.__setattr__(self, "description", _require_text(self.description, "description"))
        if not isinstance(self.requirements, list):
            raise TypeError("requirements must be a list")

    def to_dict(self) -> dict[str, Any]:
        return {
            "milestone_id": self.milestone_id,
            "title": self.title,
            "description": self.description,
            "requirements": list(self.requirements),
        }


@dataclass(frozen=True, slots=True)
class DevStatusHandoff:
    """Fresh development-status handoff."""
    timestamp: datetime
    repo: str
    branch: str
    head: str
    status: str
    diff: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "timestamp",
            _require_timestamp(self.timestamp, "timestamp"),
        )
        for field_name in ("repo", "branch", "head", "status"):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name),
            )
        if not isinstance(self.diff, str):
            raise TypeError("diff must be a string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "repo": self.repo,
            "branch": self.branch,
            "head": self.head,
            "status": self.status,
            "diff": self.diff,
        }


@dataclass(frozen=True, slots=True)
class PolicyProfile:
    """Versioned policy profile."""
    version: str
    paid_budget: float
    allowed_effects: list[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", _require_text(self.version, "version"))
        if not isinstance(self.paid_budget, (int, float)) or self.paid_budget < 0:
            raise ValueError("paid_budget must be non-negative number")
        if not isinstance(self.allowed_effects, list):
            raise TypeError("allowed_effects must be a list")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "paid_budget": self.paid_budget,
            "allowed_effects": list(self.allowed_effects),
        }


@dataclass(frozen=True, slots=True)
class ExecutionPrompt:
    """Compiled, validated execution prompt artifact."""
    kind: PromptKind
    milestone_spec: MilestoneSpec
    dev_status: DevStatusHandoff
    policy: PolicyProfile
    rendered_text: str
    prompt_fingerprint: str
    compiled_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PromptKind):
            raise TypeError("kind must be a PromptKind")
        if type(self.milestone_spec) is not MilestoneSpec:
            raise TypeError("milestone_spec must be a MilestoneSpec")
        if type(self.dev_status) is not DevStatusHandoff:
            raise TypeError("dev_status must be a DevStatusHandoff")
        if type(self.policy) is not PolicyProfile:
            raise TypeError("policy must be a PolicyProfile")
        object.__setattr__(
            self,
            "rendered_text",
            _require_text(self.rendered_text, "rendered_text"),
        )
        object.__setattr__(
            self,
            "prompt_fingerprint",
            _require_text(self.prompt_fingerprint, "prompt_fingerprint"),
        )
        object.__setattr__(
            self,
            "compiled_at",
            _require_timestamp(self.compiled_at, "compiled_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "milestone_spec": self.milestone_spec.to_dict(),
            "dev_status": self.dev_status.to_dict(),
            "policy": self.policy.to_dict(),
            "rendered_text": self.rendered_text,
            "prompt_fingerprint": self.prompt_fingerprint,
            "compiled_at": self.compiled_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionPrompt:
        """Deserialize from dictionary."""
        return cls(
            kind=PromptKind(data["kind"]),
            milestone_spec=MilestoneSpec(
                milestone_id=data["milestone_spec"]["milestone_id"],
                title=data["milestone_spec"]["title"],
                description=data["milestone_spec"]["description"],
                requirements=data["milestone_spec"]["requirements"],
            ),
            dev_status=DevStatusHandoff(
                timestamp=datetime.fromisoformat(data["dev_status"]["timestamp"]),
                repo=data["dev_status"]["repo"],
                branch=data["dev_status"]["branch"],
                head=data["dev_status"]["head"],
                status=data["dev_status"]["status"],
                diff=data["dev_status"]["diff"],
            ),
            policy=PolicyProfile(
                version=data["policy"]["version"],
                paid_budget=data["policy"]["paid_budget"],
                allowed_effects=data["policy"]["allowed_effects"],
            ),
            rendered_text=data["rendered_text"],
            prompt_fingerprint=data["prompt_fingerprint"],
            compiled_at=datetime.fromisoformat(data["compiled_at"]),
        )


__all__ = [
    "PromptKind",
    "MilestoneSpec",
    "DevStatusHandoff",
    "PolicyProfile",
    "ExecutionPrompt",
]
