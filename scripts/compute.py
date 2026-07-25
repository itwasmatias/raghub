from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from sports.compute.models import ComputeJobRequest
from sports.compute.repository import ComputeJobRepository


def repository() -> ComputeJobRepository:
    load_dotenv()
    return ComputeJobRepository(
        os.getenv("RAGHUB_COMPUTE_DATABASE", "data/compute_jobs.db")
    )


def queue_smoke_job() -> int:
    request = ComputeJobRequest(
        job_type="RUN_HEAVY_FEATURE_PIPELINE",
        domain="runtime-diagnostics",
        requested_model="worker-smoke",
        model_version="1.0.0",
        feature_version="summary-v1",
        input_snapshot_id=f"smoke-{datetime.now(timezone.utc).date().isoformat()}",
        input_snapshot_timestamp=datetime.now(timezone.utc).isoformat(),
        configuration_version="personal-v1",
        required_capabilities=("numpy",),
        payload={
            "rows": [{"value": 1}, {"value": 2}, {"value": 3}],
            "numeric_fields": ["value"],
        },
    )
    job = repository().create_job(request)
    print(json.dumps({"id": job.id, "status": job.status}, indent=2))
    return 0


def show_health() -> int:
    print(json.dumps(repository().health(), indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("queue-smoke", "health"))
    arguments = parser.parse_args()
    return queue_smoke_job() if arguments.command == "queue-smoke" else show_health()


if __name__ == "__main__":
    raise SystemExit(main())
