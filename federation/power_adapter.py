"""Typed, disabled-by-default platform power adapter boundaries."""

from dataclasses import dataclass, field

from federation.power_action import PowerAction, require_text


class PowerRefusalError(Exception):
    """A governed request failed a controller or local safety rule."""


class PowerConflictError(Exception):
    """Evidence conflicts with accepted power-control history."""


class PowerCorruptionError(Exception):
    """Durable power evidence cannot be authenticated."""


@dataclass(slots=True)
class RecordingPowerAdapter:
    adapter_id: str
    attempts: tuple[tuple[PowerAction, str, str], ...] = field(
        init=False,
        default=(),
    )
    enabled: bool = field(init=False, default=True)

    def __post_init__(self):
        self.adapter_id = require_text(self.adapter_id, "adapter_id")

    def attempt(self, action, worker_id, component_id):
        if not isinstance(action, PowerAction):
            raise TypeError("adapter action must be a PowerAction")
        worker_id = require_text(worker_id, "worker_id")
        component_id = require_text(component_id, "component_id")
        self.attempts = (*self.attempts, (action, worker_id, component_id))
        return "simulated_success"


@dataclass(slots=True)
class _DisabledPowerAdapter:
    adapter_id: str
    enabled: bool = field(init=False, default=False)

    def __post_init__(self):
        self.adapter_id = require_text(self.adapter_id, "adapter_id")

    def attempt(self, action, worker_id, component_id):
        if not isinstance(action, PowerAction):
            raise TypeError("adapter action must be a PowerAction")
        require_text(worker_id, "worker_id")
        require_text(component_id, "component_id")
        raise PowerRefusalError("production power adapter is disabled")


class DisabledFedoraPowerAdapter(_DisabledPowerAdapter):
    """Fedora boundary that cannot execute until explicitly replaced."""


class DisabledWindowsPowerAdapter(_DisabledPowerAdapter):
    """Windows boundary that cannot execute until explicitly replaced."""
