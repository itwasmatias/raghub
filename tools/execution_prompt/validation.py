"""Validation for execution prompt inputs.

M6 Execution Prompt Engine v0.1 — Validation

Validates:
- Dev-status freshness, completeness, contradictions
- Milestone spec completeness
- Secret-bearing input rejection
- .env file prohibition
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any


class DevStatusValidationError(Exception):
    """Raised when dev-status validation fails."""


class MilestoneValidationError(Exception):
    """Raised when milestone spec validation fails."""


# Secret patterns to detect
SECRET_PATTERNS = [
    r"(?i)api[_-]?key\s*[:=]\s*['\"]?[a-zA-Z0-9_-]{20,}",
    r"(?i)secret[_-]?key\s*[:=]\s*['\"]?[a-zA-Z0-9_-]{20,}",
    r"(?i)password\s*[:=]\s*['\"]?[a-zA-Z0-9_-]{8,}",
    r"(?i)AWS_SECRET_ACCESS_KEY",
    r"(?i)sk_live_[a-zA-Z0-9]{20,}",
    r"(?i)Bearer\s+[a-zA-Z0-9_-]{20,}",
]


def _detect_secrets(text: str) -> bool:
    """Detect secret patterns in text."""
    for pattern in SECRET_PATTERNS:
        if re.search(pattern, text):
            return True
    return False


def _check_env_file_reference(text: str) -> bool:
    """Check for .env file references."""
    return bool(re.search(r"\.env\b", text))


def _parse_timestamp(value: Any, field_name: str) -> datetime:
    """Parse an ISO-8601 timestamp and require timezone awareness."""
    if isinstance(value, str):
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DevStatusValidationError(
                f"{field_name} must be a valid ISO-8601 timestamp"
            ) from exc
    elif isinstance(value, datetime):
        timestamp = value
    else:
        raise DevStatusValidationError(f"{field_name} must be ISO-8601 string or datetime")

    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise DevStatusValidationError(f"{field_name} must be timezone-aware")
    return timestamp


def validate_dev_status(data: Any, *, reference_time: datetime | None = None) -> dict[str, Any]:
    """Validate dev-status handoff.

    Checks:
    - Required fields present
    - Not stale (< 24 hours old)
    - Not contradictory (clean status with dirty diff)
    - No secrets
    - No .env file references

    Args:
        data: Dev-status data to validate
        reference_time: Optional reference time for staleness check (defaults to now)
    """
    if data is None:
        raise DevStatusValidationError("dev-status is required")

    if not isinstance(data, dict):
        raise DevStatusValidationError("dev-status must be a dictionary")

    # Check required fields
    required = ["timestamp", "repo", "branch", "head", "status", "diff"]
    missing = [field for field in required if field not in data]
    if missing:
        raise DevStatusValidationError(f"dev-status missing required field: {missing[0]}")

    # Check timestamp freshness
    timestamp = _parse_timestamp(data["timestamp"], "timestamp")

    if reference_time is not None:
        if reference_time.tzinfo is None or reference_time.utcoffset() is None:
            raise DevStatusValidationError("reference_time must be timezone-aware")
        now = reference_time.astimezone(timezone.utc)
    else:
        now = datetime.now(timezone.utc)

    timestamp_utc = timestamp.astimezone(timezone.utc)
    if timestamp_utc > now:
        raise DevStatusValidationError("dev-status timestamp is in the future")

    age = now - timestamp_utc
    if age > timedelta(hours=24):
        raise DevStatusValidationError(f"dev-status is stale (>{age.total_seconds()/3600:.1f} hours old, max 24 hours)")

    # Check for contradictions
    status = data["status"]
    diff = data["diff"]
    if status == "CANONICAL_INTEGRATED" and diff.strip():
        raise DevStatusValidationError("dev-status is contradictory: CANONICAL_INTEGRATED status but has uncommitted changes")

    # Check for secrets
    combined_text = f"{data.get('diff', '')} {data.get('branch', '')}"
    if _detect_secrets(combined_text):
        raise DevStatusValidationError("dev-status contains secret pattern detected")

    # Check for .env file references
    if _check_env_file_reference(diff):
        raise DevStatusValidationError("dev-status diff contains .env file reference (prohibited)")

    return data


def validate_milestone_spec(data: Any) -> dict[str, Any]:
    """Validate milestone specification.

    Checks:
    - Required fields present
    - No secrets in description or requirements
    """
    if data is None:
        raise MilestoneValidationError("milestone spec is required")

    if not isinstance(data, dict):
        raise MilestoneValidationError("milestone spec must be a dictionary")

    # Check required fields
    required = ["milestone_id", "title", "description"]
    missing = [field for field in required if field not in data]
    if missing:
        raise MilestoneValidationError(f"milestone spec missing required field: {missing[0]}")

    # Check for secrets
    combined_text = f"{data.get('description', '')} {data.get('title', '')}"
    if "requirements" in data and isinstance(data["requirements"], list):
        combined_text += " " + " ".join(str(req) for req in data["requirements"])

    if _detect_secrets(combined_text):
        raise MilestoneValidationError("milestone spec contains secret pattern detected")

    return data


__all__ = [
    "DevStatusValidationError",
    "MilestoneValidationError",
    "validate_dev_status",
    "validate_milestone_spec",
]
