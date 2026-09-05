"""Application lifecycle ledger for Job Application Executor v0.1.

Follows the DurableContributionLedger pattern: append-only JSONL with FileLock.
Fail-closed: malformed records refuse replay; corruption prevents further appends.

State transitions are explicit and validated during replay.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from tools.ai_controller._locking import FileLock


class ApplicationState(str, Enum):
    DISCOVERED = "discovered"
    QUALIFIED = "qualified"
    PACKET_DRAFTED = "packet_drafted"
    NEEDS_USER_INPUT = "needs_user_input"
    READY_FOR_APPLICATION = "ready_for_application"
    BEGIN_APPROVED = "begin_approved"
    FORM_IN_PROGRESS = "form_in_progress"
    READY_TO_SUBMIT = "ready_to_submit"
    SUBMISSION_APPROVED = "submission_approved"
    SUBMITTING = "submitting"
    SUBMITTED_CONFIRMED = "submitted_confirmed"
    SUBMISSION_INDETERMINATE = "submission_indeterminate"
    SKIPPED = "skipped"
    REJECTED = "rejected"
    INTERVIEW = "interview"
    OFFER = "offer"
    CLOSED = "closed"


VALID_TRANSITIONS: dict[ApplicationState, frozenset[ApplicationState]] = {
    ApplicationState.DISCOVERED: frozenset({ApplicationState.QUALIFIED, ApplicationState.SKIPPED}),
    ApplicationState.QUALIFIED: frozenset({ApplicationState.PACKET_DRAFTED, ApplicationState.SKIPPED}),
    ApplicationState.PACKET_DRAFTED: frozenset({
        ApplicationState.NEEDS_USER_INPUT,
        ApplicationState.READY_FOR_APPLICATION,
    }),
    ApplicationState.NEEDS_USER_INPUT: frozenset({
        ApplicationState.READY_FOR_APPLICATION,
        ApplicationState.SKIPPED,
    }),
    ApplicationState.READY_FOR_APPLICATION: frozenset({
        ApplicationState.BEGIN_APPROVED,
        ApplicationState.SKIPPED,
    }),
    ApplicationState.BEGIN_APPROVED: frozenset({ApplicationState.FORM_IN_PROGRESS}),
    ApplicationState.FORM_IN_PROGRESS: frozenset({
        ApplicationState.READY_TO_SUBMIT,
        ApplicationState.NEEDS_USER_INPUT,
    }),
    ApplicationState.READY_TO_SUBMIT: frozenset({
        ApplicationState.SUBMISSION_APPROVED,
        ApplicationState.NEEDS_USER_INPUT,
    }),
    ApplicationState.SUBMISSION_APPROVED: frozenset({ApplicationState.SUBMITTING}),
    ApplicationState.SUBMITTING: frozenset({
        ApplicationState.SUBMITTED_CONFIRMED,
        ApplicationState.SUBMISSION_INDETERMINATE,
        ApplicationState.REJECTED,
    }),
    ApplicationState.SUBMISSION_INDETERMINATE: frozenset({
        ApplicationState.SUBMITTED_CONFIRMED,
        ApplicationState.REJECTED,
    }),
    ApplicationState.SUBMITTED_CONFIRMED: frozenset({
        ApplicationState.INTERVIEW,
        ApplicationState.REJECTED,
        ApplicationState.CLOSED,
    }),
    ApplicationState.INTERVIEW: frozenset({
        ApplicationState.OFFER,
        ApplicationState.REJECTED,
        ApplicationState.CLOSED,
    }),
    ApplicationState.OFFER: frozenset({ApplicationState.CLOSED}),
    ApplicationState.SKIPPED: frozenset(),
    ApplicationState.REJECTED: frozenset(),
    ApplicationState.CLOSED: frozenset(),
}


class ApplicationStateError(ValueError):
    """Invalid application state transition."""


@dataclass
class ApplicationRecord:
    application_id: str
    job_id: str
    company: str
    title: str
    job_url: str | None
    application_url: str | None
    posting_hash: str
    fit_score: int
    fit_rationale: str
    state: ApplicationState
    candidate_profile_hash: str
    resume_hash: str | None = None
    cover_letter_hash: str | None = None
    packet_hash: str | None = None
    submission_effect_id: str | None = None
    submission_timestamp: datetime | None = None
    submission_evidence: str | None = None
    confirmation_id: str | None = None
    reconciliation_state: str | None = None
    begin_approval_id: str | None = None
    submit_approval_id: str | None = None
    notes: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "job_id": self.job_id,
            "company": self.company,
            "title": self.title,
            "job_url": self.job_url,
            "application_url": self.application_url,
            "posting_hash": self.posting_hash,
            "fit_score": self.fit_score,
            "fit_rationale": self.fit_rationale,
            "state": self.state.value,
            "candidate_profile_hash": self.candidate_profile_hash,
            "resume_hash": self.resume_hash,
            "cover_letter_hash": self.cover_letter_hash,
            "packet_hash": self.packet_hash,
            "submission_effect_id": self.submission_effect_id,
            "submission_timestamp": (
                self.submission_timestamp.isoformat() if self.submission_timestamp else None
            ),
            "submission_evidence": self.submission_evidence,
            "confirmation_id": self.confirmation_id,
            "reconciliation_state": self.reconciliation_state,
            "begin_approval_id": self.begin_approval_id,
            "submit_approval_id": self.submit_approval_id,
            "notes": self.notes,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApplicationRecord:
        def _dt(v: str | None) -> datetime | None:
            if v is None:
                return None
            return datetime.fromisoformat(v.replace("Z", "+00:00"))

        return cls(
            application_id=data["application_id"],
            job_id=data["job_id"],
            company=data["company"],
            title=data["title"],
            job_url=data.get("job_url"),
            application_url=data.get("application_url"),
            posting_hash=data["posting_hash"],
            fit_score=data["fit_score"],
            fit_rationale=data.get("fit_rationale", ""),
            state=ApplicationState(data["state"]),
            candidate_profile_hash=data["candidate_profile_hash"],
            resume_hash=data.get("resume_hash"),
            cover_letter_hash=data.get("cover_letter_hash"),
            packet_hash=data.get("packet_hash"),
            submission_effect_id=data.get("submission_effect_id"),
            submission_timestamp=_dt(data.get("submission_timestamp")),
            submission_evidence=data.get("submission_evidence"),
            confirmation_id=data.get("confirmation_id"),
            reconciliation_state=data.get("reconciliation_state"),
            begin_approval_id=data.get("begin_approval_id"),
            submit_approval_id=data.get("submit_approval_id"),
            notes=data.get("notes", ""),
            created_at=_dt(data["created_at"]),
            updated_at=_dt(data["updated_at"]),
        )


class ApplicationLedgerCorruptionError(RuntimeError):
    """Application ledger cannot be trusted or replayed."""


class DurableApplicationLedger:
    """Append-only JSONL ledger for application lifecycle records.

    Follows the DurableContributionLedger pattern exactly:
    - Load replays from scratch under lock
    - Writes validate existing history before appending
    - All appends are flushed and fsynced
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def load_all(self) -> list[ApplicationRecord]:
        with FileLock(self.lock_path):
            return self._load_locked()

    def get(self, application_id: str) -> ApplicationRecord | None:
        for record in self.load_all():
            if record.application_id == application_id:
                return record
        return None

    def find_by_job_id(self, job_id: str) -> ApplicationRecord | None:
        for record in self.load_all():
            if record.job_id == job_id:
                return record
        return None

    def create(self, record: ApplicationRecord) -> ApplicationRecord:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path):
            existing = self._load_locked()
            for r in existing:
                if r.application_id == record.application_id:
                    raise ValueError(f"Application {record.application_id} already exists in ledger")
            self._append_locked({"record_type": "create", "data": record.to_dict()})
        return record

    def transition(
        self,
        application_id: str,
        new_state: ApplicationState,
        notes: str = "",
    ) -> ApplicationRecord:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path):
            records = self._load_locked()
            record = next((r for r in records if r.application_id == application_id), None)
            if record is None:
                raise ValueError(f"Application {application_id} not found in ledger")

            valid = VALID_TRANSITIONS.get(record.state, frozenset())
            if new_state not in valid:
                raise ApplicationStateError(
                    f"Invalid transition {record.state.value} → {new_state.value} "
                    f"for application {application_id}. "
                    f"Valid next states: {[s.value for s in valid]}"
                )

            now = datetime.now(timezone.utc)
            patch: dict[str, Any] = {"state": new_state.value, "updated_at": now.isoformat()}
            if notes:
                patch["notes"] = notes
            self._append_locked({
                "record_type": "transition",
                "application_id": application_id,
                "patch": patch,
            })
            record.state = new_state
            record.updated_at = now
            if notes:
                record.notes = notes
        return record

    def update_fields(self, application_id: str, **fields: Any) -> ApplicationRecord:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path):
            records = self._load_locked()
            record = next((r for r in records if r.application_id == application_id), None)
            if record is None:
                raise ValueError(f"Application {application_id} not found in ledger")

            now = datetime.now(timezone.utc)
            serializable: dict[str, Any] = {"updated_at": now.isoformat()}
            for k, v in fields.items():
                if isinstance(v, datetime):
                    serializable[k] = v.isoformat()
                else:
                    serializable[k] = v

            self._append_locked({
                "record_type": "update",
                "application_id": application_id,
                "fields": serializable,
            })
            for k, v in fields.items():
                if hasattr(record, k) and k not in ("application_id", "job_id"):
                    setattr(record, k, v)
            record.updated_at = now
        return record

    def _load_locked(self) -> list[ApplicationRecord]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return []
        return self._replay(raw)

    def _replay(self, raw: bytes) -> list[ApplicationRecord]:
        if raw and not raw.endswith(b"\n"):
            raise ApplicationLedgerCorruptionError(
                f"Incomplete final record (missing newline): {self.path}"
            )
        text = raw.decode("utf-8")
        records: dict[str, ApplicationRecord] = {}

        for lineno, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                raise ApplicationLedgerCorruptionError(
                    f"Empty record at line {lineno}: {self.path}"
                )
            try:
                entry = json.loads(line)
                rtype = entry.get("record_type")

                if rtype == "create":
                    r = ApplicationRecord.from_dict(entry["data"])
                    if r.application_id in records:
                        raise ValueError(f"Duplicate application_id: {r.application_id}")
                    records[r.application_id] = r

                elif rtype == "transition":
                    aid = entry["application_id"]
                    if aid not in records:
                        raise ValueError(f"Transition for unknown application: {aid}")
                    r = records[aid]
                    patch = entry["patch"]
                    new_state = ApplicationState(patch["state"])
                    valid = VALID_TRANSITIONS.get(r.state, frozenset())
                    if new_state not in valid:
                        raise ValueError(
                            f"Invalid replayed transition {r.state} → {new_state}"
                        )
                    r.state = new_state
                    if "updated_at" in patch:
                        r.updated_at = datetime.fromisoformat(
                            patch["updated_at"].replace("Z", "+00:00")
                        )
                    if "notes" in patch and patch["notes"]:
                        r.notes = patch["notes"]

                elif rtype == "update":
                    aid = entry["application_id"]
                    if aid not in records:
                        raise ValueError(f"Update for unknown application: {aid}")
                    r = records[aid]
                    for k, v in entry.get("fields", {}).items():
                        if hasattr(r, k) and k not in ("application_id", "job_id"):
                            if k in ("updated_at", "submission_timestamp") and isinstance(v, str):
                                v = datetime.fromisoformat(v.replace("Z", "+00:00"))
                            setattr(r, k, v)

                else:
                    raise ValueError(f"Unknown record_type: {rtype!r}")

            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ApplicationLedgerCorruptionError(
                    f"Malformed record at line {lineno} in {self.path}: {exc}"
                ) from exc

        return list(records.values())

    def _append_locked(self, record: dict[str, Any]) -> None:
        line = (
            json.dumps(
                record,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        with self.path.open("ab") as handle:
            handle.write(line.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
