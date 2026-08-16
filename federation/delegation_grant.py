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
_MAX_CAPABILITY_COUNT = 1000
_MAX_CAPABILITY_LENGTH = 256
_MAX_RESOURCE_SCOPE_COUNT = 10000
_MAX_RESOURCE_SCOPE_LENGTH = 512


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


@dataclass(frozen=True, slots=True)
class DelegationCapabilities:
    """Deeply immutable capability set for authority grants.

    Capabilities define what operations an agent is authorized to perform.
    This is separate from resource_scope which defines where those operations
    can be performed.
    """

    _capabilities: frozenset[str]

    def __init__(self, capabilities: frozenset[str] | set[str] | list[str] | tuple[str, ...]) -> None:
        if isinstance(capabilities, frozenset):
            frozen = capabilities
        elif isinstance(capabilities, (set, list, tuple)):
            frozen = frozenset(capabilities)
        else:
            raise TypeError("capabilities must be a frozenset, set, list, or tuple")

        if not frozen:
            raise ValueError("capabilities must be non-empty")
        if len(frozen) > _MAX_CAPABILITY_COUNT:
            raise ValueError(f"capabilities exceed {_MAX_CAPABILITY_COUNT} limit")

        for cap in frozen:
            if not isinstance(cap, str) or not cap.strip():
                raise ValueError("capability must be a non-empty string")
            if cap != cap.strip():
                raise ValueError("capability must not contain surrounding whitespace")
            if len(cap) > _MAX_CAPABILITY_LENGTH:
                raise ValueError(f"capability exceeds {_MAX_CAPABILITY_LENGTH} characters")
            if "\x00" in cap:
                raise ValueError("capability must not contain NULL bytes")

        object.__setattr__(self, "_capabilities", frozen)

    @property
    def capabilities(self) -> frozenset[str]:
        """Return the immutable capability set."""
        return self._capabilities

    def is_subset_of(self, other: DelegationCapabilities) -> bool:
        """Return True if this capability set is a subset of another."""
        if not isinstance(other, DelegationCapabilities):
            raise TypeError("other must be a DelegationCapabilities")
        return self._capabilities.issubset(other._capabilities)

    def to_sorted_list(self) -> list[str]:
        """Return a deterministic sorted list for serialization."""
        return sorted(self._capabilities)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DelegationCapabilities):
            return False
        return self._capabilities == other._capabilities

    def __hash__(self) -> int:
        return hash(self._capabilities)

    def __repr__(self) -> str:
        return f"DelegationCapabilities({self.to_sorted_list()!r})"


@dataclass(frozen=True, slots=True)
class DelegationScope:
    """Deeply immutable resource scope set for authority grants.

    Resource scope defines where an agent's capabilities can be exercised.
    This is separate from capabilities which define what operations can be
    performed.
    """

    _scope: frozenset[str]

    def __init__(self, scope: frozenset[str] | set[str] | list[str] | tuple[str, ...]) -> None:
        if isinstance(scope, frozenset):
            frozen = scope
        elif isinstance(scope, (set, list, tuple)):
            frozen = frozenset(scope)
        else:
            raise TypeError("scope must be a frozenset, set, list, or tuple")

        if not frozen:
            raise ValueError("scope must be non-empty")
        if len(frozen) > _MAX_RESOURCE_SCOPE_COUNT:
            raise ValueError(f"scope exceeds {_MAX_RESOURCE_SCOPE_COUNT} limit")

        for resource in frozen:
            if not isinstance(resource, str) or not resource.strip():
                raise ValueError("scope resource must be a non-empty string")
            if resource != resource.strip():
                raise ValueError("scope resource must not contain surrounding whitespace")
            if len(resource) > _MAX_RESOURCE_SCOPE_LENGTH:
                raise ValueError(f"scope resource exceeds {_MAX_RESOURCE_SCOPE_LENGTH} characters")
            if "\x00" in resource:
                raise ValueError("scope resource must not contain NULL bytes")

        object.__setattr__(self, "_scope", frozen)

    @property
    def scope(self) -> frozenset[str]:
        """Return the immutable scope set."""
        return self._scope

    def is_subset_of(self, other: DelegationScope) -> bool:
        """Return True if this scope is a subset of another."""
        if not isinstance(other, DelegationScope):
            raise TypeError("other must be a DelegationScope")
        return self._scope.issubset(other._scope)

    def to_sorted_list(self) -> list[str]:
        """Return a deterministic sorted list for serialization."""
        return sorted(self._scope)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DelegationScope):
            return False
        return self._scope == other._scope

    def __hash__(self) -> int:
        return hash(self._scope)

    def __repr__(self) -> str:
        return f"DelegationScope({self.to_sorted_list()!r})"


def grant_fingerprint(
    *,
    grant_id: str,
    domain_id: str,
    mission_id: str | None,
    grantor_identity: str,
    grantee_identity: str,
    created_at: datetime,
    effective_at: datetime,
    expires_at: datetime,
    status: DelegationGrantStatus,
    capabilities: DelegationCapabilities | list[str] | None = None,
    resource_scope: DelegationScope | list[str] | None = None,
    parent_grant_id: str | None = None,
    parent_grant_fingerprint: str | None = None,
    revoked_at: datetime | None = None,
    revocation_reason: str | None = None,
    authority_scope: tuple[str, ...] | None = None,
) -> str:
    """Return the stable fingerprint for immutable delegation grant provenance.

    Lifecycle metadata such as status or revocation details is intentionally not
    part of the fingerprint so the same logical grant keeps a stable identity
    across append-only lifecycle transitions.

    Supports both v0.1 (capabilities + resource_scope) and legacy (authority_scope)
    for migration compatibility.
    """

    if authority_scope is not None:
        if capabilities is not None or resource_scope is not None:
            raise ValueError("cannot specify both authority_scope and capabilities/resource_scope")
        capabilities_list = list(authority_scope)
        scope_list = list(authority_scope)
    else:
        if capabilities is None or resource_scope is None:
            raise ValueError("must specify either authority_scope or both capabilities and resource_scope")
        if isinstance(capabilities, DelegationCapabilities):
            capabilities_list = capabilities.to_sorted_list()
        else:
            capabilities_list = sorted(list(capabilities))

        if isinstance(resource_scope, DelegationScope):
            scope_list = resource_scope.to_sorted_list()
        else:
            scope_list = sorted(list(resource_scope))

    payload = {
        "capabilities": capabilities_list,
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
        "resource_scope": scope_list,
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


@dataclass(slots=True)
class DelegationGrant:
    """Request-time delegation grant root.

    Supports both v0.1 model (capabilities + resource_scope + optional mission_id)
    and legacy model (authority_scope + required mission_id) for migration.
    """

    grant_id: str
    domain_id: str
    grantor_identity: str
    grantee_identity: str
    capabilities: DelegationCapabilities | frozenset[str] | set[str] | list[str] | None = None
    resource_scope: DelegationScope | frozenset[str] | set[str] | list[str] | None = None
    mission_id: str | None = None
    authority_scope: tuple[str, ...] | list[str] | None = None
    parent_grant_id: str | None = None
    created_at: datetime | None = None
    effective_at: datetime | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        self.grant_id = _require_text(self.grant_id, "grant_id", max_length=_MAX_GRANT_ID_LENGTH)
        self.domain_id = _require_text(self.domain_id, "domain_id", max_length=_MAX_DOMAIN_ID_LENGTH)

        if self.mission_id is not None:
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

        if self.authority_scope is not None:
            if self.capabilities is not None or self.resource_scope is not None:
                raise ValueError("cannot specify both authority_scope and capabilities/resource_scope")
            self.authority_scope = _normalize_scope(self.authority_scope, "authority_scope")
            self.capabilities = DelegationCapabilities(self.authority_scope)
            self.resource_scope = DelegationScope(self.authority_scope)
        else:
            if self.capabilities is None or self.resource_scope is None:
                raise ValueError("must specify either authority_scope or both capabilities and resource_scope")
            if not isinstance(self.capabilities, DelegationCapabilities):
                self.capabilities = DelegationCapabilities(self.capabilities)
            if not isinstance(self.resource_scope, DelegationScope):
                self.resource_scope = DelegationScope(self.resource_scope)

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
            "capabilities": self.capabilities.to_sorted_list(),
            "resource_scope": self.resource_scope.to_sorted_list(),
            "parent_grant_id": self.parent_grant_id,
            "created_at": self.created_at.astimezone(timezone.utc).isoformat(),
            "effective_at": self.effective_at.astimezone(timezone.utc).isoformat(),
            "expires_at": self.expires_at.astimezone(timezone.utc).isoformat(),
        }


@dataclass(frozen=True, slots=True)
class AuthoritativeDelegationGrant:
    """Immutable snapshot of a durable delegation grant.

    Supports both v0.1 model (capabilities + resource_scope + optional mission_id)
    and legacy model (authority_scope + required mission_id) for migration.
    """

    grant_id: str
    domain_id: str
    grantor_identity: str
    grantee_identity: str
    capabilities: DelegationCapabilities
    resource_scope: DelegationScope
    created_at: datetime
    effective_at: datetime
    expires_at: datetime
    status: DelegationGrantStatus
    grant_fingerprint: str
    mission_id: str | None = None
    parent_grant_id: str | None = None
    parent_grant_fingerprint: str | None = None
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
            "capabilities": self.capabilities.to_sorted_list(),
            "resource_scope": self.resource_scope.to_sorted_list(),
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
    "DelegationCapabilities",
    "DelegationGrant",
    "DelegationGrantStatus",
    "DelegationScope",
    "grant_fingerprint",
]
