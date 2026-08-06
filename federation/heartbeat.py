"""Authenticated worker heartbeat submission model."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from federation.integrity import authentication_tag, require_integrity_key
from federation.worker_liveness import PowerCapability, PowerState


HEARTBEAT_SUBMISSION_DOMAIN = b"raghub.worker-heartbeat-submission.v1"
GENESIS_AUTHENTICATION_TAG = "0" * 64


def canonical_json(value: dict) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def normalize_timestamp(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{field_name} must be a UTC timestamp")
    try:
        return normalize_timestamp(
            datetime.fromisoformat(value.replace("Z", "+00:00")),
            field_name,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid UTC timestamp") from exc


def _identifier(value, field_name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _optional_text(value, field_name):
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string or None")
    result = value.strip()
    if not result or len(result) > 1024:
        raise ValueError(f"{field_name} must contain 1 to 1024 characters")
    return result


def _is_sha256_tag(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(slots=True, frozen=True)
class Heartbeat:
    worker_id: str
    registry_id: str
    sequence: int
    session_id: str
    worker_timestamp: datetime
    health: str
    power_capabilities: tuple[PowerCapability, ...]
    requested_power_state: PowerState
    sleep_reason: str | None
    expected_wake_time: datetime | None
    wake_method: str | None
    active_work_checkpointed: bool
    previous_authentication_tag: str
    authentication_tag: str

    def __post_init__(self):
        object.__setattr__(self, "worker_id", _identifier(self.worker_id, "worker_id"))
        object.__setattr__(
            self,
            "registry_id",
            _identifier(self.registry_id, "registry_id"),
        )
        if (
            not isinstance(self.sequence, int)
            or isinstance(self.sequence, bool)
            or self.sequence < 1
        ):
            raise ValueError("sequence must be a positive integer")
        object.__setattr__(self, "session_id", _identifier(self.session_id, "session_id"))
        object.__setattr__(
            self,
            "worker_timestamp",
            normalize_timestamp(self.worker_timestamp, "worker_timestamp"),
        )
        health = _identifier(self.health, "health").strip().casefold()
        if health not in {"healthy", "degraded", "unhealthy"}:
            raise ValueError("health must be healthy, degraded, or unhealthy")
        object.__setattr__(self, "health", health)
        try:
            capabilities = tuple(
                sorted(
                    {PowerCapability(value) for value in self.power_capabilities},
                    key=lambda value: value.value,
                ),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("power_capabilities contains an invalid value") from exc
        object.__setattr__(self, "power_capabilities", capabilities)
        try:
            power_state = PowerState(self.requested_power_state)
        except (TypeError, ValueError) as exc:
            raise ValueError("requested_power_state is invalid") from exc
        object.__setattr__(self, "requested_power_state", power_state)
        object.__setattr__(
            self,
            "sleep_reason",
            _optional_text(self.sleep_reason, "sleep_reason"),
        )
        if self.expected_wake_time is not None:
            object.__setattr__(
                self,
                "expected_wake_time",
                normalize_timestamp(self.expected_wake_time, "expected_wake_time"),
            )
        object.__setattr__(
            self,
            "wake_method",
            _optional_text(self.wake_method, "wake_method"),
        )
        if not isinstance(self.active_work_checkpointed, bool):
            raise TypeError("active_work_checkpointed must be a bool")
        if not _is_sha256_tag(self.previous_authentication_tag):
            raise ValueError("previous_authentication_tag must be a SHA-256 tag")
        if not _is_sha256_tag(self.authentication_tag):
            raise ValueError("authentication_tag must be a SHA-256 tag")
        if power_state in {
            PowerState.SLEEP,
            PowerState.HIBERNATE,
            PowerState.SHUTDOWN,
        } and self.sleep_reason is None:
            raise ValueError("sleep_reason is required for an intentional power state")
        if (
            self.expected_wake_time is not None
            and self.expected_wake_time < self.worker_timestamp
        ):
            raise ValueError("expected_wake_time cannot precede worker_timestamp")

    def unsigned_payload(self):
        return {
            "worker_id": self.worker_id,
            "registry_id": self.registry_id,
            "sequence": self.sequence,
            "session_id": self.session_id,
            "worker_timestamp": format_timestamp(self.worker_timestamp),
            "health": self.health,
            "power_capabilities": [
                capability.value for capability in self.power_capabilities
            ],
            "requested_power_state": self.requested_power_state.value,
            "sleep_reason": self.sleep_reason,
            "expected_wake_time": (
                None
                if self.expected_wake_time is None
                else format_timestamp(self.expected_wake_time)
            ),
            "wake_method": self.wake_method,
            "active_work_checkpointed": self.active_work_checkpointed,
            "previous_authentication_tag": self.previous_authentication_tag,
        }

    @classmethod
    def authenticated(cls, *, integrity_key: bytes, **values):
        key = require_integrity_key(integrity_key)
        provisional = cls(authentication_tag=GENESIS_AUTHENTICATION_TAG, **values)
        tag = authentication_tag(
            key,
            HEARTBEAT_SUBMISSION_DOMAIN,
            canonical_json(provisional.unsigned_payload()),
        )
        return cls(authentication_tag=tag, **values)
