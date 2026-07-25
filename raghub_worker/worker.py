from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests
from dotenv import load_dotenv

LOGGER = logging.getLogger("raghub.worker")


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    server_url: str
    token: str
    name: str
    poll_seconds: int
    capabilities: tuple[str, ...]
    version: str = "1.0.0"

    @classmethod
    def from_environment(cls) -> "WorkerSettings":
        load_dotenv()
        server_url = os.getenv("RAGHUB_SERVER_URL", "").rstrip("/")
        token = os.getenv("RAGHUB_WORKER_TOKEN", "")
        if not server_url or not token:
            raise ValueError("RAGHUB_SERVER_URL and RAGHUB_WORKER_TOKEN are required")
        return cls(
            server_url=server_url,
            token=token,
            name=os.getenv("RAGHUB_WORKER_NAME", "windows-compute-1"),
            poll_seconds=max(1, int(os.getenv("RAGHUB_WORKER_POLL_SECONDS", "10"))),
            capabilities=tuple(
                value.strip()
                for value in os.getenv(
                    "RAGHUB_WORKER_CAPABILITIES", "numpy,pandas,scipy,sklearn"
                ).split(",")
                if value.strip()
            ),
        )


def _heavy_feature_pipeline(job: dict[str, Any]) -> dict[str, Any]:
    """A deterministic initial worker operation; no arbitrary code is accepted."""
    rows = list(job.get("payload", {}).get("rows", []))
    numeric_fields = tuple(job.get("payload", {}).get("numeric_fields", []))
    summaries: dict[str, dict[str, float | int | None]] = {}
    for field in numeric_fields:
        values = [
            float(row[field]) for row in rows
            if isinstance(row, dict) and row.get(field) is not None
        ]
        summaries[str(field)] = {
            "count": len(values),
            "mean": sum(values) / len(values) if values else None,
            "minimum": min(values) if values else None,
            "maximum": max(values) if values else None,
        }
    return {
        "job_type": job["job_type"],
        "execution_node": "windows-worker",
        "input_snapshot_id": job["input_snapshot_id"],
        "feature_version": job["feature_version"],
        "summaries": summaries,
    }


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "RUN_HEAVY_FEATURE_PIPELINE": _heavy_feature_pipeline,
}


class ComputeWorker:
    def __init__(
        self,
        settings: WorkerSettings,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.stopping = False

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.token}"}

    def register(self) -> None:
        self._post(
            "/api/internal/workers/register",
            {
                "name": self.settings.name,
                "version": self.settings.version,
                "capabilities": self.settings.capabilities,
            },
        )

    def run_once(self) -> bool:
        response = self.session.get(
            f"{self.settings.server_url}/api/internal/compute-jobs/next",
            headers=self.headers,
            params={"worker_name": self.settings.name},
            timeout=20,
        )
        response.raise_for_status()
        job = response.json().get("job")
        if not job:
            self._post(
                "/api/internal/workers/heartbeat",
                {"worker_name": self.settings.name, "active_job_id": None},
            )
            return False
        job_id = str(job["id"])
        handler = HANDLERS.get(str(job["job_type"]))
        if handler is None:
            self._post(
                f"/api/internal/compute-jobs/{job_id}/fail",
                {
                    "worker_name": self.settings.name,
                    "error": f"Unsupported allow-listed job type: {job['job_type']}",
                },
            )
            return True
        try:
            result = handler(job)
            encoded = json.dumps(
                result, sort_keys=True, separators=(",", ":")
            ).encode()
            self._post(
                f"/api/internal/compute-jobs/{job_id}/complete",
                {
                    "worker_name": self.settings.name,
                    "result": result,
                    "checksum": hashlib.sha256(encoded).hexdigest(),
                },
            )
        except Exception as error:
            self._post(
                f"/api/internal/compute-jobs/{job_id}/fail",
                {
                    "worker_name": self.settings.name,
                    "error": f"{type(error).__name__}: {str(error)[:300]}",
                },
            )
        return True

    def run(self) -> None:
        self.register()
        while not self.stopping:
            try:
                self.run_once()
            except requests.RequestException as error:
                LOGGER.warning("worker_network_error error_type=%s", type(error).__name__)
            if not self.stopping:
                time.sleep(self.settings.poll_seconds)

    def stop(self, *_args) -> None:
        self.stopping = True

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(
            f"{self.settings.server_url}{path}",
            headers=self.headers,
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
        return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description="RAGHub Windows compute worker")
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    worker = ComputeWorker(WorkerSettings.from_environment())
    signal.signal(signal.SIGINT, worker.stop)
    signal.signal(signal.SIGTERM, worker.stop)
    if arguments.once:
        worker.register()
        worker.run_once()
    else:
        worker.run()
    return 0
