from __future__ import annotations

import hashlib
import json
import signal
import sys
from datetime import datetime, timedelta, timezone

from app import create_app
from sports.compute.models import ComputeJobRequest, RuntimeCapabilities
from sports.compute.repository import ComputeJobRepository
from sports.compute.runtime_diagnostics import diagnose_module


def _now() -> datetime:
    return datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


def _job(**overrides) -> ComputeJobRequest:
    values = {
        "job_type": "GENERATE_NBA_FORECASTS",
        "domain": "sip",
        "requested_model": "nba-baseline",
        "model_version": "1.0.0",
        "feature_version": "moneyline-v1",
        "input_snapshot_id": "snapshot-1",
        "input_snapshot_timestamp": _now().isoformat(),
        "configuration_version": "personal-v1",
        "required_capabilities": ("numpy", "sklearn"),
    }
    values.update(overrides)
    return ComputeJobRequest(**values)


def test_runtime_diagnostic_reports_sigill_without_crashing_server():
    result = diagnose_module(
        "unsafe_test_module",
        command=[sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGILL)"],
    )
    assert result.status == "SIGILL"
    assert result.signal == signal.SIGILL
    assert result.recommended_node == "windows-worker"


def test_compute_queue_claim_is_atomic_and_duplicate_is_suppressed(tmp_path):
    repository = ComputeJobRepository(tmp_path / "compute.db", clock=_now)
    first = repository.create_job(_job())
    duplicate = repository.create_job(_job())
    assert duplicate.id == first.id

    capabilities = RuntimeCapabilities(
        node_name="windows-1",
        capabilities=("numpy", "sklearn"),
    )
    claimed = repository.claim_next(
        worker_name="windows-1", capabilities=capabilities, lease_seconds=60
    )
    assert claimed is not None
    assert claimed.id == first.id
    assert repository.claim_next(
        worker_name="windows-2", capabilities=capabilities, lease_seconds=60
    ) is None


def test_expired_lease_can_be_reclaimed(tmp_path):
    current = [_now()]
    repository = ComputeJobRepository(
        tmp_path / "compute.db", clock=lambda: current[0]
    )
    repository.create_job(_job())
    capabilities = RuntimeCapabilities(
        node_name="windows-1", capabilities=("numpy", "sklearn")
    )
    repository.claim_next(
        worker_name="windows-1", capabilities=capabilities, lease_seconds=30
    )
    current[0] += timedelta(seconds=31)
    reclaimed = repository.claim_next(
        worker_name="windows-2", capabilities=capabilities, lease_seconds=30
    )
    assert reclaimed is not None
    assert reclaimed.assigned_worker == "windows-2"
    assert reclaimed.attempt_count == 2


def test_worker_result_checksum_is_validated(tmp_path):
    repository = ComputeJobRepository(tmp_path / "compute.db", clock=_now)
    job = repository.create_job(_job(required_capabilities=()))
    repository.claim_job(job.id, "worker", lease_seconds=60)
    payload = {"forecasts": [], "model": {"execution_node": "windows-worker"}}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    checksum = hashlib.sha256(encoded).hexdigest()

    completed = repository.complete_job(
        job.id, "worker", payload=payload, checksum=checksum
    )
    assert completed.status == "completed"
    assert completed.result_checksum == checksum

    other = repository.create_job(
        _job(input_snapshot_id="snapshot-2", required_capabilities=())
    )
    repository.claim_job(other.id, "worker", lease_seconds=60)
    try:
        repository.complete_job(
            other.id, "worker", payload=payload, checksum="0" * 64
        )
    except ValueError as error:
        assert "checksum" in str(error).lower()
    else:
        raise AssertionError("invalid checksum was accepted")


def test_worker_api_requires_token_and_reports_offline_health(tmp_path):
    repository = ComputeJobRepository(tmp_path / "compute.db", clock=_now)
    app = create_app(
        compute_repository=repository,
        worker_token="separate-worker-secret",
    )
    client = app.test_client()

    assert client.post("/api/internal/workers/register", json={}).status_code == 401
    health = client.get("/api/system/compute-health").get_json()
    assert health["fedora"]["core_runtime"] == "available"
    assert health["fedora"]["pure_python_forecasting"] == "available"
    assert health["windows_worker"]["status"] == "offline"
    assert health["queue"]["pending"] == 0


def test_compatible_worker_can_register_claim_and_complete(tmp_path):
    repository = ComputeJobRepository(tmp_path / "compute.db", clock=_now)
    job = repository.create_job(_job())
    app = create_app(compute_repository=repository, worker_token="worker-token")
    client = app.test_client()
    headers = {"Authorization": "Bearer worker-token"}
    registration = client.post(
        "/api/internal/workers/register",
        headers=headers,
        json={
            "name": "windows-1",
            "version": "1.0.0",
            "capabilities": ["numpy", "sklearn"],
        },
    )
    assert registration.status_code == 200

    response = client.get(
        "/api/internal/compute-jobs/next",
        headers=headers,
        query_string={"worker_name": "windows-1"},
    )
    assert response.status_code == 200
    assert response.get_json()["job"]["id"] == job.id

    payload = {"forecasts": [], "model": {"execution_node": "windows-worker"}}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    response = client.post(
        f"/api/internal/compute-jobs/{job.id}/complete",
        headers=headers,
        json={
            "worker_name": "windows-1",
            "result": payload,
            "checksum": hashlib.sha256(encoded).hexdigest(),
        },
    )
    assert response.status_code == 200
    assert response.get_json()["job"]["status"] == "completed"
