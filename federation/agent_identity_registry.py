"""Durable authenticated registry for ControlDomain-scoped agent identities."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from federation.agent_identity import (
    AgentIdentity,
    AgentIdentityLifecycle,
    AuthoritativeAgentIdentity,
    identity_fingerprint,
)
from federation.control_domain import DomainLifecycle
from federation.control_domain_registry import (
    DomainNotFoundError,
    DurableControlDomainRegistry,
)
from federation.file_lock import fcntl
from federation.integrity import authentication_tag, authenticates, require_integrity_key


_SCHEMA_VERSION = 1
_AUTHENTICATION_DOMAIN = b"raghub.agent-identity.v1"
_GENESIS_TAG = "0" * 64
_FIELDS = {
    "schema_version",
    "sequence",
    "payload",
    "predecessor_authentication_tag",
    "authentication_tag",
}
_PAYLOAD_FIELDS = {
    "agent_id",
    "domain_id",
    "domain_fingerprint",
    "name",
    "identity_fingerprint",
    "lifecycle",
    "created_at",
    "last_transition_at",
    "revoked_at",
    "archived_at",
    "revocation_reason",
    "archive_reason",
}


class AgentIdentityError(Exception):
    """Base error for durable agent identity operations."""


class AgentIdentityNotFoundError(AgentIdentityError):
    """Raised when an agent identity cannot be resolved."""


class AgentIdentityConflictError(AgentIdentityError):
    """Raised when an agent identity conflicts with authoritative evidence."""


class AgentIdentityDomainError(AgentIdentityError):
    """Raised when the referenced control domain is missing or inactive."""


class AgentIdentityLifecycleError(AgentIdentityError):
    """Raised when a lifecycle transition violates non-widening rules."""


class AgentIdentityCorruptionError(AgentIdentityError):
    """Raised when durable agent identity evidence cannot be trusted."""


class AgentIdentityClockRollbackError(AgentIdentityError):
    """Raised when an authoritative timestamp moves backwards."""


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
            raise AgentIdentityCorruptionError(
                "agent identity evidence has duplicate JSON keys",
            )
        result[key] = value
    return result


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AgentIdentityCorruptionError(f"{field_name} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AgentIdentityCorruptionError(f"{field_name} is invalid") from exc
    if parsed.tzinfo is None:
        raise AgentIdentityCorruptionError(f"{field_name} must include a timezone")
    canonical = parsed.astimezone(timezone.utc)
    if canonical.isoformat() != value:
        raise AgentIdentityCorruptionError(f"{field_name} is not canonical UTC")
    return canonical


def _parse_optional_timestamp(value: Any, field_name: str) -> datetime | None:
    return None if value is None else _parse_timestamp(value, field_name)


def _parse_reason(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise AgentIdentityCorruptionError(f"{field_name} is invalid")
    return value


def _order(lifecycle: AgentIdentityLifecycle) -> int:
    return {
        AgentIdentityLifecycle.ACTIVE: 0,
        AgentIdentityLifecycle.REVOKED: 1,
        AgentIdentityLifecycle.ARCHIVED: 2,
    }[lifecycle]


class DurableAgentIdentityRegistry:
    """Append-only authenticated registry for durable agent identities."""

    def __init__(
        self,
        path: Path,
        *,
        domain_registry: DurableControlDomainRegistry,
        integrity_key: bytes,
        clock=None,
    ) -> None:
        self.path = Path(path)
        if not isinstance(domain_registry, DurableControlDomainRegistry):
            raise TypeError("domain_registry must be a DurableControlDomainRegistry")
        self.domain_registry = domain_registry
        self._integrity_key = require_integrity_key(integrity_key)
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("agent identity clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    @contextmanager
    def _domain_registry_lock(self):
        path = self.domain_registry.path
        if not path.exists():
            yield
            return
        with path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _domain_fingerprint(self, domain_id: str) -> str:
        try:
            domain = self.domain_registry.get(domain_id)
        except DomainNotFoundError as exc:
            raise AgentIdentityDomainError(
                f"control domain {domain_id!r} is not registered",
            ) from exc
        if domain.lifecycle is not DomainLifecycle.ACTIVE:
            raise AgentIdentityDomainError(
                f"control domain {domain_id!r} is not active",
            )
        return domain.domain_fingerprint

    def register(self, identity: AgentIdentity) -> AuthoritativeAgentIdentity:
        """Register a new durable agent identity or return an exact duplicate."""

        if not isinstance(identity, AgentIdentity):
            raise TypeError("identity must be an AgentIdentity")
        if identity.lifecycle is not AgentIdentityLifecycle.ACTIVE:
            raise AgentIdentityLifecycleError(
                "registered identities must start in active state",
            )

        now = self._now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._domain_registry_lock():
            with self.path.open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    handle.seek(0)
                    records = self._decode(handle.read())
                    current = self._current_by_key(records)
                    domain_fingerprint = self._domain_fingerprint(identity.domain_id)
                    key = (identity.domain_id, identity.agent_id)
                    existing = current.get(key)
                    if existing is not None:
                        candidate_fingerprint = identity_fingerprint(
                            agent_id=identity.agent_id,
                            domain_id=identity.domain_id,
                            domain_fingerprint=domain_fingerprint,
                            name=identity.name,
                            created_at=identity.created_at,
                        )
                        if (
                            existing.lifecycle is AgentIdentityLifecycle.ACTIVE
                            and existing.identity_fingerprint == candidate_fingerprint
                        ):
                            return existing
                        raise AgentIdentityConflictError(
                            f"agent identity already exists: {identity.agent_id!r} in domain {identity.domain_id!r}",
                        )

                    payload = self._register_payload(
                        identity,
                        domain_fingerprint=domain_fingerprint,
                        last_transition_at=now,
                    )
                    record = self._append_record(records, payload)
                    handle.seek(0, 2)
                    handle.write(_canonical(record) + b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                    return self._from_record(record)
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def get(
        self,
        agent_id: str,
        *,
        domain_id: str | None = None,
    ) -> AuthoritativeAgentIdentity:
        """Resolve a single authoritative identity by agent id and optional domain."""

        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("agent_id must be a non-empty string")
        records = self._read()
        current = self._current_by_key(records)
        matches = [
            identity
            for (record_domain, record_agent), identity in current.items()
            if record_agent == agent_id and (domain_id is None or record_domain == domain_id)
        ]
        if domain_id is None and len(matches) > 1:
            raise AgentIdentityConflictError(
                f"agent_id {agent_id!r} is ambiguous across domains; provide domain_id",
            )
        if not matches:
            raise AgentIdentityNotFoundError(
                f"agent identity not found: {agent_id!r}",
            )
        return matches[0]

    def list_identities(
        self,
        *,
        domain_id: str | None = None,
        status_filter: AgentIdentityLifecycle | None = None,
    ) -> tuple[AuthoritativeAgentIdentity, ...]:
        """Return deterministic inventory snapshots for the latest state of each identity."""

        records = self._read()
        current = self._current_by_key(records)
        identities = [
            identity
            for (record_domain, _), identity in current.items()
            if domain_id is None or record_domain == domain_id
        ]
        if status_filter is not None:
            if not isinstance(status_filter, AgentIdentityLifecycle):
                raise TypeError("status_filter must be an AgentIdentityLifecycle")
            identities = [item for item in identities if item.lifecycle is status_filter]
        identities.sort(key=lambda item: (item.domain_id, item.agent_id))
        return tuple(identities)

    def update_lifecycle(
        self,
        agent_id: str,
        new_lifecycle: AgentIdentityLifecycle,
        *,
        domain_id: str | None = None,
        reason: str | None = None,
    ) -> AuthoritativeAgentIdentity:
        """Transition identity lifecycle without widening authority."""

        if not isinstance(new_lifecycle, AgentIdentityLifecycle):
            raise TypeError("new_lifecycle must be an AgentIdentityLifecycle")
        if domain_id is None:
            raise AgentIdentityDomainError(
                "explicit ControlDomain context is required for lifecycle mutations",
            )
        reason = _parse_reason(reason, "reason")
        current = self.get(agent_id, domain_id=domain_id)
        with self._domain_registry_lock():
            self._domain_fingerprint(domain_id)

            if _order(new_lifecycle) < _order(current.lifecycle):
                raise AgentIdentityLifecycleError(
                    f"Cannot transition from {current.lifecycle.value} to {new_lifecycle.value} (non-widening invariant)",
                )

            if current.lifecycle is new_lifecycle:
                if new_lifecycle is AgentIdentityLifecycle.REVOKED and current.revocation_reason != reason:
                    raise AgentIdentityConflictError(
                        "revocation reason conflicts with existing evidence",
                    )
                if new_lifecycle is AgentIdentityLifecycle.ARCHIVED and current.archive_reason != reason:
                    raise AgentIdentityConflictError(
                        "archive reason conflicts with existing evidence",
                    )
                return current

            now = self._now()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    handle.seek(0)
                    records = self._decode(handle.read())
                    current_by_key = self._current_by_key(records)
                    key = (current.domain_id, current.agent_id)
                    latest = current_by_key.get(key)
                    if latest is None or latest != current:
                        raise AgentIdentityConflictError(
                            "identity evidence changed during lifecycle transition",
                        )
                    self._domain_fingerprint(domain_id)

                    payload = self._transition_payload(
                        current,
                        new_lifecycle=new_lifecycle,
                        last_transition_at=now,
                        reason=reason,
                    )
                    record = self._append_record(records, payload)
                    handle.seek(0, 2)
                    handle.write(_canonical(record) + b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                    return self._from_record(record)
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def revoke(
        self,
        agent_id: str,
        *,
        domain_id: str | None = None,
        reason: str | None = None,
    ) -> AuthoritativeAgentIdentity:
        """Convenience wrapper for revocation."""

        return self.update_lifecycle(
            agent_id,
            AgentIdentityLifecycle.REVOKED,
            domain_id=domain_id,
            reason=reason,
        )

    def archive(
        self,
        agent_id: str,
        *,
        domain_id: str | None = None,
        reason: str | None = None,
    ) -> AuthoritativeAgentIdentity:
        """Convenience wrapper for archival."""

        return self.update_lifecycle(
            agent_id,
            AgentIdentityLifecycle.ARCHIVED,
            domain_id=domain_id,
            reason=reason,
        )

    def snapshot(self) -> tuple[AuthoritativeAgentIdentity, ...]:
        """Return a deterministic inventory snapshot of the current registry state."""

        return self.list_identities()

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
            raise AgentIdentityCorruptionError("agent identity evidence has an incomplete tail")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AgentIdentityCorruptionError("agent identity evidence is not UTF-8") from exc

        records: list[dict[str, Any]] = []
        current_by_key: dict[tuple[str, str], AuthoritativeAgentIdentity] = {}
        for sequence, line in enumerate(text.splitlines(), start=1):
            if not line:
                raise AgentIdentityCorruptionError("agent identity evidence has an empty record")
            try:
                record = json.loads(
                    line,
                    object_pairs_hook=_no_duplicate_keys,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        AgentIdentityCorruptionError("agent identity evidence is non-finite")
                    ),
                )
            except (json.JSONDecodeError, AgentIdentityCorruptionError) as exc:
                raise AgentIdentityCorruptionError("agent identity evidence is malformed") from exc

            if not isinstance(record, dict) or set(record) != _FIELDS:
                raise AgentIdentityCorruptionError("agent identity evidence schema is invalid")
            if record["schema_version"] != _SCHEMA_VERSION:
                raise AgentIdentityCorruptionError("agent identity evidence schema is invalid")
            if not _is_int(record["sequence"]) or record["sequence"] != sequence:
                raise AgentIdentityCorruptionError("agent identity sequence is invalid")
            if not isinstance(record["payload"], dict) or set(record["payload"]) != _PAYLOAD_FIELDS:
                raise AgentIdentityCorruptionError("agent identity payload schema is invalid")

            unsigned = dict(record)
            claimed_tag = unsigned.pop("authentication_tag")
            if not authenticates(
                self._integrity_key,
                _AUTHENTICATION_DOMAIN,
                _canonical(unsigned),
                claimed_tag,
            ):
                raise AgentIdentityCorruptionError("agent identity authentication failed")

            if (_canonical(record) + b"\n").decode("utf-8") != line + "\n":
                raise AgentIdentityCorruptionError("agent identity evidence is not canonical")

            identity = self._from_record(record)
            key = (identity.domain_id, identity.agent_id)
            previous = current_by_key.get(key)
            if previous is not None:
                if identity.identity_fingerprint != previous.identity_fingerprint:
                    raise AgentIdentityCorruptionError(
                        "agent identity fingerprint changed unexpectedly",
                    )
                if _order(identity.lifecycle) < _order(previous.lifecycle):
                    raise AgentIdentityCorruptionError(
                        "non-widening lifecycle violation",
                    )
                if identity.created_at != previous.created_at:
                    raise AgentIdentityCorruptionError(
                        "agent identity creation timestamp changed unexpectedly",
                    )
                if identity.domain_fingerprint != previous.domain_fingerprint:
                    raise AgentIdentityCorruptionError(
                        "agent identity domain binding changed unexpectedly",
                    )
                if identity.name != previous.name:
                    raise AgentIdentityCorruptionError(
                        "agent identity name changed unexpectedly",
                    )
            current_by_key[key] = identity
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
    ) -> dict[tuple[str, str], AuthoritativeAgentIdentity]:
        current: dict[tuple[str, str], AuthoritativeAgentIdentity] = {}
        for record in records:
            identity = self._from_record(record)
            current[(identity.domain_id, identity.agent_id)] = identity
        return current

    def _register_payload(
        self,
        identity: AgentIdentity,
        *,
        domain_fingerprint: str,
        last_transition_at: datetime,
    ) -> dict[str, Any]:
        payload = {
            "agent_id": identity.agent_id,
            "domain_id": identity.domain_id,
            "domain_fingerprint": domain_fingerprint,
            "name": identity.name,
            "identity_fingerprint": identity_fingerprint(
                agent_id=identity.agent_id,
                domain_id=identity.domain_id,
                domain_fingerprint=domain_fingerprint,
                name=identity.name,
                created_at=identity.created_at,
            ),
            "lifecycle": identity.lifecycle.value,
            "created_at": identity.created_at.astimezone(timezone.utc).isoformat(),
            "last_transition_at": last_transition_at.astimezone(timezone.utc).isoformat(),
            "revoked_at": None,
            "archived_at": None,
            "revocation_reason": None,
            "archive_reason": None,
        }
        return payload

    def _transition_payload(
        self,
        current: AuthoritativeAgentIdentity,
        *,
        new_lifecycle: AgentIdentityLifecycle,
        last_transition_at: datetime,
        reason: str | None,
    ) -> dict[str, Any]:
        payload = current.to_dict()
        payload["lifecycle"] = new_lifecycle.value
        payload["last_transition_at"] = last_transition_at.astimezone(timezone.utc).isoformat()
        payload["revocation_reason"] = current.revocation_reason
        payload["archive_reason"] = current.archive_reason
        payload["revoked_at"] = None if current.revoked_at is None else current.revoked_at.astimezone(timezone.utc).isoformat()
        payload["archived_at"] = None if current.archived_at is None else current.archived_at.astimezone(timezone.utc).isoformat()
        if new_lifecycle is AgentIdentityLifecycle.REVOKED:
            payload["revoked_at"] = last_transition_at.astimezone(timezone.utc).isoformat()
            payload["revocation_reason"] = reason
        elif new_lifecycle is AgentIdentityLifecycle.ARCHIVED:
            payload["archived_at"] = last_transition_at.astimezone(timezone.utc).isoformat()
            payload["archive_reason"] = reason
            if current.lifecycle is AgentIdentityLifecycle.REVOKED and payload["revoked_at"] is None:
                payload["revoked_at"] = current.revoked_at.astimezone(timezone.utc).isoformat() if current.revoked_at else None
                payload["revocation_reason"] = current.revocation_reason
        return payload

    @staticmethod
    def _from_record(record: dict[str, Any]) -> AuthoritativeAgentIdentity:
        payload = record["payload"]
        return AuthoritativeAgentIdentity(
            agent_id=payload["agent_id"],
            domain_id=payload["domain_id"],
            domain_fingerprint=payload["domain_fingerprint"],
            name=payload["name"],
            identity_fingerprint=payload["identity_fingerprint"],
            lifecycle=AgentIdentityLifecycle(payload["lifecycle"]),
            created_at=_parse_timestamp(payload["created_at"], "created_at"),
            last_transition_at=_parse_timestamp(payload["last_transition_at"], "last_transition_at"),
            revoked_at=_parse_optional_timestamp(payload["revoked_at"], "revoked_at"),
            archived_at=_parse_optional_timestamp(payload["archived_at"], "archived_at"),
            revocation_reason=_parse_reason(payload["revocation_reason"], "revocation_reason"),
            archive_reason=_parse_reason(payload["archive_reason"], "archive_reason"),
        )


__all__ = [
    "AgentIdentityConflictError",
    "AgentIdentityCorruptionError",
    "AgentIdentityDomainError",
    "AgentIdentityError",
    "AgentIdentityLifecycleError",
    "AgentIdentityNotFoundError",
    "DurableAgentIdentityRegistry",
]
