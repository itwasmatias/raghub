"""
Control domain model for organizational tenancy boundaries.

A ControlDomain represents an organizational boundary that scopes identity,
authority, and resource access in the RAGHub control plane.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class DomainLifecycle(str, Enum):
    """Lifecycle state of a control domain."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"


def normalize_domain_timestamp(value: datetime) -> datetime:
    """Validate a domain timestamp and normalize it to UTC."""
    if not isinstance(value, datetime):
        raise TypeError("timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(slots=True)
class ControlDomain:
    """
    Represents an organizational tenancy boundary in RAGHub.

    A ControlDomain is the root identity primitive that scopes:
    - Worker node registration
    - Mission ownership
    - Task routing and dispatch
    - Approval authority
    - Evidence boundaries

    Lifecycle is non-widening: ACTIVE -> SUSPENDED -> ARCHIVED.
    Transitions are durable and authenticated.
    """

    domain_id: str
    name: str
    owner: str
    lifecycle: DomainLifecycle = DomainLifecycle.ACTIVE
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    def __post_init__(self) -> None:
        """Validate control domain fields."""
        if not self.domain_id or not isinstance(self.domain_id, str):
            raise ValueError("domain_id must be a non-empty string")
        if not self.name or not isinstance(self.name, str):
            raise ValueError("name must be a non-empty string")
        if not self.owner or not isinstance(self.owner, str):
            raise ValueError("owner must be a non-empty string")

        if not isinstance(self.lifecycle, DomainLifecycle):
            if not isinstance(self.lifecycle, str):
                raise TypeError("lifecycle must be a DomainLifecycle or string value")
            try:
                self.lifecycle = DomainLifecycle(self.lifecycle)
            except ValueError as exc:
                raise ValueError(f"Invalid lifecycle: {self.lifecycle!r}") from exc

        self.created_at = normalize_domain_timestamp(self.created_at)

    def is_active(self) -> bool:
        """Check if the domain is active and accepting operations."""
        return self.lifecycle == DomainLifecycle.ACTIVE

    def suspend(self) -> None:
        """Suspend the domain (non-widening lifecycle transition)."""
        if self.lifecycle == DomainLifecycle.ARCHIVED:
            raise ValueError(
                "Cannot suspend an archived domain (non-widening invariant)",
            )
        self.lifecycle = DomainLifecycle.SUSPENDED

    def archive(self) -> None:
        """Archive the domain (terminal lifecycle state)."""
        self.lifecycle = DomainLifecycle.ARCHIVED
