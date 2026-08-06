"""Governed, disabled-by-default Windows display adapter."""

import math
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from federation.power_action import (
    ComponentKind,
    GovernedPowerComponent,
    PowerAction,
    normalize_timestamp,
    require_text,
    timestamp,
)
from federation.power_adapter import (
    PowerExecutionAuthorization,
    PowerExecutionAuthorizationAuthority,
)
from federation.windows_display_native import NativeDisplayApi, WindowsNativeError
from federation.windows_session import WindowsSessionProbe
from federation.worker_liveness import LivenessState


class WindowsDisplayFailureCode(str, Enum):
    NONE = "none"
    DISABLED = "disabled"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    UNSUPPORTED_ACTION = "unsupported_action"
    AUTHORIZATION_REQUIRED = "authorization_required"
    IDENTITY_MISMATCH = "identity_mismatch"
    POLICY_MISMATCH = "policy_mismatch"
    AUTHORITY_MISMATCH = "authority_mismatch"
    WORKER_UNAVAILABLE = "worker_unavailable"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    NO_ACTIVE_SESSION = "no_active_session"
    WRONG_SESSION = "wrong_session"
    NONINTERACTIVE_SESSION = "noninteractive_session"
    LOCALLY_PROHIBITED = "locally_prohibited"
    IDLE_THRESHOLD_NOT_MET = "idle_threshold_not_met"
    CONFLICTING_TRANSITION = "conflicting_transition"
    NATIVE_CALL_FAILED = "native_call_failed"


@dataclass(slots=True, frozen=True)
class WindowsDisplayDeployment:
    enabled: bool
    adapter_id: str
    worker_id: str
    component_id: str
    policy_version: str
    controller_authority: str
    integrity_authority: str
    minimum_idle_seconds: float
    native_timeout_ms: int
    allow_display_off: bool
    allow_display_on: bool
    allow_when_locked: bool

    def __post_init__(self):
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be a bool")
        for field in (
            "adapter_id",
            "worker_id",
            "component_id",
            "policy_version",
            "controller_authority",
            "integrity_authority",
        ):
            object.__setattr__(
                self,
                field,
                require_text(getattr(self, field), field),
            )
        if isinstance(self.minimum_idle_seconds, bool) or not isinstance(
            self.minimum_idle_seconds, (int, float)
        ):
            raise TypeError("minimum_idle_seconds must be numeric")
        if not math.isfinite(self.minimum_idle_seconds) or (
            self.minimum_idle_seconds < 0
        ):
            raise ValueError("minimum_idle_seconds must be finite and non-negative")
        object.__setattr__(
            self,
            "minimum_idle_seconds",
            float(self.minimum_idle_seconds),
        )
        if isinstance(self.native_timeout_ms, bool) or not isinstance(
            self.native_timeout_ms, int
        ):
            raise TypeError("native_timeout_ms must be an integer")
        if not 1 <= self.native_timeout_ms <= 5000:
            raise ValueError("native_timeout_ms must be between 1 and 5000")
        for field in (
            "allow_display_off",
            "allow_display_on",
            "allow_when_locked",
        ):
            if not isinstance(getattr(self, field), bool):
                raise TypeError(f"{field} must be a bool")


@dataclass(slots=True, frozen=True)
class WindowsDisplayResult:
    adapter_id: str
    worker_id: str
    component_id: str
    action: PowerAction
    attempted: bool
    native_call_succeeded: bool
    code: WindowsDisplayFailureCode
    reason: str
    session_id: int | None
    observed_idle_seconds: float | None
    controller_timestamp: datetime
    native_timeout_ms: int

    def __post_init__(self):
        if not isinstance(self.action, PowerAction):
            raise TypeError("action must be a PowerAction")
        if not isinstance(self.code, WindowsDisplayFailureCode):
            raise TypeError("code must be a WindowsDisplayFailureCode")
        object.__setattr__(
            self,
            "controller_timestamp",
            normalize_timestamp(
                self.controller_timestamp,
                "controller_timestamp",
            ),
        )

    @property
    def succeeded(self):
        return (
            self.attempted
            and self.native_call_succeeded
            and self.code is WindowsDisplayFailureCode.NONE
        )

    def record(self):
        return {
            "adapter_id": self.adapter_id,
            "worker_id": self.worker_id,
            "component_id": self.component_id,
            "action": self.action.value,
            "attempted": self.attempted,
            "native_call_succeeded": self.native_call_succeeded,
            "code": self.code.value,
            "reason": self.reason,
            "session_id": self.session_id,
            "observed_idle_seconds": self.observed_idle_seconds,
            "controller_timestamp": timestamp(self.controller_timestamp),
            "native_timeout_ms": self.native_timeout_ms,
        }


class WorkerStateProbe(ABC):
    @abstractmethod
    def observe(self, worker_id):
        """Return current liveness and authenticated power capabilities."""


class HeartbeatWorkerStateProbe(WorkerStateProbe):
    """Typed bridge to the controller's authenticated heartbeat registry."""

    def __init__(self, registry):
        from federation.heartbeat_registry import HeartbeatRegistry

        if not isinstance(registry, HeartbeatRegistry):
            raise TypeError("registry must be a HeartbeatRegistry")
        self._registry = registry

    def observe(self, worker_id):
        lease = self._registry.inspect(worker_id)
        if lease is None:
            return None, ()
        return lease.state, lease.power_capabilities


class WindowsDisplayAdapter:
    """Locally governed adapter invoked only with coordinator authorization."""

    requires_execution_authorization = True

    def __init__(
        self,
        *,
        deployment,
        component,
        native_api,
        session_probe,
        worker_state_probe,
        authorization_verifier,
        clock,
    ):
        if not isinstance(deployment, WindowsDisplayDeployment):
            raise TypeError("deployment must be a WindowsDisplayDeployment")
        if not isinstance(component, GovernedPowerComponent):
            raise TypeError("component must be a GovernedPowerComponent")
        if not isinstance(native_api, NativeDisplayApi):
            raise TypeError("native_api must be a NativeDisplayApi")
        if not isinstance(session_probe, WindowsSessionProbe):
            raise TypeError("session_probe must be a WindowsSessionProbe")
        if not isinstance(worker_state_probe, WorkerStateProbe):
            raise TypeError("worker_state_probe must be a WorkerStateProbe")
        if type(authorization_verifier) is not PowerExecutionAuthorizationAuthority:
            raise TypeError(
                "authorization_verifier must be a "
                "PowerExecutionAuthorizationAuthority"
            )
        if not callable(clock):
            raise TypeError("clock must be callable")
        if component.kind is not ComponentKind.DISPLAY:
            raise ValueError("Windows display adapter requires a display component")
        expected = (
            component.adapter_id,
            component.worker_id,
            component.component_id,
            component.policy.version,
            component.controller_authority,
            component.integrity_authority,
        )
        configured = (
            deployment.adapter_id,
            deployment.worker_id,
            deployment.component_id,
            deployment.policy_version,
            deployment.controller_authority,
            deployment.integrity_authority,
        )
        if configured != expected:
            raise ValueError("deployment and governed component binding differ")
        if set(component.supported_actions) - {
            PowerAction.DISPLAY_OFF,
            PowerAction.DISPLAY_ON,
        }:
            raise ValueError("display component contains unsupported actions")
        if native_api.timeout_ms != deployment.native_timeout_ms:
            raise ValueError("native timeout differs from deployment policy")
        self.adapter_id = deployment.adapter_id
        self.enabled = deployment.enabled
        self._deployment = deployment
        self._component = component
        self._native = native_api
        self._session_probe = session_probe
        self._worker_state_probe = worker_state_probe
        self._authorization_verifier = authorization_verifier
        self._clock = clock
        self._transition_lock = threading.Lock()

    def attempt(self, action, worker_id, component_id):
        if not isinstance(action, PowerAction):
            raise TypeError("adapter action must be a PowerAction")
        require_text(worker_id, "worker_id")
        require_text(component_id, "component_id")
        return self._result(
            action,
            WindowsDisplayFailureCode.AUTHORIZATION_REQUIRED,
            "Coordinator execution authorization is required",
        )

    def attempt_authorized(self, authorization):
        if not isinstance(authorization, PowerExecutionAuthorization):
            raise TypeError("authorization must be a PowerExecutionAuthorization")
        action = authorization.action
        if not self._authorization_verifier.verify(authorization):
            return self._result(
                action,
                WindowsDisplayFailureCode.AUTHORIZATION_REQUIRED,
                "Authenticated coordinator execution authorization is required",
            )
        if not self.enabled:
            return self._result(
                action,
                WindowsDisplayFailureCode.DISABLED,
                "Windows display deployment is disabled",
            )
        if action not in {PowerAction.DISPLAY_OFF, PowerAction.DISPLAY_ON}:
            return self._result(
                action,
                WindowsDisplayFailureCode.UNSUPPORTED_ACTION,
                "Only typed display actions are supported",
            )
        binding = self._validate_authorization(authorization)
        if binding is not None:
            return binding
        if not self._transition_lock.acquire(blocking=False):
            return self._result(
                action,
                WindowsDisplayFailureCode.CONFLICTING_TRANSITION,
                "Another local display transition is active",
            )
        try:
            return self._attempt_local(authorization)
        finally:
            self._transition_lock.release()

    def _validate_authorization(self, authorization):
        action = authorization.action
        if (
            authorization.worker_id != self._deployment.worker_id
            or authorization.component_id != self._deployment.component_id
            or authorization.adapter_id != self._deployment.adapter_id
        ):
            return self._result(
                action,
                WindowsDisplayFailureCode.IDENTITY_MISMATCH,
                "Authorized identity does not match local binding",
            )
        if authorization.policy_version != self._deployment.policy_version:
            return self._result(
                action,
                WindowsDisplayFailureCode.POLICY_MISMATCH,
                "Authorized policy does not match local binding",
            )
        if (
            authorization.controller_authority
            != self._deployment.controller_authority
            or authorization.integrity_authority
            != self._deployment.integrity_authority
        ):
            return self._result(
                action,
                WindowsDisplayFailureCode.AUTHORITY_MISMATCH,
                "Authorized authority does not match local binding",
            )
        now = self._clock()
        if authorization.expires_at <= now:
            return self._result(
                action,
                WindowsDisplayFailureCode.AUTHORIZATION_REQUIRED,
                "Execution authorization is expired",
            )
        return None

    def _attempt_local(self, authorization):
        action = authorization.action
        state, capabilities = self._worker_state_probe.observe(
            self._deployment.worker_id
        )
        if state is not LivenessState.ONLINE:
            return self._result(
                action,
                WindowsDisplayFailureCode.WORKER_UNAVAILABLE,
                "Worker is not currently online",
            )
        capability_names = {
            getattr(capability, "value", capability) for capability in capabilities
        }
        if "display_control" not in capability_names:
            return self._result(
                action,
                WindowsDisplayFailureCode.CAPABILITY_UNAVAILABLE,
                "Display control capability is not currently advertised",
            )
        snapshot = self._session_probe.observe()
        common = {
            "session_id": snapshot.process_session_id,
            "idle": snapshot.idle_seconds,
        }
        if snapshot.active_console_session_id is None:
            return self._result(
                action,
                WindowsDisplayFailureCode.NO_ACTIVE_SESSION,
                "No active console session is available",
                **common,
            )
        if not snapshot.process_in_active_console_session:
            return self._result(
                action,
                WindowsDisplayFailureCode.WRONG_SESSION,
                "Helper is not in the active console session",
                **common,
            )
        if not snapshot.visible_interactive_window_station:
            return self._result(
                action,
                WindowsDisplayFailureCode.NONINTERACTIVE_SESSION,
                "A visible interactive window station is required",
                **common,
            )
        if snapshot.locally_prohibited or (
            snapshot.session_locked and not self._deployment.allow_when_locked
        ):
            return self._result(
                action,
                WindowsDisplayFailureCode.LOCALLY_PROHIBITED,
                "Local session safety policy prohibits display control",
                **common,
            )
        if action is PowerAction.DISPLAY_OFF:
            if not self._deployment.allow_display_off:
                return self._result(
                    action,
                    WindowsDisplayFailureCode.LOCALLY_PROHIBITED,
                    "Local policy prohibits automatic display off",
                    **common,
                )
            if snapshot.idle_seconds < self._deployment.minimum_idle_seconds:
                return self._result(
                    action,
                    WindowsDisplayFailureCode.IDLE_THRESHOLD_NOT_MET,
                    "Configured local idle threshold is not met",
                    **common,
                )
        elif not self._deployment.allow_display_on:
            return self._result(
                action,
                WindowsDisplayFailureCode.LOCALLY_PROHIBITED,
                "Local policy prohibits display on",
                **common,
            )
        try:
            succeeded = self._native.invoke(action)
        except WindowsNativeError:
            succeeded = False
        if not succeeded:
            return self._result(
                action,
                WindowsDisplayFailureCode.NATIVE_CALL_FAILED,
                "Bounded native display call failed or timed out",
                attempted=True,
                **common,
            )
        return self._result(
            action,
            WindowsDisplayFailureCode.NONE,
            "Bounded native display call completed",
            attempted=True,
            native_succeeded=True,
            **common,
        )

    def _result(
        self,
        action,
        code,
        reason,
        *,
        attempted=False,
        native_succeeded=False,
        session_id=None,
        idle=None,
    ):
        return WindowsDisplayResult(
            adapter_id=self.adapter_id,
            worker_id=self._deployment.worker_id,
            component_id=self._deployment.component_id,
            action=action,
            attempted=attempted,
            native_call_succeeded=native_succeeded,
            code=code,
            reason=reason,
            session_id=session_id,
            observed_idle_seconds=idle,
            controller_timestamp=self._clock(),
            native_timeout_ms=self._deployment.native_timeout_ms,
        )
