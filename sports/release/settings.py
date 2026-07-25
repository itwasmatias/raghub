from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReleaseSettings:
    environment: str
    secret_key: str | None
    demo_read_only: bool
    request_timeout_seconds: float
    maximum_request_bytes: int
    rate_limit_per_minute: int
    lifecycle_database: str

    @classmethod
    def from_environment(cls) -> ReleaseSettings:
        environment = os.getenv("RAGHUB_ENV", "development").strip().lower()
        if environment not in {"development", "test", "production"}:
            raise ValueError("RAGHUB_ENV must be development, test, or production")
        secret_key = os.getenv("RAGHUB_SECRET_KEY")
        if environment == "production" and not secret_key:
            raise ValueError(
                "RAGHUB_SECRET_KEY is required when RAGHUB_ENV=production"
            )
        timeout = float(os.getenv("RAGHUB_REQUEST_TIMEOUT_SECONDS", "20"))
        if timeout <= 0:
            raise ValueError("RAGHUB_REQUEST_TIMEOUT_SECONDS must be positive")
        maximum_request_bytes = int(
            os.getenv("RAGHUB_MAX_REQUEST_BYTES", "1048576")
        )
        if maximum_request_bytes <= 0:
            raise ValueError("RAGHUB_MAX_REQUEST_BYTES must be positive")
        rate_limit = int(os.getenv("RAGHUB_RATE_LIMIT_PER_MINUTE", "120"))
        if rate_limit <= 0:
            raise ValueError("RAGHUB_RATE_LIMIT_PER_MINUTE must be positive")
        return cls(
            environment=environment,
            secret_key=secret_key,
            demo_read_only=cls._boolean(
                os.getenv("RAGHUB_DEMO_READ_ONLY", "false")
            ),
            request_timeout_seconds=timeout,
            maximum_request_bytes=maximum_request_bytes,
            rate_limit_per_minute=rate_limit,
            lifecycle_database=os.getenv(
                "RAGHUB_LIFECYCLE_DB", "data/intelligence_lifecycle.db"
            ),
        )

    @staticmethod
    def _boolean(value: str) -> bool:
        normalized = value.strip().lower()
        if normalized not in {"true", "false", "1", "0", "yes", "no"}:
            raise ValueError("boolean configuration value is invalid")
        return normalized in {"true", "1", "yes"}
