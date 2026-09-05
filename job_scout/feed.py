"""Durable scout feed for MissionaryX Job Scout v0.1.

Append-only JSONL store for ScoredOpportunity records.
Uses the same FileLock pattern as DurableOpportunityStore and DurableApplicationLedger.

Record types:
- "scored_opportunity": initial scored record
- "state_update": state change for an existing record
"""
from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.ai_controller._locking import FileLock
from job_scout.models import ScoredOpportunity, ScoutState


class ScoutFeedCorruptionError(RuntimeError):
    """Scout feed cannot be trusted or replayed."""


class DurableScoutFeed:
    """Append-only JSONL store for ScoredOpportunity records."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def load_all(self) -> tuple[ScoredOpportunity, ...]:
        with FileLock(self.lock_path):
            return self._load_locked()

    def get(self, job_id: str) -> ScoredOpportunity | None:
        for opp in self.load_all():
            if opp.job_id == job_id:
                return opp
        return None

    def store(self, scored: ScoredOpportunity) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path):
            existing = self._map_locked()
            if scored.job_id in existing:
                raise ValueError(f"ScoredOpportunity {scored.job_id} already in feed — use update_state")
            self._append_locked({
                "record_type": "scored_opportunity",
                "job_id": scored.job_id,
                "data": scored.to_dict(),
            })

    def update_state(self, job_id: str, new_state: ScoutState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path):
            existing = self._map_locked()
            if job_id not in existing:
                raise ValueError(f"Unknown job_id {job_id!r} — cannot update state")
            self._append_locked({
                "record_type": "state_update",
                "job_id": job_id,
                "new_state": new_state.value,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

    def update_executor_id(self, job_id: str, application_id: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path):
            existing = self._map_locked()
            if job_id not in existing:
                raise ValueError(f"Unknown job_id {job_id!r}")
            self._append_locked({
                "record_type": "executor_id_update",
                "job_id": job_id,
                "executor_application_id": application_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

    def _load_locked(self) -> tuple[ScoredOpportunity, ...]:
        return tuple(self._map_locked().values())

    def _map_locked(self) -> dict[str, ScoredOpportunity]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        return self._replay(raw)

    def _replay(self, raw: bytes) -> dict[str, ScoredOpportunity]:
        if raw and not raw.endswith(b"\n"):
            raise ScoutFeedCorruptionError(f"Incomplete final record: {self.path}")
        text = raw.decode("utf-8")
        records: dict[str, ScoredOpportunity] = {}
        for lineno, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                raise ScoutFeedCorruptionError(f"Empty record at line {lineno}: {self.path}")
            try:
                record = json.loads(line)
                self._apply_record(records, record, lineno)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ScoutFeedCorruptionError(
                    f"Malformed record at line {lineno}: {exc}"
                ) from exc
        return records

    def _apply_record(
        self, records: dict[str, ScoredOpportunity], record: dict[str, Any], lineno: int
    ) -> None:
        rtype = record.get("record_type")
        if rtype == "scored_opportunity":
            scored = ScoredOpportunity.from_dict(record["data"])
            if scored.job_id in records:
                raise ValueError(f"Duplicate scored_opportunity for {scored.job_id}")
            records[scored.job_id] = scored
        elif rtype == "state_update":
            job_id = record["job_id"]
            if job_id not in records:
                raise ValueError(f"state_update for unknown job_id {job_id!r}")
            records[job_id] = records[job_id].with_state(ScoutState(record["new_state"]))
        elif rtype == "executor_id_update":
            job_id = record["job_id"]
            if job_id not in records:
                raise ValueError(f"executor_id_update for unknown job_id {job_id!r}")
            records[job_id] = records[job_id].with_executor_id(record["executor_application_id"])
        else:
            raise ValueError(f"Unknown record_type: {rtype!r}")

    def _append_locked(self, record: dict[str, Any]) -> None:
        line = (
            json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
            + "\n"
        )
        with self.path.open("ab") as handle:
            handle.write(line.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
