"""Typed, disabled-by-default platform power adapter boundaries."""

from dataclasses import dataclass, field
from datetime import datetime

from federation.integrity import authentication_tag, authenticates, require_integrity_key
from federation.power_action import (
    PowerAction,
    canonical_json,
    normalize_timestamp,
    require_text,
    timestamp,
)


class PowerRefusalError(Exception):
    """A governed request failed a controller or local safety rule."""


class PowerConflictError(Exception):
    """Evidence conflicts with accepted power-control history."""


class PowerCorruptionError(Exception):
    """Durable power evidence cannot be authenticated."""


_EXECUTION_AUTHORIZATION_DOMAIN = b"raghub.power-execution-authorization.v1"


def _execution_authorization_payload(values):
    return {
        "action": values["action"].value,
        "adapter_id": values["adapter_id"],
        "authorization_sequence": values["authorization_sequence"],
        "component_id": values["component_id"],
        "controller_authority": values["controller_authority"],
        "expires_at": timestamp(values["expires_at"]),
        "integrity_authority": values["integrity_authority"],
        "issued_at": timestamp(values["issued_at"]),
        "policy_version": values["policy_version"],
        "proposal_id": values["proposal_id"],
        "worker_id": values["worker_id"],
    }


@dataclass(slots=True, frozen=True)
class PowerExecutionAuthorization:
    """Authenticated coordinator-approved context supplied to enforced adapters.

    HMAC authentication rejects untrusted reconstruction and accidental API
    misuse. Copying an unchanged immutable record preserves the same evidence.
    This is not an in-process sandbox: malicious Python already executing in
    the trusted worker could inspect keys, monkeypatch objects, call ctypes, or
    invoke operating-system APIs directly.
    """

    proposal_id: str
    adapter_id: str
    worker_id: str
    component_id: str
    action: PowerAction
    policy_version: str
    controller_authority: str
    integrity_authority: str
    authorization_sequence: int
    issued_at: datetime
    expires_at: datetime
    authentication_tag: str

    def __post_init__(self):
        for name in (
            "proposal_id",
            "adapter_id",
            "worker_id",
            "component_id",
            "policy_version",
            "controller_authority",
            "integrity_authority",
        ):
            object.__setattr__(
                self,
                name,
                require_text(getattr(self, name), name),
            )
        if not isinstance(self.action, PowerAction):
            raise TypeError("action must be a PowerAction")
        if (
            isinstance(self.authorization_sequence, bool)
            or not isinstance(self.authorization_sequence, int)
        ):
            raise TypeError("authorization_sequence must be an integer")
        if self.authorization_sequence < 1:
            raise ValueError("authorization_sequence must be positive")
        for name in ("issued_at", "expires_at"):
            object.__setattr__(
                self,
                name,
                normalize_timestamp(getattr(self, name), name),
            )
        if self.expires_at <= self.issued_at:
            raise ValueError("execution authorization must expire after issuance")
        if not isinstance(self.authentication_tag, str):
            raise TypeError("authentication_tag must be a string")

    def authenticated_payload(self):
        return _execution_authorization_payload(
            {
                name: getattr(self, name)
                for name in (
                    "proposal_id",
                    "adapter_id",
                    "worker_id",
                    "component_id",
                    "action",
                    "policy_version",
                    "controller_authority",
                    "integrity_authority",
                    "authorization_sequence",
                    "issued_at",
                    "expires_at",
                )
            }
        )


class PowerExecutionAuthorizationAuthority:
    """Narrow HMAC-SHA256 issuer and verifier for execution authorization."""

    __slots__ = ("_key",)

    def __init__(self, key):
        self._key = require_integrity_key(key)

    def issue(
        self,
        *,
        proposal_id,
        adapter_id,
        worker_id,
        component_id,
        action,
        policy_version,
        controller_authority,
        integrity_authority,
        authorization_sequence,
        issued_at,
        expires_at,
    ):
        values = {
            "proposal_id": proposal_id,
            "adapter_id": adapter_id,
            "worker_id": worker_id,
            "component_id": component_id,
            "action": action,
            "policy_version": policy_version,
            "controller_authority": controller_authority,
            "integrity_authority": integrity_authority,
            "authorization_sequence": authorization_sequence,
            "issued_at": issued_at,
            "expires_at": expires_at,
        }
        for name in (
            "proposal_id",
            "adapter_id",
            "worker_id",
            "component_id",
            "policy_version",
            "controller_authority",
            "integrity_authority",
        ):
            values[name] = require_text(values[name], name)
        if not isinstance(action, PowerAction):
            raise TypeError("action must be a PowerAction")
        if isinstance(authorization_sequence, bool) or not isinstance(
            authorization_sequence, int
        ):
            raise TypeError("authorization_sequence must be an integer")
        if authorization_sequence < 1:
            raise ValueError("authorization_sequence must be positive")
        values["issued_at"] = normalize_timestamp(issued_at, "issued_at")
        values["expires_at"] = normalize_timestamp(expires_at, "expires_at")
        if values["expires_at"] <= values["issued_at"]:
            raise ValueError("execution authorization must expire after issuance")
        return PowerExecutionAuthorization(
            **values,
            authentication_tag=authentication_tag(
                self._key,
                _EXECUTION_AUTHORIZATION_DOMAIN,
                canonical_json(_execution_authorization_payload(values)),
            ),
        )

    def verify(self, authorization):
        if type(authorization) is not PowerExecutionAuthorization:
            return False
        return authenticates(
            self._key,
            _EXECUTION_AUTHORIZATION_DOMAIN,
            canonical_json(authorization.authenticated_payload()),
            authorization.authentication_tag,
        )


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
