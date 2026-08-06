"""Immutable public contracts for durable task-dispatch state."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from federation.task_request import AuthorizationLevel


class DispatchStatus(str, Enum):
    OFFERED = "offered"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


TERMINAL_DISPATCH_STATUSES = frozenset(set(DispatchStatus) - {DispatchStatus.OFFERED})


class DispatchEventType(str, Enum):
    OFFER_CREATED = "offer_created"
    OFFER_ACCEPTED = "offer_accepted"
    OFFER_REJECTED = "offer_rejected"
    OFFER_EXPIRED = "offer_expired"
    OFFER_CANCELLED = "offer_cancelled"


class DispatchActorType(str, Enum):
    COORDINATOR = "coordinator"
    WORKER = "worker"
    SYSTEM = "system"


_EVENT_SHAPES = {
    DispatchEventType.OFFER_CREATED: (
        DispatchActorType.COORDINATOR,
        None,
        DispatchStatus.OFFERED,
    ),
    DispatchEventType.OFFER_ACCEPTED: (
        DispatchActorType.WORKER,
        DispatchStatus.OFFERED,
        DispatchStatus.ACCEPTED,
    ),
    DispatchEventType.OFFER_REJECTED: (
        DispatchActorType.WORKER,
        DispatchStatus.OFFERED,
        DispatchStatus.REJECTED,
    ),
    DispatchEventType.OFFER_EXPIRED: (
        DispatchActorType.SYSTEM,
        DispatchStatus.OFFERED,
        DispatchStatus.EXPIRED,
    ),
    DispatchEventType.OFFER_CANCELLED: (
        DispatchActorType.COORDINATOR,
        DispatchStatus.OFFERED,
        DispatchStatus.CANCELLED,
    ),
}


def normalize_dispatch_timestamp(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _enum(value, enum_type, field_name):
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field_name}: {value!r}") from exc


def _identifier(value, field_name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _digest(value, field_name):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a SHA-256 digest")
    return value


def _metadata(value, field_name):
    try:
        result = tuple(tuple(item) for item in value)
    except TypeError as exc:
        raise TypeError(f"{field_name} must contain key/value pairs") from exc
    if any(len(item) != 2 or not isinstance(item[0], str) for item in result):
        raise ValueError(f"{field_name} must contain key/value pairs")
    return result


@dataclass(slots=True, frozen=True)
class DispatchAuditEvent:
    schema_version: int
    sequence: int
    event_type: DispatchEventType
    offer_id: str
    routing_assignment_id: str
    routing_assignment_fingerprint: str
    mission_id: str
    task_id: str
    coordinator_node_id: str
    worker_node_id: str
    required_capabilities: tuple[str, ...]
    previous_state: DispatchStatus | None
    new_state: DispatchStatus
    actor_type: DispatchActorType
    actor_node_id: str | None
    authorization_metadata: tuple[tuple[str, object], ...]
    approval_metadata: tuple[tuple[str, object], ...]
    occurred_at: datetime
    expires_at: datetime
    reason: str | None
    predecessor_digest: str
    resulting_digest: str

    def __post_init__(self):
        if self.schema_version != 1:
            raise ValueError("unsupported schema_version")
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 1
        ):
            raise ValueError("sequence must be a positive integer")
        for field_name in (
            "offer_id",
            "routing_assignment_id",
            "mission_id",
            "task_id",
            "coordinator_node_id",
            "worker_node_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "routing_assignment_fingerprint",
            _digest(
                self.routing_assignment_fingerprint,
                "routing_assignment_fingerprint",
            ),
        )
        object.__setattr__(
            self,
            "event_type",
            _enum(self.event_type, DispatchEventType, "event_type"),
        )
        object.__setattr__(
            self,
            "actor_type",
            _enum(self.actor_type, DispatchActorType, "actor_type"),
        )
        if self.previous_state is not None:
            object.__setattr__(
                self,
                "previous_state",
                _enum(self.previous_state, DispatchStatus, "previous_state"),
            )
        object.__setattr__(
            self,
            "new_state",
            _enum(self.new_state, DispatchStatus, "new_state"),
        )
        capabilities = tuple(self.required_capabilities)
        if capabilities != tuple(sorted(set(capabilities))):
            raise ValueError("required_capabilities must be sorted and unique")
        object.__setattr__(self, "required_capabilities", capabilities)
        object.__setattr__(
            self,
            "authorization_metadata",
            _metadata(self.authorization_metadata, "authorization_metadata"),
        )
        object.__setattr__(
            self,
            "approval_metadata",
            _metadata(self.approval_metadata, "approval_metadata"),
        )
        object.__setattr__(
            self,
            "occurred_at",
            normalize_dispatch_timestamp(self.occurred_at, "occurred_at"),
        )
        object.__setattr__(
            self,
            "expires_at",
            normalize_dispatch_timestamp(self.expires_at, "expires_at"),
        )
        if self.reason is not None:
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("reason must be non-empty when provided")
            if len(self.reason) > 1024:
                raise ValueError("reason must be at most 1024 characters")
        object.__setattr__(
            self,
            "predecessor_digest",
            _digest(self.predecessor_digest, "predecessor_digest"),
        )
        object.__setattr__(
            self,
            "resulting_digest",
            _digest(self.resulting_digest, "resulting_digest"),
        )
        if self.actor_type is DispatchActorType.SYSTEM:
            if self.actor_node_id is not None:
                raise ValueError("system actor must not claim a node ID")
        else:
            _identifier(self.actor_node_id, "actor_node_id")
        if (
            self.actor_type is DispatchActorType.COORDINATOR
            and self.actor_node_id != self.coordinator_node_id
        ):
            raise ValueError("coordinator actor must match coordinator_node_id")
        if (
            self.actor_type is DispatchActorType.WORKER
            and self.actor_node_id != self.worker_node_id
        ):
            raise ValueError("worker actor must match worker_node_id")
        if (
            self.actor_type,
            self.previous_state,
            self.new_state,
        ) != _EVENT_SHAPES[self.event_type]:
            raise ValueError("actor or transition does not match event_type")
        if self.event_type in {
            DispatchEventType.OFFER_REJECTED,
            DispatchEventType.OFFER_EXPIRED,
            DispatchEventType.OFFER_CANCELLED,
        } and self.reason is None:
            raise ValueError("reason is required for this event_type")

    @property
    def from_status(self):
        return self.previous_state

    @property
    def to_status(self):
        return self.new_state

    @property
    def offer_sequence(self):
        return 1 if self.event_type is DispatchEventType.OFFER_CREATED else 2


@dataclass(slots=True, frozen=True)
class DispatchOfferSnapshot:
    offer_id: str
    assignment_id: str
    assignment_fingerprint: str
    task_id: str
    mission_id: str
    coordinator_node_id: str
    worker_node_id: str
    required_capabilities: tuple[str, ...]
    authorization_level: AuthorizationLevel
    approval_required: bool
    authorization_metadata: tuple[tuple[str, object], ...]
    approval_metadata: tuple[tuple[str, object], ...]
    created_at: datetime
    expires_at: datetime
    audit_history: tuple[DispatchAuditEvent, ...]
    status: DispatchStatus = DispatchStatus.OFFERED
    terminal_at: datetime | None = None
    resolution_reason: str | None = None

    def __post_init__(self):
        for field_name in (
            "offer_id",
            "assignment_id",
            "task_id",
            "mission_id",
            "coordinator_node_id",
            "worker_node_id",
        ):
            _identifier(getattr(self, field_name), field_name)
        _digest(self.assignment_fingerprint, "assignment_fingerprint")
        object.__setattr__(
            self,
            "authorization_level",
            _enum(
                self.authorization_level,
                AuthorizationLevel,
                "authorization_level",
            ),
        )
        if not isinstance(self.approval_required, bool):
            raise TypeError("approval_required must be a boolean")
        object.__setattr__(
            self,
            "required_capabilities",
            tuple(self.required_capabilities),
        )
        object.__setattr__(
            self,
            "authorization_metadata",
            _metadata(self.authorization_metadata, "authorization_metadata"),
        )
        object.__setattr__(
            self,
            "approval_metadata",
            _metadata(self.approval_metadata, "approval_metadata"),
        )
        object.__setattr__(
            self,
            "created_at",
            normalize_dispatch_timestamp(self.created_at, "created_at"),
        )
        object.__setattr__(
            self,
            "expires_at",
            normalize_dispatch_timestamp(self.expires_at, "expires_at"),
        )
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        object.__setattr__(
            self,
            "status",
            _enum(self.status, DispatchStatus, "status"),
        )
        history = tuple(self.audit_history)
        object.__setattr__(self, "audit_history", history)
        if not history or history[0].event_type is not DispatchEventType.OFFER_CREATED:
            raise ValueError("audit_history must begin with offer_created")
        previous = None
        identity = (
            self.offer_id,
            self.assignment_id,
            self.assignment_fingerprint,
            self.mission_id,
            self.task_id,
            self.coordinator_node_id,
            self.worker_node_id,
            self.required_capabilities,
            self.authorization_metadata,
            self.approval_metadata,
            self.expires_at,
        )
        for item in history:
            event_identity = (
                item.offer_id,
                item.routing_assignment_id,
                item.routing_assignment_fingerprint,
                item.mission_id,
                item.task_id,
                item.coordinator_node_id,
                item.worker_node_id,
                item.required_capabilities,
                item.authorization_metadata,
                item.approval_metadata,
                item.expires_at,
            )
            if event_identity != identity:
                raise ValueError("audit event worker or offer identity is foreign")
            if item.previous_state is not previous:
                raise ValueError("audit_history transitions must form a chain")
            previous = item.new_state
        if previous is not self.status:
            raise ValueError("audit_history must end at current status")
        if history[0].occurred_at != self.created_at:
            raise ValueError("creation timestamp mismatch")
        if self.status is DispatchStatus.OFFERED:
            if self.terminal_at is not None or self.resolution_reason is not None:
                raise ValueError("offered state cannot contain terminal metadata")
        else:
            if self.terminal_at is None:
                raise ValueError("terminal_at is required")
            object.__setattr__(
                self,
                "terminal_at",
                normalize_dispatch_timestamp(self.terminal_at, "terminal_at"),
            )
            if self.terminal_at != history[-1].occurred_at:
                raise ValueError("terminal timestamp mismatch")
            if self.resolution_reason != history[-1].reason:
                raise ValueError("terminal reason mismatch")
            if self.status is DispatchStatus.EXPIRED:
                if self.terminal_at < self.expires_at:
                    raise ValueError("expiration cannot precede deadline")
            elif self.terminal_at >= self.expires_at:
                raise ValueError("terminal decision must precede deadline")

    @property
    def is_terminal(self):
        return self.status in TERMINAL_DISPATCH_STATUSES
