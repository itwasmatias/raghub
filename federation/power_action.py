"""Immutable contracts for governed worker power actions."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from federation.integrity import authentication_tag, require_integrity_key


POWER_PROPOSAL_DOMAIN = b"raghub.power-proposal-authentication.v1"


class PowerAction(str, Enum):
    DISPLAY_OFF = "display_off"
    DISPLAY_ON = "display_on"
    SLEEP = "sleep"
    HIBERNATE = "hibernate"
    SHUTDOWN = "shutdown"
    WAKE_ON_LAN = "wake_on_lan"
    SCHEDULED_WAKE = "scheduled_wake"


class ComponentKind(str, Enum):
    DISPLAY = "display"
    SYSTEM_POWER = "system_power"
    WAKE_COORDINATOR = "wake_coordinator"


class PowerStatus(str, Enum):
    PROPOSED = "proposed"
    AUTO_APPROVED = "auto_approved"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REFUSED = "refused"
    EXECUTION_AUTHORIZED = "execution_authorized"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ApprovalType(str, Enum):
    AUTOMATIC_POLICY = "automatic_policy"
    EXPLICIT_USER = "explicit_user"


TERMINAL_STATUSES = {
    PowerStatus.REFUSED,
    PowerStatus.SUCCEEDED,
    PowerStatus.FAILED,
    PowerStatus.EXPIRED,
    PowerStatus.CANCELLED,
}


def normalize_timestamp(value, field):
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def timestamp(value):
    return normalize_timestamp(value, "timestamp").isoformat().replace("+00:00", "Z")


def parse_timestamp(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is invalid") from exc
    return normalize_timestamp(parsed, field)


def canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def require_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


@dataclass(slots=True, frozen=True)
class PowerPolicy:
    version: str
    automatic_display_control: bool = False
    interactive_session_prohibited: bool = False
    approved_wake_coordinator_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "version", require_text(self.version, "version"))
        if not isinstance(self.automatic_display_control, bool):
            raise TypeError("automatic_display_control must be a bool")
        if not isinstance(self.interactive_session_prohibited, bool):
            raise TypeError("interactive_session_prohibited must be a bool")
        if self.approved_wake_coordinator_id is not None:
            object.__setattr__(
                self,
                "approved_wake_coordinator_id",
                require_text(
                    self.approved_wake_coordinator_id,
                    "approved_wake_coordinator_id",
                ),
            )


@dataclass(slots=True, frozen=True)
class GovernedPowerComponent:
    worker_id: str
    component_id: str
    kind: ComponentKind
    supported_actions: tuple[PowerAction, ...]
    policy: PowerPolicy
    controller_authority: str
    integrity_authority: str
    adapter_id: str

    def __post_init__(self):
        for field in (
            "worker_id",
            "component_id",
            "controller_authority",
            "integrity_authority",
            "adapter_id",
        ):
            object.__setattr__(self, field, require_text(getattr(self, field), field))
        if not isinstance(self.kind, ComponentKind):
            raise TypeError("kind must be a ComponentKind")
        if not isinstance(self.policy, PowerPolicy):
            raise TypeError("policy must be a PowerPolicy")
        actions = tuple(self.supported_actions)
        if not actions or not all(isinstance(action, PowerAction) for action in actions):
            raise TypeError("supported_actions must contain PowerAction values")
        if len(actions) != len(set(actions)):
            raise ValueError("supported_actions must not contain duplicates")
        object.__setattr__(self, "supported_actions", actions)

        allowed = {
            ComponentKind.DISPLAY: {
                PowerAction.DISPLAY_OFF,
                PowerAction.DISPLAY_ON,
            },
            ComponentKind.SYSTEM_POWER: {
                PowerAction.SLEEP,
                PowerAction.HIBERNATE,
                PowerAction.SHUTDOWN,
            },
            ComponentKind.WAKE_COORDINATOR: {
                PowerAction.WAKE_ON_LAN,
                PowerAction.SCHEDULED_WAKE,
            },
        }
        if not set(actions) <= allowed[self.kind]:
            raise ValueError("component kind cannot advertise the requested action")

    def record(self):
        return {
            "worker_id": self.worker_id,
            "component_id": self.component_id,
            "kind": self.kind.value,
            "supported_actions": [action.value for action in self.supported_actions],
            "policy": {
                "version": self.policy.version,
                "automatic_display_control": self.policy.automatic_display_control,
                "interactive_session_prohibited": (
                    self.policy.interactive_session_prohibited
                ),
                "approved_wake_coordinator_id": (
                    self.policy.approved_wake_coordinator_id
                ),
            },
            "controller_authority": self.controller_authority,
            "integrity_authority": self.integrity_authority,
            "adapter_id": self.adapter_id,
        }

    @classmethod
    def from_record(cls, value):
        policy = value["policy"]
        return cls(
            worker_id=value["worker_id"],
            component_id=value["component_id"],
            kind=ComponentKind(value["kind"]),
            supported_actions=tuple(
                PowerAction(action) for action in value["supported_actions"]
            ),
            policy=PowerPolicy(
                version=policy["version"],
                automatic_display_control=policy["automatic_display_control"],
                interactive_session_prohibited=policy[
                    "interactive_session_prohibited"
                ],
                approved_wake_coordinator_id=policy[
                    "approved_wake_coordinator_id"
                ],
            ),
            controller_authority=value["controller_authority"],
            integrity_authority=value["integrity_authority"],
            adapter_id=value["adapter_id"],
        )


@dataclass(slots=True, frozen=True)
class PowerProposal:
    proposal_id: str
    worker_id: str
    component_id: str
    action: PowerAction
    sequence: int
    controller_authority: str
    integrity_authority: str
    policy_version: str
    requested_at: datetime
    authentication_tag: str
    checkpoint_evidence: str | None = None
    protected_work_safe: bool = True
    wake_path_evidence: str | None = None
    wake_coordinator_id: str | None = None
    execute_after: datetime | None = None
    expires_at: datetime | None = None

    def __post_init__(self):
        for field in (
            "proposal_id",
            "worker_id",
            "component_id",
            "controller_authority",
            "integrity_authority",
            "policy_version",
        ):
            object.__setattr__(self, field, require_text(getattr(self, field), field))
        if not isinstance(self.action, PowerAction):
            raise TypeError("action must be a PowerAction")
        if (
            not isinstance(self.authentication_tag, str)
            or len(self.authentication_tag) != 64
        ):
            raise ValueError("authentication_tag must be a SHA-256 HMAC tag")
        try:
            bytes.fromhex(self.authentication_tag)
        except ValueError as exc:
            raise ValueError("authentication_tag must be hexadecimal") from exc
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise TypeError("sequence must be an integer")
        if self.sequence < 1:
            raise ValueError("sequence must be positive")
        object.__setattr__(
            self,
            "requested_at",
            normalize_timestamp(self.requested_at, "requested_at"),
        )
        if not isinstance(self.protected_work_safe, bool):
            raise TypeError("protected_work_safe must be a bool")
        for field in (
            "checkpoint_evidence",
            "wake_path_evidence",
            "wake_coordinator_id",
        ):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, require_text(value, field))
        for field in ("execute_after", "expires_at"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, normalize_timestamp(value, field))
        expected = self.deterministic_id(self.unsigned_record())
        if self.proposal_id != expected:
            raise ValueError("proposal_id does not match proposal content")

    @classmethod
    def create(cls, **values):
        integrity_key = require_integrity_key(values.pop("integrity_key"))
        action = values.get("action")
        if not isinstance(action, PowerAction):
            raise TypeError("action must be a PowerAction")
        provisional = {
            "worker_id": values["worker_id"],
            "component_id": values["component_id"],
            "action": action.value,
            "sequence": values["sequence"],
            "controller_authority": values["controller_authority"],
            "integrity_authority": values["integrity_authority"],
            "policy_version": values["policy_version"],
            "requested_at": timestamp(values["requested_at"]),
            "checkpoint_evidence": values.get("checkpoint_evidence"),
            "protected_work_safe": values.get("protected_work_safe", True),
            "wake_path_evidence": values.get("wake_path_evidence"),
            "wake_coordinator_id": values.get("wake_coordinator_id"),
            "execute_after": (
                None
                if values.get("execute_after") is None
                else timestamp(values["execute_after"])
            ),
            "expires_at": (
                None
                if values.get("expires_at") is None
                else timestamp(values["expires_at"])
            ),
        }
        return cls(
            proposal_id=cls.deterministic_id(provisional),
            authentication_tag=authentication_tag(
                integrity_key,
                POWER_PROPOSAL_DOMAIN,
                canonical_json(provisional),
            ),
            **values,
        )

    @staticmethod
    def deterministic_id(record):
        return hashlib.sha256(
            b"raghub.power-proposal.v1\0" + canonical_json(record)
        ).hexdigest()

    def unsigned_record(self):
        return {
            "worker_id": self.worker_id,
            "component_id": self.component_id,
            "action": self.action.value,
            "sequence": self.sequence,
            "controller_authority": self.controller_authority,
            "integrity_authority": self.integrity_authority,
            "policy_version": self.policy_version,
            "requested_at": timestamp(self.requested_at),
            "checkpoint_evidence": self.checkpoint_evidence,
            "protected_work_safe": self.protected_work_safe,
            "wake_path_evidence": self.wake_path_evidence,
            "wake_coordinator_id": self.wake_coordinator_id,
            "execute_after": (
                None if self.execute_after is None else timestamp(self.execute_after)
            ),
            "expires_at": None if self.expires_at is None else timestamp(self.expires_at),
        }

    def record(self):
        return {
            "proposal_id": self.proposal_id,
            **self.unsigned_record(),
            "authentication_tag": self.authentication_tag,
        }

    @classmethod
    def from_record(cls, value):
        return cls(
            proposal_id=value["proposal_id"],
            worker_id=value["worker_id"],
            component_id=value["component_id"],
            action=PowerAction(value["action"]),
            sequence=value["sequence"],
            controller_authority=value["controller_authority"],
            integrity_authority=value["integrity_authority"],
            policy_version=value["policy_version"],
            requested_at=parse_timestamp(value["requested_at"], "requested_at"),
            authentication_tag=value["authentication_tag"],
            checkpoint_evidence=value["checkpoint_evidence"],
            protected_work_safe=value["protected_work_safe"],
            wake_path_evidence=value["wake_path_evidence"],
            wake_coordinator_id=value["wake_coordinator_id"],
            execute_after=(
                None
                if value["execute_after"] is None
                else parse_timestamp(value["execute_after"], "execute_after")
            ),
            expires_at=(
                None
                if value["expires_at"] is None
                else parse_timestamp(value["expires_at"], "expires_at")
            ),
        )


@dataclass(slots=True, frozen=True)
class PowerApproval:
    proposal_id: str
    worker_id: str
    component_id: str
    action: PowerAction
    approval_type: ApprovalType
    actor: str
    approved_at: datetime
    expires_at: datetime
    checkpoint_evidence: str | None = None
    protected_work_safe: bool = True

    def __post_init__(self):
        for field in ("proposal_id", "worker_id", "component_id", "actor"):
            object.__setattr__(self, field, require_text(getattr(self, field), field))
        if not isinstance(self.action, PowerAction):
            raise TypeError("action must be a PowerAction")
        if not isinstance(self.approval_type, ApprovalType):
            raise TypeError("approval_type must be an ApprovalType")
        for field in ("approved_at", "expires_at"):
            object.__setattr__(
                self,
                field,
                normalize_timestamp(getattr(self, field), field),
            )
        if self.expires_at <= self.approved_at:
            raise ValueError("approval expiration must be after approval time")
        if self.checkpoint_evidence is not None:
            object.__setattr__(
                self,
                "checkpoint_evidence",
                require_text(self.checkpoint_evidence, "checkpoint_evidence"),
            )
        if not isinstance(self.protected_work_safe, bool):
            raise TypeError("protected_work_safe must be a bool")


@dataclass(slots=True, frozen=True)
class PowerSnapshot:
    proposal_id: str
    worker_id: str
    component_id: str
    action: PowerAction
    status: PowerStatus
    approval_requirement: str
    refusal_reason: str | None
    checkpoint_status: str
    wake_path_status: str
    authorization_expiration: datetime | None
    latest_result: str | None
    audit_sequence: int


@dataclass(slots=True, frozen=True)
class PowerAuditEvent:
    sequence: int
    event_type: str
    proposal_id: str | None
    worker_id: str
    component_id: str
    action: str | None
    controller_timestamp: datetime
    reason: str | None
    authentication_tag: str


@dataclass(slots=True, frozen=True)
class OperationsPowerRecord:
    worker_id: str
    component_id: str
    supported_actions: tuple[str, ...]
    current_governed_state: str
    pending_proposal: str | None
    approval_requirement: str | None
    refusal_reason: str | None
    checkpoint_status: str
    wake_path_status: str
    authorization_expiration: datetime | None
    latest_result: str | None
    audit_sequence: int
