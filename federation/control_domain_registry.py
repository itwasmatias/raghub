"""Durable authenticated registry for control domain lifecycle management."""

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from federation.control_domain import ControlDomain, DomainLifecycle
from federation.file_lock import fcntl
from federation.integrity import (
    authentication_tag,
    authenticates,
    require_integrity_key,
)


_SCHEMA_VERSION = 1
_AUTHENTICATION_DOMAIN = b"raghub.control-domain.v1"
_FIELDS = {
    "schema_version",
    "sequence",
    "domain_id",
    "domain_fingerprint",
    "name",
    "owner",
    "lifecycle",
    "created_at",
    "last_transition_at",
    "authentication_tag",
}


class ControlDomainRegistryError(Exception):
    """Base error for control domain registry operations."""


class DomainNotFoundError(ControlDomainRegistryError):
    """Raised when a domain identity is not found in the registry."""


class DomainConflictError(ControlDomainRegistryError):
    """Raised when domain registration conflicts with existing domain."""


class DomainCorruptionError(ControlDomainRegistryError):
    """Raised when domain evidence cannot be trusted."""


class DomainLifecycleError(ControlDomainRegistryError):
    """Raised when lifecycle transition violates non-widening invariant."""


class DomainClockRollbackError(ControlDomainRegistryError):
    """Raised when timestamp rollback is detected."""


@dataclass(slots=True, frozen=True)
class AuthoritativeDomain:
    """Immutable control domain resolved from durable evidence."""

    domain_id: str
    domain_fingerprint: str
    name: str
    owner: str
    lifecycle: DomainLifecycle
    created_at: datetime
    last_transition_at: datetime


def _canonical(value: dict) -> bytes:
    """Deterministic JSON serialization for fingerprints."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _domain_payload(domain: ControlDomain, last_transition_at: datetime) -> dict:
    """Extract canonical domain payload for fingerprinting."""
    return {
        "domain_id": domain.domain_id,
        "name": domain.name,
        "owner": domain.owner,
        "lifecycle": domain.lifecycle.value,
        "created_at": domain.created_at.isoformat(),
        "last_transition_at": last_transition_at.isoformat(),
    }


def _no_duplicate_keys(pairs):
    """JSON object_pairs_hook that detects duplicate keys."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise DomainCorruptionError("domain evidence is corrupt")
        result[key] = value
    return result


def _is_int(value) -> bool:
    """Check if value is int but not bool."""
    return isinstance(value, int) and not isinstance(value, bool)


class DurableControlDomainRegistry:
    """
    Append-only authenticated registry for control domains.

    Provides:
    - Explicit domain registration with conflict detection
    - Non-widening lifecycle transitions (ACTIVE -> SUSPENDED -> ARCHIVED)
    - Authenticated JSONL evidence with HMAC-SHA256
    - Deterministic domain fingerprints
    - Clock-rollback protection
    - Concurrent registration conflict detection
    """

    def __init__(self, path, *, integrity_key: bytes):
        """
        Initialize the control domain registry.

        Args:
            path: Path to the JSONL evidence file.
            integrity_key: HMAC-SHA256 key (minimum 32 bytes).
        """
        self.path = Path(path)
        self._integrity_key = require_integrity_key(integrity_key)

    def _integrity_key_matches(self, integrity_key: bytes) -> bool:
        """Check if provided integrity key matches registry key."""
        candidate = require_integrity_key(integrity_key)
        return hmac.compare_digest(self._integrity_key, candidate)

    def register(self, domain: ControlDomain) -> AuthoritativeDomain:
        """
        Register a new control domain.

        Args:
            domain: The ControlDomain to register.

        Returns:
            AuthoritativeDomain with durable evidence.

        Raises:
            DomainConflictError: If domain_id already registered.
            DomainClockRollbackError: If timestamp rollback detected.
        """
        if not isinstance(domain, ControlDomain):
            raise TypeError("domain must be a ControlDomain")

        now = datetime.now(timezone.utc)
        payload = _domain_payload(domain, last_transition_at=now)
        fingerprint = hashlib.sha256(_canonical(payload)).hexdigest()

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                records = self._decode(handle.read())
                by_id = {item.domain_id: item for item in records}

                # Check for duplicate domain_id
                if domain.domain_id in by_id:
                    raise DomainConflictError(
                        f"Domain already registered: {domain.domain_id!r}",
                    )

                # Clock-rollback protection: new domain must have timestamp >= latest
                if records:
                    latest_timestamp = max(
                        item.last_transition_at for item in records
                    )
                    if now < latest_timestamp:
                        raise DomainClockRollbackError(
                            f"Clock rollback detected: {now} < {latest_timestamp}",
                        )

                record = {
                    "schema_version": _SCHEMA_VERSION,
                    "sequence": len(records) + 1,
                    "domain_id": domain.domain_id,
                    "domain_fingerprint": fingerprint,
                    **payload,
                }
                record["authentication_tag"] = authentication_tag(
                    self._integrity_key,
                    _AUTHENTICATION_DOMAIN,
                    _canonical(record),
                )

                handle.seek(0, 2)
                handle.write(_canonical(record) + b"\n")
                handle.flush()
                __import__("os").fsync(handle.fileno())
                return self._from_record(record)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def get(self, domain_id: str) -> AuthoritativeDomain:
        """
        Retrieve a domain by ID.

        Args:
            domain_id: The domain identifier.

        Returns:
            AuthoritativeDomain if found.

        Raises:
            DomainNotFoundError: If domain not found.
        """
        if not isinstance(domain_id, str) or not domain_id.strip():
            raise ValueError("domain_id must be a non-empty string")

        records = self._read()
        matches = [item for item in records if item.domain_id == domain_id]
        if not matches:
            raise DomainNotFoundError(
                f"Domain not found: {domain_id!r}",
            )
        # Return the latest record for this domain_id (for lifecycle updates)
        return matches[-1]

    def list_domains(self) -> tuple[AuthoritativeDomain, ...]:
        """
        List all domains (returns latest state for each domain_id).

        Returns:
            Tuple of AuthoritativeDomain objects.
        """
        records = self._read()
        # Group by domain_id, return latest
        by_id = {}
        for record in records:
            by_id[record.domain_id] = record
        return tuple(by_id.values())

    def update_lifecycle(
        self,
        domain_id: str,
        new_lifecycle: DomainLifecycle,
    ) -> AuthoritativeDomain:
        """
        Update domain lifecycle (non-widening only).

        Args:
            domain_id: The domain to update.
            new_lifecycle: The new lifecycle state.

        Returns:
            Updated AuthoritativeDomain.

        Raises:
            DomainLifecycleError: If transition violates non-widening.
            DomainNotFoundError: If domain not found.
            DomainClockRollbackError: If timestamp rollback detected.
        """
        if not isinstance(new_lifecycle, DomainLifecycle):
            raise TypeError("new_lifecycle must be a DomainLifecycle")

        current = self.get(domain_id)

        # Validate non-widening transition
        order = {
            DomainLifecycle.ACTIVE: 0,
            DomainLifecycle.SUSPENDED: 1,
            DomainLifecycle.ARCHIVED: 2,
        }
        if order[new_lifecycle] < order[current.lifecycle]:
            raise DomainLifecycleError(
                f"Cannot transition from {current.lifecycle.value} to "
                f"{new_lifecycle.value} (non-widening invariant)",
            )

        if current.lifecycle == new_lifecycle:
            # No change needed
            return current

        now = datetime.now(timezone.utc)
        domain = ControlDomain(
            domain_id=current.domain_id,
            name=current.name,
            owner=current.owner,
            lifecycle=new_lifecycle,
            created_at=current.created_at,
        )
        payload = _domain_payload(domain, last_transition_at=now)
        fingerprint = hashlib.sha256(_canonical(payload)).hexdigest()

        with self.path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                records = self._decode(handle.read())

                # Re-validate current state and clock
                latest_timestamp = max(item.last_transition_at for item in records)
                if now < latest_timestamp:
                    raise DomainClockRollbackError(
                        f"Clock rollback detected: {now} < {latest_timestamp}",
                    )

                record = {
                    "schema_version": _SCHEMA_VERSION,
                    "sequence": len(records) + 1,
                    "domain_id": domain.domain_id,
                    "domain_fingerprint": fingerprint,
                    **payload,
                }
                record["authentication_tag"] = authentication_tag(
                    self._integrity_key,
                    _AUTHENTICATION_DOMAIN,
                    _canonical(record),
                )

                handle.seek(0, 2)
                handle.write(_canonical(record) + b"\n")
                handle.flush()
                __import__("os").fsync(handle.fileno())
                return self._from_record(record)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self) -> tuple[AuthoritativeDomain, ...]:
        """Read all domain records from evidence file."""
        if not self.path.exists():
            return ()
        with self.path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return self._decode(handle.read())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _decode(self, data: bytes) -> tuple[AuthoritativeDomain, ...]:
        """Decode and validate JSONL evidence."""
        if not data:
            return ()
        if not data.endswith(b"\n"):
            raise DomainCorruptionError("domain evidence is incomplete")

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DomainCorruptionError("domain evidence is corrupt") from exc

        records = []
        domain_seen = {}  # Track domain_id -> sequence for validation

        for expected_sequence, line in enumerate(text.splitlines(), 1):
            try:
                record = json.loads(line, object_pairs_hook=_no_duplicate_keys)
            except (json.JSONDecodeError, DomainCorruptionError) as exc:
                raise DomainCorruptionError("domain evidence is corrupt") from exc

            if not isinstance(record, dict) or set(record) != _FIELDS:
                raise DomainCorruptionError("domain evidence schema is invalid")

            if record["schema_version"] != _SCHEMA_VERSION:
                raise DomainCorruptionError("domain evidence schema is invalid")

            if not _is_int(record["sequence"]) or (
                record["sequence"] != expected_sequence
            ):
                raise DomainCorruptionError("domain sequence is invalid")

            # Validate authentication tag
            authenticated = dict(record)
            claimed_tag = authenticated.pop("authentication_tag")
            if not authenticates(
                self._integrity_key,
                _AUTHENTICATION_DOMAIN,
                _canonical(authenticated),
                claimed_tag,
            ):
                raise DomainCorruptionError("domain authentication tag is invalid")

            # Validate string fields
            string_fields = ("domain_id", "domain_fingerprint", "name", "owner")
            if any(
                not isinstance(record[field], str) or not record[field].strip()
                for field in string_fields
            ):
                raise DomainCorruptionError("domain identity is invalid")

            # Validate lifecycle
            if not isinstance(record["lifecycle"], str):
                raise DomainCorruptionError("domain lifecycle is invalid")
            try:
                lifecycle = DomainLifecycle(record["lifecycle"])
            except ValueError as exc:
                raise DomainCorruptionError("domain lifecycle is invalid") from exc

            # Validate timestamps
            try:
                created_at = datetime.fromisoformat(record["created_at"])
                last_transition_at = datetime.fromisoformat(
                    record["last_transition_at"]
                )
            except (ValueError, TypeError) as exc:
                raise DomainCorruptionError("domain timestamp is invalid") from exc

            if created_at.tzinfo is None or last_transition_at.tzinfo is None:
                raise DomainCorruptionError("domain timestamp must be timezone-aware")

            # Validate fingerprint
            payload = {
                "domain_id": record["domain_id"],
                "name": record["name"],
                "owner": record["owner"],
                "lifecycle": record["lifecycle"],
                "created_at": record["created_at"],
                "last_transition_at": record["last_transition_at"],
            }
            fingerprint = hashlib.sha256(_canonical(payload)).hexdigest()
            if record["domain_fingerprint"] != fingerprint:
                raise DomainCorruptionError("domain fingerprint is invalid")

            # Validate non-widening lifecycle for updates
            domain_id = record["domain_id"]
            if domain_id in domain_seen:
                # This is an update, validate non-widening
                prev_idx = domain_seen[domain_id]
                prev_lifecycle = records[prev_idx].lifecycle
                order = {
                    DomainLifecycle.ACTIVE: 0,
                    DomainLifecycle.SUSPENDED: 1,
                    DomainLifecycle.ARCHIVED: 2,
                }
                if order[lifecycle] < order[prev_lifecycle]:
                    raise DomainCorruptionError(
                        f"Non-widening lifecycle violation: {prev_lifecycle.value} "
                        f"-> {lifecycle.value}",
                    )
            domain_seen[domain_id] = len(records)

            records.append(self._from_record(record))

        return tuple(records)

    @staticmethod
    def _from_record(record: dict) -> AuthoritativeDomain:
        """Convert JSON record to AuthoritativeDomain."""
        return AuthoritativeDomain(
            domain_id=record["domain_id"],
            domain_fingerprint=record["domain_fingerprint"],
            name=record["name"],
            owner=record["owner"],
            lifecycle=DomainLifecycle(record["lifecycle"]),
            created_at=datetime.fromisoformat(record["created_at"]),
            last_transition_at=datetime.fromisoformat(record["last_transition_at"]),
        )
