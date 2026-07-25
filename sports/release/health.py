from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


class SystemHealthService:
    def __init__(
        self,
        probes: dict[str, Callable[[], dict[str, Any]]],
    ) -> None:
        self.probes = dict(probes)

    def snapshot(self) -> dict[str, Any]:
        components: dict[str, dict[str, Any]] = {}
        for name, probe in self.probes.items():
            request_id = f"{name}-{uuid4().hex[:12]}"
            try:
                result = dict(probe())
            except Exception as error:
                result = {
                    "status": "unavailable",
                    "cause": f"{type(error).__name__}: {error}",
                    "cached_available": False,
                }
            result.setdefault("request_id", request_id)
            result.setdefault("last_successful_refresh", None)
            result.setdefault("cached_available", False)
            result.setdefault("retry_after_seconds", None)
            components[name] = result
        statuses = {item.get("status") for item in components.values()}
        overall = (
            "healthy"
            if statuses <= {"healthy"}
            else "unavailable"
            if statuses == {"unavailable"}
            else "degraded"
        )
        return {
            "status": overall,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "components": components,
        }

