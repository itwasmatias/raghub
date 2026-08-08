"""Job tracking and management for Console Server."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4


class JobStatus(str, Enum):
    """
    Job lifecycle states.

    Note: For approval-required actions (Tier 2), jobs remain in PENDING until
    a hypothetical external approval workflow advances them. Approval enforcement
    is out of scope for v0.1 - the Console Server creates jobs and performs
    routing but does not implement execution runtime or approval state transitions.
    """

    PENDING = "pending"  # Created, not yet started (or awaiting approval for Tier 2)
    RUNNING = "running"  # Currently executing
    SUCCEEDED = "succeeded"  # Completed successfully
    FAILED = "failed"  # Failed with error
    TIMEOUT = "timeout"  # Exceeded timeout
    CANCELLED = "cancelled"  # Cancelled before completion


_TERMINAL_STATES = {
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.TIMEOUT,
    JobStatus.CANCELLED,
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
    idempotency_key: str | None = None
    request_fingerprint: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    truncated: bool = False
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
            "created_at": self.created_at,
            "created_by": self.created_by,
            "status": self.status.value,
            "idempotency_key": self.idempotency_key,
            "request_fingerprint": self.request_fingerprint,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "truncated": self.truncated,
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

    def create_job(
        self,
        action_type: str,
        workspace_id: str,
        created_by: str,
        *,
        target_node_id: str | None = None,
        mission_id: str | None = None,
        idempotency_key: str | None = None,
        request_fingerprint: str | None = None,
    ) -> JobRecord:
        """
        Create a new job record.

        Args:
            action_type: Type of action
            workspace_id: Workspace identifier
            created_by: Principal who created the job
            target_node_id: Optional target node
            mission_id: Optional mission identifier
            idempotency_key: Optional idempotency key for deduplication
            request_fingerprint: Optional fingerprint of request parameters

        Returns:
            JobRecord for the created job
        """
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
            created_at=now,
            created_by=created_by,
            status=JobStatus.PENDING,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )

        self._save(job)
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
        job_path = self._storage_path / f"{job_id}.json"
        if not job_path.exists():
            raise ValueError(f"Job not found: {job_id}")

        data = json.loads(job_path.read_text(encoding="utf-8"))
        return JobRecord(
            job_id=data["job_id"],
            action_id=data["action_id"],
            action_type=data["action_type"],
            workspace_id=data["workspace_id"],
            target_node_id=data.get("target_node_id"),
            mission_id=data.get("mission_id"),
            created_at=data["created_at"],
            created_by=data["created_by"],
            status=JobStatus(data["status"]),
            idempotency_key=data.get("idempotency_key"),
            request_fingerprint=data.get("request_fingerprint"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            duration_ms=data.get("duration_ms"),
            exit_code=data.get("exit_code"),
            stdout=data.get("stdout"),
            stderr=data.get("stderr"),
            truncated=data.get("truncated", False),
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
        job = self.get_job(job_id)

        # Enforce terminal state finality
        if job.status in _TERMINAL_STATES:
            raise ValueError(
                f"Job {job_id} is already in terminal state {job.status.value}"
            )

        # Create updated job
        updated = JobRecord(
            job_id=job.job_id,
            action_id=job.action_id,
            action_type=job.action_type,
            workspace_id=job.workspace_id,
            target_node_id=job.target_node_id,
            mission_id=job.mission_id,
            created_at=job.created_at,
            created_by=job.created_by,
            status=new_status,
            started_at=started_at or job.started_at,
            finished_at=finished_at or job.finished_at,
            duration_ms=duration_ms if duration_ms is not None else job.duration_ms,
            exit_code=exit_code if exit_code is not None else job.exit_code,
            stdout=stdout if stdout is not None else job.stdout,
            stderr=stderr if stderr is not None else job.stderr,
            truncated=truncated or job.truncated,
            failure_reason=failure_reason or job.failure_reason,
        )

        self._save(updated)
        return updated

    def list_jobs(self, *, limit: int = 100) -> list[JobRecord]:
        """
        List recent jobs.

        Args:
            limit: Maximum number of jobs to return

        Returns:
            List of JobRecords, most recent first
        """
        job_files = sorted(
            self._storage_path.glob("job-*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        jobs = []
        for job_file in job_files[:limit]:
            try:
                jobs.append(self.get_job(job_file.stem))
            except (ValueError, json.JSONDecodeError, KeyError):
                # Skip corrupted job files
                continue

        return jobs

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

        # Linear scan - acceptable for small job counts, could be indexed later
        for job_file in self._storage_path.glob("job-*.json"):
            try:
                data = json.loads(job_file.read_text(encoding="utf-8"))
                if data.get("idempotency_key") == idempotency_key:
                    return JobRecord(
                        job_id=data["job_id"],
                        action_id=data["action_id"],
                        action_type=data["action_type"],
                        workspace_id=data["workspace_id"],
                        target_node_id=data.get("target_node_id"),
                        mission_id=data.get("mission_id"),
                        created_at=data["created_at"],
                        created_by=data["created_by"],
                        status=JobStatus(data["status"]),
                        idempotency_key=data.get("idempotency_key"),
                        request_fingerprint=data.get("request_fingerprint"),
                        started_at=data.get("started_at"),
                        finished_at=data.get("finished_at"),
                        duration_ms=data.get("duration_ms"),
                        exit_code=data.get("exit_code"),
                        stdout=data.get("stdout"),
                        stderr=data.get("stderr"),
                        truncated=data.get("truncated", False),
                        failure_reason=data.get("failure_reason"),
                    )
            except (json.JSONDecodeError, KeyError, ValueError):
                # Skip corrupted job files
                continue

        return None

    def _save(self, job: JobRecord) -> None:
        """Save job to storage."""
        job_path = self._storage_path / f"{job.job_id}.json"
        job_path.write_text(
            json.dumps(job.to_dict(), indent=2) + "\n",
            encoding="utf-8",
        )
