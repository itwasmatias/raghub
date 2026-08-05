from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flask import g


@dataclass
class APIError(Exception):
    code: str
    message: str
    status: int
    retryable: bool = False
    current_revision: str | None = None

    def response(self) -> tuple[dict[str, Any], int]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "correlation_id": g.get("controller_request_id", "unavailable"),
            "retryable": self.retryable,
        }
        if self.current_revision is not None:
            payload["current_revision"] = self.current_revision
        return {"error": payload}, self.status


def invalid(message: str) -> APIError:
    return APIError("INVALID_REQUEST", message, 400)

