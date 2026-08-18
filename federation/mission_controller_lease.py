"""Mission controller lease ownership and generation tracking.

MissionaryX v0.1 — Mission Controller Lease Phase A

Establishes durable controller ownership generations for long-running missions.
Each controller lease is identified by a monotonically increasing generation number
that prevents stale controllers from affecting mission state.

This module defines:
- Lease value types (ownership records)
- Lease duration policy (bounded lease lifetime)
- Typed errors for lease conflicts and violations

Critical invariants:
- Controller generations are strictly monotonic (never decrease or reuse)
- Lease ownership does not imply mutation authority (fencing pending Phase B)
- Lease expiry/release requires generation N+1 for reacquisition
- Same controller reacquiring active lease returns same generation (idempotent)
- Clock rollback is rejected
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from federation.control_domain import validate_domain_id

# Lease duration policy
DEFAULT_CONTROLLER_LEASE_DURATION = timedelta(seconds=30)
MAX_CONTROLLER_LEASE_DURATION = timedelta(minutes=5)

# Identifier length bounds (consistent with Mission Runtime conventions)
MAX_IDENTIFIER_LENGTH = 255


class MissionControllerLeaseError(Exception):
    """Base error for mission controller lease operations."""


class MissionControllerLeaseConflictError(MissionControllerLeaseError):
    """Raised when controller lease is held by another controller."""


class MissionControllerLeaseNotFoundError(MissionControllerLeaseError):
    """Raised when no controller lease exists for the mission."""


class MissionControllerLeaseExpiredError(MissionControllerLeaseError):
    """Raised when attempting to operate on an expired lease."""


class MissionControllerLeaseStaleError(MissionControllerLeaseError):
    """Raised when operation uses a stale generation."""


class MissionControllerClockError(MissionControllerLeaseError):
    """Raised when clock rollback is detected."""


def validate_controller_id(value: object) -> str:
    """Validate a controller identifier.

    Args:
        value: Controller identifier to validate

    Returns:
        Validated controller identifier

    Raises:
        ValueError: If identifier is invalid
        TypeError: If identifier is not a string
    """
    if not isinstance(value, str):
        raise TypeError("controller_id must be a string")
    if not value.strip():
        raise ValueError("controller_id must be a non-empty string")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"controller_id exceeds {MAX_IDENTIFIER_LENGTH} characters")
    if "\x00" in value:
        raise ValueError("controller_id must not contain NUL bytes")
    return value


def validate_generation(value: object) -> int:
    """Validate a controller generation number.

    Args:
        value: Generation to validate

    Returns:
        Validated generation number

    Raises:
        ValueError: If generation is invalid
        TypeError: If generation is not exactly int
    """
    if type(value) is not int:
        raise TypeError("generation must be an exact int (not bool, not subclass)")
    if value < 1:
        raise ValueError("generation must be a positive integer (>= 1)")
    return value


def validate_lease_duration(duration: timedelta) -> timedelta:
    """Validate a lease duration.

    Args:
        duration: Lease duration to validate

    Returns:
        Validated lease duration

    Raises:
        ValueError: If duration is invalid
        TypeError: If duration is not a timedelta
    """
    if not isinstance(duration, timedelta):
        raise TypeError("lease_duration must be a timedelta")
    if duration <= timedelta(0):
        raise ValueError("lease_duration must be positive")
    if duration > MAX_CONTROLLER_LEASE_DURATION:
        raise ValueError(
            f"lease_duration must not exceed {MAX_CONTROLLER_LEASE_DURATION.total_seconds()}s"
        )
    return duration


def normalize_lease_timestamp(dt: datetime) -> datetime:
    """Normalize a lease timestamp to UTC.

    Args:
        dt: Timestamp to normalize

    Returns:
        UTC-normalized timestamp

    Raises:
        ValueError: If timestamp is naive
        TypeError: If not a datetime
    """
    if not isinstance(dt, datetime):
        raise TypeError("timestamp must be a datetime")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class MissionControllerLease:
    """Immutable controller lease ownership record.

    Represents a single controller generation's ownership of a mission.
    All controller lease records are append-only; generations are never reused.

    Attributes:
        control_domain: ControlDomain identifier
        mission_id: Mission identifier
        controller_id: Controller instance identifier
        generation: Monotonic generation number (>= 1)
        acquired_at: UTC timestamp when lease was first acquired
        renewed_at: UTC timestamp of most recent renewal
        expires_at: UTC timestamp when lease expires
        released_at: UTC timestamp when lease was released (None if active)
    """

    control_domain: str
    mission_id: str
    controller_id: str
    generation: int
    acquired_at: datetime
    renewed_at: datetime
    expires_at: datetime
    released_at: datetime | None

    def __post_init__(self) -> None:
        """Validate lease invariants."""
        # Validate identifiers
        object.__setattr__(self, "control_domain", validate_domain_id(self.control_domain, "control_domain"))

        if not isinstance(self.mission_id, str) or not self.mission_id.strip():
            raise ValueError("mission_id must be a non-empty string")
        if len(self.mission_id) > MAX_IDENTIFIER_LENGTH:
            raise ValueError(f"mission_id exceeds {MAX_IDENTIFIER_LENGTH} characters")
        if "\x00" in self.mission_id:
            raise ValueError("mission_id must not contain NUL bytes")

        object.__setattr__(self, "controller_id", validate_controller_id(self.controller_id))
        object.__setattr__(self, "generation", validate_generation(self.generation))

        # Normalize timestamps
        object.__setattr__(self, "acquired_at", normalize_lease_timestamp(self.acquired_at))
        object.__setattr__(self, "renewed_at", normalize_lease_timestamp(self.renewed_at))
        object.__setattr__(self, "expires_at", normalize_lease_timestamp(self.expires_at))

        if self.released_at is not None:
            object.__setattr__(self, "released_at", normalize_lease_timestamp(self.released_at))

        # Validate timestamp ordering
        if self.acquired_at > self.renewed_at:
            raise ValueError("acquired_at must not be after renewed_at")
        if self.renewed_at >= self.expires_at:
            raise ValueError("renewed_at must be before expires_at")
        if self.released_at is not None and self.released_at < self.renewed_at:
            raise ValueError("released_at must not be before renewed_at")

    def is_active(self, now: datetime) -> bool:
        """Check if lease is currently active.

        Args:
            now: Current timestamp

        Returns:
            True if lease is active (not released and not expired)
        """
        if self.released_at is not None:
            return False
        return now < self.expires_at

    def is_expired(self, now: datetime) -> bool:
        """Check if lease has expired.

        Args:
            now: Current timestamp

        Returns:
            True if lease has expired
        """
        return now >= self.expires_at

    def is_released(self) -> bool:
        """Check if lease has been explicitly released.

        Returns:
            True if lease has been released
        """
        return self.released_at is not None


__all__ = [
    "DEFAULT_CONTROLLER_LEASE_DURATION",
    "MAX_CONTROLLER_LEASE_DURATION",
    "MAX_IDENTIFIER_LENGTH",
    "MissionControllerLease",
    "MissionControllerLeaseError",
    "MissionControllerLeaseConflictError",
    "MissionControllerLeaseNotFoundError",
    "MissionControllerLeaseExpiredError",
    "MissionControllerLeaseStaleError",
    "MissionControllerClockError",
    "validate_controller_id",
    "validate_generation",
    "validate_lease_duration",
    "normalize_lease_timestamp",
]
