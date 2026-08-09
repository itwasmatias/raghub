"""Governed lifecycle for the single allowlisted local llama.cpp server.

This module launches no caller-supplied command.  It composes authoritative
worker execution, artifact registry, and host capacity evidence around one
fixed Fedora llama.cpp profile, then persists every lifecycle transition in an
authenticated append-only history.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import signal
import socket
import stat
import subprocess
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol

from federation.file_lock import fcntl
from federation.host_resource_capacity import (
    HostCapacityDecision,
    HostCapacityGuard,
    HostCapacityPolicy,
    HostCapacityStatus,
    HostResourceCollector,
    HostResourceRequirement,
    HostResourceSnapshot,
)
from federation.integrity import authentication_tag, authenticates, require_integrity_key
from federation.local_model_artifact_registry import (
    ArtifactState,
    ArtifactStatus,
    LocalModelArtifactRegistry,
)
from federation.worker_execution import (
    WorkerExecutionCoordinator,
    WorkerExecutionResultEnvelope,
    WorkerExecutionStatus,
)


FEDORA_LLAMA_SERVER_PATH = (
    "/home/matias/local-ai/llama.cpp/build/bin/llama-server"
)
FEDORA_LLAMA_SERVER_SHA256 = (
    "51101f3ef423200f9096ed07fc186e4670fa7b99ccea2d9a571fdd2ff5851b04"
)
FEDORA_LLAMA_CPP_SOURCE_COMMIT = "876a4321163249c43ca4e986818fab5ab081f282"
FEDORA_MODEL_ALIAS = "raghub-qwen2.5-0.5b-q4km"
GOVERNED_HOST = "127.0.0.1"
GOVERNED_PORT = 18080

_SCHEMA_VERSION = 1
_DOMAIN = b"raghub.local-model-server-lifecycle.v1"
_GENESIS_TAG = "0" * 64
_MAX_TIMEOUT_SECONDS = 300.0
_MAX_ATTESTATION_BYTES = 1 << 20
_MAX_OUTPUT_BYTES = 1 << 20
_MINIMAL_ENVIRONMENT = {
    "HOME": "/",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/bin:/bin",
    "TMPDIR": "/tmp",
}
_REMOVED_ENVIRONMENT_NAMES = (
    "ALL_PROXY",
    "HF_TOKEN",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "all_proxy",
    "http_proxy",
    "https_proxy",
)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _identifier(value: Any, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty string without surrounding whitespace")
    return value


def _sha256(value: Any, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be lowercase SHA-256 hex")
    return value


def _positive_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _timeout(value: Any, name: str) -> float:
    if type(value) not in (int, float) or isinstance(value, bool):
        raise TypeError(f"{name} must be a finite positive number")
    normalized = float(value)
    if not 0 < normalized <= _MAX_TIMEOUT_SECONDS:
        raise ValueError(f"{name} must be within (0, {_MAX_TIMEOUT_SECONDS}]")
    return normalized


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat()


def _parse_timestamp(value: Any, name: str) -> datetime:
    if type(value) is not str:
        raise ValueError(f"{name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO timestamp") from exc
    return _utc(parsed, name)


def _strict_dict(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{name} schema is invalid")
    return value


class LocalModelServerLifecycleError(Exception):
    """Base lifecycle failure."""


class LifecycleAuthorizationError(LocalModelServerLifecycleError):
    """Worker execution evidence does not authorize this lifecycle."""


class LifecycleArtifactError(LocalModelServerLifecycleError):
    """Artifact registry evidence is absent, stale, or untrusted."""


class LifecycleBinaryIdentityError(LocalModelServerLifecycleError):
    """The allowlisted llama-server executable failed identity checks."""


class LifecycleCapacityUnsafeError(LocalModelServerLifecycleError):
    """Host capacity is unsafe for this server."""


class LifecycleCapacityConstrainedError(LocalModelServerLifecycleError):
    """Host capacity is constrained and policy did not permit launch."""


class LifecyclePortConflictError(LocalModelServerLifecycleError):
    """The governed endpoint is occupied by unprovable ownership."""


class LifecycleConflictError(LocalModelServerLifecycleError):
    """An immutable lifecycle or stop identity was reused inconsistently."""


class LifecycleStateError(LocalModelServerLifecycleError):
    """The requested lifecycle transition is not permitted."""


class LifecycleProcessLaunchError(LocalModelServerLifecycleError):
    """The fixed llama.cpp process could not be launched."""


class LifecycleStartupTimeoutError(LocalModelServerLifecycleError):
    """The governed server did not attest before its startup deadline."""


class LifecycleProcessExitedError(LocalModelServerLifecycleError):
    """The governed process exited before startup attestation."""


class LifecycleHealthAttestationError(LocalModelServerLifecycleError):
    """The llama.cpp health endpoint did not attest."""


class LifecycleModelAttestationError(LocalModelServerLifecycleError):
    """The llama.cpp model endpoint did not attest."""


class LifecyclePropsAttestationError(LocalModelServerLifecycleError):
    """The llama.cpp chat properties endpoint did not attest."""


class LifecycleStopIdentityError(LocalModelServerLifecycleError):
    """Stored and observed process identity do not prove a safe stop target."""


class LifecycleShutdownTimeoutError(LocalModelServerLifecycleError):
    """Normal termination did not complete within the shutdown timeout."""


class LifecycleStoreIntegrityError(LocalModelServerLifecycleError):
    """Durable lifecycle evidence cannot be trusted."""


class LifecycleReconciliationRequired(LocalModelServerLifecycleError):
    """Execution or process state is ambiguous and must not be guessed."""


class LocalModelServerLifecycleState(StrEnum):
    REQUESTED = "requested"
    PREFLIGHT_PASSED = "preflight_passed"
    STARTING = "starting"
    RUNNING_UNATTESTED = "running_unattested"
    ATTESTED = "attested"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


TERMINAL_LOCAL_MODEL_SERVER_STATES = frozenset(
    {
        LocalModelServerLifecycleState.STOPPED,
        LocalModelServerLifecycleState.FAILED,
        LocalModelServerLifecycleState.RECONCILIATION_REQUIRED,
    }
)

_TRANSITIONS = {
    LocalModelServerLifecycleState.REQUESTED: {
        LocalModelServerLifecycleState.PREFLIGHT_PASSED,
        LocalModelServerLifecycleState.FAILED,
        LocalModelServerLifecycleState.RECONCILIATION_REQUIRED,
    },
    LocalModelServerLifecycleState.PREFLIGHT_PASSED: {
        LocalModelServerLifecycleState.STARTING,
        LocalModelServerLifecycleState.FAILED,
        LocalModelServerLifecycleState.RECONCILIATION_REQUIRED,
    },
    LocalModelServerLifecycleState.STARTING: {
        LocalModelServerLifecycleState.RUNNING_UNATTESTED,
        LocalModelServerLifecycleState.FAILED,
        LocalModelServerLifecycleState.RECONCILIATION_REQUIRED,
    },
    LocalModelServerLifecycleState.RUNNING_UNATTESTED: {
        LocalModelServerLifecycleState.ATTESTED,
        LocalModelServerLifecycleState.STOPPING,
        LocalModelServerLifecycleState.FAILED,
        LocalModelServerLifecycleState.RECONCILIATION_REQUIRED,
    },
    LocalModelServerLifecycleState.ATTESTED: {
        LocalModelServerLifecycleState.STOPPING,
        LocalModelServerLifecycleState.FAILED,
        LocalModelServerLifecycleState.RECONCILIATION_REQUIRED,
    },
    LocalModelServerLifecycleState.STOPPING: {
        LocalModelServerLifecycleState.STOPPED,
        LocalModelServerLifecycleState.RECONCILIATION_REQUIRED,
    },
}


@dataclass(frozen=True, slots=True)
class LocalModelServerProfile:
    """The one allowlisted llama.cpp server profile supported by v0.1."""

    profile_id: str = "fedora-llama-server-qwen2.5-0.5b-v0.1"
    binary_path: str = FEDORA_LLAMA_SERVER_PATH
    binary_sha256: str = FEDORA_LLAMA_SERVER_SHA256
    source_commit: str = FEDORA_LLAMA_CPP_SOURCE_COMMIT
    model_alias: str = FEDORA_MODEL_ALIAS
    bind_host: str = GOVERNED_HOST
    port: int = GOVERNED_PORT
    context_size: int = 1024
    thread_count: int = 4
    batch_thread_count: int = 4
    parallel_count: int = 1
    gpu_layer_count: int = 0
    offline: bool = True
    web_ui_disabled: bool = True
    chat_capability: bool = True

    def __post_init__(self) -> None:
        expected = {
            "profile_id": "fedora-llama-server-qwen2.5-0.5b-v0.1",
            "binary_path": FEDORA_LLAMA_SERVER_PATH,
            "binary_sha256": FEDORA_LLAMA_SERVER_SHA256,
            "source_commit": FEDORA_LLAMA_CPP_SOURCE_COMMIT,
            "model_alias": FEDORA_MODEL_ALIAS,
            "bind_host": GOVERNED_HOST,
            "port": GOVERNED_PORT,
            "context_size": 1024,
            "thread_count": 4,
            "batch_thread_count": 4,
            "parallel_count": 1,
            "gpu_layer_count": 0,
            "offline": True,
            "web_ui_disabled": True,
            "chat_capability": True,
        }
        for name, required in expected.items():
            if getattr(self, name) != required or type(getattr(self, name)) is not type(required):
                raise ValueError(f"{name} is fixed by the v0.1 allowlisted profile")

    @property
    def endpoint(self) -> str:
        return f"http://{self.bind_host}:{self.port}"

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "binary_path": self.binary_path,
            "binary_sha256": self.binary_sha256,
            "source_commit": self.source_commit,
            "model_alias": self.model_alias,
            "bind_host": self.bind_host,
            "port": self.port,
            "context_size": self.context_size,
            "thread_count": self.thread_count,
            "batch_thread_count": self.batch_thread_count,
            "parallel_count": self.parallel_count,
            "gpu_layer_count": self.gpu_layer_count,
            "offline": self.offline,
            "web_ui_disabled": self.web_ui_disabled,
            "chat_capability": self.chat_capability,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "LocalModelServerProfile":
        fields = set(cls().to_dict())
        return cls(**_strict_dict(value, fields, "profile"))


@dataclass(frozen=True, slots=True)
class LocalModelServerStartRequest:
    lifecycle_request_id: str
    worker_node_id: str
    execution_attempt_id: str
    execution_fingerprint: str
    artifact_id: str
    artifact_fingerprint: str
    artifact_sha256: str
    profile: LocalModelServerProfile
    capacity_requirement: HostResourceRequirement
    capacity_policy: HostCapacityPolicy
    allow_constrained_capacity: bool = False
    startup_timeout_seconds: float = 60.0
    shutdown_timeout_seconds: float = 15.0
    request_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "lifecycle_request_id", "worker_node_id", "execution_attempt_id", "artifact_id"
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        for name in ("execution_fingerprint", "artifact_fingerprint", "artifact_sha256"):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        if type(self.profile) is not LocalModelServerProfile:
            raise TypeError("profile must be LocalModelServerProfile")
        if type(self.capacity_requirement) is not HostResourceRequirement:
            raise TypeError("capacity_requirement must be HostResourceRequirement")
        if type(self.capacity_policy) is not HostCapacityPolicy:
            raise TypeError("capacity_policy must be HostCapacityPolicy")
        if type(self.allow_constrained_capacity) is not bool:
            raise TypeError("allow_constrained_capacity must be boolean")
        object.__setattr__(
            self, "startup_timeout_seconds",
            _timeout(self.startup_timeout_seconds, "startup_timeout_seconds"),
        )
        object.__setattr__(
            self, "shutdown_timeout_seconds",
            _timeout(self.shutdown_timeout_seconds, "shutdown_timeout_seconds"),
        )
        object.__setattr__(self, "request_fingerprint", _fingerprint(self.to_dict(False)))

    def to_dict(self, include_fingerprint: bool = True) -> dict[str, Any]:
        value = {
            "lifecycle_request_id": self.lifecycle_request_id,
            "worker_node_id": self.worker_node_id,
            "execution_attempt_id": self.execution_attempt_id,
            "execution_fingerprint": self.execution_fingerprint,
            "artifact_id": self.artifact_id,
            "artifact_fingerprint": self.artifact_fingerprint,
            "artifact_sha256": self.artifact_sha256,
            "profile": self.profile.to_dict(),
            "capacity_requirement": _requirement_to_dict(self.capacity_requirement),
            "capacity_policy": _policy_to_dict(self.capacity_policy),
            "allow_constrained_capacity": self.allow_constrained_capacity,
            "startup_timeout_seconds": self.startup_timeout_seconds,
            "shutdown_timeout_seconds": self.shutdown_timeout_seconds,
        }
        if include_fingerprint:
            value["request_fingerprint"] = self.request_fingerprint
        return value

    @classmethod
    def from_dict(cls, value: Any) -> "LocalModelServerStartRequest":
        fields = {
            "lifecycle_request_id", "worker_node_id", "execution_attempt_id",
            "execution_fingerprint", "artifact_id", "artifact_fingerprint",
            "artifact_sha256", "profile", "capacity_requirement", "capacity_policy",
            "allow_constrained_capacity", "startup_timeout_seconds",
            "shutdown_timeout_seconds", "request_fingerprint",
        }
        raw = dict(_strict_dict(value, fields, "start request"))
        claimed = raw.pop("request_fingerprint")
        raw["profile"] = LocalModelServerProfile.from_dict(raw["profile"])
        raw["capacity_requirement"] = _requirement_from_dict(raw["capacity_requirement"])
        raw["capacity_policy"] = _policy_from_dict(raw["capacity_policy"])
        request = cls(**raw)
        if claimed != request.request_fingerprint:
            raise ValueError("start request fingerprint is invalid")
        return request


@dataclass(frozen=True, slots=True)
class StableFileIdentity:
    canonical_path: str
    sha256: str
    size_bytes: int
    device: int
    inode: int
    mode: int
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.canonical_path) is not str or not os.path.isabs(self.canonical_path):
            raise ValueError("canonical_path must be absolute")
        object.__setattr__(self, "sha256", _sha256(self.sha256, "sha256"))
        for name in ("size_bytes", "device", "inode", "mode"):
            object.__setattr__(self, name, _nonnegative_int(getattr(self, name), name))
        object.__setattr__(self, "fingerprint", _fingerprint(self.to_dict(False)))

    def to_dict(self, include_fingerprint: bool = True) -> dict[str, Any]:
        value = {
            "canonical_path": self.canonical_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "device": self.device,
            "inode": self.inode,
            "mode": self.mode,
        }
        if include_fingerprint:
            value["fingerprint"] = self.fingerprint
        return value

    @classmethod
    def from_dict(cls, value: Any) -> "StableFileIdentity":
        fields = {
            "canonical_path", "sha256", "size_bytes", "device", "inode", "mode",
            "fingerprint",
        }
        raw = dict(_strict_dict(value, fields, "stable file identity"))
        claimed = raw.pop("fingerprint")
        identity = cls(**raw)
        if claimed != identity.fingerprint:
            raise ValueError("stable file identity fingerprint is invalid")
        return identity


@dataclass(frozen=True, slots=True)
class CapacityPreflightEvidence:
    snapshot_fingerprint: str
    requirement_fingerprint: str
    policy_fingerprint: str
    decision_fingerprint: str
    status: HostCapacityStatus
    reasons: tuple[str, ...]
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "snapshot_fingerprint", "requirement_fingerprint", "policy_fingerprint",
            "decision_fingerprint",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        try:
            object.__setattr__(self, "status", HostCapacityStatus(self.status))
        except (TypeError, ValueError) as exc:
            raise ValueError("capacity status is invalid") from exc
        reasons = tuple(sorted(_identifier(item, "capacity reason") for item in self.reasons))
        if len(set(reasons)) != len(reasons):
            raise ValueError("capacity reasons must be unique")
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(self, "fingerprint", _fingerprint(self.to_dict(False)))

    @classmethod
    def from_decision(cls, decision: HostCapacityDecision) -> "CapacityPreflightEvidence":
        return cls(
            snapshot_fingerprint=decision.snapshot_fingerprint,
            requirement_fingerprint=decision.requirement_fingerprint,
            policy_fingerprint=decision.policy_fingerprint,
            decision_fingerprint=decision.fingerprint,
            status=decision.status,
            reasons=tuple(reason.value for reason in decision.reasons),
        )

    def to_dict(self, include_fingerprint: bool = True) -> dict[str, Any]:
        value = {
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "requirement_fingerprint": self.requirement_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
            "decision_fingerprint": self.decision_fingerprint,
            "status": self.status.value,
            "reasons": list(self.reasons),
        }
        if include_fingerprint:
            value["fingerprint"] = self.fingerprint
        return value

    @classmethod
    def from_dict(cls, value: Any) -> "CapacityPreflightEvidence":
        fields = {
            "snapshot_fingerprint", "requirement_fingerprint", "policy_fingerprint",
            "decision_fingerprint", "status", "reasons", "fingerprint",
        }
        raw = dict(_strict_dict(value, fields, "capacity evidence"))
        claimed = raw.pop("fingerprint")
        raw["reasons"] = tuple(raw["reasons"])
        evidence = cls(**raw)
        if claimed != evidence.fingerprint:
            raise ValueError("capacity evidence fingerprint is invalid")
        return evidence


@dataclass(frozen=True, slots=True)
class ArtifactPreflightEvidence:
    artifact_id: str
    artifact_fingerprint: str
    artifact_sha256: str
    canonical_path: str
    size_bytes: int
    device: int
    inode: int
    verified_at: datetime
    stable_file_fingerprint: str
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _identifier(self.artifact_id, "artifact_id"))
        for name in ("artifact_fingerprint", "artifact_sha256", "stable_file_fingerprint"):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        if type(self.canonical_path) is not str or not os.path.isabs(self.canonical_path):
            raise ValueError("artifact canonical_path must be absolute")
        for name in ("size_bytes", "device", "inode"):
            object.__setattr__(self, name, _nonnegative_int(getattr(self, name), name))
        object.__setattr__(self, "verified_at", _utc(self.verified_at, "verified_at"))
        object.__setattr__(self, "fingerprint", _fingerprint(self.to_dict(False)))

    def to_dict(self, include_fingerprint: bool = True) -> dict[str, Any]:
        value = {
            "artifact_id": self.artifact_id,
            "artifact_fingerprint": self.artifact_fingerprint,
            "artifact_sha256": self.artifact_sha256,
            "canonical_path": self.canonical_path,
            "size_bytes": self.size_bytes,
            "device": self.device,
            "inode": self.inode,
            "verified_at": _timestamp(self.verified_at),
            "stable_file_fingerprint": self.stable_file_fingerprint,
        }
        if include_fingerprint:
            value["fingerprint"] = self.fingerprint
        return value

    @classmethod
    def from_dict(cls, value: Any) -> "ArtifactPreflightEvidence":
        fields = {
            "artifact_id", "artifact_fingerprint", "artifact_sha256",
            "canonical_path", "size_bytes", "device", "inode", "verified_at",
            "stable_file_fingerprint", "fingerprint",
        }
        raw = dict(_strict_dict(value, fields, "artifact evidence"))
        claimed = raw.pop("fingerprint")
        raw["verified_at"] = _parse_timestamp(raw["verified_at"], "verified_at")
        evidence = cls(**raw)
        if claimed != evidence.fingerprint:
            raise ValueError("artifact evidence fingerprint is invalid")
        return evidence


@dataclass(frozen=True, slots=True)
class LocalModelServerIdentity:
    lifecycle_request_id: str
    pid: int
    kernel_start_ticks: int
    boot_id: str
    executable: StableFileIdentity
    argv: tuple[str, ...]
    argv_fingerprint: str
    environment_policy_fingerprint: str
    endpoint: str
    created_at: datetime
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "lifecycle_request_id",
            _identifier(self.lifecycle_request_id, "lifecycle_request_id"),
        )
        object.__setattr__(self, "pid", _positive_int(self.pid, "pid"))
        object.__setattr__(
            self, "kernel_start_ticks",
            _nonnegative_int(self.kernel_start_ticks, "kernel_start_ticks"),
        )
        object.__setattr__(self, "boot_id", _identifier(self.boot_id, "boot_id"))
        if type(self.executable) is not StableFileIdentity:
            raise TypeError("executable must be StableFileIdentity")
        argv = tuple(_identifier(item, "argv item") for item in self.argv)
        if not argv:
            raise ValueError("argv must not be empty")
        object.__setattr__(self, "argv", argv)
        if _fingerprint(list(argv)) != self.argv_fingerprint:
            raise ValueError("argv_fingerprint does not match argv")
        object.__setattr__(
            self, "environment_policy_fingerprint",
            _sha256(self.environment_policy_fingerprint, "environment_policy_fingerprint"),
        )
        expected_endpoint = f"http://{GOVERNED_HOST}:{GOVERNED_PORT}"
        if self.endpoint != expected_endpoint:
            raise ValueError("endpoint is not the governed loopback endpoint")
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        object.__setattr__(self, "fingerprint", _fingerprint(self.to_dict(False)))

    def to_dict(self, include_fingerprint: bool = True) -> dict[str, Any]:
        value = {
            "lifecycle_request_id": self.lifecycle_request_id,
            "pid": self.pid,
            "kernel_start_ticks": self.kernel_start_ticks,
            "boot_id": self.boot_id,
            "executable": self.executable.to_dict(),
            "argv": list(self.argv),
            "argv_fingerprint": self.argv_fingerprint,
            "environment_policy_fingerprint": self.environment_policy_fingerprint,
            "endpoint": self.endpoint,
            "created_at": _timestamp(self.created_at),
        }
        if include_fingerprint:
            value["fingerprint"] = self.fingerprint
        return value

    @classmethod
    def from_dict(cls, value: Any) -> "LocalModelServerIdentity":
        fields = {
            "lifecycle_request_id", "pid", "kernel_start_ticks", "boot_id",
            "executable", "argv", "argv_fingerprint", "environment_policy_fingerprint",
            "endpoint", "created_at", "fingerprint",
        }
        raw = dict(_strict_dict(value, fields, "process identity"))
        claimed = raw.pop("fingerprint")
        raw["executable"] = StableFileIdentity.from_dict(raw["executable"])
        raw["argv"] = tuple(raw["argv"])
        raw["created_at"] = _parse_timestamp(raw["created_at"], "created_at")
        identity = cls(**raw)
        if claimed != identity.fingerprint:
            raise ValueError("process identity fingerprint is invalid")
        return identity


@dataclass(frozen=True, slots=True)
class HttpAttestationEvidence:
    route: str
    status: int
    body_size: int
    body_sha256: str
    semantic_fingerprint: str
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if self.route not in ("/health", "/v1/models", "/props"):
            raise ValueError("attestation route is not allowlisted")
        if type(self.status) is not int or not 100 <= self.status <= 599:
            raise ValueError("HTTP status is invalid")
        object.__setattr__(self, "body_size", _nonnegative_int(self.body_size, "body_size"))
        for name in ("body_sha256", "semantic_fingerprint"):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        object.__setattr__(self, "fingerprint", _fingerprint(self.to_dict(False)))

    def to_dict(self, include_fingerprint: bool = True) -> dict[str, Any]:
        value = {
            "route": self.route,
            "status": self.status,
            "body_size": self.body_size,
            "body_sha256": self.body_sha256,
            "semantic_fingerprint": self.semantic_fingerprint,
        }
        if include_fingerprint:
            value["fingerprint"] = self.fingerprint
        return value

    @classmethod
    def from_dict(cls, value: Any) -> "HttpAttestationEvidence":
        fields = {
            "route", "status", "body_size", "body_sha256",
            "semantic_fingerprint", "fingerprint",
        }
        raw = dict(_strict_dict(value, fields, "HTTP attestation evidence"))
        claimed = raw.pop("fingerprint")
        evidence = cls(**raw)
        if claimed != evidence.fingerprint:
            raise ValueError("HTTP attestation fingerprint is invalid")
        return evidence


@dataclass(frozen=True, slots=True)
class LocalModelServerAttestation:
    lifecycle_request_fingerprint: str
    execution_attempt_id: str
    execution_fingerprint: str
    artifact_evidence_fingerprint: str
    binary_fingerprint: str
    capacity_evidence_fingerprint: str
    process_identity_fingerprint: str
    endpoint: str
    invocation_fingerprint: str
    health: HttpAttestationEvidence
    models: HttpAttestationEvidence
    props: HttpAttestationEvidence | None
    model_alias: str
    attested_at: datetime
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "lifecycle_request_fingerprint", "execution_fingerprint",
            "artifact_evidence_fingerprint", "binary_fingerprint",
            "capacity_evidence_fingerprint", "process_identity_fingerprint",
            "invocation_fingerprint",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        object.__setattr__(
            self, "execution_attempt_id",
            _identifier(self.execution_attempt_id, "execution_attempt_id"),
        )
        if self.endpoint != f"http://{GOVERNED_HOST}:{GOVERNED_PORT}":
            raise ValueError("attestation endpoint is invalid")
        if type(self.health) is not HttpAttestationEvidence:
            raise TypeError("health evidence is invalid")
        if type(self.models) is not HttpAttestationEvidence:
            raise TypeError("models evidence is invalid")
        if self.props is not None and type(self.props) is not HttpAttestationEvidence:
            raise TypeError("props evidence is invalid")
        if self.model_alias != FEDORA_MODEL_ALIAS:
            raise ValueError("attested model alias is invalid")
        object.__setattr__(self, "attested_at", _utc(self.attested_at, "attested_at"))
        object.__setattr__(self, "fingerprint", _fingerprint(self.to_dict(False)))

    def to_dict(self, include_fingerprint: bool = True) -> dict[str, Any]:
        value = {
            "lifecycle_request_fingerprint": self.lifecycle_request_fingerprint,
            "execution_attempt_id": self.execution_attempt_id,
            "execution_fingerprint": self.execution_fingerprint,
            "artifact_evidence_fingerprint": self.artifact_evidence_fingerprint,
            "binary_fingerprint": self.binary_fingerprint,
            "capacity_evidence_fingerprint": self.capacity_evidence_fingerprint,
            "process_identity_fingerprint": self.process_identity_fingerprint,
            "endpoint": self.endpoint,
            "invocation_fingerprint": self.invocation_fingerprint,
            "health": self.health.to_dict(),
            "models": self.models.to_dict(),
            "props": None if self.props is None else self.props.to_dict(),
            "model_alias": self.model_alias,
            "attested_at": _timestamp(self.attested_at),
        }
        if include_fingerprint:
            value["fingerprint"] = self.fingerprint
        return value

    @classmethod
    def from_dict(cls, value: Any) -> "LocalModelServerAttestation":
        fields = {
            "lifecycle_request_fingerprint", "execution_attempt_id",
            "execution_fingerprint", "artifact_evidence_fingerprint",
            "binary_fingerprint", "capacity_evidence_fingerprint",
            "process_identity_fingerprint", "endpoint", "invocation_fingerprint",
            "health", "models", "props", "model_alias", "attested_at", "fingerprint",
        }
        raw = dict(_strict_dict(value, fields, "attestation"))
        claimed = raw.pop("fingerprint")
        raw["health"] = HttpAttestationEvidence.from_dict(raw["health"])
        raw["models"] = HttpAttestationEvidence.from_dict(raw["models"])
        if raw["props"] is not None:
            raw["props"] = HttpAttestationEvidence.from_dict(raw["props"])
        raw["attested_at"] = _parse_timestamp(raw["attested_at"], "attested_at")
        attestation = cls(**raw)
        if claimed != attestation.fingerprint:
            raise ValueError("attestation fingerprint is invalid")
        return attestation


@dataclass(frozen=True, slots=True)
class LocalModelServerStopRequest:
    stop_request_id: str
    lifecycle_request_id: str
    expected_lifecycle_fingerprint: str
    expected_process_identity: LocalModelServerIdentity
    worker_node_id: str
    execution_attempt_id: str
    execution_fingerprint: str
    shutdown_timeout_seconds: float
    request_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "stop_request_id", "lifecycle_request_id", "worker_node_id",
            "execution_attempt_id",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        for name in ("expected_lifecycle_fingerprint", "execution_fingerprint"):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        if type(self.expected_process_identity) is not LocalModelServerIdentity:
            raise TypeError("expected_process_identity must be LocalModelServerIdentity")
        if self.expected_process_identity.lifecycle_request_id != self.lifecycle_request_id:
            raise ValueError("stop request process identity belongs to another lifecycle")
        object.__setattr__(
            self, "shutdown_timeout_seconds",
            _timeout(self.shutdown_timeout_seconds, "shutdown_timeout_seconds"),
        )
        object.__setattr__(self, "request_fingerprint", _fingerprint(self.to_dict(False)))

    def to_dict(self, include_fingerprint: bool = True) -> dict[str, Any]:
        value = {
            "stop_request_id": self.stop_request_id,
            "lifecycle_request_id": self.lifecycle_request_id,
            "expected_lifecycle_fingerprint": self.expected_lifecycle_fingerprint,
            "expected_process_identity": self.expected_process_identity.to_dict(),
            "worker_node_id": self.worker_node_id,
            "execution_attempt_id": self.execution_attempt_id,
            "execution_fingerprint": self.execution_fingerprint,
            "shutdown_timeout_seconds": self.shutdown_timeout_seconds,
        }
        if include_fingerprint:
            value["request_fingerprint"] = self.request_fingerprint
        return value

    @classmethod
    def from_dict(cls, value: Any) -> "LocalModelServerStopRequest":
        fields = {
            "stop_request_id", "lifecycle_request_id", "expected_lifecycle_fingerprint",
            "expected_process_identity", "worker_node_id", "execution_attempt_id",
            "execution_fingerprint", "shutdown_timeout_seconds", "request_fingerprint",
        }
        raw = dict(_strict_dict(value, fields, "stop request"))
        claimed = raw.pop("request_fingerprint")
        raw["expected_process_identity"] = LocalModelServerIdentity.from_dict(
            raw["expected_process_identity"]
        )
        request = cls(**raw)
        if claimed != request.request_fingerprint:
            raise ValueError("stop request fingerprint is invalid")
        return request


@dataclass(frozen=True, slots=True)
class EndpointObservation:
    occupied: bool
    owner_pid: int | None
    observed_at: datetime
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.occupied) is not bool:
            raise TypeError("occupied must be boolean")
        if self.owner_pid is not None:
            object.__setattr__(self, "owner_pid", _positive_int(self.owner_pid, "owner_pid"))
        if not self.occupied and self.owner_pid is not None:
            raise ValueError("a closed endpoint cannot have an owner PID")
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        object.__setattr__(self, "fingerprint", _fingerprint(self.to_dict(False)))

    def to_dict(self, include_fingerprint: bool = True) -> dict[str, Any]:
        value = {
            "occupied": self.occupied,
            "owner_pid": self.owner_pid,
            "observed_at": _timestamp(self.observed_at),
        }
        if include_fingerprint:
            value["fingerprint"] = self.fingerprint
        return value

    @classmethod
    def from_dict(cls, value: Any) -> "EndpointObservation":
        fields = {"occupied", "owner_pid", "observed_at", "fingerprint"}
        raw = dict(_strict_dict(value, fields, "endpoint observation"))
        claimed = raw.pop("fingerprint")
        raw["observed_at"] = _parse_timestamp(raw["observed_at"], "observed_at")
        observation = cls(**raw)
        if claimed != observation.fingerprint:
            raise ValueError("endpoint observation fingerprint is invalid")
        return observation


@dataclass(frozen=True, slots=True)
class LocalModelServerRecord:
    request: LocalModelServerStartRequest
    state: LocalModelServerLifecycleState
    updated_at: datetime
    previous_record_fingerprint: str | None
    capacity_evidence: CapacityPreflightEvidence | None = None
    artifact_evidence: ArtifactPreflightEvidence | None = None
    binary_identity: StableFileIdentity | None = None
    process_identity: LocalModelServerIdentity | None = None
    attestation: LocalModelServerAttestation | None = None
    stop_request: LocalModelServerStopRequest | None = None
    stop_started_at: datetime | None = None
    stop_completed_at: datetime | None = None
    final_process_observation: str | None = None
    final_endpoint_observation: EndpointObservation | None = None
    failure_code: str | None = None
    failure_detail: str | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    output_truncated: bool = False
    record_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.request) is not LocalModelServerStartRequest:
            raise TypeError("request must be LocalModelServerStartRequest")
        try:
            object.__setattr__(self, "state", LocalModelServerLifecycleState(self.state))
        except (TypeError, ValueError) as exc:
            raise ValueError("lifecycle state is invalid") from exc
        object.__setattr__(self, "updated_at", _utc(self.updated_at, "updated_at"))
        if self.previous_record_fingerprint is not None:
            object.__setattr__(
                self, "previous_record_fingerprint",
                _sha256(self.previous_record_fingerprint, "previous_record_fingerprint"),
            )
        typed = (
            ("capacity_evidence", CapacityPreflightEvidence),
            ("artifact_evidence", ArtifactPreflightEvidence),
            ("binary_identity", StableFileIdentity),
            ("process_identity", LocalModelServerIdentity),
            ("attestation", LocalModelServerAttestation),
            ("stop_request", LocalModelServerStopRequest),
            ("final_endpoint_observation", EndpointObservation),
        )
        for name, expected in typed:
            value = getattr(self, name)
            if value is not None and type(value) is not expected:
                raise TypeError(f"{name} has invalid type")
        for name in ("stop_started_at", "stop_completed_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _utc(value, name))
        for name in ("failure_code", "failure_detail", "final_process_observation"):
            value = getattr(self, name)
            if value is not None:
                _identifier(value, name)
        if type(self.stdout_tail) is not str or type(self.stderr_tail) is not str:
            raise TypeError("output tails must be strings")
        if len(self.stdout_tail.encode("utf-8")) + len(self.stderr_tail.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            raise ValueError("retained output exceeds the lifecycle bound")
        if type(self.output_truncated) is not bool:
            raise TypeError("output_truncated must be boolean")
        self._validate_state_shape()
        object.__setattr__(self, "record_fingerprint", _fingerprint(self.to_dict(False)))

    def _validate_state_shape(self) -> None:
        if self.state is LocalModelServerLifecycleState.REQUESTED:
            if any((self.capacity_evidence, self.artifact_evidence, self.binary_identity)):
                raise ValueError("requested record cannot contain preflight evidence")
        if self.state in (
            LocalModelServerLifecycleState.PREFLIGHT_PASSED,
            LocalModelServerLifecycleState.STARTING,
            LocalModelServerLifecycleState.RUNNING_UNATTESTED,
            LocalModelServerLifecycleState.ATTESTED,
            LocalModelServerLifecycleState.STOPPING,
            LocalModelServerLifecycleState.STOPPED,
        ):
            if any(
                value is None
                for value in (
                    self.capacity_evidence,
                    self.artifact_evidence,
                    self.binary_identity,
                )
            ):
                raise ValueError("post-preflight record is missing preflight evidence")
        if self.state in (
            LocalModelServerLifecycleState.RUNNING_UNATTESTED,
            LocalModelServerLifecycleState.ATTESTED,
            LocalModelServerLifecycleState.STOPPING,
            LocalModelServerLifecycleState.STOPPED,
        ) and self.process_identity is None:
            raise ValueError("running or stopped lifecycle requires process identity")
        if self.state in (
            LocalModelServerLifecycleState.ATTESTED,
            LocalModelServerLifecycleState.STOPPING,
            LocalModelServerLifecycleState.STOPPED,
        ) and self.attestation is None:
            raise ValueError("attested lifecycle requires attestation evidence")
        if self.state in (
            LocalModelServerLifecycleState.STOPPING,
            LocalModelServerLifecycleState.STOPPED,
        ) and (self.stop_request is None or self.stop_started_at is None):
            raise ValueError("stop transition requires stop request evidence")
        if self.state is LocalModelServerLifecycleState.STOPPED and (
            self.stop_completed_at is None
            or self.final_process_observation != "absent"
            or self.final_endpoint_observation is None
        ):
            raise ValueError("stopped record requires deterministic final observations")
        if self.state in (
            LocalModelServerLifecycleState.FAILED,
            LocalModelServerLifecycleState.RECONCILIATION_REQUIRED,
        ) and (self.failure_code is None or self.failure_detail is None):
            raise ValueError("failure state requires typed failure evidence")
        if self.state not in (
            LocalModelServerLifecycleState.FAILED,
            LocalModelServerLifecycleState.RECONCILIATION_REQUIRED,
        ) and (self.failure_code is not None or self.failure_detail is not None):
            raise ValueError("non-failure state cannot contain failure evidence")

    @property
    def lifecycle_request_id(self) -> str:
        return self.request.lifecycle_request_id

    def to_dict(self, include_fingerprint: bool = True) -> dict[str, Any]:
        value = {
            "request": self.request.to_dict(),
            "state": self.state.value,
            "updated_at": _timestamp(self.updated_at),
            "previous_record_fingerprint": self.previous_record_fingerprint,
            "capacity_evidence": (
                None if self.capacity_evidence is None else self.capacity_evidence.to_dict()
            ),
            "artifact_evidence": (
                None if self.artifact_evidence is None else self.artifact_evidence.to_dict()
            ),
            "binary_identity": (
                None if self.binary_identity is None else self.binary_identity.to_dict()
            ),
            "process_identity": (
                None if self.process_identity is None else self.process_identity.to_dict()
            ),
            "attestation": None if self.attestation is None else self.attestation.to_dict(),
            "stop_request": None if self.stop_request is None else self.stop_request.to_dict(),
            "stop_started_at": (
                None if self.stop_started_at is None else _timestamp(self.stop_started_at)
            ),
            "stop_completed_at": (
                None if self.stop_completed_at is None else _timestamp(self.stop_completed_at)
            ),
            "final_process_observation": self.final_process_observation,
            "final_endpoint_observation": (
                None
                if self.final_endpoint_observation is None
                else self.final_endpoint_observation.to_dict()
            ),
            "failure_code": self.failure_code,
            "failure_detail": self.failure_detail,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "output_truncated": self.output_truncated,
        }
        if include_fingerprint:
            value["record_fingerprint"] = self.record_fingerprint
        return value

    @classmethod
    def from_dict(cls, value: Any) -> "LocalModelServerRecord":
        fields = {
            "request", "state", "updated_at", "previous_record_fingerprint",
            "capacity_evidence", "artifact_evidence", "binary_identity",
            "process_identity", "attestation", "stop_request", "stop_started_at",
            "stop_completed_at", "final_process_observation",
            "final_endpoint_observation", "failure_code", "failure_detail",
            "stdout_tail", "stderr_tail", "output_truncated", "record_fingerprint",
        }
        raw = dict(_strict_dict(value, fields, "lifecycle record"))
        claimed = raw.pop("record_fingerprint")
        raw["request"] = LocalModelServerStartRequest.from_dict(raw["request"])
        raw["updated_at"] = _parse_timestamp(raw["updated_at"], "updated_at")
        converters = {
            "capacity_evidence": CapacityPreflightEvidence.from_dict,
            "artifact_evidence": ArtifactPreflightEvidence.from_dict,
            "binary_identity": StableFileIdentity.from_dict,
            "process_identity": LocalModelServerIdentity.from_dict,
            "attestation": LocalModelServerAttestation.from_dict,
            "stop_request": LocalModelServerStopRequest.from_dict,
            "final_endpoint_observation": EndpointObservation.from_dict,
        }
        for name, converter in converters.items():
            if raw[name] is not None:
                raw[name] = converter(raw[name])
        for name in ("stop_started_at", "stop_completed_at"):
            if raw[name] is not None:
                raw[name] = _parse_timestamp(raw[name], name)
        record = cls(**raw)
        if claimed != record.record_fingerprint:
            raise ValueError("lifecycle record fingerprint is invalid")
        return record


def _requirement_to_dict(value: HostResourceRequirement) -> dict[str, Any]:
    return {
        "minimum_available_memory_bytes": value.minimum_available_memory_bytes,
        "minimum_available_storage_bytes": value.minimum_available_storage_bytes,
        "storage_root_path": value.storage_root_path,
        "maximum_normalized_cpu_load": value.maximum_normalized_cpu_load,
        "minimum_swap_headroom_bytes": value.minimum_swap_headroom_bytes,
        "reserve_memory_bytes": value.reserve_memory_bytes,
        "reserve_storage_bytes": value.reserve_storage_bytes,
        "fingerprint": value.fingerprint,
    }


def _requirement_from_dict(value: Any) -> HostResourceRequirement:
    fields = {
        "minimum_available_memory_bytes", "minimum_available_storage_bytes",
        "storage_root_path", "maximum_normalized_cpu_load",
        "minimum_swap_headroom_bytes", "reserve_memory_bytes",
        "reserve_storage_bytes", "fingerprint",
    }
    return HostResourceRequirement(**_strict_dict(value, fields, "capacity requirement"))


def _policy_to_dict(value: HostCapacityPolicy) -> dict[str, Any]:
    return {
        "minimum_host_memory_reserve_bytes": value.minimum_host_memory_reserve_bytes,
        "minimum_swap_reserve_bytes": value.minimum_swap_reserve_bytes,
        "maximum_normalized_load_threshold": value.maximum_normalized_load_threshold,
        "minimum_storage_reserve_bytes": value.minimum_storage_reserve_bytes,
        "swap_pressure_threshold_bytes": value.swap_pressure_threshold_bytes,
        "memory_constrained_reserve_multiplier": value.memory_constrained_reserve_multiplier,
        "cpu_constrained_threshold_ratio": value.cpu_constrained_threshold_ratio,
        "storage_constrained_reserve_multiplier": value.storage_constrained_reserve_multiplier,
        "fingerprint": value.fingerprint,
    }


def _policy_from_dict(value: Any) -> HostCapacityPolicy:
    fields = {
        "minimum_host_memory_reserve_bytes", "minimum_swap_reserve_bytes",
        "maximum_normalized_load_threshold", "minimum_storage_reserve_bytes",
        "swap_pressure_threshold_bytes", "memory_constrained_reserve_multiplier",
        "cpu_constrained_threshold_ratio", "storage_constrained_reserve_multiplier",
        "fingerprint",
    }
    return HostCapacityPolicy(**_strict_dict(value, fields, "capacity policy"))


class LocalModelServerLifecycleStore:
    """Authenticated append-only lifecycle history with process-safe locking."""

    def __init__(self, path: str | os.PathLike[str], *, integrity_key: bytes):
        self.path = Path(path)
        self._key = require_integrity_key(integrity_key)
        self._thread_lock = threading.RLock()

    def current(self, lifecycle_request_id: str) -> LocalModelServerRecord | None:
        lifecycle_request_id = _identifier(lifecycle_request_id, "lifecycle_request_id")
        records = self._records(self._read_envelopes())
        return records.get(lifecycle_request_id)

    def list_records(self) -> tuple[LocalModelServerRecord, ...]:
        records = self._records(self._read_envelopes())
        return tuple(records[key] for key in sorted(records))

    def history(self, lifecycle_request_id: str) -> tuple[LocalModelServerRecord, ...]:
        lifecycle_request_id = _identifier(lifecycle_request_id, "lifecycle_request_id")
        return tuple(
            LocalModelServerRecord.from_dict(envelope["record"])
            for envelope in self._read_envelopes()
            if envelope["record"]["request"]["lifecycle_request_id"] == lifecycle_request_id
        )

    def by_execution_attempt(self, execution_attempt_id: str) -> LocalModelServerRecord | None:
        execution_attempt_id = _identifier(execution_attempt_id, "execution_attempt_id")
        matches = [
            record for record in self.list_records()
            if record.request.execution_attempt_id == execution_attempt_id
        ]
        if len(matches) > 1:
            raise LifecycleStoreIntegrityError(
                "one execution attempt is bound to multiple lifecycle identities"
            )
        return matches[0] if matches else None

    def append(self, record: LocalModelServerRecord) -> LocalModelServerRecord:
        if type(record) is not LocalModelServerRecord:
            raise TypeError("record must be LocalModelServerRecord")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        created = not self.path.exists()
        with self._thread_lock, self.path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                envelopes = self._decode(handle.read())
                current = self._records(envelopes).get(record.lifecycle_request_id)
                self._validate_append(current, record)
                envelope = {
                    "schema_version": _SCHEMA_VERSION,
                    "sequence": len(envelopes) + 1,
                    "predecessor_tag": (
                        envelopes[-1]["authentication_tag"] if envelopes else _GENESIS_TAG
                    ),
                    "record": record.to_dict(),
                }
                envelope["authentication_tag"] = authentication_tag(
                    self._key, _DOMAIN, _canonical(envelope)
                )
                handle.seek(0, os.SEEK_END)
                handle.write(_canonical(envelope) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
                if created:
                    self._fsync_directory()
                return record
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _validate_append(
        current: LocalModelServerRecord | None, proposed: LocalModelServerRecord
    ) -> None:
        if current is None:
            if (
                proposed.state is not LocalModelServerLifecycleState.REQUESTED
                or proposed.previous_record_fingerprint is not None
            ):
                raise LifecycleStateError("new lifecycle must begin with REQUESTED")
            return
        if proposed.request.request_fingerprint != current.request.request_fingerprint:
            raise LifecycleConflictError("lifecycle request identity conflicts")
        if proposed.previous_record_fingerprint != current.record_fingerprint:
            raise LifecycleConflictError("lifecycle predecessor fingerprint conflicts")
        if proposed.state not in _TRANSITIONS.get(current.state, set()):
            raise LifecycleStateError(
                f"transition {current.state.value} -> {proposed.state.value} is not permitted"
            )
        immutable = (
            "capacity_evidence", "artifact_evidence", "binary_identity",
            "process_identity", "attestation",
        )
        for name in immutable:
            old = getattr(current, name)
            new = getattr(proposed, name)
            if old is not None and old != new:
                raise LifecycleConflictError(f"{name} mutated across lifecycle transition")

    def _read_envelopes(self) -> tuple[dict[str, Any], ...]:
        if not self.path.exists():
            return ()
        with self._thread_lock, self.path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return self._decode(handle.read())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _decode(self, data: bytes) -> tuple[dict[str, Any], ...]:
        if not data:
            return ()
        if not data.endswith(b"\n"):
            raise LifecycleStoreIntegrityError("lifecycle history is truncated")
        envelopes: list[dict[str, Any]] = []
        predecessor = _GENESIS_TAG
        for sequence, line in enumerate(data.splitlines(), 1):
            try:
                envelope = json.loads(
                    line, object_pairs_hook=self._unique, parse_constant=self._reject_constant
                )
                _strict_dict(
                    envelope,
                    {
                        "schema_version", "sequence", "predecessor_tag", "record",
                        "authentication_tag",
                    },
                    "lifecycle envelope",
                )
                if (
                    envelope["schema_version"] != _SCHEMA_VERSION
                    or type(envelope["sequence"]) is not int
                    or envelope["sequence"] != sequence
                    or envelope["predecessor_tag"] != predecessor
                ):
                    raise ValueError("lifecycle envelope sequence or chain is invalid")
                unsigned = dict(envelope)
                claimed = unsigned.pop("authentication_tag")
                if not authenticates(self._key, _DOMAIN, _canonical(unsigned), claimed):
                    raise ValueError("lifecycle authentication tag is invalid")
                LocalModelServerRecord.from_dict(envelope["record"])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                if isinstance(exc, LifecycleStoreIntegrityError):
                    raise
                raise LifecycleStoreIntegrityError("lifecycle history is malformed") from exc
            envelopes.append(envelope)
            predecessor = envelope["authentication_tag"]
        self._records(tuple(envelopes))
        return tuple(envelopes)

    def _records(
        self, envelopes: tuple[dict[str, Any], ...]
    ) -> dict[str, LocalModelServerRecord]:
        projected: dict[str, LocalModelServerRecord] = {}
        execution_bindings: dict[str, str] = {}
        for envelope in envelopes:
            try:
                record = LocalModelServerRecord.from_dict(envelope["record"])
                current = projected.get(record.lifecycle_request_id)
                self._validate_append(current, record)
                bound = execution_bindings.get(record.request.execution_attempt_id)
                if bound not in (None, record.lifecycle_request_id):
                    raise LifecycleStoreIntegrityError(
                        "execution attempt is reused by another lifecycle"
                    )
                execution_bindings[record.request.execution_attempt_id] = (
                    record.lifecycle_request_id
                )
                projected[record.lifecycle_request_id] = record
            except LocalModelServerLifecycleError:
                raise
            except (TypeError, ValueError) as exc:
                raise LifecycleStoreIntegrityError(
                    "lifecycle projection is invalid"
                ) from exc
        return projected

    @staticmethod
    def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise LifecycleStoreIntegrityError("duplicate JSON key in lifecycle history")
            value[key] = item
        return value

    @staticmethod
    def _reject_constant(value: str) -> None:
        raise LifecycleStoreIntegrityError(f"invalid JSON constant {value}")

    def _fsync_directory(self) -> None:
        descriptor = os.open(
            self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class ProcessObservation(StrEnum):
    MATCHING = "matching"
    ABSENT = "absent"
    IDENTITY_MISMATCH = "identity_mismatch"


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    stdout_tail: str
    stderr_tail: str
    truncated: bool

    def __post_init__(self) -> None:
        if type(self.stdout_tail) is not str or type(self.stderr_tail) is not str:
            raise TypeError("process output tails must be strings")
        if len(self.stdout_tail.encode()) + len(self.stderr_tail.encode()) > _MAX_OUTPUT_BYTES:
            raise ValueError("process output exceeds retained bound")
        if type(self.truncated) is not bool:
            raise TypeError("truncated must be boolean")


class OpenedStableFile(Protocol):
    identity: StableFileIdentity
    launch_path: str


class LocalModelServerProcessOperations(Protocol):
    def open_binary(self, profile: LocalModelServerProfile) -> Any: ...
    def open_artifact(self, artifact: ArtifactState) -> Any: ...
    def endpoint_observation(self) -> EndpointObservation: ...
    def launch(
        self,
        request: LocalModelServerStartRequest,
        binary: OpenedStableFile,
        artifact: OpenedStableFile,
    ) -> LocalModelServerIdentity: ...
    def observe(self, identity: LocalModelServerIdentity) -> ProcessObservation: ...
    def terminate(self, identity: LocalModelServerIdentity) -> None: ...
    def wait(
        self, identity: LocalModelServerIdentity, timeout_seconds: float
    ) -> ProcessObservation: ...
    def output(self, identity: LocalModelServerIdentity) -> ProcessOutput: ...


@dataclass(frozen=True, slots=True)
class HttpResult:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def __post_init__(self) -> None:
        if type(self.status) is not int or not 100 <= self.status <= 599:
            raise ValueError("HTTP result status is invalid")
        headers = tuple((str(name).lower(), str(value)) for name, value in self.headers)
        object.__setattr__(self, "headers", headers)
        if type(self.body) is not bytes:
            raise TypeError("HTTP result body must be bytes")
        if len(self.body) > _MAX_ATTESTATION_BYTES:
            raise ValueError("HTTP result exceeds attestation bound")


class LocalModelServerHttpTransport(Protocol):
    def get(self, route: str, timeout_seconds: float) -> HttpResult: ...


class LoopbackHttpTransport:
    """Direct no-proxy transport for the one governed loopback endpoint."""

    def get(self, route: str, timeout_seconds: float) -> HttpResult:
        if route not in ("/health", "/v1/models", "/props"):
            raise ValueError("HTTP route is not allowlisted")
        connection = http.client.HTTPConnection(
            GOVERNED_HOST, GOVERNED_PORT, timeout=_timeout(timeout_seconds, "timeout_seconds")
        )
        try:
            connection.request("GET", route, headers={"Accept": "application/json"})
            response = connection.getresponse()
            body = response.read(_MAX_ATTESTATION_BYTES + 1)
            if len(body) > _MAX_ATTESTATION_BYTES:
                raise ValueError("attestation response exceeds maximum size")
            return HttpResult(
                status=response.status,
                headers=tuple(response.getheaders()),
                body=body,
            )
        finally:
            connection.close()


class _OpenedFile:
    def __init__(self, descriptor: int, identity: StableFileIdentity):
        self.descriptor = descriptor
        self.identity = identity
        self.launch_path = f"/proc/self/fd/{descriptor}"

    def __enter__(self) -> "_OpenedFile":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        os.close(self.descriptor)


class _BoundedDrain:
    def __init__(self, handle: Any, maximum: int):
        self._handle = handle
        self._maximum = maximum
        self._tail: deque[bytes] = deque()
        self._retained = 0
        self._total = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                chunk = self._handle.read(4096)
                if not chunk:
                    return
                with self._lock:
                    self._total += len(chunk)
                    self._tail.append(chunk)
                    self._retained += len(chunk)
                    while self._retained > self._maximum and self._tail:
                        overflow = self._retained - self._maximum
                        first = self._tail[0]
                        if len(first) <= overflow:
                            self._tail.popleft()
                            self._retained -= len(first)
                        else:
                            self._tail[0] = first[overflow:]
                            self._retained -= overflow
        finally:
            self._handle.close()

    def snapshot(self) -> tuple[str, bool]:
        with self._lock:
            value = b"".join(self._tail).decode("utf-8", "replace")
            return value, self._total > self._retained


@dataclass(slots=True)
class _LiveProcess:
    process: subprocess.Popen[bytes]
    stdout: _BoundedDrain
    stderr: _BoundedDrain


class SystemLocalModelServerProcessOperations:
    """Linux/Fedora process implementation for the fixed lifecycle profile."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleeper or time.sleep
        self._live: dict[int, _LiveProcess] = {}
        self._lock = threading.RLock()

    def open_binary(self, profile: LocalModelServerProfile) -> _OpenedFile:
        if type(profile) is not LocalModelServerProfile:
            raise TypeError("profile must be LocalModelServerProfile")
        opened = self._open_file(
            profile.binary_path, profile.binary_sha256, require_executable=True
        )
        if opened.identity.canonical_path != profile.binary_path:
            opened.__exit__(None, None, None)
            raise LifecycleBinaryIdentityError("binary canonical path changed")
        return opened

    def open_artifact(self, artifact: ArtifactState) -> _OpenedFile:
        if type(artifact) is not ArtifactState:
            raise TypeError("artifact must be ArtifactState")
        opened = self._open_file(
            artifact.path, artifact.expected_sha256, require_executable=False
        )
        identity = opened.identity
        if (
            identity.size_bytes != artifact.size_bytes
            or identity.device != artifact.device
            or identity.inode != artifact.inode
        ):
            opened.__exit__(None, None, None)
            raise LifecycleArtifactError(
                "artifact physical identity changed between registry verification and launch"
            )
        return opened

    def _open_file(
        self, path: str, expected_sha256: str, *, require_executable: bool
    ) -> _OpenedFile:
        if not os.path.isabs(path):
            raise ValueError("stable file path must be absolute")
        canonical = os.path.realpath(path)
        if canonical != path:
            error = (
                LifecycleBinaryIdentityError
                if require_executable else LifecycleArtifactError
            )
            raise error("symlinks are forbidden in stable launch paths")
        current = Path("/")
        for part in Path(path).parts[1:]:
            current /= part
            if os.path.islink(current):
                error = (
                    LifecycleBinaryIdentityError
                    if require_executable else LifecycleArtifactError
                )
                raise error("symlink component detected in stable launch path")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            error = LifecycleBinaryIdentityError if require_executable else LifecycleArtifactError
            raise error(f"stable launch file could not be opened: {exc}") from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("stable launch file is not regular")
            if require_executable and not before.st_mode & 0o111:
                raise ValueError("allowlisted binary is not executable")
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(os.dup(descriptor), "rb") as handle:
                while chunk := handle.read(1 << 20):
                    digest.update(chunk)
                    size += len(chunk)
            after = os.fstat(descriptor)
            if (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or before.st_mode != after.st_mode
                or size != after.st_size
                or digest.hexdigest() != expected_sha256
            ):
                raise ValueError("stable launch file identity or digest mismatch")
            identity = StableFileIdentity(
                canonical_path=canonical,
                sha256=digest.hexdigest(),
                size_bytes=size,
                device=int(after.st_dev),
                inode=int(after.st_ino),
                mode=int(after.st_mode),
            )
            return _OpenedFile(descriptor, identity)
        except Exception as exc:
            os.close(descriptor)
            error = LifecycleBinaryIdentityError if require_executable else LifecycleArtifactError
            if isinstance(exc, error):
                raise
            raise error(str(exc)) from exc

    def endpoint_observation(self) -> EndpointObservation:
        now = _utc(self._clock(), "clock result")
        owner = self._loopback_listener_owner()
        if owner is not None:
            return EndpointObservation(True, owner, now)
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            probe.bind((GOVERNED_HOST, GOVERNED_PORT))
            return EndpointObservation(False, None, now)
        except OSError:
            return EndpointObservation(True, None, now)
        finally:
            probe.close()

    def _loopback_listener_owner(self) -> int | None:
        target = f"0100007F:{GOVERNED_PORT:04X}"
        inodes: set[str] = set()
        try:
            for table in ("/proc/net/tcp", "/proc/net/tcp6"):
                try:
                    lines = Path(table).read_text().splitlines()[1:]
                except OSError:
                    continue
                for line in lines:
                    columns = line.split()
                    if len(columns) >= 10 and columns[1].upper() == target and columns[3] == "0A":
                        inodes.add(columns[9])
            if not inodes:
                return None
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    for descriptor in (entry / "fd").iterdir():
                        link = os.readlink(descriptor)
                        if link.startswith("socket:[") and link[8:-1] in inodes:
                            return int(entry.name)
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    continue
        except OSError:
            return None
        return None

    def launch(
        self,
        request: LocalModelServerStartRequest,
        binary: OpenedStableFile,
        artifact: OpenedStableFile,
    ) -> LocalModelServerIdentity:
        if type(request) is not LocalModelServerStartRequest:
            raise TypeError("request must be LocalModelServerStartRequest")
        if not isinstance(binary, _OpenedFile) or not isinstance(artifact, _OpenedFile):
            raise TypeError("system launch requires stable opened files")
        argv = _fixed_argv(request.profile, artifact.launch_path)
        environment = dict(_MINIMAL_ENVIRONMENT)
        environment_policy_fingerprint = _environment_policy_fingerprint()
        try:
            process = subprocess.Popen(
                argv,
                executable=binary.launch_path,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                cwd="/",
                env=environment,
                close_fds=True,
                pass_fds=(binary.descriptor, artifact.descriptor),
                start_new_session=True,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise LifecycleProcessLaunchError("allowlisted llama-server launch failed") from exc
        assert process.stdout is not None and process.stderr is not None
        with self._lock:
            self._live[process.pid] = _LiveProcess(
                process=process,
                stdout=_BoundedDrain(process.stdout, _MAX_OUTPUT_BYTES // 2),
                stderr=_BoundedDrain(process.stderr, _MAX_OUTPUT_BYTES // 2),
            )
        try:
            start_ticks = self._process_start_ticks(process.pid)
            boot_id = self._boot_id()
        except Exception as exc:
            if process.poll() is None:
                process.terminate()
            raise LifecycleProcessLaunchError(
                "launched process birth identity could not be captured"
            ) from exc
        return LocalModelServerIdentity(
            lifecycle_request_id=request.lifecycle_request_id,
            pid=process.pid,
            kernel_start_ticks=start_ticks,
            boot_id=boot_id,
            executable=binary.identity,
            argv=argv,
            argv_fingerprint=_fingerprint(list(argv)),
            environment_policy_fingerprint=environment_policy_fingerprint,
            endpoint=request.profile.endpoint,
            created_at=_utc(self._clock(), "clock result"),
        )

    def observe(self, identity: LocalModelServerIdentity) -> ProcessObservation:
        if type(identity) is not LocalModelServerIdentity:
            raise TypeError("identity must be LocalModelServerIdentity")
        proc = Path(f"/proc/{identity.pid}")
        if not proc.exists():
            return ProcessObservation.ABSENT
        try:
            if (
                self._boot_id() != identity.boot_id
                or self._process_start_ticks(identity.pid) != identity.kernel_start_ticks
            ):
                return ProcessObservation.IDENTITY_MISMATCH
            executable_info = os.stat(proc / "exe")
            executable_path = os.path.realpath(os.readlink(proc / "exe"))
            if (
                executable_info.st_dev != identity.executable.device
                or executable_info.st_ino != identity.executable.inode
                or executable_path != identity.executable.canonical_path
            ):
                return ProcessObservation.IDENTITY_MISMATCH
            raw = (proc / "cmdline").read_bytes()
            argv = tuple(
                item.decode("utf-8", "surrogateescape")
                for item in raw.rstrip(b"\0").split(b"\0")
            )
            if argv != identity.argv or _fingerprint(list(argv)) != identity.argv_fingerprint:
                return ProcessObservation.IDENTITY_MISMATCH
        except FileNotFoundError:
            return ProcessObservation.ABSENT
        except (OSError, ValueError):
            return ProcessObservation.IDENTITY_MISMATCH
        return ProcessObservation.MATCHING

    def terminate(self, identity: LocalModelServerIdentity) -> None:
        if self.observe(identity) is not ProcessObservation.MATCHING:
            raise LifecycleStopIdentityError("process identity changed before SIGTERM")
        try:
            os.kill(identity.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise LifecycleStopIdentityError("SIGTERM could not be delivered safely") from exc

    def wait(
        self, identity: LocalModelServerIdentity, timeout_seconds: float
    ) -> ProcessObservation:
        timeout_seconds = _timeout(timeout_seconds, "timeout_seconds")
        deadline = self._monotonic() + timeout_seconds
        while True:
            observation = self.observe(identity)
            if observation is not ProcessObservation.MATCHING:
                return observation
            if self._monotonic() >= deadline:
                return observation
            self._sleep(min(0.05, max(0.0, deadline - self._monotonic())))

    def output(self, identity: LocalModelServerIdentity) -> ProcessOutput:
        with self._lock:
            live = self._live.get(identity.pid)
        if live is None:
            return ProcessOutput("", "", False)
        stdout, stdout_truncated = live.stdout.snapshot()
        stderr, stderr_truncated = live.stderr.snapshot()
        return ProcessOutput(stdout, stderr, stdout_truncated or stderr_truncated)

    @staticmethod
    def _process_start_ticks(pid: int) -> int:
        value = Path(f"/proc/{pid}/stat").read_text()
        suffix = value.rsplit(")", 1)[1].split()
        return int(suffix[19])

    @staticmethod
    def _boot_id() -> str:
        return _identifier(
            Path("/proc/sys/kernel/random/boot_id").read_text().strip(), "boot_id"
        )


def _fixed_argv(
    profile: LocalModelServerProfile, stable_model_launch_path: str
) -> tuple[str, ...]:
    if type(profile) is not LocalModelServerProfile:
        raise TypeError("profile must be LocalModelServerProfile")
    if (
        type(stable_model_launch_path) is not str
        or not stable_model_launch_path.startswith("/proc/self/fd/")
        or not stable_model_launch_path.removeprefix("/proc/self/fd/").isdigit()
    ):
        raise ValueError("model launch path must be a stable inherited descriptor")
    return (
        profile.binary_path,
        "--model", stable_model_launch_path,
        "--alias", profile.model_alias,
        "--host", profile.bind_host,
        "--port", str(profile.port),
        "--ctx-size", str(profile.context_size),
        "--threads", str(profile.thread_count),
        "--threads-batch", str(profile.batch_thread_count),
        "--parallel", str(profile.parallel_count),
        "--n-gpu-layers", str(profile.gpu_layer_count),
        "--offline",
        "--no-webui",
    )


def _environment_policy_fingerprint() -> str:
    return _fingerprint(
        {
            "environment": _MINIMAL_ENVIRONMENT,
            "removed_names": list(_REMOVED_ENVIRONMENT_NAMES),
            "inherit_environment": False,
        }
    )


class _AttestationPending(Exception):
    pass


class LocalModelServerAttestor:
    """Bounded startup attestation using accepted llama.cpp endpoint semantics."""

    def __init__(
        self,
        *,
        transport: LocalModelServerHttpTransport,
        process_operations: LocalModelServerProcessOperations,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        if not callable(getattr(transport, "get", None)):
            raise TypeError("transport must implement get")
        self._transport = transport
        self._process = process_operations
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleeper or time.sleep

    def attest(
        self,
        request: LocalModelServerStartRequest,
        process: LocalModelServerIdentity,
        artifact: ArtifactPreflightEvidence,
        binary: StableFileIdentity,
        capacity: CapacityPreflightEvidence,
    ) -> LocalModelServerAttestation:
        deadline = self._monotonic() + request.startup_timeout_seconds
        last_pending: Exception | None = None
        while True:
            observation = self._process.observe(process)
            if observation is ProcessObservation.ABSENT:
                raise LifecycleProcessExitedError(
                    "llama-server exited before complete startup attestation"
                )
            if observation is ProcessObservation.IDENTITY_MISMATCH:
                raise LifecycleReconciliationRequired(
                    "process identity changed during startup attestation"
                )
            try:
                attestation = self._attempt(
                    request, process, artifact, binary, capacity, deadline
                )
                endpoint = self._process.endpoint_observation()
                if not endpoint.occupied or endpoint.owner_pid != process.pid:
                    raise LifecycleReconciliationRequired(
                        "attested endpoint ownership does not match the governed process"
                    )
                return attestation
            except _AttestationPending as exc:
                last_pending = exc
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise LifecycleStartupTimeoutError(
                    "startup attestation timed out"
                ) from last_pending
            self._sleep(min(0.1, remaining))

    def _attempt(
        self,
        request: LocalModelServerStartRequest,
        process: LocalModelServerIdentity,
        artifact: ArtifactPreflightEvidence,
        binary: StableFileIdentity,
        capacity: CapacityPreflightEvidence,
        deadline: float,
    ) -> LocalModelServerAttestation:
        health_result = self._get("/health", deadline)
        health_payload = self._json_object(
            health_result, "/health", LifecycleHealthAttestationError
        )
        if health_result.status != 200:
            raise _AttestationPending(f"health returned HTTP {health_result.status}")
        if health_payload.get("status") != "ok":
            raise LifecycleHealthAttestationError("health status is not exactly 'ok'")
        health = self._evidence("/health", health_result, {"status": "ok"})

        models_result = self._get("/v1/models", deadline)
        if models_result.status != 200:
            raise LifecycleModelAttestationError(
                f"models endpoint returned HTTP {models_result.status}"
            )
        models_payload = self._json_object(
            models_result, "/v1/models", LifecycleModelAttestationError
        )
        models = models_payload.get("data")
        if type(models) is not list or len(models) != 1 or type(models[0]) is not dict:
            raise LifecycleModelAttestationError(
                "models endpoint must advertise exactly one model object"
            )
        if models[0].get("id") != request.profile.model_alias:
            raise LifecycleModelAttestationError("advertised model alias is not exact")
        models_evidence = self._evidence(
            "/v1/models", models_result, {"model_ids": [models[0]["id"]]}
        )

        props_evidence = None
        if request.profile.chat_capability:
            props_result = self._get("/props", deadline)
            if props_result.status != 200:
                raise LifecyclePropsAttestationError(
                    f"props endpoint returned HTTP {props_result.status}"
                )
            props = self._json_object(
                props_result, "/props", LifecyclePropsAttestationError
            )
            template = props.get("chat_template")
            capabilities = props.get("chat_template_caps")
            if type(template) is not str or not template.strip():
                raise LifecyclePropsAttestationError("chat_template is missing or empty")
            if capabilities is not None and type(capabilities) is not dict:
                raise LifecyclePropsAttestationError("chat_template_caps is malformed")
            props_evidence = self._evidence(
                "/props",
                props_result,
                {
                    "chat_template_sha256": hashlib.sha256(template.encode()).hexdigest(),
                    "chat_template_caps": capabilities,
                },
            )

        return LocalModelServerAttestation(
            lifecycle_request_fingerprint=request.request_fingerprint,
            execution_attempt_id=request.execution_attempt_id,
            execution_fingerprint=request.execution_fingerprint,
            artifact_evidence_fingerprint=artifact.fingerprint,
            binary_fingerprint=binary.fingerprint,
            capacity_evidence_fingerprint=capacity.fingerprint,
            process_identity_fingerprint=process.fingerprint,
            endpoint=request.profile.endpoint,
            invocation_fingerprint=process.argv_fingerprint,
            health=health,
            models=models_evidence,
            props=props_evidence,
            model_alias=request.profile.model_alias,
            attested_at=_utc(self._clock(), "clock result"),
        )

    def _get(self, route: str, deadline: float) -> HttpResult:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise _AttestationPending("attestation deadline expired")
        try:
            result = self._transport.get(route, min(remaining, 2.0))
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise _AttestationPending(f"{route} is not ready") from exc
        if any(name == "location" for name, _ in result.headers) or 300 <= result.status < 400:
            error = {
                "/health": LifecycleHealthAttestationError,
                "/v1/models": LifecycleModelAttestationError,
                "/props": LifecyclePropsAttestationError,
            }[route]
            raise error("redirects are forbidden during startup attestation")
        return result

    @staticmethod
    def _json_object(
        result: HttpResult,
        route: str,
        error: type[LocalModelServerLifecycleError],
    ) -> dict[str, Any]:
        try:
            payload = json.loads(result.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise error(f"{route} response is not valid JSON") from exc
        if type(payload) is not dict:
            raise error(f"{route} response is not a JSON object")
        return payload

    @staticmethod
    def _evidence(
        route: str, result: HttpResult, semantic_value: Any
    ) -> HttpAttestationEvidence:
        return HttpAttestationEvidence(
            route=route,
            status=result.status,
            body_size=len(result.body),
            body_sha256=hashlib.sha256(result.body).hexdigest(),
            semantic_fingerprint=_fingerprint(semantic_value),
        )


class LocalModelServerLifecycleCoordinator:
    """Compose authority, trust, capacity, process, attestation, and durable state."""

    def __init__(
        self,
        *,
        store: LocalModelServerLifecycleStore,
        worker_execution: WorkerExecutionCoordinator,
        artifact_registry: LocalModelArtifactRegistry,
        host_collector: HostResourceCollector,
        capacity_guard: HostCapacityGuard,
        process_operations: LocalModelServerProcessOperations,
        http_transport: LocalModelServerHttpTransport,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        if type(store) is not LocalModelServerLifecycleStore:
            raise TypeError("store must be LocalModelServerLifecycleStore")
        if type(worker_execution) is not WorkerExecutionCoordinator:
            raise TypeError("worker_execution must be authoritative WorkerExecutionCoordinator")
        if type(artifact_registry) is not LocalModelArtifactRegistry:
            raise TypeError("artifact_registry must be LocalModelArtifactRegistry")
        if not callable(getattr(host_collector, "collect", None)):
            raise TypeError("host_collector must implement collect")
        if not callable(getattr(capacity_guard, "evaluate", None)):
            raise TypeError("capacity_guard must implement evaluate")
        required_process_methods = (
            "open_binary", "open_artifact", "endpoint_observation", "launch",
            "observe", "terminate", "wait", "output",
        )
        if any(not callable(getattr(process_operations, name, None)) for name in required_process_methods):
            raise TypeError("process_operations does not implement the lifecycle interface")
        self.store = store
        self.worker_execution = worker_execution
        self.artifact_registry = artifact_registry
        self.host_collector = host_collector
        self.capacity_guard = capacity_guard
        self.process = process_operations
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if not callable(self._clock):
            raise TypeError("clock must be callable")
        self._attestor = LocalModelServerAttestor(
            transport=http_transport,
            process_operations=process_operations,
            clock=self._clock,
            monotonic=monotonic,
            sleeper=sleeper,
        )
        self._endpoint_lock_path = store.path.with_suffix(store.path.suffix + ".endpoint.lock")
        self._thread_lock = threading.RLock()

    def start(self, request: LocalModelServerStartRequest) -> LocalModelServerRecord:
        if type(request) is not LocalModelServerStartRequest:
            raise TypeError("request must be LocalModelServerStartRequest")
        with self._endpoint_lock():
            existing = self.store.current(request.lifecycle_request_id)
            if existing is not None:
                if existing.request.request_fingerprint != request.request_fingerprint:
                    raise LifecycleConflictError("lifecycle_request_id was reused inconsistently")
                if existing.state is LocalModelServerLifecycleState.ATTESTED:
                    if self.process.observe(existing.process_identity) is ProcessObservation.MATCHING:
                        return existing
                    return self._require_reconciliation(
                        existing, "attested_process_identity_changed",
                        "attested process identity is no longer provable",
                    )
                if existing.state in (
                    LocalModelServerLifecycleState.STARTING,
                    LocalModelServerLifecycleState.RUNNING_UNATTESTED,
                    LocalModelServerLifecycleState.STOPPING,
                ):
                    return self._reconcile_locked(existing)
                if existing.state is LocalModelServerLifecycleState.RECONCILIATION_REQUIRED:
                    raise LifecycleReconciliationRequired(
                        "lifecycle is already reconciliation-required"
                    )
                raise LifecycleStateError(
                    "terminal lifecycle identity cannot be replayed; use a new request identity"
                )
            bound = self.store.by_execution_attempt(request.execution_attempt_id)
            if bound is not None:
                raise LifecycleConflictError(
                    "execution attempt is already bound to another lifecycle identity"
                )
            authority = self._validate_authority(request, WorkerExecutionStatus.CLAIMED)
            current = self.store.append(
                LocalModelServerRecord(
                    request=request,
                    state=LocalModelServerLifecycleState.REQUESTED,
                    updated_at=self._now(),
                    previous_record_fingerprint=None,
                )
            )
            process_identity: LocalModelServerIdentity | None = None
            authority_running = False
            try:
                artifact_state = self._verify_artifact(request)
                storage_roots = (
                    (request.capacity_requirement.storage_root_path,)
                    if request.capacity_requirement.storage_root_path is not None else ()
                )
                snapshot = self.host_collector.collect(
                    request.worker_node_id, storage_roots=storage_roots
                )
                if type(snapshot) is not HostResourceSnapshot:
                    raise LifecycleCapacityUnsafeError(
                        "host collector returned non-authoritative snapshot"
                    )
                decision = self.capacity_guard.evaluate(
                    snapshot, request.capacity_requirement, request.capacity_policy
                )
                capacity = CapacityPreflightEvidence.from_decision(decision)
                self._validate_capacity(request, decision)
                endpoint = self.process.endpoint_observation()
                if endpoint.occupied:
                    current = self._transition(
                        current,
                        LocalModelServerLifecycleState.RECONCILIATION_REQUIRED,
                        capacity_evidence=capacity,
                        failure_code="port_occupied_unknown",
                        failure_detail="governed endpoint is occupied without lifecycle ownership",
                        final_endpoint_observation=endpoint,
                    )
                    raise LifecyclePortConflictError(
                        "governed endpoint is occupied and will not be stolen"
                    )
                with self.process.open_binary(request.profile) as binary:
                    self._validate_binary(request.profile, binary.identity)
                    with self.process.open_artifact(artifact_state) as artifact:
                        artifact_evidence = self._artifact_evidence(
                            request, artifact_state, artifact.identity
                        )
                        current = self._transition(
                            current,
                            LocalModelServerLifecycleState.PREFLIGHT_PASSED,
                            capacity_evidence=capacity,
                            artifact_evidence=artifact_evidence,
                            binary_identity=binary.identity,
                        )
                        current = self._transition(
                            current, LocalModelServerLifecycleState.STARTING
                        )
                        self.worker_execution.start(
                            execution_attempt_id=request.execution_attempt_id,
                            actor_node_id=request.worker_node_id,
                        )
                        authority_running = True
                        process_identity = self.process.launch(
                            request, binary, artifact
                        )
                        current = self._transition(
                            current,
                            LocalModelServerLifecycleState.RUNNING_UNATTESTED,
                            process_identity=process_identity,
                        )
                attestation = self._attestor.attest(
                    request,
                    process_identity,
                    current.artifact_evidence,
                    current.binary_identity,
                    current.capacity_evidence,
                )
                output = self.process.output(process_identity)
                return self._transition(
                    current,
                    LocalModelServerLifecycleState.ATTESTED,
                    attestation=attestation,
                    stdout_tail=output.stdout_tail,
                    stderr_tail=output.stderr_tail,
                    output_truncated=output.truncated,
                )
            except LifecyclePortConflictError:
                raise
            except LocalModelServerLifecycleError as exc:
                self._record_start_failure(
                    current, exc, process_identity, authority_running
                )
                raise
            except Exception as exc:
                wrapped = LifecycleProcessLaunchError(
                    "unexpected failure at the governed process launch boundary"
                )
                self._record_start_failure(
                    current, wrapped, process_identity, authority_running
                )
                raise wrapped from exc

    def stop(self, request: LocalModelServerStopRequest) -> LocalModelServerRecord:
        if type(request) is not LocalModelServerStopRequest:
            raise TypeError("request must be LocalModelServerStopRequest")
        with self._endpoint_lock():
            current = self.store.current(request.lifecycle_request_id)
            if current is None:
                raise LifecycleStateError("unknown lifecycle identity")
            if current.state is LocalModelServerLifecycleState.STOPPED:
                if (
                    current.stop_request is not None
                    and current.stop_request.request_fingerprint == request.request_fingerprint
                ):
                    return current
                raise LifecycleConflictError("stopped lifecycle cannot accept another stop identity")
            if current.state is LocalModelServerLifecycleState.STOPPING:
                if (
                    current.stop_request is not None
                    and current.stop_request.request_fingerprint == request.request_fingerprint
                ):
                    return self._reconcile_locked(current)
                raise LifecycleConflictError("stop identity conflicts with in-progress stop")
            if current.state not in (
                LocalModelServerLifecycleState.ATTESTED,
                LocalModelServerLifecycleState.RUNNING_UNATTESTED,
            ):
                raise LifecycleStateError("lifecycle state cannot be stopped")
            self._validate_stop_request(current, request)
            self._validate_authority(current.request, WorkerExecutionStatus.RUNNING)
            process_observation = self.process.observe(current.process_identity)
            endpoint = self.process.endpoint_observation()
            if process_observation is not ProcessObservation.MATCHING:
                return self._require_reconciliation(
                    current, "stop_identity_mismatch",
                    "stored process birth/executable/argv identity does not match",
                )
            if endpoint.occupied and endpoint.owner_pid != current.process_identity.pid:
                return self._require_reconciliation(
                    current, "stop_endpoint_owner_mismatch",
                    "endpoint owner cannot be tied to the governed process",
                )
            current = self._transition(
                current,
                LocalModelServerLifecycleState.STOPPING,
                stop_request=request,
                stop_started_at=self._now(),
            )
            try:
                self.process.terminate(current.process_identity)
            except LifecycleStopIdentityError as exc:
                return self._require_reconciliation(
                    current, "stop_identity_changed_before_signal", str(exc)
                )
            observation = self.process.wait(
                current.process_identity, request.shutdown_timeout_seconds
            )
            if observation is ProcessObservation.MATCHING:
                self._mark_reconciliation(
                    current, "shutdown_timeout",
                    "normal termination timed out; SIGKILL is forbidden by v0.1 policy",
                )
                raise LifecycleShutdownTimeoutError("normal termination timed out")
            if observation is ProcessObservation.IDENTITY_MISMATCH:
                return self._require_reconciliation(
                    current, "shutdown_identity_ambiguous",
                    "PID identity changed while waiting for normal termination",
                )
            final_endpoint = self.process.endpoint_observation()
            self.worker_execution.succeed(
                execution_attempt_id=current.request.execution_attempt_id,
                actor_node_id=current.request.worker_node_id,
                result=WorkerExecutionResultEnvelope(
                    "governed local model server stopped",
                    (current.record_fingerprint, request.request_fingerprint),
                ),
            )
            output = self.process.output(current.process_identity)
            return self._transition(
                current,
                LocalModelServerLifecycleState.STOPPED,
                stop_completed_at=self._now(),
                final_process_observation="absent",
                final_endpoint_observation=final_endpoint,
                stdout_tail=output.stdout_tail,
                stderr_tail=output.stderr_tail,
                output_truncated=output.truncated,
            )

    def reconcile(self, lifecycle_request_id: str) -> LocalModelServerRecord:
        lifecycle_request_id = _identifier(
            lifecycle_request_id, "lifecycle_request_id"
        )
        with self._endpoint_lock():
            current = self.store.current(lifecycle_request_id)
            if current is None:
                raise LifecycleStateError("unknown lifecycle identity")
            return self._reconcile_locked(current)

    def _reconcile_locked(self, current: LocalModelServerRecord) -> LocalModelServerRecord:
        if current.state in (
            LocalModelServerLifecycleState.STOPPED,
            LocalModelServerLifecycleState.FAILED,
        ):
            return current
        if current.state is LocalModelServerLifecycleState.RECONCILIATION_REQUIRED:
            raise LifecycleReconciliationRequired(
                "reconciliation-required is terminal in v0.1"
            )
        authority = self.worker_execution.inspect(current.request.execution_attempt_id)
        if current.process_identity is None:
            endpoint = self.process.endpoint_observation()
            if endpoint.occupied:
                return self._require_reconciliation(
                    current, "unbound_process_possible",
                    "endpoint is occupied but no durable process identity exists",
                )
            if authority.status is WorkerExecutionStatus.RUNNING:
                self.worker_execution.fail(
                    execution_attempt_id=current.request.execution_attempt_id,
                    actor_node_id=current.request.worker_node_id,
                    reason="lifecycle interrupted before durable process identity",
                )
            return self._transition(
                current,
                LocalModelServerLifecycleState.FAILED,
                failure_code="process_absent_after_interruption",
                failure_detail="no process identity exists and governed endpoint is closed",
                final_endpoint_observation=endpoint,
            )
        observation = self.process.observe(current.process_identity)
        if observation is ProcessObservation.IDENTITY_MISMATCH:
            return self._require_reconciliation(
                current, "pid_reuse_or_identity_mismatch",
                "stored PID exists but exact process identity differs",
            )
        if current.state is LocalModelServerLifecycleState.STOPPING:
            if observation is ProcessObservation.MATCHING:
                return self._require_reconciliation(
                    current, "interrupted_stop_process_running",
                    "interrupted stop left the exact process running; no signal was replayed",
                )
            final_endpoint = self.process.endpoint_observation()
            if authority.status is WorkerExecutionStatus.RUNNING:
                self.worker_execution.succeed(
                    execution_attempt_id=current.request.execution_attempt_id,
                    actor_node_id=current.request.worker_node_id,
                    result=WorkerExecutionResultEnvelope(
                        "governed local model server stopped during reconciliation",
                        (current.record_fingerprint,),
                    ),
                )
            return self._transition(
                current,
                LocalModelServerLifecycleState.STOPPED,
                stop_completed_at=self._now(),
                final_process_observation="absent",
                final_endpoint_observation=final_endpoint,
            )
        if observation is ProcessObservation.ABSENT:
            if authority.status is WorkerExecutionStatus.RUNNING:
                self.worker_execution.fail(
                    execution_attempt_id=current.request.execution_attempt_id,
                    actor_node_id=current.request.worker_node_id,
                    reason="governed server absent during lifecycle reconciliation",
                )
            return self._transition(
                current,
                LocalModelServerLifecycleState.FAILED,
                failure_code="process_absent",
                failure_detail="exact governed process is definitely absent",
                final_endpoint_observation=self.process.endpoint_observation(),
            )
        if current.state is LocalModelServerLifecycleState.ATTESTED:
            return current
        if authority.status is not WorkerExecutionStatus.RUNNING:
            return self._require_reconciliation(
                current, "authority_state_mismatch",
                "matching process exists without RUNNING worker execution authority",
            )
        attestation = self._attestor.attest(
            current.request,
            current.process_identity,
            current.artifact_evidence,
            current.binary_identity,
            current.capacity_evidence,
        )
        return self._transition(
            current,
            LocalModelServerLifecycleState.ATTESTED,
            attestation=attestation,
        )

    def _record_start_failure(
        self,
        current: LocalModelServerRecord,
        error: LocalModelServerLifecycleError,
        process_identity: LocalModelServerIdentity | None,
        authority_running: bool,
    ) -> None:
        current = self.store.current(current.lifecycle_request_id) or current
        if current.state in TERMINAL_LOCAL_MODEL_SERVER_STATES:
            return
        if isinstance(error, LifecycleReconciliationRequired):
            self._mark_reconciliation(
                current,
                type(error).__name__,
                str(error) or type(error).__name__,
            )
            return
        if process_identity is not None:
            observation = self.process.observe(process_identity)
            if observation is ProcessObservation.MATCHING:
                try:
                    self.process.terminate(process_identity)
                    observation = self.process.wait(
                        process_identity, current.request.shutdown_timeout_seconds
                    )
                except LocalModelServerLifecycleError:
                    observation = ProcessObservation.IDENTITY_MISMATCH
            if observation is not ProcessObservation.ABSENT:
                self._mark_reconciliation(
                    current,
                    "startup_cleanup_ambiguous",
                    "failed startup could not safely prove exact process shutdown",
                )
                return
        if authority_running:
            try:
                self.worker_execution.fail(
                    execution_attempt_id=current.request.execution_attempt_id,
                    actor_node_id=current.request.worker_node_id,
                    reason=type(error).__name__,
                )
            except Exception:
                self._mark_reconciliation(
                    current,
                    "authority_terminal_transition_failed",
                    "worker execution authority could not be closed after startup failure",
                )
                return
        output = (
            self.process.output(process_identity)
            if process_identity is not None else ProcessOutput("", "", False)
        )
        self._transition(
            current,
            LocalModelServerLifecycleState.FAILED,
            failure_code=type(error).__name__,
            failure_detail=str(error) or type(error).__name__,
            final_endpoint_observation=self.process.endpoint_observation(),
            stdout_tail=output.stdout_tail,
            stderr_tail=output.stderr_tail,
            output_truncated=output.truncated,
        )

    def _verify_artifact(self, request: LocalModelServerStartRequest) -> ArtifactState:
        try:
            artifact = self.artifact_registry.verify(request.artifact_id)
        except Exception as exc:
            raise LifecycleArtifactError(
                "artifact registry verification failed closed"
            ) from exc
        if artifact.status is not ArtifactStatus.VERIFIED:
            raise LifecycleArtifactError("artifact is not in VERIFIED state")
        if (
            artifact.fingerprint != request.artifact_fingerprint
            or artifact.expected_sha256 != request.artifact_sha256
            or artifact.alias != request.profile.model_alias
        ):
            raise LifecycleArtifactError("artifact identity does not match lifecycle request")
        return artifact

    @staticmethod
    def _artifact_evidence(
        request: LocalModelServerStartRequest,
        artifact: ArtifactState,
        stable: StableFileIdentity,
    ) -> ArtifactPreflightEvidence:
        if (
            stable.canonical_path != artifact.path
            or stable.sha256 != artifact.expected_sha256
            or stable.size_bytes != artifact.size_bytes
            or stable.device != artifact.device
            or stable.inode != artifact.inode
            or artifact.verified_at is None
        ):
            raise LifecycleArtifactError(
                "stable artifact descriptor does not match verified registry evidence"
            )
        return ArtifactPreflightEvidence(
            artifact_id=request.artifact_id,
            artifact_fingerprint=artifact.fingerprint,
            artifact_sha256=stable.sha256,
            canonical_path=stable.canonical_path,
            size_bytes=stable.size_bytes,
            device=stable.device,
            inode=stable.inode,
            verified_at=artifact.verified_at,
            stable_file_fingerprint=stable.fingerprint,
        )

    @staticmethod
    def _validate_capacity(
        request: LocalModelServerStartRequest, decision: HostCapacityDecision
    ) -> None:
        if decision.status is HostCapacityStatus.UNSAFE_TO_START:
            raise LifecycleCapacityUnsafeError("host capacity is unsafe to start")
        if (
            decision.status is HostCapacityStatus.CONSTRAINED
            and not request.allow_constrained_capacity
        ):
            raise LifecycleCapacityConstrainedError(
                "host capacity is constrained and v0.1 defaults to fail closed"
            )

    @staticmethod
    def _validate_binary(
        profile: LocalModelServerProfile, identity: StableFileIdentity
    ) -> None:
        if (
            identity.canonical_path != profile.binary_path
            or identity.sha256 != profile.binary_sha256
            or not stat.S_ISREG(identity.mode)
            or not identity.mode & 0o111
        ):
            raise LifecycleBinaryIdentityError(
                "server binary does not match the allowlisted executable identity"
            )

    def _validate_authority(
        self,
        request: LocalModelServerStartRequest,
        required_status: WorkerExecutionStatus,
    ) -> Any:
        try:
            attempt = self.worker_execution.inspect(request.execution_attempt_id)
        except Exception as exc:
            raise LifecycleAuthorizationError(
                "authoritative execution attempt does not exist"
            ) from exc
        if attempt.request.execution_fingerprint is None:
            raise LifecycleAuthorizationError(
                "lifecycle authority requires a real upstream execution fingerprint"
            )
        if (
            attempt.status is not required_status
            or attempt.request.worker_node_id != request.worker_node_id
            or attempt.request.execution_fingerprint != request.execution_fingerprint
        ):
            raise LifecycleAuthorizationError(
                "worker, execution fingerprint, or authority state is not exact"
            )
        return attempt

    def _validate_stop_request(
        self,
        current: LocalModelServerRecord,
        request: LocalModelServerStopRequest,
    ) -> None:
        if (
            request.expected_lifecycle_fingerprint != current.record_fingerprint
            or request.expected_process_identity != current.process_identity
            or request.worker_node_id != current.request.worker_node_id
            or request.execution_attempt_id != current.request.execution_attempt_id
            or request.execution_fingerprint != current.request.execution_fingerprint
        ):
            raise LifecycleStopIdentityError(
                "stop request does not bind the exact current governed lifecycle"
            )

    def _require_reconciliation(
        self,
        current: LocalModelServerRecord,
        code: str,
        detail: str,
    ) -> LocalModelServerRecord:
        self._mark_reconciliation(current, code, detail)
        raise LifecycleReconciliationRequired(detail)

    def _mark_reconciliation(
        self,
        current: LocalModelServerRecord,
        code: str,
        detail: str,
    ) -> LocalModelServerRecord:
        if current.state is LocalModelServerLifecycleState.RECONCILIATION_REQUIRED:
            return current
        return self._transition(
            current,
            LocalModelServerLifecycleState.RECONCILIATION_REQUIRED,
            failure_code=_identifier(code, "failure_code"),
            failure_detail=_identifier(detail, "failure_detail"),
            final_endpoint_observation=self.process.endpoint_observation(),
        )

    def _transition(
        self,
        current: LocalModelServerRecord,
        state: LocalModelServerLifecycleState,
        **changes: Any,
    ) -> LocalModelServerRecord:
        return self.store.append(
            replace(
                current,
                state=state,
                updated_at=self._now(),
                previous_record_fingerprint=current.record_fingerprint,
                **changes,
            )
        )

    def _now(self) -> datetime:
        return _utc(self._clock(), "clock result")

    @contextmanager
    def _endpoint_lock(self) -> Iterator[None]:
        self._endpoint_lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock, self._endpoint_lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = [
    "FEDORA_LLAMA_SERVER_PATH",
    "FEDORA_LLAMA_SERVER_SHA256",
    "FEDORA_LLAMA_CPP_SOURCE_COMMIT",
    "FEDORA_MODEL_ALIAS",
    "GOVERNED_HOST",
    "GOVERNED_PORT",
    "ArtifactPreflightEvidence",
    "CapacityPreflightEvidence",
    "EndpointObservation",
    "HttpAttestationEvidence",
    "HttpResult",
    "LifecycleArtifactError",
    "LifecycleAuthorizationError",
    "LifecycleBinaryIdentityError",
    "LifecycleCapacityConstrainedError",
    "LifecycleCapacityUnsafeError",
    "LifecycleConflictError",
    "LifecycleHealthAttestationError",
    "LifecycleModelAttestationError",
    "LifecyclePortConflictError",
    "LifecycleProcessExitedError",
    "LifecycleProcessLaunchError",
    "LifecyclePropsAttestationError",
    "LifecycleReconciliationRequired",
    "LifecycleShutdownTimeoutError",
    "LifecycleStartupTimeoutError",
    "LifecycleStateError",
    "LifecycleStopIdentityError",
    "LifecycleStoreIntegrityError",
    "LocalModelServerAttestation",
    "LocalModelServerLifecycleCoordinator",
    "LocalModelServerLifecycleError",
    "LocalModelServerLifecycleState",
    "LocalModelServerLifecycleStore",
    "LocalModelServerProfile",
    "LocalModelServerRecord",
    "LocalModelServerStartRequest",
    "LocalModelServerStopRequest",
    "LoopbackHttpTransport",
    "ProcessObservation",
    "ProcessOutput",
    "StableFileIdentity",
    "SystemLocalModelServerProcessOperations",
    "TERMINAL_LOCAL_MODEL_SERVER_STATES",
]
