"""Durable agent identity primitives for ControlDomain-scoped principals."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


_MAX_AGENT_ID_LENGTH = 255
_MAX_DOMAIN_ID_LENGTH = 255
_MAX_NAME_LENGTH = 1024


class AgentIdentityLifecycle(str, Enum):
    """Lifecycle state of a durable agent identity."""

    ACTIVE = "active"
    REVOKED = "revoked"
    ARCHIVED = "archived"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _require_text(value: Any, field_name: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field_name} must not contain surrounding whitespace")
    if len(value) > max_length:
        raise ValueError(f"{field_name} exceeds {max_length} characters")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NULL bytes")
    return value


def _normalize_timestamp(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def identity_fingerprint(
    *,
    agent_id: str,
    domain_id: str,
    domain_fingerprint: str,
    name: str,
    created_at: datetime,
) -> str:
    """Return the stable root fingerprint for a durable agent identity."""

    payload = {
        "agent_id": agent_id,
        "domain_id": domain_id,
        "domain_fingerprint": domain_fingerprint,
        "name": name,
        "created_at": created_at.astimezone(timezone.utc).isoformat(),
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


@dataclass(slots=True)
class AgentIdentity:
    """Mutable root identity whose lifecycle is controlled by the registry."""

    agent_id: str
    domain_id: str
    name: str
    lifecycle: AgentIdentityLifecycle = AgentIdentityLifecycle.ACTIVE
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        self.agent_id = _require_text(
            self.agent_id,
            "agent_id",
            max_length=_MAX_AGENT_ID_LENGTH,
        )
        self.domain_id = _require_text(
            self.domain_id,
            "domain_id",
            max_length=_MAX_DOMAIN_ID_LENGTH,
        )
        self.name = _require_text(self.name, "name", max_length=_MAX_NAME_LENGTH)
        if not isinstance(self.lifecycle, AgentIdentityLifecycle):
            if not isinstance(self.lifecycle, str):
                raise TypeError("lifecycle must be an AgentIdentityLifecycle or string")
            try:
                self.lifecycle = AgentIdentityLifecycle(self.lifecycle)
            except ValueError as exc:
                raise ValueError(f"Invalid lifecycle: {self.lifecycle!r}") from exc
        self.created_at = _normalize_timestamp(self.created_at, "created_at")

    def is_active(self) -> bool:
        """Return True when the identity is currently active."""

        return self.lifecycle == AgentIdentityLifecycle.ACTIVE

    def revoke(self) -> None:
        """Transition the identity to revoked state."""

        self._transition(AgentIdentityLifecycle.REVOKED)

    def archive(self) -> None:
        """Transition the identity to archived state."""

        self._transition(AgentIdentityLifecycle.ARCHIVED)

    def _transition(self, new_lifecycle: AgentIdentityLifecycle) -> None:
        order = {
            AgentIdentityLifecycle.ACTIVE: 0,
            AgentIdentityLifecycle.REVOKED: 1,
            AgentIdentityLifecycle.ARCHIVED: 2,
        }
        if order[new_lifecycle] < order[self.lifecycle]:
            raise ValueError(
                f"Cannot transition from {self.lifecycle.value} to "
                f"{new_lifecycle.value} (non-widening invariant)",
            )
        if self.lifecycle == new_lifecycle:
            return
        self.lifecycle = new_lifecycle

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "domain_id": self.domain_id,
            "name": self.name,
            "lifecycle": self.lifecycle.value,
            "created_at": self.created_at.astimezone(timezone.utc).isoformat(),
        }


@dataclass(frozen=True, slots=True)
class AuthoritativeAgentIdentity:
    """Immutable snapshot of a durable agent identity resolved from evidence."""

    agent_id: str
    domain_id: str
    domain_fingerprint: str
    name: str
    identity_fingerprint: str
    lifecycle: AgentIdentityLifecycle
    created_at: datetime
    last_transition_at: datetime
    revoked_at: datetime | None = None
    archived_at: datetime | None = None
    revocation_reason: str | None = None
    archive_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "domain_id": self.domain_id,
            "domain_fingerprint": self.domain_fingerprint,
            "name": self.name,
            "identity_fingerprint": self.identity_fingerprint,
            "lifecycle": self.lifecycle.value,
            "created_at": self.created_at.astimezone(timezone.utc).isoformat(),
            "last_transition_at": self.last_transition_at.astimezone(timezone.utc).isoformat(),
            "revoked_at": None if self.revoked_at is None else self.revoked_at.astimezone(timezone.utc).isoformat(),
            "archived_at": None if self.archived_at is None else self.archived_at.astimezone(timezone.utc).isoformat(),
            "revocation_reason": self.revocation_reason,
            "archive_reason": self.archive_reason,
        }


__all__ = [
    "AgentIdentity",
    "AgentIdentityLifecycle",
    "AuthoritativeAgentIdentity",
    "identity_fingerprint",
]
