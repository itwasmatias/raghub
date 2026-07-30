"""
Durable event log for mission orchestration.

Events are appended to a per-mission JSONL file under a shared lock so that
multiple processes can safely write concurrently.  Reads are lock-free: the
file is read in full each time and parsed line-by-line so a partially-written
line (which cannot occur because we hold the lock across the write) would
simply be skipped.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from tools.ai_controller._locking import FileLock
from .models import MissionEvent

logger = logging.getLogger(__name__)


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
        self._events_dir = Path(events_dir)
        self._mission_id = mission_id
        self._log_path = self._events_dir / f"{mission_id}.jsonl"
        self._lock_path = self._events_dir / f"{mission_id}.jsonl.lock"

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def append(self, event: MissionEvent) -> None:
        """Append *event* to the JSONL file under an exclusive lock."""
        self._events_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False) + "\n"
        with FileLock(self._lock_path):
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
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
        if not self._log_path.exists():
            return []
        events: list[MissionEvent] = []
        try:
            text = self._log_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("could not read event log %s: %s", self._log_path, exc)
            return events
        for lineno, raw in enumerate(text.splitlines(), start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
                events.append(
                    MissionEvent(
                        event_type=data.get("event_type", ""),
                        timestamp=data.get("timestamp", ""),
                        mission_id=data.get("mission_id", self._mission_id),
                        task_id=data.get("task_id"),
                        reason=data.get("reason"),
                        queue_task_id=data.get("queue_task_id"),
                        report_path=data.get("report_path"),
                        provider=data.get("provider"),
                        metadata=dict(data.get("metadata", {})),
                    )
                )
            except (json.JSONDecodeError, TypeError, KeyError) as exc:
                logger.warning(
                    "skipping malformed event at line %d in %s: %s",
                    lineno,
                    self._log_path,
                    exc,
                )
        return events

    def last_event_of_type(self, event_type: str) -> MissionEvent | None:
        """Return the most recent event whose ``event_type`` matches *event_type*."""
        result: MissionEvent | None = None
        for event in self.read_all():
            if event.event_type == event_type:
                result = event
        return result
