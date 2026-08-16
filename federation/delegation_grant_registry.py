"""Durable authenticated registry for mission-scoped delegation grants."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from federation.agent_identity import AgentIdentityLifecycle
from federation.agent_identity_registry import (
    AgentIdentityConflictError,
    AgentIdentityCorruptionError,
    AgentIdentityDomainError,
    AgentIdentityError,
    AgentIdentityLifecycleError,
    AgentIdentityNotFoundError,
    DurableAgentIdentityRegistry,
)
from federation.control_domain import DomainLifecycle
from federation.control_domain_registry import (
    DomainConflictError,
    DomainCorruptionError,
    DomainLifecycleError,
    DomainNotFoundError,
    DurableControlDomainRegistry,
)
from federation.delegation_grant import (
    AuthoritativeDelegationGrant,
    DelegationCapabilities,
    DelegationGrant,
    DelegationGrantStatus,
    DelegationScope,
    _normalize_scope,
    _require_text,
    grant_fingerprint,
)
from federation.file_lock import fcntl
from federation.integrity import authentication_tag, authenticates, require_integrity_key


_SCHEMA_VERSION = 1
_AUTHENTICATION_DOMAIN = b"raghub.delegation-grant.v1"
_GENESIS_TAG = "0" * 64
_MAX_REASON_LENGTH = 1000
_FIELDS = {
    "schema_version",
    "sequence",
    "payload",
    "predecessor_authentication_tag",
    "authentication_tag",
}
_PAYLOAD_FIELDS = {
    "grant_id",
    "domain_id",
    "mission_id",
    "grantor_identity",
    "grantee_identity",
    "capabilities",
    "resource_scope",
    "parent_grant_id",
    "parent_grant_fingerprint",
    "created_at",
    "effective_at",
    "expires_at",
    "status",
    "grant_fingerprint",
    "revoked_at",
    "revocation_reason",
}


class DelegationGrantError(Exception):
    """Base error for durable delegation grant operations."""


class DelegationGrantNotFoundError(DelegationGrantError):
    """Raised when a grant or parent grant cannot be resolved."""


class DelegationGrantConflictError(DelegationGrantError):
    """Raised when durable grant evidence conflicts with a request."""


class DelegationGrantDomainError(DelegationGrantError):
    """Raised when a referenced ControlDomain is missing or inactive."""


class DelegationGrantIdentityError(DelegationGrantError):
    """Raised when a grant references a missing or inactive identity."""


class DelegationGrantLifecycleError(DelegationGrantError):
    """Raised when a grant lifecycle transition would widen authority."""


class DelegationGrantScopeError(DelegationGrantError):
    """Raised when a child grant would widen the parent's authority scope."""


class DelegationGrantCorruptionError(DelegationGrantError):
    """Raised when durable delegation evidence cannot be trusted."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DelegationGrantCorruptionError(
                "delegation grant evidence has duplicate JSON keys",
            )
        result[key] = value
    return result


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DelegationGrantCorruptionError(f"{field_name} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DelegationGrantCorruptionError(f"{field_name} is invalid") from exc
    if parsed.tzinfo is None:
        raise DelegationGrantCorruptionError(f"{field_name} must include a timezone")
    canonical = parsed.astimezone(timezone.utc)
    if canonical.isoformat() != value:
        raise DelegationGrantCorruptionError(f"{field_name} is not canonical UTC")
    return canonical


def _parse_optional_timestamp(value: Any, field_name: str) -> datetime | None:
    return None if value is None else _parse_timestamp(value, field_name)


def _parse_reason(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_REASON_LENGTH:
        raise DelegationGrantCorruptionError(f"{field_name} is invalid")
    return value


def _parse_status(value: Any) -> DelegationGrantStatus:
    if not isinstance(value, str):
        raise DelegationGrantCorruptionError("delegation grant status is invalid")
    if value not in {
        DelegationGrantStatus.ACTIVE.value,
        DelegationGrantStatus.REVOKED.value,
    }:
        raise DelegationGrantCorruptionError("delegation grant status is invalid")
    try:
        return DelegationGrantStatus(value)
    except ValueError as exc:
        raise DelegationGrantCorruptionError("delegation grant status is invalid") from exc


def _status_order(status: DelegationGrantStatus) -> int:
    if status is DelegationGrantStatus.ACTIVE:
        return 0
    if status is DelegationGrantStatus.REVOKED:
        return 1
    raise DelegationGrantCorruptionError("delegation grant status is invalid")


def _grant_key(grant: AuthoritativeDelegationGrant) -> tuple[str, str, str]:
    return grant.domain_id, grant.mission_id, grant.grant_id


def _scope_subset(child: tuple[str, ...], parent: tuple[str, ...]) -> bool:
    return set(child).issubset(parent)


def _capabilities_non_widening(child: DelegationCapabilities, parent: DelegationCapabilities) -> bool:
    """Return True if child capabilities are a subset of parent capabilities."""
    return child.is_subset_of(parent)


def _resource_scope_non_widening(child: DelegationScope, parent: DelegationScope) -> bool:
    """Return True if child resource scope is a subset of parent scope."""
    return child.is_subset_of(parent)


def _resolve_parent_deterministic(
    domain_id: str,
    parent_grant_id: str,
    parent_grant_fingerprint: str,
    child_mission_id: str | None,
    current_by_key: dict[tuple[str, str | None, str], AuthoritativeDelegationGrant],
) -> tuple[tuple[str, str | None, str], AuthoritativeDelegationGrant]:
    """Resolve parent grant deterministically using exact lineage evidence.

    Resolution order:
    1. Try exact match with child mission_id
    2. Try unbound parent (mission_id=None)
    3. If neither found, search all to detect ambiguity and fail closed

    Returns:
        (parent_key, parent_grant) tuple

    Raises:
        DelegationGrantNotFoundError: Parent not found
        DelegationGrantCorruptionError: Parent fingerprint mismatch or ambiguous lineage
    """
    # Try exact match with child mission_id
    parent_key = (domain_id, child_mission_id, parent_grant_id)
    parent = current_by_key.get(parent_key)

    if parent is not None:
        if parent.grant_fingerprint != parent_grant_fingerprint:
            raise DelegationGrantCorruptionError(
                f"parent grant {parent_grant_id!r} fingerprint mismatch: "
                f"expected {parent_grant_fingerprint!r}, got {parent.grant_fingerprint!r}"
            )
        return (parent_key, parent)

    # Try unbound parent (mission_id=None) if child is mission-bound
    if child_mission_id is not None:
        parent_key = (domain_id, None, parent_grant_id)
        parent = current_by_key.get(parent_key)

        if parent is not None:
            if parent.grant_fingerprint != parent_grant_fingerprint:
                raise DelegationGrantCorruptionError(
                    f"parent grant {parent_grant_id!r} fingerprint mismatch: "
                    f"expected {parent_grant_fingerprint!r}, got {parent.grant_fingerprint!r}"
                )
            return (parent_key, parent)

    # Neither direct match found - search all to detect ambiguity
    # Collect ALL matching parents by domain + grant_id
    candidates: list[tuple[tuple[str, str | None, str], AuthoritativeDelegationGrant]] = []
    for key, potential_parent in current_by_key.items():
        if key[0] == domain_id and key[2] == parent_grant_id:
            candidates.append((key, potential_parent))

    if not candidates:
        raise DelegationGrantNotFoundError(
            f"parent grant {parent_grant_id!r} not found in domain {domain_id!r}"
        )

    # If multiple candidates exist, this is ambiguous - fail closed
    if len(candidates) > 1:
        mission_ids = sorted({str(key[1]) for key, _ in candidates})
        raise DelegationGrantCorruptionError(
            f"parent grant {parent_grant_id!r} in domain {domain_id!r} is ambiguous: "
            f"found {len(candidates)} candidates with mission_ids {mission_ids}"
        )

    # Single candidate found
    parent_key, parent = candidates[0]

    if parent.grant_fingerprint != parent_grant_fingerprint:
        raise DelegationGrantCorruptionError(
            f"parent grant {parent_grant_id!r} fingerprint mismatch: "
            f"expected {parent_grant_fingerprint!r}, got {parent.grant_fingerprint!r}"
        )

    return (parent_key, parent)


class DelegationGrantRegistry:
    """Append-only authenticated registry of mission-scoped delegation grants."""

    def __init__(
        self,
        path: Path,
        *,
        domain_registry: DurableControlDomainRegistry,
        identity_registry: DurableAgentIdentityRegistry,
        integrity_key: bytes,
        clock=None,
    ) -> None:
        self.path = Path(path)
        if not isinstance(domain_registry, DurableControlDomainRegistry):
            raise TypeError("domain_registry must be a DurableControlDomainRegistry")
        if not isinstance(identity_registry, DurableAgentIdentityRegistry):
            raise TypeError("identity_registry must be a DurableAgentIdentityRegistry")
        self.domain_registry = domain_registry
        self.identity_registry = identity_registry
        self._integrity_key = require_integrity_key(integrity_key)
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("delegation grant clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    @contextmanager
    def _shared_lock(self, path: Path):
        if not path.exists():
            yield
            return
        with path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _domain_status(
        self,
        domain_id: str,
        *,
        strict: bool,
        cache: dict[str, DelegationGrantStatus],
    ) -> DelegationGrantStatus:
        cached = cache.get(domain_id)
        if cached is not None:
            return cached
        try:
            domain = self.domain_registry.get(domain_id)
        except DomainNotFoundError as exc:
            if strict:
                raise DelegationGrantDomainError(
                    f"control domain {domain_id!r} is not registered",
                ) from exc
            cache[domain_id] = DelegationGrantStatus.REVOKED
            return DelegationGrantStatus.REVOKED
        except DomainCorruptionError as exc:
            raise DelegationGrantCorruptionError(
                "control domain evidence cannot be trusted",
            ) from exc
        except DomainConflictError as exc:
            raise DelegationGrantCorruptionError(
                "control domain evidence is ambiguous",
            ) from exc
        if domain.lifecycle is not DomainLifecycle.ACTIVE:
            if strict:
                raise DelegationGrantDomainError(
                    f"control domain {domain_id!r} is not active",
                )
            cache[domain_id] = DelegationGrantStatus.REVOKED
            return DelegationGrantStatus.REVOKED
        cache[domain_id] = DelegationGrantStatus.ACTIVE
        return DelegationGrantStatus.ACTIVE

    def _identity_status(
        self,
        domain_id: str,
        identity_id: str,
        *,
        strict: bool,
        cache: dict[tuple[str, str], DelegationGrantStatus],
    ) -> DelegationGrantStatus:
        key = (domain_id, identity_id)
        cached = cache.get(key)
        if cached is not None:
            return cached
        try:
            identity = self.identity_registry.get(identity_id, domain_id=domain_id)
        except AgentIdentityNotFoundError as exc:
            if strict:
                raise DelegationGrantIdentityError(
                    f"agent identity {identity_id!r} is not registered in domain {domain_id!r}",
                ) from exc
            cache[key] = DelegationGrantStatus.REVOKED
            return DelegationGrantStatus.REVOKED
        except AgentIdentityConflictError as exc:
            raise DelegationGrantCorruptionError(
                "agent identity evidence is ambiguous",
            ) from exc
        except AgentIdentityCorruptionError as exc:
            raise DelegationGrantCorruptionError(
                "agent identity evidence cannot be trusted",
            ) from exc
        except AgentIdentityError as exc:
            if strict:
                raise DelegationGrantIdentityError(
                    f"agent identity {identity_id!r} is not usable in domain {domain_id!r}",
                ) from exc
            cache[key] = DelegationGrantStatus.REVOKED
            return DelegationGrantStatus.REVOKED
        if identity.lifecycle is not AgentIdentityLifecycle.ACTIVE:
            if strict:
                raise DelegationGrantIdentityError(
                    f"agent identity {identity_id!r} is not active in domain {domain_id!r}",
                )
            cache[key] = DelegationGrantStatus.REVOKED
            return DelegationGrantStatus.REVOKED
        cache[key] = DelegationGrantStatus.ACTIVE
        return DelegationGrantStatus.ACTIVE

    def _effective_status(
        self,
        key: tuple[str, str, str],
        current_by_key: dict[tuple[str, str, str], AuthoritativeDelegationGrant],
        now: datetime,
        *,
        strict_identity: bool,
        status_cache: dict[tuple[str, str, str], DelegationGrantStatus],
        identity_cache: dict[tuple[str, str], DelegationGrantStatus],
        domain_cache: dict[str, DelegationGrantStatus],
        visiting: set[tuple[str, str, str]],
    ) -> DelegationGrantStatus:
        cached = status_cache.get(key)
        if cached is not None:
            return cached
        if key in visiting:
            raise DelegationGrantCorruptionError(
                "delegation grant history is cyclic",
            )
        visiting.add(key)
        try:
            grant = current_by_key.get(key)
            if grant is None:
                raise DelegationGrantCorruptionError(
                    "delegation grant history references a missing grant",
                )

            result = grant.status
            if result is DelegationGrantStatus.ACTIVE and now >= grant.expires_at:
                result = DelegationGrantStatus.EXPIRED
            if result is DelegationGrantStatus.ACTIVE and now < grant.effective_at:
                result = DelegationGrantStatus.PENDING

            if result in {
                DelegationGrantStatus.ACTIVE,
                DelegationGrantStatus.PENDING,
            }:
                if self._domain_status(
                    grant.domain_id,
                    strict=strict_identity,
                    cache=domain_cache,
                ) is not DelegationGrantStatus.ACTIVE:
                    result = DelegationGrantStatus.REVOKED

            if result in {
                DelegationGrantStatus.ACTIVE,
                DelegationGrantStatus.PENDING,
            }:
                if self._identity_status(
                    grant.domain_id,
                    grant.grantor_identity,
                    strict=strict_identity,
                    cache=identity_cache,
                ) is not DelegationGrantStatus.ACTIVE:
                    result = DelegationGrantStatus.REVOKED

            if result in {
                DelegationGrantStatus.ACTIVE,
                DelegationGrantStatus.PENDING,
            }:
                if self._identity_status(
                    grant.domain_id,
                    grant.grantee_identity,
                    strict=strict_identity,
                    cache=identity_cache,
                ) is not DelegationGrantStatus.ACTIVE:
                    result = DelegationGrantStatus.REVOKED

            if result in {
                DelegationGrantStatus.ACTIVE,
                DelegationGrantStatus.PENDING,
            } and grant.parent_grant_id is not None:
                # Resolve parent deterministically
                try:
                    parent_key, parent = _resolve_parent_deterministic(
                        grant.domain_id,
                        grant.parent_grant_id,
                        grant.parent_grant_fingerprint,
                        grant.mission_id,
                        current_by_key,
                    )
                except DelegationGrantNotFoundError:
                    raise DelegationGrantCorruptionError(
                        "delegation grant history references a missing parent grant",
                    )
                if grant.grantor_identity != parent.grantee_identity:
                    raise DelegationGrantCorruptionError(
                        "delegation grant attenuation origin is invalid",
                    )
                if parent.mission_id is not None and grant.mission_id != parent.mission_id:
                    raise DelegationGrantCorruptionError(
                        "delegation grant mission binding widens parent authority",
                    )
                if not _capabilities_non_widening(grant.capabilities, parent.capabilities):
                    raise DelegationGrantCorruptionError(
                        "delegation grant capabilities widen parent authority",
                    )
                if not _resource_scope_non_widening(grant.resource_scope, parent.resource_scope):
                    raise DelegationGrantCorruptionError(
                        "delegation grant resource scope widens parent authority",
                    )
                if grant.effective_at < parent.effective_at or grant.expires_at > parent.expires_at:
                    raise DelegationGrantCorruptionError(
                        "delegation grant timing widens parent authority",
                    )
                parent_status = self._effective_status(
                    parent_key,
                    current_by_key,
                    now,
                    strict_identity=strict_identity,
                    status_cache=status_cache,
                    identity_cache=identity_cache,
                    domain_cache=domain_cache,
                    visiting=visiting,
                )
                if parent_status is not DelegationGrantStatus.ACTIVE:
                    result = parent_status

            status_cache[key] = result
            return result
        finally:
            visiting.remove(key)

    def _materialize(
        self,
        grant: AuthoritativeDelegationGrant,
        current_by_key: dict[tuple[str, str, str], AuthoritativeDelegationGrant],
        now: datetime,
        *,
        strict_identity: bool,
        status_cache: dict[tuple[str, str, str], DelegationGrantStatus] | None = None,
        identity_cache: dict[tuple[str, str], DelegationGrantStatus] | None = None,
        domain_cache: dict[str, DelegationGrantStatus] | None = None,
    ) -> AuthoritativeDelegationGrant:
        status_cache = {} if status_cache is None else status_cache
        identity_cache = {} if identity_cache is None else identity_cache
        domain_cache = {} if domain_cache is None else domain_cache
        effective = self._effective_status(
            _grant_key(grant),
            current_by_key,
            now,
            strict_identity=strict_identity,
            status_cache=status_cache,
            identity_cache=identity_cache,
            domain_cache=domain_cache,
            visiting=set(),
        )
        return replace(grant, status=effective)

    def _candidate_from_grant(
        self,
        grant: DelegationGrant,
        current_by_key: dict[tuple[str, str, str], AuthoritativeDelegationGrant],
    ) -> AuthoritativeDelegationGrant:
        parent_grant_fingerprint = None
        if grant.parent_grant_id is not None:
            # Compute expected fingerprint for parent lookup
            # We don't have it yet, so we need to find parent first then validate
            # Try to find parent with same mission_id first
            parent_key = (grant.domain_id, grant.mission_id, grant.parent_grant_id)
            parent = current_by_key.get(parent_key)

            # If not found and child has a mission_id, try unbound parent (mission_id=None)
            if parent is None and grant.mission_id is not None:
                parent_key = (grant.domain_id, None, grant.parent_grant_id)
                parent = current_by_key.get(parent_key)

            # If still not found, collect all matching to detect ambiguity
            if parent is None:
                candidates: list[AuthoritativeDelegationGrant] = []
                for key, potential_parent in current_by_key.items():
                    if key[0] == grant.domain_id and key[2] == grant.parent_grant_id:
                        candidates.append(potential_parent)

                if not candidates:
                    raise DelegationGrantNotFoundError(
                        f"parent grant {grant.parent_grant_id!r} is not registered",
                    )

                if len(candidates) > 1:
                    raise DelegationGrantNotFoundError(
                        f"parent grant {grant.parent_grant_id!r} is ambiguous: "
                        f"found {len(candidates)} candidates across missions"
                    )

                parent = candidates[0]

            parent_grant_fingerprint = parent.grant_fingerprint

        return AuthoritativeDelegationGrant(
            grant_id=grant.grant_id,
            domain_id=grant.domain_id,
            mission_id=grant.mission_id,
            grantor_identity=grant.grantor_identity,
            grantee_identity=grant.grantee_identity,
            capabilities=grant.capabilities,
            resource_scope=grant.resource_scope,
            parent_grant_id=grant.parent_grant_id,
            parent_grant_fingerprint=parent_grant_fingerprint,
            created_at=grant.created_at,
            effective_at=grant.effective_at,
            expires_at=grant.expires_at,
            status=DelegationGrantStatus.ACTIVE,
            grant_fingerprint=grant_fingerprint(
                grant_id=grant.grant_id,
                domain_id=grant.domain_id,
                mission_id=grant.mission_id,
                grantor_identity=grant.grantor_identity,
                grantee_identity=grant.grantee_identity,
                capabilities=grant.capabilities,
                resource_scope=grant.resource_scope,
                created_at=grant.created_at,
                effective_at=grant.effective_at,
                expires_at=grant.expires_at,
                status=DelegationGrantStatus.ACTIVE,
                parent_grant_id=grant.parent_grant_id,
                parent_grant_fingerprint=parent_grant_fingerprint,
                revoked_at=None,
                revocation_reason=None,
            ),
        )

    def _register_payload(
        self,
        grant: AuthoritativeDelegationGrant,
    ) -> dict[str, Any]:
        payload = {
            "grant_id": grant.grant_id,
            "domain_id": grant.domain_id,
            "mission_id": grant.mission_id,
            "grantor_identity": grant.grantor_identity,
            "grantee_identity": grant.grantee_identity,
            "capabilities": grant.capabilities.to_sorted_list(),
            "resource_scope": grant.resource_scope.to_sorted_list(),
            "parent_grant_id": grant.parent_grant_id,
            "parent_grant_fingerprint": grant.parent_grant_fingerprint,
            "created_at": grant.created_at.astimezone(timezone.utc).isoformat(),
            "effective_at": grant.effective_at.astimezone(timezone.utc).isoformat(),
            "expires_at": grant.expires_at.astimezone(timezone.utc).isoformat(),
            "status": grant.status.value,
            "grant_fingerprint": grant.grant_fingerprint,
            "revoked_at": None,
            "revocation_reason": None,
        }
        return payload

    def _revoke_payload(
        self,
        grant: AuthoritativeDelegationGrant,
        *,
        revoked_at: datetime,
        reason: str | None,
    ) -> dict[str, Any]:
        payload = {
            "grant_id": grant.grant_id,
            "domain_id": grant.domain_id,
            "mission_id": grant.mission_id,
            "grantor_identity": grant.grantor_identity,
            "grantee_identity": grant.grantee_identity,
            "capabilities": grant.capabilities.to_sorted_list(),
            "resource_scope": grant.resource_scope.to_sorted_list(),
            "parent_grant_id": grant.parent_grant_id,
            "parent_grant_fingerprint": grant.parent_grant_fingerprint,
            "created_at": grant.created_at.astimezone(timezone.utc).isoformat(),
            "effective_at": grant.effective_at.astimezone(timezone.utc).isoformat(),
            "expires_at": grant.expires_at.astimezone(timezone.utc).isoformat(),
            "status": DelegationGrantStatus.REVOKED.value,
            "grant_fingerprint": grant.grant_fingerprint,
            "revoked_at": revoked_at.astimezone(timezone.utc).isoformat(),
            "revocation_reason": reason,
        }
        return payload

    def register(self, grant: DelegationGrant) -> AuthoritativeDelegationGrant:
        """Register a new grant or return an exact durable duplicate."""

        if not isinstance(grant, DelegationGrant):
            raise TypeError("grant must be a DelegationGrant")

        now = self._now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._shared_lock(self.domain_registry.path), self._shared_lock(self.identity_registry.path):
            with self.path.open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    handle.seek(0)
                    records = self._decode(handle.read())
                    current_by_key = self._current_by_key(records)
                    candidate = self._candidate_from_grant(grant, current_by_key)
                    self._domain_status(
                        grant.domain_id,
                        strict=True,
                        cache={},
                    )
                    self._identity_status(
                        grant.domain_id,
                        grant.grantor_identity,
                        strict=True,
                        cache={},
                    )
                    self._identity_status(
                        grant.domain_id,
                        grant.grantee_identity,
                        strict=True,
                        cache={},
                    )
                    if grant.parent_grant_id is not None:
                        # Resolve parent deterministically
                        parent_key, parent = _resolve_parent_deterministic(
                            grant.domain_id,
                            grant.parent_grant_id,
                            candidate.parent_grant_fingerprint,
                            grant.mission_id,
                            current_by_key,
                        )
                        parent_status = self._effective_status(
                            parent_key,
                            current_by_key,
                            now,
                            strict_identity=True,
                            status_cache={},
                            identity_cache={},
                            domain_cache={},
                            visiting=set(),
                        )
                        if parent_status is not DelegationGrantStatus.ACTIVE:
                            raise DelegationGrantLifecycleError(
                                "parent grant is not active",
                            )
                        if grant.grantor_identity != parent.grantee_identity:
                            raise DelegationGrantIdentityError(
                                "child grantor must match the parent grantee",
                            )
                        if parent.mission_id is not None and candidate.mission_id != parent.mission_id:
                            raise DelegationGrantScopeError(
                                "child grant mission binding widens parent authority",
                            )
                        if not _capabilities_non_widening(candidate.capabilities, parent.capabilities):
                            raise DelegationGrantScopeError(
                                "child grant capabilities widens the parent grant",
                            )
                        if not _resource_scope_non_widening(candidate.resource_scope, parent.resource_scope):
                            raise DelegationGrantScopeError(
                                "child grant resource scope widens the parent grant",
                            )
                        if (
                            candidate.effective_at < parent.effective_at
                            or candidate.expires_at > parent.expires_at
                        ):
                            raise DelegationGrantLifecycleError(
                                "child grant timing widens the parent grant",
                            )
                    current = current_by_key.get(_grant_key(candidate))
                    if current is not None and current.grant_fingerprint == candidate.grant_fingerprint:
                        return self._materialize(
                            current,
                            current_by_key,
                            now,
                            strict_identity=False,
                        )
                    if current is not None:
                        raise DelegationGrantConflictError(
                            f"grant already exists: {grant.grant_id!r} in domain {grant.domain_id!r} mission {grant.mission_id!r}",
                        )

                    payload = self._register_payload(candidate)
                    record = self._append_record(records, payload)
                    handle.seek(0, 2)
                    handle.write(_canonical(record) + b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                    new_current = dict(current_by_key)
                    new_current[_grant_key(candidate)] = self._from_record(record)
                    return self._materialize(
                        new_current[_grant_key(candidate)],
                        new_current,
                        now,
                        strict_identity=False,
                    )
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def revoke(
        self,
        grant_id: str,
        *,
        domain_id: str | None = None,
        mission_id: str | None = None,
        reason: str | None = None,
    ) -> AuthoritativeDelegationGrant:
        """Revoke a grant without widening authority."""

        if not isinstance(grant_id, str) or not grant_id.strip():
            raise ValueError("grant_id must be a non-empty string")
        if domain_id is None or mission_id is None:
            raise DelegationGrantDomainError(
                "explicit ControlDomain and mission context is required for grant revocation",
            )
        if not isinstance(domain_id, str) or not domain_id.strip():
            raise ValueError("domain_id must be a non-empty string")
        if not isinstance(mission_id, str) or not mission_id.strip():
            raise ValueError("mission_id must be a non-empty string")
        reason = _parse_reason(reason, "reason")

        now = self._now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._shared_lock(self.domain_registry.path), self._shared_lock(self.identity_registry.path):
            with self.path.open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    handle.seek(0)
                    records = self._decode(handle.read())
                    current_by_key = self._current_by_key(records)
                    key = (domain_id, mission_id, grant_id)
                    current = current_by_key.get(key)
                    if current is None:
                        raise DelegationGrantNotFoundError(
                            f"grant {grant_id!r} is not registered in domain {domain_id!r} mission {mission_id!r}",
                        )
                    if current.status is DelegationGrantStatus.REVOKED:
                        if current.revocation_reason == reason:
                            return self._materialize(
                                current,
                                current_by_key,
                                now,
                                strict_identity=False,
                            )
                        raise DelegationGrantConflictError(
                            "revocation reason conflicts with existing evidence",
                        )

                    self._domain_status(domain_id, strict=True, cache={})
                    current_status = self._effective_status(
                        key,
                        current_by_key,
                        now,
                        strict_identity=True,
                        status_cache={},
                        identity_cache={},
                        domain_cache={},
                        visiting=set(),
                    )
                    if current_status not in {
                        DelegationGrantStatus.ACTIVE,
                        DelegationGrantStatus.PENDING,
                    }:
                        raise DelegationGrantLifecycleError(
                            "grant is not active",
                        )

                    payload = self._revoke_payload(
                        current,
                        revoked_at=now,
                        reason=reason,
                    )
                    record = self._append_record(records, payload)
                    handle.seek(0, 2)
                    handle.write(_canonical(record) + b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                    new_current = dict(current_by_key)
                    new_current[key] = self._from_record(record)
                    return self._materialize(
                        new_current[key],
                        new_current,
                        now,
                        strict_identity=False,
                    )
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def get(
        self,
        grant_id: str,
        *,
        domain_id: str | None = None,
        mission_id: str | None = None,
    ) -> AuthoritativeDelegationGrant:
        """Resolve a single grant by its explicit ControlDomain and mission."""

        if not isinstance(grant_id, str) or not grant_id.strip():
            raise ValueError("grant_id must be a non-empty string")
        if domain_id is None or mission_id is None:
            raise DelegationGrantDomainError(
                "explicit ControlDomain and mission context is required for grant lookup",
            )
        if not isinstance(domain_id, str) or not domain_id.strip():
            raise ValueError("domain_id must be a non-empty string")
        if not isinstance(mission_id, str) or not mission_id.strip():
            raise ValueError("mission_id must be a non-empty string")

        now = self._now()
        with self._shared_lock(self.domain_registry.path), self._shared_lock(self.identity_registry.path):
            records = self._read()
        current_by_key = self._current_by_key(records)
        key = (domain_id, mission_id, grant_id)
        grant = current_by_key.get(key)
        if grant is None:
            raise DelegationGrantNotFoundError(
                f"grant {grant_id!r} is not registered in domain {domain_id!r} mission {mission_id!r}",
            )
        return self._materialize(
            grant,
            current_by_key,
            now,
            strict_identity=False,
        )

    def list_grants(
        self,
        *,
        domain_id: str | None = None,
        mission_id: str | None = None,
        status_filter: DelegationGrantStatus | None = None,
    ) -> tuple[AuthoritativeDelegationGrant, ...]:
        """Return a deterministic inventory of current grants."""

        if domain_id is not None and (not isinstance(domain_id, str) or not domain_id.strip()):
            raise ValueError("domain_id must be a non-empty string")
        if mission_id is not None and (not isinstance(mission_id, str) or not mission_id.strip()):
            raise ValueError("mission_id must be a non-empty string")
        if status_filter is not None and not isinstance(status_filter, DelegationGrantStatus):
            raise TypeError("status_filter must be a DelegationGrantStatus")

        now = self._now()
        with self._shared_lock(self.domain_registry.path), self._shared_lock(self.identity_registry.path):
            records = self._read()
        current_by_key = self._current_by_key(records)
        status_cache: dict[tuple[str, str, str], DelegationGrantStatus] = {}
        identity_cache: dict[tuple[str, str], DelegationGrantStatus] = {}
        domain_cache: dict[str, DelegationGrantStatus] = {}
        grants = [
            self._materialize(
                grant,
                current_by_key,
                now,
                strict_identity=False,
                status_cache=status_cache,
                identity_cache=identity_cache,
                domain_cache=domain_cache,
            )
            for grant in current_by_key.values()
            if (domain_id is None or grant.domain_id == domain_id)
            and (mission_id is None or grant.mission_id == mission_id)
        ]
        if status_filter is not None:
            grants = [grant for grant in grants if grant.status is status_filter]
        grants.sort(key=lambda grant: (grant.domain_id, grant.mission_id, grant.grant_id))
        return tuple(grants)

    def snapshot(self) -> tuple[AuthoritativeDelegationGrant, ...]:
        """Return the full deterministic grant inventory."""

        return self.list_grants()

    def _append_record(
        self,
        records: tuple[dict[str, Any], ...],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        unsigned = {
            "schema_version": _SCHEMA_VERSION,
            "sequence": len(records) + 1,
            "payload": payload,
            "predecessor_authentication_tag": (
                _GENESIS_TAG if not records else records[-1]["authentication_tag"]
            ),
        }
        return {
            **unsigned,
            "authentication_tag": authentication_tag(
                self._integrity_key,
                _AUTHENTICATION_DOMAIN,
                _canonical(unsigned),
            ),
        }

    def _decode(self, raw: bytes) -> tuple[dict[str, Any], ...]:
        if raw and not raw.endswith(b"\n"):
            raise DelegationGrantCorruptionError(
                "delegation grant evidence has an incomplete tail",
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DelegationGrantCorruptionError(
                "delegation grant evidence is not UTF-8",
            ) from exc

        records: list[dict[str, Any]] = []
        current_by_key: dict[tuple[str, str, str], AuthoritativeDelegationGrant] = {}
        for sequence, line in enumerate(text.splitlines(), start=1):
            if not line:
                raise DelegationGrantCorruptionError(
                    "delegation grant evidence has an empty record",
                )
            try:
                record = json.loads(
                    line,
                    object_pairs_hook=_no_duplicate_keys,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        DelegationGrantCorruptionError(
                            "delegation grant evidence is non-finite",
                        )
                    ),
                )
            except (json.JSONDecodeError, DelegationGrantCorruptionError) as exc:
                raise DelegationGrantCorruptionError(
                    "delegation grant evidence is malformed",
                ) from exc

            if not isinstance(record, dict) or set(record) != _FIELDS:
                raise DelegationGrantCorruptionError(
                    "delegation grant evidence schema is invalid",
                )
            if record["schema_version"] != _SCHEMA_VERSION:
                raise DelegationGrantCorruptionError(
                    "delegation grant evidence schema is invalid",
                )
            if not _is_int(record["sequence"]) or record["sequence"] != sequence:
                raise DelegationGrantCorruptionError(
                    "delegation grant sequence is invalid",
                )
            if not isinstance(record["payload"], dict) or set(record["payload"]) != _PAYLOAD_FIELDS:
                raise DelegationGrantCorruptionError(
                    "delegation grant payload schema is invalid",
                )

            unsigned = dict(record)
            claimed_tag = unsigned.pop("authentication_tag")
            if not authenticates(
                self._integrity_key,
                _AUTHENTICATION_DOMAIN,
                _canonical(unsigned),
                claimed_tag,
            ):
                raise DelegationGrantCorruptionError(
                    "delegation grant authentication failed",
                )

            if (_canonical(record) + b"\n").decode("utf-8") != line + "\n":
                raise DelegationGrantCorruptionError(
                    "delegation grant evidence is not canonical",
                )

            grant = self._from_record(record)
            key = _grant_key(grant)
            previous = current_by_key.get(key)
            if previous is not None:
                if grant.grant_fingerprint != previous.grant_fingerprint:
                    raise DelegationGrantCorruptionError(
                        "delegation grant fingerprint changed unexpectedly",
                    )
                if grant.domain_id != previous.domain_id or grant.mission_id != previous.mission_id:
                    raise DelegationGrantCorruptionError(
                        "delegation grant scope changed unexpectedly",
                    )
                if grant.grantor_identity != previous.grantor_identity or grant.grantee_identity != previous.grantee_identity:
                    raise DelegationGrantCorruptionError(
                        "delegation grant identity binding changed unexpectedly",
                    )
                if grant.capabilities != previous.capabilities:
                    raise DelegationGrantCorruptionError(
                        "delegation grant capabilities changed unexpectedly",
                    )
                if grant.resource_scope != previous.resource_scope:
                    raise DelegationGrantCorruptionError(
                        "delegation grant resource scope changed unexpectedly",
                    )
                if grant.parent_grant_id != previous.parent_grant_id:
                    raise DelegationGrantCorruptionError(
                        "delegation grant parent reference changed unexpectedly",
                    )
                if grant.parent_grant_fingerprint != previous.parent_grant_fingerprint:
                    raise DelegationGrantCorruptionError(
                        "delegation grant parent fingerprint changed unexpectedly",
                    )
                if grant.created_at != previous.created_at:
                    raise DelegationGrantCorruptionError(
                        "delegation grant creation timestamp changed unexpectedly",
                    )
                if grant.effective_at != previous.effective_at:
                    raise DelegationGrantCorruptionError(
                        "delegation grant effective timestamp changed unexpectedly",
                    )
                if grant.expires_at != previous.expires_at:
                    raise DelegationGrantCorruptionError(
                        "delegation grant expiration changed unexpectedly",
                    )
                if _status_order(grant.status) < _status_order(previous.status):
                    raise DelegationGrantCorruptionError(
                        "delegation grant lifecycle regressed unexpectedly",
                    )
                if grant.status is DelegationGrantStatus.REVOKED:
                    if grant.revoked_at is None or grant.revocation_reason is None:
                        raise DelegationGrantCorruptionError(
                            "delegation grant revocation metadata is invalid",
                        )
                else:
                    if grant.revoked_at is not None or grant.revocation_reason is not None:
                        raise DelegationGrantCorruptionError(
                            "delegation grant lifecycle metadata is invalid",
                        )
                if grant.status is DelegationGrantStatus.REVOKED and previous.status is DelegationGrantStatus.REVOKED:
                    if (
                        grant.revoked_at != previous.revoked_at
                        or grant.revocation_reason != previous.revocation_reason
                    ):
                        raise DelegationGrantCorruptionError(
                            "delegation grant revocation evidence changed unexpectedly",
                        )

            if grant.parent_grant_id is not None:
                # Resolve parent deterministically
                try:
                    parent_key, parent = _resolve_parent_deterministic(
                        grant.domain_id,
                        grant.parent_grant_id,
                        grant.parent_grant_fingerprint,
                        grant.mission_id,
                        current_by_key,
                    )
                except DelegationGrantNotFoundError:
                    raise DelegationGrantCorruptionError(
                        "delegation grant history references a missing parent grant",
                    )
                if grant.grantor_identity != parent.grantee_identity:
                    raise DelegationGrantCorruptionError(
                        "delegation grant attenuation origin is invalid",
                    )
                if parent.mission_id is not None and grant.mission_id != parent.mission_id:
                    raise DelegationGrantCorruptionError(
                        "delegation grant mission binding widens parent authority",
                    )
                if not _capabilities_non_widening(grant.capabilities, parent.capabilities):
                    raise DelegationGrantCorruptionError(
                        "delegation grant capabilities widen parent authority",
                    )
                if not _resource_scope_non_widening(grant.resource_scope, parent.resource_scope):
                    raise DelegationGrantCorruptionError(
                        "delegation grant resource scope widens parent authority",
                    )
                if grant.effective_at < parent.effective_at or grant.expires_at > parent.expires_at:
                    raise DelegationGrantCorruptionError(
                        "delegation grant timing widens parent authority",
                    )
                if parent.status is DelegationGrantStatus.REVOKED and grant.status is DelegationGrantStatus.ACTIVE:
                    raise DelegationGrantCorruptionError(
                        "delegation grant was appended after its parent was revoked",
                    )

            current_by_key[key] = grant
            records.append(record)

        return tuple(records)

    def _read(self) -> tuple[dict[str, Any], ...]:
        if not self.path.exists():
            return ()
        with self.path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return self._decode(handle.read())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _current_by_key(
        self,
        records: tuple[dict[str, Any], ...],
    ) -> dict[tuple[str, str, str], AuthoritativeDelegationGrant]:
        current: dict[tuple[str, str, str], AuthoritativeDelegationGrant] = {}
        for record in records:
            grant = self._from_record(record)
            current[_grant_key(grant)] = grant
        return current

    @staticmethod
    def _from_record(record: dict[str, Any]) -> AuthoritativeDelegationGrant:
        payload = record["payload"]

        if not isinstance(payload.get("capabilities"), list):
            raise DelegationGrantCorruptionError("capabilities must be a list")
        if not isinstance(payload.get("resource_scope"), list):
            raise DelegationGrantCorruptionError("resource_scope must be a list")

        capabilities_list = payload["capabilities"]
        scope_list = payload["resource_scope"]

        if sorted(capabilities_list) != capabilities_list:
            raise DelegationGrantCorruptionError("capabilities must be sorted")
        if sorted(scope_list) != scope_list:
            raise DelegationGrantCorruptionError("resource_scope must be sorted")

        mission_id_value = payload.get("mission_id")
        grant = AuthoritativeDelegationGrant(
            grant_id=_require_text(payload["grant_id"], "grant_id", max_length=255),
            domain_id=_require_text(payload["domain_id"], "domain_id", max_length=255),
            mission_id=None if mission_id_value is None else _require_text(mission_id_value, "mission_id", max_length=255),
            grantor_identity=_require_text(
                payload["grantor_identity"],
                "grantor_identity",
                max_length=255,
            ),
            grantee_identity=_require_text(
                payload["grantee_identity"],
                "grantee_identity",
                max_length=255,
            ),
            capabilities=DelegationCapabilities(capabilities_list),
            resource_scope=DelegationScope(scope_list),
            parent_grant_id=(
                None
                if payload["parent_grant_id"] is None
                else _require_text(
                    payload["parent_grant_id"],
                    "parent_grant_id",
                    max_length=255,
                )
            ),
            parent_grant_fingerprint=(
                None
                if payload["parent_grant_fingerprint"] is None
                else _require_text(
                    payload["parent_grant_fingerprint"],
                    "parent_grant_fingerprint",
                    max_length=255,
                )
            ),
            created_at=_parse_timestamp(payload["created_at"], "created_at"),
            effective_at=_parse_timestamp(payload["effective_at"], "effective_at"),
            expires_at=_parse_timestamp(payload["expires_at"], "expires_at"),
            status=_parse_status(payload["status"]),
            grant_fingerprint=_require_text(
                payload["grant_fingerprint"],
                "grant_fingerprint",
                max_length=255,
            ),
            revoked_at=_parse_optional_timestamp(payload["revoked_at"], "revoked_at"),
            revocation_reason=_parse_reason(
                payload["revocation_reason"],
                "revocation_reason",
            ),
        )
        if grant.status is DelegationGrantStatus.REVOKED:
            if grant.revoked_at is None or grant.revocation_reason is None:
                raise DelegationGrantCorruptionError(
                    "delegation grant revocation metadata is invalid",
                )
        else:
            if grant.revoked_at is not None or grant.revocation_reason is not None:
                raise DelegationGrantCorruptionError(
                    "delegation grant lifecycle metadata is invalid",
                )
        return grant


__all__ = [
    "DelegationGrantConflictError",
    "DelegationGrantCorruptionError",
    "DelegationGrantDomainError",
    "DelegationGrantError",
    "DelegationGrantIdentityError",
    "DelegationGrantLifecycleError",
    "DelegationGrantNotFoundError",
    "DelegationGrantRegistry",
    "DelegationGrantScopeError",
]
