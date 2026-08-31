"""Common AppEvent contract for MissionaryX Revenue Bridge v0.1.

This module defines the normalized immutable event representation that all
inbound sources (e.g. GitHub, Email) normalize into.

Key Invariants:
- Immutable (frozen dataclass)
- Canonical SHA-256 fingerprinting
- Provenance and raw evidence retention
- Explicit uncertainty tracking (missing evidence remains unknown; never fabricated)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


def _canonical_bytes(value: Any) -> bytes:
    """Canonical JSON serialization for hashing and fingerprints."""
    def _default(obj: Any) -> Any:
        if isinstance(obj, datetime):
            if obj.tzinfo is None or obj.utcoffset() is None:
                raise TypeError("Datetime must be timezone-aware for canonical serialization")
            return obj.astimezone(timezone.utc).isoformat()
        if isinstance(obj, (set, frozenset)):
            return sorted(list(obj))
        if isinstance(obj, tuple):
            return list(obj)
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

    return json.dumps(
        value,
        default=_default,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _require_text(value: Any, field_name: str) -> str:
    """Validate and return non-empty trimmed string."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _require_timestamp(value: Any, field_name: str) -> datetime:
    """Validate and normalize timezone-aware datetime to UTC."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class AppEvent:
    """Normalized immutable representation of an observed inbound application event.

    Preserves provenance, raw evidence references, capabilities, and uncertainty.
    """
    event_id: str
    source_app: str
    event_type: str
    actor: str
    thread_id: str
    content: str
    source_url: str | None
    observed_at: datetime
    raw_evidence_ref: str
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    uncertainty: tuple[str, ...] = field(default_factory=tuple)
    metadata: tuple[tuple[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # Field validation
        object.__setattr__(self, "event_id", _require_text(self.event_id, "event_id"))
        object.__setattr__(self, "source_app", _require_text(self.source_app, "source_app"))
        object.__setattr__(self, "event_type", _require_text(self.event_type, "event_type"))
        object.__setattr__(self, "actor", _require_text(self.actor, "actor"))
        object.__setattr__(self, "thread_id", _require_text(self.thread_id, "thread_id"))
        if not isinstance(self.content, str):
            raise TypeError("content must be a string")
        object.__setattr__(self, "observed_at", _require_timestamp(self.observed_at, "observed_at"))
        object.__setattr__(self, "raw_evidence_ref", _require_text(self.raw_evidence_ref, "raw_evidence_ref"))

        # Ensure capabilities and uncertainty are immutable tuples of strings
        if not isinstance(self.capabilities, tuple):
            object.__setattr__(self, "capabilities", tuple(self.capabilities))
        if not isinstance(self.uncertainty, tuple):
            object.__setattr__(self, "uncertainty", tuple(self.uncertainty))
        if not isinstance(self.metadata, tuple):
            object.__setattr__(self, "metadata", tuple(self.metadata))

    @property
    def fingerprint(self) -> str:
        """Deterministic SHA-256 fingerprint of the normalized event payload."""
        payload = {
            "event_id": self.event_id,
            "source_app": self.source_app,
            "event_type": self.event_type,
            "actor": self.actor,
            "thread_id": self.thread_id,
            "content": self.content,
            "source_url": self.source_url,
            "observed_at": self.observed_at.isoformat(),
            "raw_evidence_ref": self.raw_evidence_ref,
            "capabilities": sorted(self.capabilities),
            "uncertainty": sorted(self.uncertainty),
            "metadata": sorted([(k, str(v)) for k, v in self.metadata]),
        }
        return hashlib.sha256(_canonical_bytes(payload)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        return {
            "event_id": self.event_id,
            "source_app": self.source_app,
            "event_type": self.event_type,
            "actor": self.actor,
            "thread_id": self.thread_id,
            "content": self.content,
            "source_url": self.source_url,
            "observed_at": self.observed_at.isoformat(),
            "raw_evidence_ref": self.raw_evidence_ref,
            "capabilities": list(self.capabilities),
            "uncertainty": list(self.uncertainty),
            "metadata": dict(self.metadata),
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AppEvent:
        """Create AppEvent from dictionary representation."""
        observed_at = data["observed_at"]
        if isinstance(observed_at, str):
            observed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))

        metadata_val = data.get("metadata", {})
        if isinstance(metadata_val, dict):
            metadata_tuples = tuple(metadata_val.items())
        elif isinstance(metadata_val, (list, tuple)):
            metadata_tuples = tuple(metadata_val)
        else:
            metadata_tuples = ()

        return cls(
            event_id=data["event_id"],
            source_app=data["source_app"],
            event_type=data["event_type"],
            actor=data["actor"],
            thread_id=data["thread_id"],
            content=data.get("content", ""),
            source_url=data.get("source_url"),
            observed_at=observed_at,
            raw_evidence_ref=data["raw_evidence_ref"],
            capabilities=tuple(data.get("capabilities", ())),
            uncertainty=tuple(data.get("uncertainty", ())),
            metadata=metadata_tuples,
        )
