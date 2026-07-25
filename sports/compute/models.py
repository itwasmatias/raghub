from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    node_name: str
    capabilities: tuple[str, ...] = ()
    core_runtime_available: bool = True
    pure_python_forecasting_available: bool = True
    accelerated_analytics_available: bool = False
    remote_compute_available: bool = True
    remote_training_available: bool = True
    software_version: str = "1.0.0"

    def supports(self, required: tuple[str, ...]) -> bool:
        return set(required).issubset(self.capabilities)


@dataclass(frozen=True, slots=True)
class ComputeJobRequest:
    job_type: str
    domain: str
    requested_model: str
    model_version: str
    feature_version: str
    input_snapshot_id: str
    input_snapshot_timestamp: str
    configuration_version: str
    required_capabilities: tuple[str, ...] = ()
    priority: int = 100
    claim_expiration_time: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    max_attempts: int = 3


@dataclass(frozen=True, slots=True)
class ComputeJob:
    id: str
    job_type: str
    domain: str
    requested_model: str
    model_version: str
    feature_version: str
    input_snapshot_id: str
    input_snapshot_timestamp: str
    configuration_version: str
    required_capabilities: tuple[str, ...]
    priority: int
    requested_time: str
    claim_expiration_time: str | None
    status: str
    assigned_worker: str | None
    attempt_count: int
    max_attempts: int
    last_error: str | None
    started_time: str | None
    completed_time: str | None
    lease_expires_at: str | None
    result_reference: str | None
    result_checksum: str | None
    payload: dict[str, Any]

