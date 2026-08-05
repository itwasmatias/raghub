from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any


SCHEMA_VERSION = "controller-proposal-v0.1"
LIFECYCLE_ACTIONS = {
    "MISSION_START",
    "MISSION_PAUSE",
    "MISSION_RESUME",
    "MISSION_CANCEL",
    "TASK_RETRY",
}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def proposal_revision(proposal: dict[str, Any]) -> str:
    immutable = {
        key: value
        for key, value in proposal.items()
        if key
        not in {
            "status",
            "proposal_revision",
            "decision",
            "application",
            "supersedes_action_ids",
        }
    }
    return fingerprint(immutable)


def lifecycle_action_id(binding: dict[str, Any]) -> str:
    return "controller-" + fingerprint(
        {"schema_version": SCHEMA_VERSION, **binding}
    )


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)
