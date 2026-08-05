"""
Durable event log for mission orchestration.

Events are appended to a per-mission JSONL file under a shared lock. Existing
bytes are validated before every append; malformed or incomplete evidence is
never skipped or extended.
"""

from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tools.ai_controller._locking import FileLock
from .models import MissionEvent

logger = logging.getLogger(__name__)


class EventLogCorruptionError(ValueError):
    """Raised when durable event bytes are not a complete valid JSONL log."""


_EVENT_FIELDS = {
    "event_type",
    "timestamp",
    "mission_id",
    "task_id",
    "reason",
    "queue_task_id",
    "report_path",
    "provider",
    "metadata",
}
_TASK_IDENTITY_EVENTS = {
    "repair_started",
    "repair_applied",
    "repair_expired",
}
_QUEUE_IDENTITY_EVENTS = {
    "task_enqueued",
    "task_running",
    "task_succeeded",
    "task_failed",
    "task_lost",
    "task_retry_queued",
    *_TASK_IDENTITY_EVENTS,
}


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TypeError(f"duplicate field {key!r}")
        result[key] = value
    return result


def _validate_json_value(value: Any) -> None:
    """Reject values that cannot form one finite, unambiguous JSON event."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("event JSON values must be finite")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("event metadata keys must be strings")
            _validate_json_value(item)
        return
    raise TypeError("event contains a non-JSON value")


def _event_from_data(
    data: dict[str, Any],
    *,
    expected_mission_id: str | None = None,
) -> MissionEvent:
    required = {"event_type", "timestamp", "mission_id"}
    if required - set(data) or set(data) - _EVENT_FIELDS:
        raise TypeError("event record has missing or unknown fields")
    event_type = data["event_type"]
    timestamp = data["timestamp"]
    mission_id = data["mission_id"]
    metadata = data.get("metadata", {})
    if (
        not isinstance(event_type, str)
        or not event_type
        or event_type != event_type.strip()
        or not isinstance(timestamp, str)
        or not timestamp
        or not isinstance(mission_id, str)
        or not mission_id
        or not isinstance(metadata, dict)
        or (
            expected_mission_id is not None
            and mission_id != expected_mission_id
        )
    ):
        raise TypeError("event record has invalid base fields")
    parsed_time = datetime.fromisoformat(timestamp)
    if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
        raise TypeError("event timestamp must include a timezone")
    task_id = data.get("task_id")
    queue_task_id = data.get("queue_task_id")
    if (
        event_type.startswith("task_") or event_type in _TASK_IDENTITY_EVENTS
    ) and (not isinstance(task_id, str) or not task_id):
        raise TypeError("task event requires a mission task ID")
    if event_type in _QUEUE_IDENTITY_EVENTS and (
        not isinstance(queue_task_id, str) or not queue_task_id
    ):
        raise TypeError("task event requires a queue identity")
    for field in (
        "task_id",
        "reason",
        "queue_task_id",
        "report_path",
        "provider",
    ):
        if data.get(field) is not None and not isinstance(data[field], str):
            raise TypeError(f"event field {field} has invalid type")
    _validate_json_value(metadata)
    return MissionEvent(
        event_type=event_type,
        timestamp=timestamp,
        mission_id=mission_id,
        task_id=task_id,
        reason=data.get("reason"),
        queue_task_id=queue_task_id,
        report_path=data.get("report_path"),
        provider=data.get("provider"),
        metadata=dict(metadata),
    )


@dataclass(frozen=True)
class EventSnapshot:
    events: tuple[MissionEvent, ...]
    revision: str
    raw: bytes


@dataclass(eq=False)
class _EventLockScope:
    event_log: "MissionEventLog"
    thread_id: int
    active: bool = True


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def make_event(event_type: str, mission_id: str, **kwargs) -> MissionEvent:
    """Create a MissionEvent stamped with the current UTC time."""
    return MissionEvent(
        event_type=event_type,
        timestamp=datetime.now(timezone.utc).isoformat(),
        mission_id=mission_id,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# MissionEventLog
# ---------------------------------------------------------------------------

class MissionEventLog:
    """Append-only JSONL event log for a single mission.

    File layout::

        {events_dir}/{mission_id}.jsonl        — event records, one JSON object per line
        {events_dir}/{mission_id}.jsonl.lock   — FileLock sentinel
    """

    def __init__(self, events_dir: Path, mission_id: str) -> None:
        if (
            not isinstance(mission_id, str)
            or not mission_id
            or mission_id != mission_id.strip()
            or Path(f"{mission_id}.jsonl").name != f"{mission_id}.jsonl"
        ):
            raise EventLogCorruptionError("event log mission owner is invalid")
        self._events_dir = Path(events_dir)
        self._mission_id = mission_id
        self._log_path = self._events_dir / f"{mission_id}.jsonl"
        self._lock_path = self._events_dir / f"{mission_id}.jsonl.lock"
        self._scope_guard = threading.Lock()
        self._active_scopes: set[_EventLockScope] = set()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def append(self, event: MissionEvent) -> None:
        """Append *event* to the JSONL file under an exclusive lock."""
        self._validate_owner_path()
        self._validate_event(event)
        self._events_dir.mkdir(parents=True, exist_ok=True)
        with self.locked() as token:
            self.append_locked(event, token)

    @contextmanager
    def locked(self):
        """Hold the authoritative event lock for snapshot and append work."""
        with FileLock(self._lock_path):
            token = _EventLockScope(self, threading.get_ident())
            with self._scope_guard:
                self._active_scopes.add(token)
            try:
                yield token
            finally:
                with self._scope_guard:
                    token.active = False
                    self._active_scopes.discard(token)

    def _require_active_scope(self, token: object) -> None:
        with self._scope_guard:
            valid = (
                isinstance(token, _EventLockScope)
                and token.event_log is self
                and token.thread_id == threading.get_ident()
                and token.active
                and token in self._active_scopes
            )
        if not valid:
            raise RuntimeError(
                "event operation requires the active MissionEventLog.locked() scope"
            )

    def append_locked(self, event: MissionEvent, token: object) -> None:
        """Append while this instance owns its authoritative lock."""
        self._require_active_scope(token)
        self._validate_owner_path()
        self._validate_event(event)
        self._events_dir.mkdir(parents=True, exist_ok=True)
        self.read_snapshot_locked()
        line = json.dumps(
            event.to_dict(), ensure_ascii=False, allow_nan=False
        ) + "\n"
        with self._log_path.open("ab") as fh:
            fh.write(line.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        logger.debug(
            "event appended mission=%s type=%s",
            self._mission_id,
            event.event_type,
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read_all(self) -> list[MissionEvent]:
        """Return all events in the order they were appended."""
        try:
            return list(self.read_snapshot().events)
        except FileNotFoundError:
            return []

    def read_snapshot(self) -> EventSnapshot:
        """Read, hash, and parse exactly one immutable byte snapshot."""
        self._validate_owner_path()
        try:
            raw = self._log_path.read_bytes()
        except FileNotFoundError:
            raw = b""
        return self._snapshot_from_bytes(raw)

    def read_snapshot_locked(self) -> EventSnapshot:
        """Read one snapshot while this instance owns its event lock."""
        with self._scope_guard:
            owns_active_scope = any(
                token.active
                and token.thread_id == threading.get_ident()
                for token in self._active_scopes
            )
        if not owns_active_scope:
            raise RuntimeError("event snapshot requires MissionEventLog.locked()")
        return self.read_snapshot()

    def _snapshot_from_bytes(self, raw: bytes) -> EventSnapshot:
        revision = hashlib.sha256(raw).hexdigest()
        if raw and not raw.endswith(b"\n"):
            raise EventLogCorruptionError(
                f"event log has an incomplete final record: {self._log_path}"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EventLogCorruptionError(
                f"event log is not valid UTF-8: {self._log_path}"
            ) from exc
        events: list[MissionEvent] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                raise EventLogCorruptionError(
                    f"event log has an empty record at line {lineno}: {self._log_path}"
                )
            try:
                data = json.loads(line, object_pairs_hook=_strict_object)
                if not isinstance(data, dict):
                    raise TypeError("event record is not an object")
                events.append(
                    _event_from_data(
                        data,
                        expected_mission_id=self._mission_id,
                    )
                )
            except (json.JSONDecodeError, TypeError, KeyError, ValueError) as exc:
                raise EventLogCorruptionError(
                    f"malformed event at line {lineno} in {self._log_path}: {exc}"
                ) from exc
        return EventSnapshot(tuple(events), revision, raw)

    def _validate_event(self, event: MissionEvent) -> None:
        if not isinstance(event, MissionEvent):
            raise EventLogCorruptionError("event is not a MissionEvent")
        try:
            _event_from_data(
                event.to_dict(),
                expected_mission_id=self._mission_id,
            )
        except (TypeError, ValueError) as exc:
            raise EventLogCorruptionError(
                f"event is invalid for {self._log_path}: {exc}"
            ) from exc

    def _validate_owner_path(self) -> None:
        expected = self._events_dir / f"{self._mission_id}.jsonl"
        if self._log_path != expected or self._log_path.stem != self._mission_id:
            raise EventLogCorruptionError(
                "event log path does not match its immutable mission owner"
            )

    def last_event_of_type(self, event_type: str) -> MissionEvent | None:
        """Return the most recent event whose ``event_type`` matches *event_type*."""
        result: MissionEvent | None = None
        for event in self.read_all():
            if event.event_type == event_type:
                result = event
        return result
