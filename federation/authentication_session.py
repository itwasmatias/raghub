"""Authentication session model for provider authentication flows."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

_MAX_TEXT_LENGTH = 255


def _canonical(value: Any) -> bytes:
    """Return canonical JSON bytes for fingerprinting."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _require_text(value: Any, field_name: str, *, max_length: int = _MAX_TEXT_LENGTH) -> str:
    """Validate text field with strict rules."""
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
    """Validate and normalize timestamp to UTC."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


class AuthenticationState(str, Enum):
    """State of an authentication session."""

    INITIATED = "initiated"
    CHALLENGE_CREATED = "challenge_created"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class AuthenticationSession:
    """Represents a provider authentication flow session.

    Authentication sessions must be:
    - Domain-bound
    - Provider-bound
    - Mission-bound when initiated for a mission
    - Time-bounded
    - Single-completion / replay resistant

    Attack scenarios that must fail:
    - Wrong domain completion
    - Wrong provider completion
    - Wrong session id
    - Expired session
    - Second completion/replay
    - Cross-mission substitution
    """

    session_id: str
    domain_id: str
    provider: str
    mission_id: str
    state: AuthenticationState
    initiated_at: datetime
    expires_at: datetime
    challenge_data: str | None = None
    completed_at: datetime | None = None
    failed_at: datetime | None = None
    failure_reason: str | None = None
    cancelled_at: datetime | None = None
    cancellation_reason: str | None = None

    def __post_init__(self) -> None:
        """Validate all fields and ensure consistency."""
        object.__setattr__(self, "session_id", _require_text(self.session_id, "session_id"))
        object.__setattr__(self, "domain_id", _require_text(self.domain_id, "domain_id"))
        object.__setattr__(self, "provider", _require_text(self.provider, "provider"))
        object.__setattr__(self, "mission_id", _require_text(self.mission_id, "mission_id"))

        # Validate state
        if not isinstance(self.state, AuthenticationState):
            if isinstance(self.state, str):
                try:
                    state_enum = AuthenticationState(self.state)
                except ValueError as exc:
                    raise ValueError(f"Invalid state: {self.state!r}") from exc
                object.__setattr__(self, "state", state_enum)
            else:
                raise TypeError("state must be an AuthenticationState or string value")

        # Validate and normalize timestamps
        object.__setattr__(self, "initiated_at", _normalize_timestamp(self.initiated_at, "initiated_at"))
        object.__setattr__(self, "expires_at", _normalize_timestamp(self.expires_at, "expires_at"))

        if self.expires_at <= self.initiated_at:
            raise ValueError("expires_at must be after initiated_at")

        # Validate optional challenge data
        if self.challenge_data is not None:
            object.__setattr__(
                self,
                "challenge_data",
                _require_text(self.challenge_data, "challenge_data", max_length=4096),
            )

        # Validate completion
        if self.completed_at is not None:
            object.__setattr__(self, "completed_at", _normalize_timestamp(self.completed_at, "completed_at"))
            if self.completed_at < self.initiated_at:
                raise ValueError("completed_at must not precede initiated_at")

        # Validate failure
        if self.failed_at is not None:
            object.__setattr__(self, "failed_at", _normalize_timestamp(self.failed_at, "failed_at"))
            if self.failure_reason is None:
                raise ValueError("failure_reason required when failed_at is set")
            object.__setattr__(
                self,
                "failure_reason",
                _require_text(self.failure_reason, "failure_reason", max_length=1000),
            )

        # Validate cancellation
        if self.cancelled_at is not None:
            object.__setattr__(self, "cancelled_at", _normalize_timestamp(self.cancelled_at, "cancelled_at"))
            if self.cancellation_reason is None:
                raise ValueError("cancellation_reason required when cancelled_at is set")
            object.__setattr__(
                self,
                "cancellation_reason",
                _require_text(self.cancellation_reason, "cancellation_reason", max_length=1000),
            )

    def is_expired(self, now: datetime | None = None) -> bool:
        """Return True if session is expired."""
        current = datetime.now(timezone.utc) if now is None else _normalize_timestamp(now, "now")
        return current >= self.expires_at

    def is_completed(self) -> bool:
        """Return True if session completed successfully."""
        return self.state is AuthenticationState.COMPLETED

    def is_failed(self) -> bool:
        """Return True if session failed."""
        return self.state is AuthenticationState.FAILED

    def is_cancelled(self) -> bool:
        """Return True if session was cancelled."""
        return self.state is AuthenticationState.CANCELLED

    def is_terminal(self) -> bool:
        """Return True if session is in a terminal state."""
        return self.state in (
            AuthenticationState.COMPLETED,
            AuthenticationState.FAILED,
            AuthenticationState.CANCELLED,
            AuthenticationState.EXPIRED,
        )

    def completion_fingerprint(self) -> str:
        """Return fingerprint for completion validation.

        Must bind all security-critical fields to prevent confused deputy attacks.
        """
        payload = {
            "domain_id": self.domain_id,
            "mission_id": self.mission_id,
            "provider": self.provider,
            "session_id": self.session_id,
        }
        return hashlib.sha256(_canonical(payload)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Return safe serializable representation."""
        return {
            "session_id": self.session_id,
            "domain_id": self.domain_id,
            "provider": self.provider,
            "mission_id": self.mission_id,
            "state": self.state.value,
            "initiated_at": self.initiated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "challenge_data": self.challenge_data,
            "completed_at": None if self.completed_at is None else self.completed_at.isoformat(),
            "failed_at": None if self.failed_at is None else self.failed_at.isoformat(),
            "failure_reason": self.failure_reason,
            "cancelled_at": None if self.cancelled_at is None else self.cancelled_at.isoformat(),
            "cancellation_reason": self.cancellation_reason,
        }


def create_authentication_session(
    *,
    session_id: str,
    domain_id: str,
    provider: str,
    mission_id: str,
    now: datetime | None = None,
    ttl_seconds: int = 600,
) -> AuthenticationSession:
    """Create a new authentication session with default expiration.

    Default TTL is 10 minutes for authentication sessions.
    """
    current = datetime.now(timezone.utc) if now is None else _normalize_timestamp(now, "now")
    expires = current + timedelta(seconds=ttl_seconds)

    return AuthenticationSession(
        session_id=session_id,
        domain_id=domain_id,
        provider=provider,
        mission_id=mission_id,
        state=AuthenticationState.INITIATED,
        initiated_at=current,
        expires_at=expires,
    )


__all__ = [
    "AuthenticationSession",
    "AuthenticationState",
    "create_authentication_session",
]
