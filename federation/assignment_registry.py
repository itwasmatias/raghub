"""Durable provenance records for successful routing assignments."""


import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path

from federation.file_lock import fcntl
from federation.task_assignment import TaskAssignment
from federation.integrity import (
    authentication_tag,
    authenticates,
    require_integrity_key,
)


_SCHEMA_VERSION = 1
_AUTHENTICATION_DOMAIN = b"raghub.assignment.v1"
_FIELDS = {
    "schema_version",
    "sequence",
    "assignment_id",
    "assignment_fingerprint",
    "routing_revision",
    "mission_id",
    "task_id",
    "coordinator_node_id",
    "worker_node_id",
    "required_capabilities",
    "authorization_metadata",
    "approval_metadata",
    "authentication_tag",
}


class AssignmentRegistryError(Exception):
    """Base error for authoritative assignment records."""


class AssignmentNotFoundError(AssignmentRegistryError):
    """Raised when an assignment identity is not authoritative."""


class AssignmentConflictError(AssignmentRegistryError):
    """Raised when routing provenance contradicts an existing assignment."""


class AssignmentCorruptionError(AssignmentRegistryError):
    """Raised when durable assignment evidence cannot be trusted."""


@dataclass(slots=True, frozen=True)
class AuthoritativeAssignment:
    """Immutable routing assignment resolved from durable evidence."""

    assignment_id: str
    assignment_fingerprint: str
    routing_revision: int
    mission_id: str
    task_id: str
    coordinator_node_id: str
    worker_node_id: str
    required_capabilities: tuple[str, ...]
    authorization_metadata: tuple[tuple[str, object], ...]
    approval_metadata: tuple[tuple[str, object], ...]


def _canonical(value: dict) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _routing_payload(
    assignment: TaskAssignment,
    coordinator_node_id: str,
    routing_revision: int,
) -> dict:
    authorization_metadata = {
        "level": assignment.task_request.authorization_level.value,
    }
    execution_fingerprint = assignment.task_request.input_data.get(
        "execution_fingerprint"
    )
    if execution_fingerprint is not None:
        if (
            not isinstance(execution_fingerprint, str)
            or len(execution_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in execution_fingerprint)
        ):
            raise ValueError("execution_fingerprint must be a lowercase SHA-256 digest")
        authorization_metadata["execution_fingerprint"] = execution_fingerprint
    return {
        "routing_revision": routing_revision,
        "mission_id": assignment.mission_id,
        "task_id": assignment.task_id,
        "coordinator_node_id": coordinator_node_id,
        "worker_node_id": assignment.node_id,
        "required_capabilities": sorted(
            str(item) for item in assignment.task_request.required_capabilities
        ),
        "authorization_metadata": authorization_metadata,
        "approval_metadata": {
            "required": assignment.task_request.approval_required,
        },
    }


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AssignmentCorruptionError("assignment evidence is corrupt")
        result[key] = value
    return result


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class DurableAssignmentRegistry:
    """Append-only authoritative registry of routing results."""

    def __init__(self, path, *, coordinator_node_id: str, integrity_key: bytes):
        if not isinstance(coordinator_node_id, str) or not coordinator_node_id.strip():
            raise ValueError("coordinator_node_id must be a non-empty string")
        self.path = Path(path)
        self.coordinator_node_id = coordinator_node_id
        self._integrity_key = require_integrity_key(integrity_key)

    def _integrity_key_matches(self, integrity_key: bytes) -> bool:
        candidate = require_integrity_key(integrity_key)
        return hmac.compare_digest(self._integrity_key, candidate)

    def record(
        self,
        assignment: TaskAssignment,
        *,
        routing_revision: int = 1,
    ) -> AuthoritativeAssignment:
        if not isinstance(assignment, TaskAssignment):
            raise TypeError("assignment must be a TaskAssignment")
        if not _is_int(routing_revision) or routing_revision < 1:
            raise ValueError("routing_revision must be a positive integer")

        payload = _routing_payload(
            assignment,
            self.coordinator_node_id,
            routing_revision,
        )
        fingerprint = hashlib.sha256(_canonical(payload)).hexdigest()
        assignment_id = f"assignment-{fingerprint}"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                records = self._decode(handle.read())
                by_id = {item.assignment_id: item for item in records}
                existing = by_id.get(assignment_id)
                if existing is not None:
                    return existing
                task_key = (assignment.mission_id, assignment.task_id)
                if any(
                    (item.mission_id, item.task_id) == task_key for item in records
                ):
                    raise AssignmentConflictError(
                        "mission/task already has an authoritative assignment",
                    )
                record = {
                    "schema_version": _SCHEMA_VERSION,
                    "sequence": len(records) + 1,
                    "assignment_id": assignment_id,
                    "assignment_fingerprint": fingerprint,
                    **payload,
                }
                record["authentication_tag"] = authentication_tag(
                    self._integrity_key,
                    _AUTHENTICATION_DOMAIN,
                    _canonical(record),
                )
                handle.seek(0, 2)
                handle.write(_canonical(record) + b"\n")
                handle.flush()
                __import__("os").fsync(handle.fileno())
                return self._from_record(record)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def resolve(self, assignment_id: str) -> AuthoritativeAssignment:
        if not isinstance(assignment_id, str) or not assignment_id.strip():
            raise ValueError("assignment_id must be a non-empty string")
        records = self._read()
        matches = [item for item in records if item.assignment_id == assignment_id]
        if not matches:
            raise AssignmentNotFoundError(
                f"Unknown authoritative assignment: {assignment_id!r}",
            )
        if len(matches) != 1:
            raise AssignmentCorruptionError("assignment evidence is ambiguous")
        return matches[0]

    def _read(self) -> tuple[AuthoritativeAssignment, ...]:
        if not self.path.exists():
            return ()
        with self.path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return self._decode(handle.read())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _decode(self, data: bytes) -> tuple[AuthoritativeAssignment, ...]:
        if not data:
            return ()
        if not data.endswith(b"\n"):
            raise AssignmentCorruptionError("assignment evidence is incomplete")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AssignmentCorruptionError("assignment evidence is corrupt") from exc

        records = []
        task_keys = set()
        for expected_sequence, line in enumerate(text.splitlines(), 1):
            try:
                record = json.loads(line, object_pairs_hook=_no_duplicate_keys)
            except (json.JSONDecodeError, AssignmentCorruptionError) as exc:
                raise AssignmentCorruptionError(
                    "assignment evidence is corrupt",
                ) from exc
            if not isinstance(record, dict) or set(record) != _FIELDS:
                raise AssignmentCorruptionError("assignment evidence schema is invalid")
            if record["schema_version"] != _SCHEMA_VERSION:
                raise AssignmentCorruptionError("assignment evidence schema is invalid")
            if not _is_int(record["sequence"]) or (
                record["sequence"] != expected_sequence
            ):
                raise AssignmentCorruptionError("assignment sequence is invalid")
            authenticated = dict(record)
            claimed_tag = authenticated.pop("authentication_tag")
            if not authenticates(
                self._integrity_key,
                _AUTHENTICATION_DOMAIN,
                _canonical(authenticated),
                claimed_tag,
            ):
                raise AssignmentCorruptionError(
                    "assignment authentication tag is invalid",
                )
            if not _is_int(record["routing_revision"]) or (
                record["routing_revision"] < 1
            ):
                raise AssignmentCorruptionError("routing revision is invalid")
            string_fields = (
                "assignment_id",
                "assignment_fingerprint",
                "mission_id",
                "task_id",
                "coordinator_node_id",
                "worker_node_id",
            )
            if any(
                not isinstance(record[field], str) or not record[field].strip()
                for field in string_fields
            ):
                raise AssignmentCorruptionError("assignment identity is invalid")
            if (
                not isinstance(record["required_capabilities"], list)
                or any(
                    not isinstance(item, str) or not item
                    for item in record["required_capabilities"]
                )
                or record["required_capabilities"]
                != sorted(set(record["required_capabilities"]))
            ):
                raise AssignmentCorruptionError("assignment capabilities are invalid")
            authorization_metadata = record["authorization_metadata"]
            if not isinstance(authorization_metadata, dict):
                raise AssignmentCorruptionError("authorization metadata is invalid")

            authorization_keys = set(authorization_metadata)
            if authorization_keys not in (
                {"level"},
                {"level", "execution_fingerprint"},
            ) or not isinstance(
                authorization_metadata["level"],
                str,
            ):
                raise AssignmentCorruptionError("authorization metadata is invalid")

            execution_fingerprint = authorization_metadata.get(
                "execution_fingerprint"
            )
            if execution_fingerprint is not None and (
                not isinstance(execution_fingerprint, str)
                or len(execution_fingerprint) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in execution_fingerprint
                )
            ):
                raise AssignmentCorruptionError("authorization metadata is invalid")
            if set(record["approval_metadata"]) != {"required"} or not isinstance(
                record["approval_metadata"]["required"],
                bool,
            ):
                raise AssignmentCorruptionError("approval metadata is invalid")

            payload = {
                key: record[key]
                for key in (
                    "routing_revision",
                    "mission_id",
                    "task_id",
                    "coordinator_node_id",
                    "worker_node_id",
                    "required_capabilities",
                    "authorization_metadata",
                    "approval_metadata",
                )
            }
            fingerprint = hashlib.sha256(_canonical(payload)).hexdigest()
            if record["assignment_fingerprint"] != fingerprint or (
                record["assignment_id"] != f"assignment-{fingerprint}"
            ):
                raise AssignmentCorruptionError("assignment fingerprint is invalid")
            key = (record["mission_id"], record["task_id"])
            if key in task_keys:
                raise AssignmentCorruptionError("assignment evidence is ambiguous")
            task_keys.add(key)
            records.append(self._from_record(record))
        return tuple(records)

    @staticmethod
    def _from_record(record: dict) -> AuthoritativeAssignment:
        return AuthoritativeAssignment(
            assignment_id=record["assignment_id"],
            assignment_fingerprint=record["assignment_fingerprint"],
            routing_revision=record["routing_revision"],
            mission_id=record["mission_id"],
            task_id=record["task_id"],
            coordinator_node_id=record["coordinator_node_id"],
            worker_node_id=record["worker_node_id"],
            required_capabilities=tuple(record["required_capabilities"]),
            authorization_metadata=tuple(
                sorted(record["authorization_metadata"].items()),
            ),
            approval_metadata=tuple(sorted(record["approval_metadata"].items())),
        )
