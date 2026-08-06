"""Immutable worker liveness and power-state values."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class LivenessState(str, Enum):
    ONLINE = "online"
    STALE = "stale"
    OFFLINE = "offline"
    INTENTIONALLY_SLEEPING = "intentionally_sleeping"
    WAKING = "waking"


class PowerState(str, Enum):
    ACTIVE = "active"
    SLEEP = "sleep"
    HIBERNATE = "hibernate"
    SHUTDOWN = "shutdown"
    WAKING = "waking"


class PowerCapability(str, Enum):
    DISPLAY_CONTROL = "display_control"
    SLEEP = "sleep"
    HIBERNATE = "hibernate"
    SHUTDOWN = "shutdown"
    WAKE_ON_LAN = "wake_on_lan"
    SCHEDULED_WAKE = "scheduled_wake"


@dataclass(slots=True, frozen=True)
class WorkerLease:
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
    submission_authentication_tag: str
    controller_received_at: datetime
    lease_expires_at: datetime
    authentication_tag: str
    state: LivenessState
    routing_eligible: bool
