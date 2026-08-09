"""Durable, provider-independent local model artifact registry (v0.1).

This module records the *identity and byte-level integrity* of local model
artifacts (initially GGUF files) that live inside explicitly trusted
filesystem roots. It is intentionally transport-neutral and provider
independent: it hashes bytes with :mod:`hashlib` in bounded chunks and never
launches a subprocess, opens a socket, or touches the network.

The registry answers exactly one question: *does a concrete regular file at a
canonical path still have the exact bytes and filesystem identity that were
bound when the artifact was registered?* A ``VERIFIED`` artifact is evidence
about bytes only. It does **not** authorize execution. Any later attempt to run
a model must still pass, independently, through RAGHub authorization, human
approval, worker execution, and lifecycle governance.

Durability follows the existing federation conventions: an append-only JSONL
log, HMAC-SHA-256 authentication tags built from the shared integrity helpers,
a predecessor authentication chain, a cross-process ``fcntl`` lock, and
``fsync`` of both the file and its parent directory when the store is created.
Decoding fails closed on truncation, malformed JSON, duplicate keys, schema or
sequence errors, a broken authentication chain, or any tampering. The HMAC key
is never serialized.
"""

from __future__ import annotations

import os
import stat as _stat
import hashlib
import hmac
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from federation.file_lock import fcntl
from federation.integrity import (
    authentication_tag,
    authenticates,
    require_integrity_key,
)


_SCHEMA_VERSION = 1
_EVENT_DOMAIN = b"raghub.local-model-artifact-event.v1"
_GENESIS_TAG = "0" * 64
_SHA256_LENGTH = 64
_HEX_DIGITS = frozenset("0123456789abcdef")
_MAX_METADATA_DEPTH = 64
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))

#: Default bounded read size used while hashing artifact bytes.
DEFAULT_CHUNK_SIZE = 1 << 20

#: Documented GGUF magic. Only the leading bytes are inspected; the file is
#: never parsed and never executed.
GGUF_MAGIC = b"GGUF"

_EVENT_REGISTERED = "registered"
_EVENT_VERIFIED = "verified"
_EVENT_INVALIDATED = "invalidated"
_EVENT_TYPES = frozenset({_EVENT_REGISTERED, _EVENT_VERIFIED, _EVENT_INVALIDATED})

_ENVELOPE_FIELDS = frozenset(
    {
        "schema_version",
        "sequence",
        "predecessor_tag",
        "event_type",
        "payload",
        "authentication_tag",
    }
)

_REGISTERED_FIELDS = frozenset(
    {
        "artifact_id",
        "model_id",
        "display_name",
        "alias",
        "path",
        "format",
        "quantization",
        "expected_sha256",
        "size_bytes",
        "device",
        "inode",
        "runtime_compatibility",
        "metadata",
        "fingerprint",
        "registered_at",
    }
)

_VERIFIED_FIELDS = frozenset(
    {
        "artifact_id",
        "fingerprint",
        "observed_sha256",
        "observed_size",
        "observed_device",
        "observed_inode",
        "verified_at",
    }
)

_INVALIDATED_FIELDS = frozenset(
    {
        "artifact_id",
        "fingerprint",
        "observed_sha256",
        "observed_size",
        "observed_device",
        "observed_inode",
        "reason",
        "detail",
        "invalidated_at",
    }
)


class ArtifactFormat(str, Enum):
    """Provider-independent artifact container format."""

    GGUF = "gguf"


class ArtifactStatus(str, Enum):
    """Derived lifecycle state of an artifact identity."""

    REGISTERED = "registered"
    VERIFIED = "verified"
    INVALIDATED = "invalidated"


class InvalidationReason(str, Enum):
    """Why an artifact identity failed closed during verification."""

    DISAPPEARED = "disappeared"
    NOT_REGULAR_FILE = "not_regular_file"
    SYMLINK_DETECTED = "symlink_detected"
    PATH_UNTRUSTED = "path_untrusted"
    INODE_REPLACEMENT = "inode_replacement"
    SIZE_CHANGED = "size_changed"
    TRUNCATED = "truncated"
    CONTENT_MISMATCH = "content_mismatch"


class LocalModelArtifactError(Exception):
    """Base error for the local model artifact registry."""


class ArtifactNotFoundError(LocalModelArtifactError):
    """Raised when an artifact identity is not authoritative."""


class ArtifactConflictError(LocalModelArtifactError):
    """Raised when a registration contradicts an existing immutable identity."""


class ArtifactPathError(LocalModelArtifactError):
    """Raised when an artifact path is untrusted, unsafe, or not a regular file."""


class ArtifactFormatError(LocalModelArtifactError):
    """Raised when artifact bytes do not match the declared container format."""


class ArtifactVerificationError(LocalModelArtifactError):
    """Raised when registration evidence does not match the expected hash."""


class ArtifactCorruptionError(LocalModelArtifactError):
    """Raised when durable artifact evidence cannot be trusted."""


@dataclass(frozen=True, slots=True)
class ArtifactState:
    """Immutable, detached snapshot of an artifact identity and its state."""

    artifact_id: str
    model_id: str
    display_name: str | None
    alias: str | None
    path: str
    format: ArtifactFormat
    quantization: str | None
    expected_sha256: str
    size_bytes: int
    device: int | None
    inode: int | None
    runtime_compatibility: Mapping[str, Any]
    metadata: Mapping[str, Any]
    fingerprint: str
    status: ArtifactStatus
    registered_at: datetime
    verified_at: datetime | None
    invalidated_at: datetime | None
    last_observed_sha256: str | None
    invalidation_reason: InvalidationReason | None
    invalidation_detail: str | None


@dataclass(frozen=True, slots=True)
class ArtifactEvent:
    """Immutable auditable record of one artifact lifecycle event."""

    sequence: int
    event_type: str
    artifact_id: str
    fingerprint: str
    occurred_at: datetime
    observed_sha256: str | None
    observed_size: int | None
    observed_device: int | None
    observed_inode: int | None
    reason: InvalidationReason | None
    detail: str | None


@dataclass(frozen=True, slots=True)
class _Evidence:
    """Byte-level and filesystem identity captured from a concrete file."""

    canonical_path: str
    size_bytes: int
    device: int
    inode: int
    sha256: str
    header: bytes


class _PathRejection(Exception):
    """Internal signal that a path is unsafe or not a usable regular file."""

    def __init__(self, reason: InvalidationReason, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def _canonical(value: dict) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _identifier(value, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _optional_identifier(value, field_name: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, field_name)


def _require_sha256(value, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    if len(value) != _SHA256_LENGTH or any(
        character not in _HEX_DIGITS for character in value
    ):
        raise ValueError(
            f"{field_name} must be a canonical lowercase SHA-256 hex digest"
        )
    return value


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_timestamp(value, field_name: str) -> datetime:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    normalized = parsed.astimezone(timezone.utc)
    if _format_timestamp(normalized) != value:
        raise ValueError(f"{field_name} must be canonical UTC ISO-8601")
    return normalized


def _normalize_json_value(value, field_name: str, ancestors: set[int], depth: int):
    """Return a fresh plain JSON copy, rejecting anything that cannot be
    faithfully and immutably represented."""

    value_type = type(value)
    if value is None or value_type in (bool, int, str):
        return value
    if value_type is float:
        if not isfinite(value):
            raise ValueError(f"{field_name} float values must be finite")
        return value
    if value_type in (dict, _MAPPING_PROXY_TYPE):
        if depth >= _MAX_METADATA_DEPTH:
            raise ValueError(f"{field_name} nesting exceeds depth {_MAX_METADATA_DEPTH}")
        identity = id(value)
        if identity in ancestors:
            raise ValueError(f"{field_name} must not be cyclic")
        if any(type(key) is not str for key in value):
            raise TypeError(f"{field_name} keys must be strings")
        ancestors.add(identity)
        try:
            return {
                key: _normalize_json_value(
                    value[key], f"{field_name}.{key}", ancestors, depth + 1
                )
                for key in sorted(value)
            }
        finally:
            ancestors.remove(identity)
    if value_type in (list, tuple):
        if depth >= _MAX_METADATA_DEPTH:
            raise ValueError(f"{field_name} nesting exceeds depth {_MAX_METADATA_DEPTH}")
        identity = id(value)
        if identity in ancestors:
            raise ValueError(f"{field_name} must not be cyclic")
        ancestors.add(identity)
        try:
            return [
                _normalize_json_value(item, f"{field_name}[]", ancestors, depth + 1)
                for item in value
            ]
        finally:
            ancestors.remove(identity)
    raise TypeError(
        f"{field_name} values must be JSON-compatible primitives, lists, or dicts"
    )


def _normalize_metadata(value, field_name: str) -> dict:
    if value is None:
        return {}
    if type(value) not in (dict, _MAPPING_PROXY_TYPE):
        raise TypeError(f"{field_name} must be a mapping")
    return _normalize_json_value(value, field_name, set(), 0)


def _freeze_json(value):
    value_type = type(value)
    if value_type is dict:
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if value_type is list:
        return tuple(_freeze_json(item) for item in value)
    return value


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactCorruptionError("artifact evidence has duplicate JSON keys")
        result[key] = value
    return result


def _reject_constant(token):
    raise ArtifactCorruptionError(
        f"artifact evidence contains a non-finite JSON constant: {token}"
    )


@dataclass(frozen=True, slots=True)
class ArtifactRegistration:
    """Immutable registration request describing an artifact identity."""

    artifact_id: str
    model_id: str
    path: str
    expected_sha256: str
    format: ArtifactFormat = ArtifactFormat.GGUF
    quantization: str | None = None
    display_name: str | None = None
    alias: str | None = None
    runtime_compatibility: Any = None
    metadata: Any = None


class LocalModelArtifactRegistry:
    """Append-only, restart-safe authority over local model artifact identity."""

    def __init__(
        self,
        path,
        *,
        integrity_key: bytes,
        trusted_roots,
        clock=None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ):
        self.path = Path(path)
        self._integrity_key = require_integrity_key(integrity_key)
        self._trusted_roots = self._canonical_roots(trusted_roots)
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if not _is_int(chunk_size) or chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer")
        self._chunk_size = chunk_size

    # -- construction helpers ------------------------------------------------

    @staticmethod
    def _canonical_roots(trusted_roots) -> tuple[str, ...]:
        if isinstance(trusted_roots, (str, bytes, os.PathLike)):
            trusted_roots = [trusted_roots]
        roots = []
        for entry in trusted_roots:
            candidate = Path(os.fspath(entry))
            if not candidate.is_absolute():
                raise ValueError("trusted roots must be absolute paths")
            real = os.path.realpath(str(candidate))
            if not os.path.isdir(real):
                raise ValueError(f"trusted root is not an existing directory: {real!r}")
            roots.append(real)
        if not roots:
            raise ValueError("at least one trusted root must be configured")
        return tuple(sorted(set(roots)))

    def _integrity_key_matches(self, integrity_key: bytes) -> bool:
        candidate = require_integrity_key(integrity_key)
        return hmac.compare_digest(self._integrity_key, candidate)

    # -- clock ---------------------------------------------------------------

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("clock must return a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    # -- path and byte evidence ---------------------------------------------

    def _evaluate_path(self, raw) -> str:
        """Return the canonical path or raise :class:`_PathRejection`."""

        text = os.fspath(raw)
        if isinstance(text, bytes):
            text = text.decode("utf-8", "surrogateescape")
        if not text.strip():
            raise _PathRejection(
                InvalidationReason.PATH_UNTRUSTED, "artifact path must be non-empty"
            )
        candidate = os.path.abspath(text)
        root = self._match_root(candidate)
        if root is None:
            raise _PathRejection(
                InvalidationReason.PATH_UNTRUSTED,
                "artifact path is outside every trusted root",
            )
        current = Path(root)
        for part in Path(candidate).relative_to(root).parts:
            current = current / part
            if os.path.islink(current):
                raise _PathRejection(
                    InvalidationReason.SYMLINK_DETECTED,
                    "artifact path contains a symlink component",
                )
        try:
            info = os.lstat(candidate)
        except FileNotFoundError as exc:
            raise _PathRejection(
                InvalidationReason.DISAPPEARED, "artifact file does not exist"
            ) from exc
        except OSError as exc:
            raise _PathRejection(
                InvalidationReason.NOT_REGULAR_FILE,
                f"artifact path is not accessible: {exc}",
            ) from exc
        if _stat.S_ISLNK(info.st_mode):
            raise _PathRejection(
                InvalidationReason.SYMLINK_DETECTED, "artifact path is a symlink"
            )
        if not _stat.S_ISREG(info.st_mode):
            raise _PathRejection(
                InvalidationReason.NOT_REGULAR_FILE,
                "artifact path is not a regular file",
            )
        return candidate

    def _match_root(self, candidate: str) -> str | None:
        for root in self._trusted_roots:
            try:
                Path(candidate).relative_to(root)
            except ValueError:
                continue
            return root
        return None

    def _read_evidence(self, raw) -> _Evidence:
        canonical = self._evaluate_path(raw)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(canonical, flags)
        except FileNotFoundError as exc:
            raise _PathRejection(
                InvalidationReason.DISAPPEARED, "artifact file does not exist"
            ) from exc
        except OSError as exc:
            raise _PathRejection(
                InvalidationReason.SYMLINK_DETECTED,
                f"artifact file could not be opened safely: {exc}",
            ) from exc
        try:
            info = os.fstat(descriptor)
            if not _stat.S_ISREG(info.st_mode):
                raise _PathRejection(
                    InvalidationReason.NOT_REGULAR_FILE,
                    "artifact path is not a regular file",
                )
            digest = hashlib.sha256()
            header = b""
            size = 0
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                descriptor = None
                while True:
                    chunk = handle.read(self._chunk_size)
                    if not chunk:
                        break
                    if len(header) < 4:
                        header += chunk[: 4 - len(header)]
                    digest.update(chunk)
                    size += len(chunk)
        finally:
            if descriptor is not None:
                os.close(descriptor)
        return _Evidence(
            canonical_path=canonical,
            size_bytes=size,
            device=int(info.st_dev),
            inode=int(info.st_ino),
            sha256=digest.hexdigest(),
            header=header[:4],
        )

    # -- fingerprint ---------------------------------------------------------

    @staticmethod
    def _fingerprint(payload: dict) -> str:
        identity = {
            "artifact_id": payload["artifact_id"],
            "model_id": payload["model_id"],
            "display_name": payload["display_name"],
            "alias": payload["alias"],
            "path": payload["path"],
            "format": payload["format"],
            "quantization": payload["quantization"],
            "expected_sha256": payload["expected_sha256"],
            "size_bytes": payload["size_bytes"],
            "device": payload["device"],
            "inode": payload["inode"],
            "runtime_compatibility": payload["runtime_compatibility"],
            "metadata": payload["metadata"],
        }
        return hashlib.sha256(_canonical(identity)).hexdigest()

    # -- registration -------------------------------------------------------

    def register(self, registration: ArtifactRegistration | None = None, **kwargs):
        if registration is None:
            registration = ArtifactRegistration(**kwargs)
        elif kwargs:
            raise TypeError("provide either a registration object or keyword fields")
        if not isinstance(registration, ArtifactRegistration):
            raise TypeError("registration must be an ArtifactRegistration")

        artifact_id = _identifier(registration.artifact_id, "artifact_id")
        model_id = _identifier(registration.model_id, "model_id")
        display_name = _optional_identifier(registration.display_name, "display_name")
        alias = _optional_identifier(registration.alias, "alias")
        quantization = _optional_identifier(registration.quantization, "quantization")
        expected_sha256 = _require_sha256(
            registration.expected_sha256, "expected_sha256"
        )
        artifact_format = registration.format
        if isinstance(artifact_format, str) and not isinstance(
            artifact_format, ArtifactFormat
        ):
            artifact_format = ArtifactFormat(artifact_format)
        if not isinstance(artifact_format, ArtifactFormat):
            raise TypeError("format must be an ArtifactFormat")
        runtime_compatibility = _normalize_metadata(
            registration.runtime_compatibility, "runtime_compatibility"
        )
        metadata = _normalize_metadata(registration.metadata, "metadata")

        try:
            evidence = self._read_evidence(registration.path)
        except _PathRejection as rejection:
            raise ArtifactPathError(rejection.message) from rejection

        if evidence.sha256 != expected_sha256:
            raise ArtifactVerificationError(
                "artifact bytes do not match the expected SHA-256 digest"
            )
        if artifact_format is ArtifactFormat.GGUF and evidence.header != GGUF_MAGIC:
            raise ArtifactFormatError("artifact bytes do not start with the GGUF magic")

        registered_at = self._now()
        payload = {
            "artifact_id": artifact_id,
            "model_id": model_id,
            "display_name": display_name,
            "alias": alias,
            "path": evidence.canonical_path,
            "format": artifact_format.value,
            "quantization": quantization,
            "expected_sha256": expected_sha256,
            "size_bytes": evidence.size_bytes,
            "device": evidence.device,
            "inode": evidence.inode,
            "runtime_compatibility": runtime_compatibility,
            "metadata": metadata,
            "registered_at": _format_timestamp(registered_at),
        }
        payload["fingerprint"] = self._fingerprint(payload)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        created = not self.path.exists()
        with self.path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                records = self._decode(handle.read())
                states = self._project(records)
                existing = states.get(artifact_id)
                if existing is not None:
                    if existing.fingerprint == payload["fingerprint"]:
                        return existing
                    raise ArtifactConflictError(
                        "artifact_id already bound to a different immutable identity"
                    )
                envelope = self._append(handle, records, _EVENT_REGISTERED, payload)
                if created:
                    self._fsync_directory()
                return self._project(records + (envelope,))[artifact_id]
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # -- verification -------------------------------------------------------

    def verify(self, artifact_id: str) -> ArtifactState:
        artifact_id = _identifier(artifact_id, "artifact_id")
        if not self.path.exists():
            raise ArtifactNotFoundError(f"unknown artifact: {artifact_id!r}")
        with self.path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                records = self._decode(handle.read())
                states = self._project(records)
                state = states.get(artifact_id)
                if state is None:
                    raise ArtifactNotFoundError(f"unknown artifact: {artifact_id!r}")
                if state.status is ArtifactStatus.INVALIDATED:
                    return state

                observed, rejection = self._observe(state.path)
                verdict = self._compare(state, observed, rejection)
                if verdict is None:
                    if state.status is ArtifactStatus.VERIFIED:
                        return state
                    payload = {
                        "artifact_id": artifact_id,
                        "fingerprint": state.fingerprint,
                        "observed_sha256": observed.sha256,
                        "observed_size": observed.size_bytes,
                        "observed_device": observed.device,
                        "observed_inode": observed.inode,
                        "verified_at": _format_timestamp(self._now()),
                    }
                    envelope = self._append(handle, records, _EVENT_VERIFIED, payload)
                    return self._project(records + (envelope,))[artifact_id]
                reason, detail, observed_hash, observed_size, observed_dev, observed_ino = verdict
                payload = {
                    "artifact_id": artifact_id,
                    "fingerprint": state.fingerprint,
                    "observed_sha256": observed_hash,
                    "observed_size": observed_size,
                    "observed_device": observed_dev,
                    "observed_inode": observed_ino,
                    "reason": reason.value,
                    "detail": detail,
                    "invalidated_at": _format_timestamp(self._now()),
                }
                envelope = self._append(handle, records, _EVENT_INVALIDATED, payload)
                return self._project(records + (envelope,))[artifact_id]
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _observe(self, path):
        try:
            return self._read_evidence(path), None
        except _PathRejection as rejection:
            return None, rejection

    @staticmethod
    def _compare(state: ArtifactState, observed, rejection):
        """Return ``None`` if evidence matches, else an invalidation verdict."""

        if rejection is not None:
            return (rejection.reason, rejection.message, None, None, None, None)
        if state.inode is not None and observed.inode != state.inode:
            return (
                InvalidationReason.INODE_REPLACEMENT,
                "artifact inode changed since registration",
                observed.sha256,
                observed.size_bytes,
                observed.device,
                observed.inode,
            )
        if state.device is not None and observed.device != state.device:
            return (
                InvalidationReason.INODE_REPLACEMENT,
                "artifact device changed since registration",
                observed.sha256,
                observed.size_bytes,
                observed.device,
                observed.inode,
            )
        if observed.size_bytes != state.size_bytes:
            reason = (
                InvalidationReason.TRUNCATED
                if observed.size_bytes < state.size_bytes
                else InvalidationReason.SIZE_CHANGED
            )
            return (
                reason,
                "artifact size changed since registration",
                observed.sha256,
                observed.size_bytes,
                observed.device,
                observed.inode,
            )
        if observed.sha256 != state.expected_sha256:
            return (
                InvalidationReason.CONTENT_MISMATCH,
                "artifact bytes changed since registration",
                observed.sha256,
                observed.size_bytes,
                observed.device,
                observed.inode,
            )
        return None

    # -- read-only projection -----------------------------------------------

    def resolve(self, artifact_id: str) -> ArtifactState:
        artifact_id = _identifier(artifact_id, "artifact_id")
        state = self._project(self._read()).get(artifact_id)
        if state is None:
            raise ArtifactNotFoundError(f"unknown artifact: {artifact_id!r}")
        return state

    def current(self, artifact_id: str) -> ArtifactState | None:
        artifact_id = _identifier(artifact_id, "artifact_id")
        return self._project(self._read()).get(artifact_id)

    def is_verified(self, artifact_id: str) -> bool:
        state = self.current(artifact_id)
        return state is not None and state.status is ArtifactStatus.VERIFIED

    def list_artifacts(self) -> tuple[ArtifactState, ...]:
        states = self._project(self._read())
        return tuple(states[key] for key in sorted(states))

    def history(self, artifact_id: str) -> tuple[ArtifactEvent, ...]:
        artifact_id = _identifier(artifact_id, "artifact_id")
        events = []
        for record in self._read():
            payload = record["payload"]
            if payload["artifact_id"] != artifact_id:
                continue
            events.append(self._event_from_record(record))
        return tuple(events)

    def _event_from_record(self, record: dict) -> ArtifactEvent:
        event_type = record["event_type"]
        payload = record["payload"]
        if event_type == _EVENT_REGISTERED:
            occurred = _parse_timestamp(payload["registered_at"], "registered_at")
            return ArtifactEvent(
                sequence=record["sequence"],
                event_type=event_type,
                artifact_id=payload["artifact_id"],
                fingerprint=payload["fingerprint"],
                occurred_at=occurred,
                observed_sha256=payload["expected_sha256"],
                observed_size=payload["size_bytes"],
                observed_device=payload["device"],
                observed_inode=payload["inode"],
                reason=None,
                detail=None,
            )
        if event_type == _EVENT_VERIFIED:
            occurred = _parse_timestamp(payload["verified_at"], "verified_at")
            reason = None
            detail = None
        else:
            occurred = _parse_timestamp(payload["invalidated_at"], "invalidated_at")
            reason = InvalidationReason(payload["reason"])
            detail = payload["detail"]
        return ArtifactEvent(
            sequence=record["sequence"],
            event_type=event_type,
            artifact_id=payload["artifact_id"],
            fingerprint=payload["fingerprint"],
            occurred_at=occurred,
            observed_sha256=payload["observed_sha256"],
            observed_size=payload["observed_size"],
            observed_device=payload["observed_device"],
            observed_inode=payload["observed_inode"],
            reason=reason,
            detail=detail,
        )

    def _project(self, records) -> dict[str, ArtifactState]:
        states: dict[str, ArtifactState] = {}
        for record in records:
            event_type = record["event_type"]
            payload = record["payload"]
            artifact_id = payload["artifact_id"]
            if event_type == _EVENT_REGISTERED:
                states[artifact_id] = self._state_from_registration(payload)
            elif event_type == _EVENT_VERIFIED:
                states[artifact_id] = self._apply_verified(states[artifact_id], payload)
            else:
                states[artifact_id] = self._apply_invalidated(
                    states[artifact_id], payload
                )
        return states

    @staticmethod
    def _state_from_registration(payload: dict) -> ArtifactState:
        return ArtifactState(
            artifact_id=payload["artifact_id"],
            model_id=payload["model_id"],
            display_name=payload["display_name"],
            alias=payload["alias"],
            path=payload["path"],
            format=ArtifactFormat(payload["format"]),
            quantization=payload["quantization"],
            expected_sha256=payload["expected_sha256"],
            size_bytes=payload["size_bytes"],
            device=payload["device"],
            inode=payload["inode"],
            runtime_compatibility=_freeze_json(payload["runtime_compatibility"]),
            metadata=_freeze_json(payload["metadata"]),
            fingerprint=payload["fingerprint"],
            status=ArtifactStatus.REGISTERED,
            registered_at=_parse_timestamp(payload["registered_at"], "registered_at"),
            verified_at=None,
            invalidated_at=None,
            last_observed_sha256=payload["expected_sha256"],
            invalidation_reason=None,
            invalidation_detail=None,
        )

    @staticmethod
    def _apply_verified(state: ArtifactState, payload: dict) -> ArtifactState:
        return replace(
            state,
            status=ArtifactStatus.VERIFIED,
            verified_at=_parse_timestamp(payload["verified_at"], "verified_at"),
            last_observed_sha256=payload["observed_sha256"],
        )

    @staticmethod
    def _apply_invalidated(state: ArtifactState, payload: dict) -> ArtifactState:
        return replace(
            state,
            status=ArtifactStatus.INVALIDATED,
            invalidated_at=_parse_timestamp(payload["invalidated_at"], "invalidated_at"),
            last_observed_sha256=payload["observed_sha256"],
            invalidation_reason=InvalidationReason(payload["reason"]),
            invalidation_detail=payload["detail"],
        )

    # -- durable log ---------------------------------------------------------

    def _append(self, handle, records, event_type: str, payload: dict) -> dict:
        envelope = self._envelope_for(records, event_type, payload)
        handle.seek(0, os.SEEK_END)
        handle.write(_canonical(envelope) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
        return envelope

    def _envelope_for(self, records, event_type: str, payload: dict) -> dict:
        predecessor = records[-1]["authentication_tag"] if records else _GENESIS_TAG
        unsigned = {
            "schema_version": _SCHEMA_VERSION,
            "sequence": len(records) + 1,
            "predecessor_tag": predecessor,
            "event_type": event_type,
            "payload": payload,
        }
        tag = authentication_tag(self._integrity_key, _EVENT_DOMAIN, _canonical(unsigned))
        return {**unsigned, "authentication_tag": tag}

    def _fsync_directory(self) -> None:
        directory = os.open(
            self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _read(self) -> tuple[dict, ...]:
        if not self.path.exists():
            return ()
        with self.path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return self._decode(handle.read())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _decode(self, data: bytes) -> tuple[dict, ...]:
        if not data:
            return ()
        if not data.endswith(b"\n"):
            raise ArtifactCorruptionError("artifact evidence is truncated")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArtifactCorruptionError("artifact evidence is not UTF-8") from exc

        records: list[dict] = []
        predecessor = _GENESIS_TAG
        statuses: dict[str, str] = {}
        fingerprints: dict[str, str] = {}
        for expected_sequence, line in enumerate(text.splitlines(), 1):
            try:
                record = json.loads(
                    line,
                    object_pairs_hook=_no_duplicate_keys,
                    parse_constant=_reject_constant,
                )
            except (json.JSONDecodeError, ArtifactCorruptionError) as exc:
                raise ArtifactCorruptionError("artifact evidence is malformed") from exc
            self._validate_envelope(record, expected_sequence, predecessor)
            self._validate_payload(record, statuses, fingerprints)
            records.append(record)
            predecessor = record["authentication_tag"]
        return tuple(records)

    def _validate_envelope(self, record, expected_sequence, predecessor) -> None:
        if not isinstance(record, dict) or set(record) != _ENVELOPE_FIELDS:
            raise ArtifactCorruptionError("artifact evidence schema is invalid")
        if record["schema_version"] != _SCHEMA_VERSION:
            raise ArtifactCorruptionError("artifact evidence schema version is invalid")
        if not _is_int(record["sequence"]) or record["sequence"] != expected_sequence:
            raise ArtifactCorruptionError("artifact evidence sequence is invalid")
        if record["predecessor_tag"] != predecessor:
            raise ArtifactCorruptionError("artifact authentication chain is broken")
        if record["event_type"] not in _EVENT_TYPES:
            raise ArtifactCorruptionError("artifact event type is invalid")
        if not isinstance(record["payload"], dict):
            raise ArtifactCorruptionError("artifact payload is invalid")
        unsigned = {key: record[key] for key in record if key != "authentication_tag"}
        claimed = record["authentication_tag"]
        if not isinstance(claimed, str) or not authenticates(
            self._integrity_key, _EVENT_DOMAIN, _canonical(unsigned), claimed
        ):
            raise ArtifactCorruptionError("artifact authentication tag is invalid")

    def _validate_payload(self, record, statuses, fingerprints) -> None:
        event_type = record["event_type"]
        payload = record["payload"]
        if event_type == _EVENT_REGISTERED:
            self._validate_registered_payload(payload, statuses, fingerprints)
        elif event_type == _EVENT_VERIFIED:
            self._validate_transition_payload(
                payload, statuses, fingerprints, _VERIFIED_FIELDS, allow_after_verified=False
            )
            statuses[payload["artifact_id"]] = _EVENT_VERIFIED
        else:
            self._validate_transition_payload(
                payload, statuses, fingerprints, _INVALIDATED_FIELDS, allow_after_verified=True
            )
            if payload["reason"] not in {reason.value for reason in InvalidationReason}:
                raise ArtifactCorruptionError("artifact invalidation reason is invalid")
            if type(payload["detail"]) is not str or not payload["detail"]:
                raise ArtifactCorruptionError("artifact invalidation detail is invalid")
            statuses[payload["artifact_id"]] = _EVENT_INVALIDATED

    def _validate_registered_payload(self, payload, statuses, fingerprints) -> None:
        if set(payload) != _REGISTERED_FIELDS:
            raise ArtifactCorruptionError("artifact registration schema is invalid")
        try:
            artifact_id = _identifier(payload["artifact_id"], "artifact_id")
            _identifier(payload["model_id"], "model_id")
            _optional_identifier(payload["display_name"], "display_name")
            _optional_identifier(payload["alias"], "alias")
            _optional_identifier(payload["quantization"], "quantization")
            _require_sha256(payload["expected_sha256"], "expected_sha256")
        except (TypeError, ValueError) as exc:
            raise ArtifactCorruptionError("artifact registration is invalid") from exc
        if artifact_id in statuses:
            raise ArtifactCorruptionError("artifact registered more than once")
        if payload["format"] not in {fmt.value for fmt in ArtifactFormat}:
            raise ArtifactCorruptionError("artifact format is invalid")
        if not _is_int(payload["size_bytes"]) or payload["size_bytes"] < 0:
            raise ArtifactCorruptionError("artifact size is invalid")
        for field_name in ("device", "inode"):
            value = payload[field_name]
            if value is not None and not _is_int(value):
                raise ArtifactCorruptionError(f"artifact {field_name} is invalid")
        for field_name in ("runtime_compatibility", "metadata"):
            try:
                normalized = _normalize_metadata(payload[field_name], field_name)
            except (TypeError, ValueError) as exc:
                raise ArtifactCorruptionError(
                    f"artifact {field_name} is invalid"
                ) from exc
            if normalized != payload[field_name]:
                raise ArtifactCorruptionError(
                    f"artifact {field_name} is not canonical"
                )
        _parse_timestamp(payload["registered_at"], "registered_at")
        if payload["fingerprint"] != self._fingerprint(payload):
            raise ArtifactCorruptionError("artifact fingerprint does not match identity")
        statuses[artifact_id] = _EVENT_REGISTERED
        fingerprints[artifact_id] = payload["fingerprint"]

    @staticmethod
    def _validate_transition_payload(
        payload, statuses, fingerprints, fields, *, allow_after_verified
    ) -> None:
        if set(payload) != fields:
            raise ArtifactCorruptionError("artifact transition schema is invalid")
        artifact_id = payload["artifact_id"]
        if type(artifact_id) is not str or artifact_id not in statuses:
            raise ArtifactCorruptionError("artifact transition references unknown identity")
        previous = statuses[artifact_id]
        if previous == _EVENT_INVALIDATED:
            raise ArtifactCorruptionError("artifact transition after invalidation")
        if previous == _EVENT_VERIFIED and not allow_after_verified:
            raise ArtifactCorruptionError("artifact verified more than once")
        if payload["fingerprint"] != fingerprints.get(artifact_id):
            raise ArtifactCorruptionError("artifact transition fingerprint mismatch")
        for field_name in ("observed_size", "observed_device", "observed_inode"):
            value = payload[field_name]
            if value is not None and not _is_int(value):
                raise ArtifactCorruptionError(f"artifact {field_name} is invalid")
        observed_hash = payload["observed_sha256"]
        if observed_hash is not None and (
            type(observed_hash) is not str
            or len(observed_hash) != _SHA256_LENGTH
            or any(character not in _HEX_DIGITS for character in observed_hash)
        ):
            raise ArtifactCorruptionError("artifact observed digest is invalid")
        timestamp_field = "verified_at" if "verified_at" in fields else "invalidated_at"
        _parse_timestamp(payload[timestamp_field], timestamp_field)
