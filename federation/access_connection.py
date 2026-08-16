"""Reusable access connection model with lifecycle management."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

_MAX_TEXT_LENGTH = 255
_MAX_ACCOUNT_ID_LENGTH = 512


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


def _canonicalize_provider_scopes(scopes: Any, field_name: str = "provider_scopes") -> tuple[str, ...]:
    """Canonicalize provider scopes to sorted immutable tuple."""
    if isinstance(scopes, str):
        raise TypeError(f"{field_name} must be an iterable of strings, not a single string")

    try:
        items = list(scopes)
    except TypeError as exc:
        raise TypeError(f"{field_name} must be an iterable of strings") from exc

    if not items:
        raise ValueError(f"{field_name} must contain at least one scope")

    validated: list[str] = []
    seen: set[str] = set()

    for scope in items:
        scope_text = _require_text(scope, field_name)
        if scope_text in seen:
            raise ValueError(f"{field_name} must not contain duplicates")
        seen.add(scope_text)
        validated.append(scope_text)

    # Canonical ordering: sorted
    return tuple(sorted(validated))


class ConnectionLifecycle(str, Enum):
    """Lifecycle state of a reusable access connection."""

    ACTIVE = "active"
    REVOKED = "revoked"
    REAUTH_REQUIRED = "reauth_required"


@dataclass(frozen=True, slots=True)
class AccessConnection:
    """Reusable authenticated connection to a provider/account.

    Represents: "MissionaryX has an authenticated relationship with this provider/account."

    Does NOT mean: "Any agent may use it."

    Connection metadata binds:
    - connection_id (domain-scoped identity)
    - ControlDomain
    - Provider/service
    - External account identifier (provider's subject ID)
    - Provider-granted scopes
    - Credential backend reference (opaque handle, NOT raw secret)
    - Lifecycle state
    - Timestamps

    Immutable after creation. Lifecycle transitions create new snapshots.
    """

    connection_id: str
    domain_id: str
    provider: str
    account_id: str
    granted_scopes: tuple[str, ...]
    credential_backend_ref: str
    lifecycle: ConnectionLifecycle
    created_at: datetime
    credential_generation: int = 1
    revoked_at: datetime | None = None
    revocation_reason: str | None = None
    reauth_required_at: datetime | None = None
    reauth_required_reason: str | None = None

    def __post_init__(self) -> None:
        """Validate all fields and ensure deep immutability."""
        object.__setattr__(self, "connection_id", _require_text(self.connection_id, "connection_id"))
        object.__setattr__(self, "domain_id", _require_text(self.domain_id, "domain_id"))
        object.__setattr__(self, "provider", _require_text(self.provider, "provider"))
        object.__setattr__(
            self,
            "account_id",
            _require_text(self.account_id, "account_id", max_length=_MAX_ACCOUNT_ID_LENGTH),
        )
        object.__setattr__(
            self,
            "credential_backend_ref",
            _require_text(self.credential_backend_ref, "credential_backend_ref"),
        )

        # Canonicalize granted scopes
        object.__setattr__(
            self,
            "granted_scopes",
            _canonicalize_provider_scopes(self.granted_scopes, "granted_scopes"),
        )

        # Validate lifecycle
        if not isinstance(self.lifecycle, ConnectionLifecycle):
            if isinstance(self.lifecycle, str):
                try:
                    lifecycle_enum = ConnectionLifecycle(self.lifecycle)
                except ValueError as exc:
                    raise ValueError(f"Invalid lifecycle: {self.lifecycle!r}") from exc
                object.__setattr__(self, "lifecycle", lifecycle_enum)
            else:
                raise TypeError("lifecycle must be a ConnectionLifecycle or string value")

        # Validate and normalize timestamps
        object.__setattr__(self, "created_at", _normalize_timestamp(self.created_at, "created_at"))

        if self.revoked_at is not None:
            object.__setattr__(self, "revoked_at", _normalize_timestamp(self.revoked_at, "revoked_at"))
            if self.revocation_reason is None:
                raise ValueError("revocation_reason required when revoked_at is set")
            object.__setattr__(
                self,
                "revocation_reason",
                _require_text(self.revocation_reason, "revocation_reason", max_length=1000),
            )

        if self.reauth_required_at is not None:
            object.__setattr__(
                self,
                "reauth_required_at",
                _normalize_timestamp(self.reauth_required_at, "reauth_required_at"),
            )
            if self.reauth_required_reason is None:
                raise ValueError("reauth_required_reason required when reauth_required_at is set")
            object.__setattr__(
                self,
                "reauth_required_reason",
                _require_text(self.reauth_required_reason, "reauth_required_reason", max_length=1000),
            )

        # Validate credential generation
        if not isinstance(self.credential_generation, int) or self.credential_generation < 1:
            raise ValueError("credential_generation must be a positive integer")

    def is_active(self) -> bool:
        """Return True if connection is active and usable."""
        return self.lifecycle is ConnectionLifecycle.ACTIVE

    def is_revoked(self) -> bool:
        """Return True if connection is revoked."""
        return self.lifecycle is ConnectionLifecycle.REVOKED

    def requires_reauth(self) -> bool:
        """Return True if connection requires reauthentication."""
        return self.lifecycle is ConnectionLifecycle.REAUTH_REQUIRED

    def identity_fingerprint(self) -> str:
        """Return stable identity fingerprint for connection matching.

        Fingerprint includes immutable identity fields only, not lifecycle state.
        """
        payload = {
            "account_id": self.account_id,
            "connection_id": self.connection_id,
            "domain_id": self.domain_id,
            "granted_scopes": list(self.granted_scopes),
            "provider": self.provider,
        }
        return hashlib.sha256(_canonical(payload)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Return safe serializable representation (NO raw secrets)."""
        return {
            "connection_id": self.connection_id,
            "domain_id": self.domain_id,
            "provider": self.provider,
            "account_id": self.account_id,
            "granted_scopes": list(self.granted_scopes),
            "credential_backend_ref": self.credential_backend_ref,
            "lifecycle": self.lifecycle.value,
            "created_at": self.created_at.isoformat(),
            "credential_generation": self.credential_generation,
            "revoked_at": None if self.revoked_at is None else self.revoked_at.isoformat(),
            "revocation_reason": self.revocation_reason,
            "reauth_required_at": (
                None if self.reauth_required_at is None else self.reauth_required_at.isoformat()
            ),
            "reauth_required_reason": self.reauth_required_reason,
        }


__all__ = [
    "AccessConnection",
    "ConnectionLifecycle",
]
