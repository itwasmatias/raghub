"""Durable delegation grant primitives for mission-scoped authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any


_MAX_GRANT_ID_LENGTH = 255
_MAX_DOMAIN_ID_LENGTH = 255
_MAX_MISSION_ID_LENGTH = 255
_MAX_IDENTITY_LENGTH = 255
_MAX_SCOPE_ITEM_LENGTH = 256
_MAX_REASON_LENGTH = 1000


class DelegationGrantStatus(str, Enum):
    """Lifecycle state of a durable delegation grant."""

    ACTIVE = "active"
    PENDING = "pending"
    REVOKED = "revoked"
    EXPIRED = "expired"


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


def _normalize_scope(value: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        raise TypeError(f"{field_name} must be an iterable of strings")
    try:
        items = list(value)
    except TypeError as exc:
        raise TypeError(f"{field_name} must be an iterable of strings") from exc
    if not items:
        raise ValueError(f"{field_name} must contain at least one authority atom")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = _require_text(item, field_name, max_length=_MAX_SCOPE_ITEM_LENGTH)
        if text in seen:
            raise ValueError(f"{field_name} must not contain duplicates")
        seen.add(text)
        normalized.append(text)
    return tuple(sorted(normalized))


def grant_fingerprint(
    *,
    grant_id: str,
    domain_id: str,
    mission_id: str,
    grantor_identity: str,
    grantee_identity: str,
    authority_scope: tuple[str, ...],
    created_at: datetime,
    effective_at: datetime,
    expires_at: datetime,
    status: DelegationGrantStatus,
    parent_grant_id: str | None = None,
    parent_grant_fingerprint: str | None = None,
    revoked_at: datetime | None = None,
    revocation_reason: str | None = None,
) -> str:
    """Return the stable fingerprint for immutable delegation grant provenance.

    Lifecycle metadata such as status or revocation details is intentionally not
    part of the fingerprint so the same logical grant keeps a stable identity
    across append-only lifecycle transitions.
    """

    payload = {
        "authority_scope": list(authority_scope),
        "created_at": created_at.astimezone(timezone.utc).isoformat(),
        "domain_id": domain_id,
        "effective_at": effective_at.astimezone(timezone.utc).isoformat(),
        "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
        "grant_id": grant_id,
        "grantee_identity": grantee_identity,
        "grantor_identity": grantor_identity,
        "mission_id": mission_id,
        "parent_grant_fingerprint": parent_grant_fingerprint,
        "parent_grant_id": parent_grant_id,
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


@dataclass(slots=True)
class DelegationGrant:
    """Request-time delegation grant root."""

    grant_id: str
    domain_id: str
    mission_id: str
    grantor_identity: str
    grantee_identity: str
    authority_scope: tuple[str, ...] | list[str]
    parent_grant_id: str | None = None
    created_at: datetime | None = None
    effective_at: datetime | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        self.grant_id = _require_text(self.grant_id, "grant_id", max_length=_MAX_GRANT_ID_LENGTH)
        self.domain_id = _require_text(self.domain_id, "domain_id", max_length=_MAX_DOMAIN_ID_LENGTH)
        self.mission_id = _require_text(self.mission_id, "mission_id", max_length=_MAX_MISSION_ID_LENGTH)
        self.grantor_identity = _require_text(
            self.grantor_identity,
            "grantor_identity",
            max_length=_MAX_IDENTITY_LENGTH,
        )
        self.grantee_identity = _require_text(
            self.grantee_identity,
            "grantee_identity",
            max_length=_MAX_IDENTITY_LENGTH,
        )
        self.authority_scope = _normalize_scope(self.authority_scope, "authority_scope")
        if self.parent_grant_id is not None:
            self.parent_grant_id = _require_text(
                self.parent_grant_id,
                "parent_grant_id",
                max_length=_MAX_GRANT_ID_LENGTH,
            )

        now = datetime.now(timezone.utc)
        if self.created_at is None:
            self.created_at = now
        self.created_at = _normalize_timestamp(self.created_at, "created_at")

        if self.effective_at is None:
            self.effective_at = self.created_at
        self.effective_at = _normalize_timestamp(self.effective_at, "effective_at")

        if self.expires_at is None:
            self.expires_at = self.effective_at + timedelta(hours=1)
        self.expires_at = _normalize_timestamp(self.expires_at, "expires_at")

        if self.effective_at < self.created_at:
            raise ValueError("effective_at must not precede created_at")
        if self.expires_at <= self.effective_at:
            raise ValueError("expires_at must follow effective_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "domain_id": self.domain_id,
            "mission_id": self.mission_id,
            "grantor_identity": self.grantor_identity,
            "grantee_identity": self.grantee_identity,
            "authority_scope": list(self.authority_scope),
            "parent_grant_id": self.parent_grant_id,
            "created_at": self.created_at.astimezone(timezone.utc).isoformat(),
            "effective_at": self.effective_at.astimezone(timezone.utc).isoformat(),
            "expires_at": self.expires_at.astimezone(timezone.utc).isoformat(),
        }


@dataclass(frozen=True, slots=True)
class AuthoritativeDelegationGrant:
    """Immutable snapshot of a durable delegation grant."""

    grant_id: str
    domain_id: str
    mission_id: str
    grantor_identity: str
    grantee_identity: str
    authority_scope: tuple[str, ...]
    parent_grant_id: str | None
    parent_grant_fingerprint: str | None
    created_at: datetime
    effective_at: datetime
    expires_at: datetime
    status: DelegationGrantStatus
    grant_fingerprint: str
    revoked_at: datetime | None = None
    revocation_reason: str | None = None

    def current_status(self, now: datetime | None = None) -> DelegationGrantStatus:
        current = datetime.now(timezone.utc) if now is None else _normalize_timestamp(now, "now")
        if self.status is DelegationGrantStatus.REVOKED:
            return DelegationGrantStatus.REVOKED
        if self.status is DelegationGrantStatus.EXPIRED:
            return DelegationGrantStatus.EXPIRED
        if current < self.effective_at:
            return DelegationGrantStatus.PENDING
        if current >= self.expires_at:
            return DelegationGrantStatus.EXPIRED
        return DelegationGrantStatus.ACTIVE

    def is_active(self, now: datetime | None = None) -> bool:
        return self.current_status(now) is DelegationGrantStatus.ACTIVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "domain_id": self.domain_id,
            "mission_id": self.mission_id,
            "grantor_identity": self.grantor_identity,
            "grantee_identity": self.grantee_identity,
            "authority_scope": list(self.authority_scope),
            "parent_grant_id": self.parent_grant_id,
            "parent_grant_fingerprint": self.parent_grant_fingerprint,
            "created_at": self.created_at.astimezone(timezone.utc).isoformat(),
            "effective_at": self.effective_at.astimezone(timezone.utc).isoformat(),
            "expires_at": self.expires_at.astimezone(timezone.utc).isoformat(),
            "status": self.current_status().value,
            "grant_fingerprint": self.grant_fingerprint,
            "revoked_at": None if self.revoked_at is None else self.revoked_at.astimezone(timezone.utc).isoformat(),
            "revocation_reason": self.revocation_reason,
        }


__all__ = [
    "AuthoritativeDelegationGrant",
    "DelegationGrant",
    "DelegationGrantStatus",
    "grant_fingerprint",
]
