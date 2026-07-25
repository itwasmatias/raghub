from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from sports.compute.models import ComputeJob, ComputeJobRequest, RuntimeCapabilities


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ComputeJobRepository:
    def __init__(
        self,
        database_path: str | Path,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.database_path = str(database_path)
        self.clock = clock
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def migrate(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS compute_jobs (
                    id TEXT PRIMARY KEY,
                    deduplication_key TEXT NOT NULL UNIQUE,
                    job_type TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    requested_model TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    feature_version TEXT NOT NULL,
                    input_snapshot_id TEXT NOT NULL,
                    input_snapshot_timestamp TEXT NOT NULL,
                    configuration_version TEXT NOT NULL,
                    required_capabilities_json TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    requested_time TEXT NOT NULL,
                    claim_expiration_time TEXT,
                    status TEXT NOT NULL,
                    assigned_worker TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    last_error TEXT,
                    started_time TEXT,
                    completed_time TEXT,
                    lease_expires_at TEXT,
                    result_reference TEXT,
                    result_checksum TEXT,
                    payload_json TEXT NOT NULL,
                    result_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_compute_jobs_queue
                    ON compute_jobs(status, priority, requested_time);
                CREATE TABLE IF NOT EXISTS compute_workers (
                    name TEXT PRIMARY KEY,
                    version TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    registered_at TEXT NOT NULL,
                    last_heartbeat TEXT NOT NULL,
                    active_job_id TEXT,
                    last_successful_job TEXT,
                    last_failure TEXT
                );
                """
            )

    def create_job(self, request: ComputeJobRequest) -> ComputeJob:
        now = self.clock().isoformat()
        dedupe = "|".join(
            (
                request.job_type,
                request.domain,
                request.requested_model,
                request.model_version,
                request.feature_version,
                request.input_snapshot_id,
                request.configuration_version,
            )
        )
        job_id = f"job-{uuid4().hex}"
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO compute_jobs (
                    id, deduplication_key, job_type, domain, requested_model,
                    model_version, feature_version, input_snapshot_id,
                    input_snapshot_timestamp, configuration_version,
                    required_capabilities_json, priority, requested_time,
                    claim_expiration_time, status, max_attempts, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    job_id, dedupe, request.job_type, request.domain,
                    request.requested_model, request.model_version,
                    request.feature_version, request.input_snapshot_id,
                    request.input_snapshot_timestamp, request.configuration_version,
                    json.dumps(request.required_capabilities), request.priority, now,
                    request.claim_expiration_time, request.max_attempts,
                    json.dumps(request.payload, sort_keys=True),
                ),
            )
            row = connection.execute(
                "SELECT * FROM compute_jobs WHERE deduplication_key=?", (dedupe,)
            ).fetchone()
        return self._job(row)

    def claim_next(
        self,
        *,
        worker_name: str,
        capabilities: RuntimeCapabilities,
        lease_seconds: int = 120,
    ) -> ComputeJob | None:
        now = self.clock()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM compute_jobs
                WHERE (
                    status='pending' OR
                    (status='claimed' AND lease_expires_at < ?)
                ) AND attempt_count < max_attempts
                ORDER BY priority ASC, requested_time ASC
                """,
                (now.isoformat(),),
            ).fetchall()
            row = next(
                (
                    item for item in rows
                    if capabilities.supports(
                        tuple(json.loads(item["required_capabilities_json"]))
                    )
                ),
                None,
            )
            if row is None:
                connection.commit()
                return None
            lease = (now + timedelta(seconds=lease_seconds)).isoformat()
            connection.execute(
                """
                UPDATE compute_jobs SET status='claimed', assigned_worker=?,
                    attempt_count=attempt_count+1, started_time=COALESCE(started_time, ?),
                    lease_expires_at=? WHERE id=?
                """,
                (worker_name, now.isoformat(), lease, row["id"]),
            )
            connection.commit()
        return self.get_job(str(row["id"]))

    def claim_job(
        self, job_id: str, worker_name: str, *, lease_seconds: int
    ) -> ComputeJob:
        capabilities = RuntimeCapabilities(node_name=worker_name)
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.required_capabilities:
            capabilities = RuntimeCapabilities(
                node_name=worker_name, capabilities=job.required_capabilities
            )
        claimed = self.claim_next(
            worker_name=worker_name,
            capabilities=capabilities,
            lease_seconds=lease_seconds,
        )
        if claimed is None or claimed.id != job_id:
            raise ValueError("job is not claimable")
        return claimed

    def complete_job(
        self,
        job_id: str,
        worker_name: str,
        *,
        payload: dict,
        checksum: str,
    ) -> ComputeJob:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode()
        actual = hashlib.sha256(encoded).hexdigest()
        if not checksum or actual != checksum:
            raise ValueError("result checksum mismatch")
        now = self.clock().isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE compute_jobs SET status='completed', completed_time=?,
                    result_checksum=?, result_reference=?, result_json=?,
                    lease_expires_at=NULL
                WHERE id=? AND assigned_worker=? AND status='claimed'
                """,
                (
                    now, checksum, f"database:compute_jobs:{job_id}",
                    json.dumps(payload, sort_keys=True), job_id, worker_name,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("job is not claimed by this worker")
            connection.execute(
                """
                UPDATE compute_workers SET active_job_id=NULL,
                    last_successful_job=? WHERE name=?
                """,
                (job_id, worker_name),
            )
        return self.get_job(job_id)

    def fail_job(self, job_id: str, worker_name: str, error: str) -> ComputeJob:
        safe_error = str(error).replace("\n", " ")[:500]
        with self.connect() as connection:
            row = connection.execute(
                "SELECT attempt_count, max_attempts FROM compute_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            status = "failed" if row["attempt_count"] >= row["max_attempts"] else "pending"
            connection.execute(
                """
                UPDATE compute_jobs SET status=?, last_error=?,
                    assigned_worker=NULL, lease_expires_at=NULL
                WHERE id=? AND assigned_worker=?
                """,
                (status, safe_error, job_id, worker_name),
            )
            connection.execute(
                "UPDATE compute_workers SET active_job_id=NULL, last_failure=? WHERE name=?",
                (safe_error, worker_name),
            )
        return self.get_job(job_id)

    def register_worker(
        self, name: str, version: str, capabilities: tuple[str, ...]
    ) -> None:
        now = self.clock().isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO compute_workers VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL)
                ON CONFLICT(name) DO UPDATE SET version=excluded.version,
                    capabilities_json=excluded.capabilities_json,
                    last_heartbeat=excluded.last_heartbeat
                """,
                (name, version, json.dumps(capabilities), now, now),
            )

    def heartbeat_worker(self, name: str, active_job_id: str | None = None) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE compute_workers SET last_heartbeat=?, active_job_id=?
                WHERE name=?
                """,
                (self.clock().isoformat(), active_job_id, name),
            )
            if cursor.rowcount != 1:
                raise KeyError(name)

    def worker_capabilities(self, name: str) -> RuntimeCapabilities | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM compute_workers WHERE name=?", (name,)
            ).fetchone()
        if row is None:
            return None
        return RuntimeCapabilities(
            node_name=name,
            capabilities=tuple(json.loads(row["capabilities_json"])),
            accelerated_analytics_available=True,
            software_version=row["version"],
        )

    def get_job(self, job_id: str) -> ComputeJob | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM compute_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return self._job(row) if row else None

    def health(self, *, offline_after_seconds: int = 60) -> dict:
        now = self.clock()
        with self.connect() as connection:
            counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM compute_jobs GROUP BY status"
                )
            }
            workers = connection.execute(
                "SELECT * FROM compute_workers ORDER BY name"
            ).fetchall()
        worker_rows = []
        for row in workers:
            seen = datetime.fromisoformat(row["last_heartbeat"])
            online = (now - seen).total_seconds() <= offline_after_seconds
            worker_rows.append(
                {
                    "name": row["name"],
                    "status": "online" if online else "offline",
                    "last_heartbeat": row["last_heartbeat"],
                    "capabilities": json.loads(row["capabilities_json"]),
                    "active_job": row["active_job_id"],
                    "last_successful_job": row["last_successful_job"],
                    "last_failure": row["last_failure"],
                    "software_version": row["version"],
                }
            )
        primary = worker_rows[0] if worker_rows else {
            "status": "offline", "name": None, "last_heartbeat": None,
            "capabilities": [], "active_job": None,
            "last_successful_job": None, "last_failure": None,
            "software_version": None,
        }
        return {
            "fedora": {
                "core_runtime": "available",
                "pure_python_forecasting": "available",
                "accelerated_analytics": "remote_only",
            },
            "windows_worker": primary,
            "workers": worker_rows,
            "queue": {
                "pending": counts.get("pending", 0),
                "claimed": counts.get("claimed", 0),
                "completed": counts.get("completed", 0),
                "failed": counts.get("failed", 0),
            },
        }

    @staticmethod
    def _job(row: sqlite3.Row) -> ComputeJob:
        values = dict(row)
        values["required_capabilities"] = tuple(
            json.loads(values.pop("required_capabilities_json"))
        )
        values["payload"] = json.loads(values.pop("payload_json"))
        values.pop("deduplication_key")
        values.pop("result_json")
        return ComputeJob(**values)
