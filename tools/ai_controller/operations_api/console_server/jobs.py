"""Job tracking and management for Console Server."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

import fcntl


class JobCorruptionError(ValueError):
    """Raised when durable job evidence cannot be decoded safely."""


class JobStatus(str, Enum):
    """
    Job lifecycle states.

    Tier-1 ``inspect_git_status`` jobs may execute through the governed runtime.
    Approval-required actions remain PENDING because approval state transitions
    are intentionally outside Console Action Execution Runtime v0.1.
    """

    PENDING = "pending"  # Created, not yet started (or awaiting approval for Tier 2)
    RUNNING = "running"  # Currently executing
    SUCCEEDED = "succeeded"  # Completed successfully
    FAILED = "failed"  # Failed with error
    TIMEOUT = "timeout"  # Exceeded timeout
    CANCELLED = "cancelled"  # Cancelled before completion
    RECONCILIATION_REQUIRED = "reconciliation_required"  # Outcome is ambiguous


_TERMINAL_STATES = {
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.TIMEOUT,
    JobStatus.CANCELLED,
    JobStatus.RECONCILIATION_REQUIRED,
}


@dataclass
class JobRecord:
    """
    Record of a Console Server job.

    Terminal state is final and cannot be changed.
    """

    job_id: str
    action_id: str
    action_type: str
    workspace_id: str
    target_node_id: str | None
    mission_id: str | None
    created_at: str
    created_by: str
    status: JobStatus
    task_id: str | None = None
    timeout_seconds: int | None = None
    assignment_id: str | None = None
    dispatch_offer_id: str | None = None
    idempotency_key: str | None = None
    request_fingerprint: str | None = None
    execution_fingerprint: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    truncated: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "job_id": self.job_id,
            "action_id": self.action_id,
            "action_type": self.action_type,
            "workspace_id": self.workspace_id,
            "target_node_id": self.target_node_id,
            "mission_id": self.mission_id,
            "task_id": self.task_id,
            "timeout_seconds": self.timeout_seconds,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "status": self.status.value,
            "assignment_id": self.assignment_id,
            "dispatch_offer_id": self.dispatch_offer_id,
            "idempotency_key": self.idempotency_key,
            "request_fingerprint": self.request_fingerprint,
            "execution_fingerprint": self.execution_fingerprint,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "truncated": self.truncated,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "failure_reason": self.failure_reason,
        }


class JobTracker:
    """
    Tracks Console Server jobs.

    Enforces terminal state finality and provides job lookup.
    """

    def __init__(self, storage_path: Path):
        """
        Initialize job tracker.

        Args:
            storage_path: Directory for job storage
        """
        if not isinstance(storage_path, Path):
            raise TypeError("storage_path must be a Path")

        self._storage_path = storage_path
        self._storage_path.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.RLock()
        self._transaction_state = threading.local()
        self._lock_path = self._storage_path / ".jobs.lock"

    def create_job(
        self,
        action_type: str,
        workspace_id: str,
        created_by: str,
        *,
        target_node_id: str | None = None,
        mission_id: str | None = None,
        task_id: str | None = None,
        timeout_seconds: int | None = None,
        assignment_id: str | None = None,
        dispatch_offer_id: str | None = None,
        idempotency_key: str | None = None,
        request_fingerprint: str | None = None,
        execution_fingerprint: str | None = None,
    ) -> JobRecord:
        """
        Create a new job record.

        Args:
            action_type: Type of action
            workspace_id: Workspace identifier
            created_by: Principal who created the job
            target_node_id: Optional target node
            mission_id: Optional mission identifier
            assignment_id: Optional routing assignment ID
            dispatch_offer_id: Optional dispatch offer ID
            idempotency_key: Optional idempotency key for deduplication
            request_fingerprint: Optional fingerprint of request parameters

        Returns:
            JobRecord for the created job
        """
        with self.transaction():
            job_id = f"job-{uuid4().hex}"
            action_id = f"action-{uuid4().hex}"
            now = datetime.now(timezone.utc).isoformat()
            job = JobRecord(
                job_id=job_id,
                action_id=action_id,
                action_type=action_type,
                workspace_id=workspace_id,
                target_node_id=target_node_id,
                mission_id=mission_id,
                task_id=task_id,
                timeout_seconds=timeout_seconds,
                created_at=now,
                created_by=created_by,
                status=JobStatus.PENDING,
                assignment_id=assignment_id,
                dispatch_offer_id=dispatch_offer_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                execution_fingerprint=execution_fingerprint,
            )
            self._save_unlocked(job)
            return job

    def get_job(self, job_id: str) -> JobRecord:
        """
        Retrieve a job by ID.

        Args:
            job_id: Job identifier

        Returns:
            JobRecord

        Raises:
            ValueError: If job not found
        """
        with self.transaction():
            job_path = self._storage_path / f"{job_id}.json"
            if not job_path.exists():
                raise ValueError(f"Job not found: {job_id}")
            try:
                data = json.loads(job_path.read_text(encoding="utf-8"))
                return self._record_from_data(data)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise JobCorruptionError(
                    f"Authoritative job record is corrupt: {job_id}"
                ) from exc

    @staticmethod
    def _record_from_data(data: dict[str, Any]) -> JobRecord:
        return JobRecord(
            job_id=data["job_id"],
            action_id=data["action_id"],
            action_type=data["action_type"],
            workspace_id=data["workspace_id"],
            target_node_id=data.get("target_node_id"),
            mission_id=data.get("mission_id"),
            task_id=data.get("task_id"),
            timeout_seconds=data.get("timeout_seconds"),
            created_at=data["created_at"],
            created_by=data["created_by"],
            status=JobStatus(data["status"]),
            assignment_id=data.get("assignment_id"),
            dispatch_offer_id=data.get("dispatch_offer_id"),
            idempotency_key=data.get("idempotency_key"),
            request_fingerprint=data.get("request_fingerprint"),
            execution_fingerprint=data.get("execution_fingerprint"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            duration_ms=data.get("duration_ms"),
            exit_code=data.get("exit_code"),
            stdout=data.get("stdout"),
            stderr=data.get("stderr"),
            truncated=data.get("truncated", False),
            stdout_truncated=data.get(
                "stdout_truncated", data.get("truncated", False)
            ),
            stderr_truncated=data.get(
                "stderr_truncated", data.get("truncated", False)
            ),
            failure_reason=data.get("failure_reason"),
        )

    def update_status(
        self,
        job_id: str,
        new_status: JobStatus,
        *,
        started_at: str | None = None,
        finished_at: str | None = None,
        duration_ms: int | None = None,
        exit_code: int | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
        truncated: bool = False,
        stdout_truncated: bool = False,
        stderr_truncated: bool = False,
        failure_reason: str | None = None,
    ) -> JobRecord:
        """
        Update job status.

        Args:
            job_id: Job identifier
            new_status: New status
            started_at: Optional started timestamp
            finished_at: Optional finished timestamp
            duration_ms: Optional duration in milliseconds
            exit_code: Optional exit code
            stdout: Optional stdout content
            stderr: Optional stderr content
            truncated: Whether output was truncated
            failure_reason: Optional failure reason

        Returns:
            Updated JobRecord

        Raises:
            ValueError: If job is already in terminal state
        """
        with self.transaction():
            job = self.get_job(job_id)
            if job.status in _TERMINAL_STATES:
                raise ValueError(
                    f"Job {job_id} is already in terminal state {job.status.value}"
                )
            updated = JobRecord(
            job_id=job.job_id,
            action_id=job.action_id,
            action_type=job.action_type,
            workspace_id=job.workspace_id,
            target_node_id=job.target_node_id,
            mission_id=job.mission_id,
            task_id=job.task_id,
            timeout_seconds=job.timeout_seconds,
            created_at=job.created_at,
            created_by=job.created_by,
            status=new_status,
            assignment_id=job.assignment_id,
            dispatch_offer_id=job.dispatch_offer_id,
            idempotency_key=job.idempotency_key,
            request_fingerprint=job.request_fingerprint,
            execution_fingerprint=job.execution_fingerprint,
            started_at=started_at or job.started_at,
            finished_at=finished_at or job.finished_at,
            duration_ms=duration_ms if duration_ms is not None else job.duration_ms,
            exit_code=exit_code if exit_code is not None else job.exit_code,
            stdout=stdout if stdout is not None else job.stdout,
            stderr=stderr if stderr is not None else job.stderr,
            truncated=truncated or job.truncated,
            stdout_truncated=stdout_truncated or job.stdout_truncated,
            stderr_truncated=stderr_truncated or job.stderr_truncated,
            failure_reason=failure_reason or job.failure_reason,
            )
            self._save_unlocked(updated)
            return updated

    def list_jobs(self, *, limit: int = 100) -> list[JobRecord]:
        """
        List recent jobs.

        Args:
            limit: Maximum number of jobs to return

        Returns:
            List of JobRecords, most recent first
        """
        with self.transaction():
            job_files = sorted(
                self._storage_path.glob("job-*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            return [self.get_job(job_file.stem) for job_file in job_files[:limit]]

    def find_by_idempotency_key(self, idempotency_key: str) -> JobRecord | None:
        """
        Find a job by idempotency key.

        Args:
            idempotency_key: Idempotency key to search for

        Returns:
            JobRecord if found, None otherwise
        """
        if not idempotency_key:
            return None

        with self.transaction():
            for job_file in self._storage_path.glob("job-*.json"):
                try:
                    data = json.loads(job_file.read_text(encoding="utf-8"))
                    record = self._record_from_data(data)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    raise JobCorruptionError(
                        f"Authoritative job record is corrupt: {job_file.name}"
                    ) from exc
                if record.idempotency_key == idempotency_key:
                    return record
            return None

    def _save(self, job: JobRecord) -> None:
        """Save job to storage."""
        with self.transaction():
            self._save_unlocked(job)

    def _save_unlocked(self, job: JobRecord) -> None:
        """Atomically replace one authoritative record while the lock is held."""
        job_path = self._storage_path / f"{job.job_id}.json"
        payload = (json.dumps(job.to_dict(), indent=2) + "\n").encode("utf-8")
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{job.job_id}.", suffix=".tmp", dir=self._storage_path
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, job_path)
            self._fsync_directory()
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    def _fsync_directory(self) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(self._storage_path, flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @contextmanager
    def transaction(self):
        """Serialize job lookup and mutation across threads and processes."""
        with self._thread_lock:
            depth = getattr(self._transaction_state, "depth", 0)
            if depth:
                self._transaction_state.depth = depth + 1
                try:
                    yield self
                finally:
                    self._transaction_state.depth -= 1
                return
            with self._lock_path.open("a+b") as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                self._transaction_state.depth = 1
                try:
                    yield self
                finally:
                    self._transaction_state.depth = 0
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def execution_lock(self, job_id: str):
        """Claim one execution across processes without waiting for a duplicate."""
        lock_path = self._storage_path / f".{job_id}.execution.lock"
        with lock_path.open("a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
