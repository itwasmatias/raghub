"""Normalized read-model spine for mission-adjacent evidence.

The spine is intentionally read-only. It normalizes evidence references from
existing authoritative stores without becoming a new store of record.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping

from federation.worker_execution import WorkerExecutionAttempt
from research_mission.checkpoint import MissionCheckpoint
from research_mission.recovery import MissionRecoveryEvidence
from research_mission.results import ResearchEvidence
from tools.ai_controller.mission.events import EventSnapshot
from tools.ai_controller.operations_api.approvals import ApprovalSnapshot


SCHEMA_VERSION = "raghub.evidence-spine.v0.1"


class EvidenceSpineError(Exception):
    """Base error for evidence spine normalization."""


class EvidenceSpineConflictError(EvidenceSpineError):
    """Raised when two records claim the same correlation key with different content."""


class EvidenceSpineCorruptionError(EvidenceSpineError):
    """Raised when a record cannot be normalized safely."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _stable_source_revision(value: Any) -> str:
    return _fingerprint(_normalize_json(value))


def _require_text(value: Any, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field_name} must not contain surrounding whitespace")
    return value


def _require_optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field_name)


def _require_timestamp(value: Any, field_name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _normalize_json(value: Any) -> Any:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("payload contains non-finite float")
        return value
    if type(value) is bytes:
        return value.hex()
    if isinstance(value, datetime):
        return _require_timestamp(value, "payload datetime").isoformat()
    if isinstance(value, Enum):
        return _normalize_json(value.value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key in sorted(value):
            if type(key) is not str:
                raise TypeError("mapping keys must be strings")
            normalized[key] = _normalize_json(value[key])
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_normalize_json(item) for item in value]
        items.sort(key=_canonical)
        return items
    if is_dataclass(value):
        return _normalize_json(asdict(value))
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _normalize_json(value.to_dict())
    raise TypeError(f"unsupported evidence value: {type(value).__name__}")


def _payload_copy(payload: Mapping[str, Any] | dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    return dict(payload)


def _record_sort_key(record: "EvidenceRecord") -> tuple[str, str, str, str, str]:
    return (
        record.key.domain_id or "",
        record.key.mission_id or "",
        record.key.task_id or "",
        record.key.source,
        record.key.record_id,
    )


@dataclass(frozen=True, slots=True)
class EvidenceCorrelationKey:
    source: str
    record_id: str
    mission_id: str | None = None
    task_id: str | None = None
    domain_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _require_text(self.source, "source"))
        object.__setattr__(self, "record_id", _require_text(self.record_id, "record_id"))
        object.__setattr__(self, "mission_id", _require_optional_text(self.mission_id, "mission_id"))
        object.__setattr__(self, "task_id", _require_optional_text(self.task_id, "task_id"))
        object.__setattr__(self, "domain_id", _require_optional_text(self.domain_id, "domain_id"))

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "source": self.source,
            "record_id": self.record_id,
        }
        if self.mission_id is not None:
            payload["mission_id"] = self.mission_id
        if self.task_id is not None:
            payload["task_id"] = self.task_id
        if self.domain_id is not None:
            payload["domain_id"] = self.domain_id
        return payload


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    source_revision: str
    fingerprint: str
    observed_at: datetime
    summary: str
    reference: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_revision", _require_text(self.source_revision, "source_revision"))
        object.__setattr__(self, "fingerprint", _require_text(self.fingerprint, "fingerprint"))
        if len(self.fingerprint) != 64 or any(char not in "0123456789abcdef" for char in self.fingerprint):
            raise ValueError("fingerprint must be lowercase SHA-256 hex")
        object.__setattr__(self, "observed_at", _require_timestamp(self.observed_at, "observed_at"))
        object.__setattr__(self, "summary", _require_text(self.summary, "summary"))
        object.__setattr__(self, "reference", _require_optional_text(self.reference, "reference"))

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "source_revision": self.source_revision,
            "fingerprint": self.fingerprint,
            "observed_at": self.observed_at.isoformat(),
            "summary": self.summary,
            "reference": self.reference,
        }
        return {key: value for key, value in payload.items() if value is not None}


@dataclass(frozen=True, slots=True)
class EvidencePointer:
    """Verifiable evidence record locator binding key, reference, and fingerprint.

    An EvidencePointer uniquely identifies a specific EvidenceRecord and cannot be
    satisfied by a fabricated or mismatched record. The triple of correlation key,
    reference fingerprint, and record fingerprint ensures exact matching against
    an authoritative EvidenceSpine.
    """
    key: EvidenceCorrelationKey
    reference_fingerprint: str
    record_fingerprint: str

    def __post_init__(self) -> None:
        if type(self.key) is not EvidenceCorrelationKey:
            raise TypeError("key must be an EvidenceCorrelationKey")
        object.__setattr__(self, "reference_fingerprint", _require_text(self.reference_fingerprint, "reference_fingerprint"))
        object.__setattr__(self, "record_fingerprint", _require_text(self.record_fingerprint, "record_fingerprint"))
        if len(self.reference_fingerprint) != 64 or any(char not in "0123456789abcdef" for char in self.reference_fingerprint):
            raise ValueError("reference_fingerprint must be lowercase SHA-256 hex")
        if len(self.record_fingerprint) != 64 or any(char not in "0123456789abcdef" for char in self.record_fingerprint):
            raise ValueError("record_fingerprint must be lowercase SHA-256 hex")

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key.to_dict(),
            "reference_fingerprint": self.reference_fingerprint,
            "record_fingerprint": self.record_fingerprint,
        }

    @classmethod
    def from_record(cls, record: EvidenceRecord) -> "EvidencePointer":
        """Create pointer from existing verified EvidenceRecord."""
        if type(record) is not EvidenceRecord:
            raise TypeError("record must be an EvidenceRecord")
        return cls(
            key=record.key,
            reference_fingerprint=record.reference.fingerprint,
            record_fingerprint=record.record_fingerprint,
        )


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    key: EvidenceCorrelationKey
    reference: EvidenceReference
    payload: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    record_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.key) is not EvidenceCorrelationKey:
            raise TypeError("key must be an EvidenceCorrelationKey")
        if type(self.reference) is not EvidenceReference:
            raise TypeError("reference must be an EvidenceReference")
        object.__setattr__(self, "payload", _payload_copy(self.payload))
        object.__setattr__(self, "metadata", _payload_copy(self.metadata))
        fingerprint_payload = {
            "key": self.key.to_dict(),
            "reference": self.reference.to_dict(),
            "payload": _normalize_json(self.payload),
            "metadata": _normalize_json(self.metadata),
        }
        object.__setattr__(self, "record_fingerprint", _fingerprint(fingerprint_payload))

    @property
    def evidence_id(self) -> str:
        return self.record_fingerprint

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key.to_dict(),
            "reference": self.reference.to_dict(),
            "payload": _normalize_json(self.payload),
            "metadata": _normalize_json(self.metadata),
            "record_fingerprint": self.record_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class EvidenceChain:
    records: tuple[EvidenceRecord, ...]
    chain_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [record.to_dict() for record in self.records],
            "chain_fingerprint": self.chain_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class EvidenceExport:
    generated_at: datetime
    schema_version: str
    records: tuple[EvidenceRecord, ...]
    chain: EvidenceChain
    source_revisions: tuple[tuple[str, str], ...]
    summary: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "generated_at", _require_timestamp(self.generated_at, "generated_at"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, "schema_version"))
        object.__setattr__(self, "summary", _require_text(self.summary, "summary"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "schema_version": self.schema_version,
            "records": [record.to_dict() for record in self.records],
            "chain": self.chain.to_dict(),
            "source_revisions": [
                {"source": source, "source_revision": revision}
                for source, revision in self.source_revisions
            ],
            "summary": self.summary,
        }


class EvidenceSpine:
    """Immutable normalized evidence inventory."""

    def __init__(self, records: Iterable[EvidenceRecord] = ()) -> None:
        normalized = self._normalize_records(records)
        self._records = normalized
        self._chain = EvidenceChain(
            normalized,
            _fingerprint([record.to_dict() for record in normalized]),
        )
        # Build lookup index for verification
        self._index: dict[tuple[str, str, str | None, str | None, str | None], EvidenceRecord] = {}
        for record in normalized:
            key_tuple = (
                record.key.source,
                record.key.record_id,
                record.key.domain_id,
                record.key.mission_id,
                record.key.task_id,
            )
            self._index[key_tuple] = record

    @classmethod
    def from_records(cls, records: Iterable[EvidenceRecord]) -> "EvidenceSpine":
        return cls(records)

    @property
    def records(self) -> tuple[EvidenceRecord, ...]:
        return self._records

    @property
    def chain(self) -> EvidenceChain:
        return self._chain

    @property
    def chain_fingerprint(self) -> str:
        return self._chain.chain_fingerprint

    @property
    def source_revisions(self) -> tuple[tuple[str, str], ...]:
        revisions = sorted(
            {
                (record.key.source, record.reference.source_revision)
                for record in self._records
            },
            key=lambda item: (item[0], item[1]),
        )
        return tuple(revisions)

    def __iter__(self):
        return iter(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def records_for_mission(self, mission_id: str) -> tuple[EvidenceRecord, ...]:
        mission_id = _require_text(mission_id, "mission_id")
        return tuple(record for record in self._records if record.key.mission_id == mission_id)

    def records_for_task(self, mission_id: str, task_id: str) -> tuple[EvidenceRecord, ...]:
        mission_id = _require_text(mission_id, "mission_id")
        task_id = _require_text(task_id, "task_id")
        return tuple(
            record
            for record in self._records
            if record.key.mission_id == mission_id and record.key.task_id == task_id
        )

    def records_for_source(self, source: str) -> tuple[EvidenceRecord, ...]:
        source = _require_text(source, "source")
        return tuple(record for record in self._records if record.key.source == source)

    def verify_evidence(self, pointer: EvidencePointer) -> EvidenceRecord:
        """Verify evidence pointer resolves to exact record in spine.

        Verifies:
        - Correlation key exists in spine
        - Reference fingerprint matches exactly
        - Record fingerprint matches exactly

        Fails closed:
        - If key not found in spine
        - If fingerprints mismatch
        - If pointer is not an EvidencePointer

        Returns:
            The verified EvidenceRecord

        Raises:
            TypeError: If pointer is not an EvidencePointer
            EvidenceSpineError: If verification fails
        """
        if type(pointer) is not EvidencePointer:
            raise TypeError("pointer must be an EvidencePointer")

        # Lookup by correlation key
        key_tuple = (
            pointer.key.source,
            pointer.key.record_id,
            pointer.key.domain_id,
            pointer.key.mission_id,
            pointer.key.task_id,
        )
        record = self._index.get(key_tuple)

        # Fail closed if not found
        if record is None:
            raise EvidenceSpineError(
                f"Evidence not found: source={pointer.key.source!r} "
                f"record_id={pointer.key.record_id!r}"
            )

        # Verify reference fingerprint matches
        if record.reference.fingerprint != pointer.reference_fingerprint:
            raise EvidenceSpineError(
                f"Reference fingerprint mismatch for {pointer.key.source!r} "
                f"{pointer.key.record_id!r}: expected {pointer.reference_fingerprint}, "
                f"found {record.reference.fingerprint}"
            )

        # Verify record fingerprint matches
        if record.record_fingerprint != pointer.record_fingerprint:
            raise EvidenceSpineError(
                f"Record fingerprint mismatch for {pointer.key.source!r} "
                f"{pointer.key.record_id!r}: expected {pointer.record_fingerprint}, "
                f"found {record.record_fingerprint}"
            )

        return record

    def export(self, *, generated_at: datetime | None = None) -> EvidenceExport:
        generated_at = datetime.now(timezone.utc) if generated_at is None else generated_at
        generated_at = _require_timestamp(generated_at, "generated_at")
        return EvidenceExport(
            generated_at=generated_at,
            schema_version=SCHEMA_VERSION,
            records=self._records,
            chain=self._chain,
            source_revisions=self.source_revisions,
            summary=self._summary(),
        )

    def to_dict(self, *, generated_at: datetime | None = None) -> dict[str, Any]:
        return self.export(generated_at=generated_at).to_dict()

    def _summary(self) -> str:
        sources = len({record.key.source for record in self._records})
        missions = len({record.key.mission_id for record in self._records if record.key.mission_id is not None})
        tasks = len({(record.key.mission_id, record.key.task_id) for record in self._records if record.key.task_id is not None})
        return f"{len(self._records)} records from {sources} sources across {missions} missions and {tasks} task scopes"

    @staticmethod
    def _normalize_records(records: Iterable[EvidenceRecord]) -> tuple[EvidenceRecord, ...]:
        deduped: dict[tuple[str, str, str | None, str | None, str | None], EvidenceRecord] = {}
        for record in records:
            if type(record) is not EvidenceRecord:
                raise TypeError("records must contain only EvidenceRecord values")
            expected = _fingerprint(
                {
                    "key": record.key.to_dict(),
                    "reference": record.reference.to_dict(),
                    "payload": _normalize_json(record.payload),
                    "metadata": _normalize_json(record.metadata),
                }
            )
            if record.record_fingerprint != expected:
                raise EvidenceSpineCorruptionError(
                    f"record fingerprint mismatch for {record.key.record_id!r}"
                )
            key = (
                record.key.source,
                record.key.record_id,
                record.key.domain_id,
                record.key.mission_id,
                record.key.task_id,
            )
            existing = deduped.get(key)
            if existing is None:
                deduped[key] = record
                continue
            if existing.record_fingerprint != record.record_fingerprint:
                raise EvidenceSpineConflictError(
                    f"ambiguous evidence correlation for {record.key.source!r} {record.key.record_id!r}"
                )
        return tuple(sorted(deduped.values(), key=_record_sort_key))


def checkpoint_record(
    checkpoint: MissionCheckpoint,
    *,
    source_revision: str | None = None,
    domain_id: str | None = None,
) -> EvidenceRecord:
    if type(checkpoint) is not MissionCheckpoint:
        raise TypeError("checkpoint must be a MissionCheckpoint")
    source_revision = source_revision or f"revision:{checkpoint.revision}"
    payload = asdict(checkpoint)
    payload["source_revision"] = source_revision
    reference = EvidenceReference(
        source_revision=source_revision,
        fingerprint=checkpoint.state_fingerprint,
        observed_at=checkpoint.created_at,
        summary=(
            f"mission checkpoint revision {checkpoint.revision} "
            f"for mission {checkpoint.mission_id}"
        ),
        reference=checkpoint.checkpoint_id,
    )
    return EvidenceRecord(
        key=EvidenceCorrelationKey(
            source="mission_checkpoint",
            record_id=checkpoint.checkpoint_id,
            mission_id=checkpoint.mission_id,
            domain_id=domain_id,
        ),
        reference=reference,
        payload=payload,
        metadata={
            "plan_fingerprint": checkpoint.plan_fingerprint,
            "revision": checkpoint.revision,
            "mission_status": checkpoint.mission_status,
        },
    )


def recovery_record(
    evidence: MissionRecoveryEvidence,
    *,
    source_revision: str | None = None,
    domain_id: str | None = None,
) -> EvidenceRecord:
    if type(evidence) is not MissionRecoveryEvidence:
        raise TypeError("evidence must be a MissionRecoveryEvidence")
    source_revision = source_revision or f"{evidence.checkpoint_id}:{evidence.checkpoint_revision}"
    payload = asdict(evidence)
    payload["source_revision"] = source_revision
    normalized_payload = _normalize_json(payload)
    reference = EvidenceReference(
        source_revision=source_revision,
        fingerprint=_fingerprint(normalized_payload),
        observed_at=evidence.created_at,
        summary=f"mission recovery evidence for mission {evidence.mission_id}",
        reference=evidence.checkpoint_id,
    )
    return EvidenceRecord(
        key=EvidenceCorrelationKey(
            source="mission_recovery",
            record_id=evidence.recovery_id,
            mission_id=evidence.mission_id,
            domain_id=domain_id,
        ),
        reference=reference,
        payload=payload,
        metadata={
            "checkpoint_id": evidence.checkpoint_id,
            "checkpoint_revision": evidence.checkpoint_revision,
            "replication_id": evidence.replication_id,
        },
    )


def research_evidence_record(
    evidence: ResearchEvidence,
    *,
    source_revision: str | None = None,
    domain_id: str | None = None,
) -> EvidenceRecord:
    if type(evidence) is not ResearchEvidence:
        raise TypeError("evidence must be a ResearchEvidence")
    payload = {
        "evidence_id": evidence.evidence_id,
        "mission_id": evidence.mission_id,
        "task_id": evidence.task_id,
        "evidence_type": evidence.evidence_type,
        "source": evidence.source,
        "summary": evidence.summary,
        "observed_at": evidence.observed_at.isoformat(),
        "reference": evidence.reference,
        "metadata": _normalize_json(evidence.metadata),
    }
    source_revision = source_revision or (evidence.reference or evidence.evidence_id)
    reference = EvidenceReference(
        source_revision=source_revision,
        fingerprint=_fingerprint(payload),
        observed_at=evidence.observed_at,
        summary=evidence.summary,
        reference=evidence.reference,
    )
    return EvidenceRecord(
        key=EvidenceCorrelationKey(
            source="research_evidence",
            record_id=evidence.evidence_id,
            mission_id=evidence.mission_id,
            task_id=evidence.task_id,
            domain_id=domain_id,
        ),
        reference=reference,
        payload=payload,
        metadata=dict(evidence.metadata),
    )


def event_records(
    snapshot: EventSnapshot,
    *,
    domain_id: str | None = None,
) -> tuple[EvidenceRecord, ...]:
    if type(snapshot) is not EventSnapshot:
        raise TypeError("snapshot must be an EventSnapshot")
    records = []
    for index, event in enumerate(snapshot.events, start=1):
        event_payload = event.to_dict()
        fingerprint = _fingerprint(event_payload)
        mission_id = event_payload["mission_id"]
        task_id = event_payload.get("task_id")
        source_revision = _stable_source_revision(event_payload)
        reference = EvidenceReference(
            source_revision=source_revision,
            fingerprint=fingerprint,
            observed_at=_require_timestamp(
                datetime.fromisoformat(event_payload["timestamp"]),
                "event timestamp",
            ),
            summary=event_payload["event_type"],
            reference=f"event:{index}",
        )
        records.append(
            EvidenceRecord(
                key=EvidenceCorrelationKey(
                    source="mission_event",
                    record_id=fingerprint,
                    mission_id=mission_id,
                    task_id=task_id,
                    domain_id=domain_id,
                ),
                reference=reference,
                payload=event_payload,
                metadata={
                    "event_index": index,
                    "event_type": event_payload["event_type"],
                },
            )
        )
    return tuple(records)


def approval_records(
    snapshot: ApprovalSnapshot,
    *,
    domain_id: str | None = None,
) -> tuple[EvidenceRecord, ...]:
    if type(snapshot) is not ApprovalSnapshot:
        raise TypeError("snapshot must be an ApprovalSnapshot")

    mission_context: dict[str, tuple[str | None, str | None]] = {}
    for wrapper in snapshot.records:
        if not isinstance(wrapper, Mapping) or "record_type" not in wrapper:
            continue
        record_type = wrapper["record_type"]
        payload = wrapper.get(record_type)
        if not isinstance(payload, Mapping):
            continue
        action_id = payload.get("action_id")
        if not isinstance(action_id, str) or not action_id.strip():
            continue
        mission_id = payload.get("mission_id")
        task_id = payload.get("mission_task_id")
        if isinstance(mission_id, str) and mission_id.strip():
            mission_context[action_id] = (mission_id, task_id if isinstance(task_id, str) and task_id.strip() else None)

    records = []
    for index, wrapper in enumerate(snapshot.records, start=1):
        if not isinstance(wrapper, Mapping) or "record_type" not in wrapper:
            raise TypeError("approval snapshot records must be mappings with a record_type")
        record_type = wrapper["record_type"]
        if not isinstance(record_type, str) or not record_type.strip():
            raise TypeError("approval record_type must be a non-empty string")
        inner = wrapper.get(record_type)
        if not isinstance(inner, Mapping):
            raise TypeError("approval record payload is invalid")
        action_id = inner.get("action_id")
        if not isinstance(action_id, str) or not action_id.strip():
            raise TypeError("approval record action_id must be a non-empty string")
        mission_id = inner.get("mission_id")
        task_id = inner.get("mission_task_id")
        if not isinstance(mission_id, str) or not mission_id.strip():
            fallback = mission_context.get(action_id)
            mission_id = fallback[0] if fallback else None
            task_id = fallback[1] if fallback else None
        elif not (isinstance(task_id, str) and task_id.strip()):
            fallback = mission_context.get(action_id)
            task_id = fallback[1] if fallback else None
        payload = _normalize_json(wrapper)
        fingerprint = _fingerprint(payload)
        source_revision = _stable_source_revision(payload)
        reference = EvidenceReference(
            source_revision=source_revision,
            fingerprint=fingerprint,
            observed_at=_require_timestamp(
                datetime.fromisoformat(
                    str(inner.get("created_at") or inner.get("decision_time") or inner.get("applied_at"))
                ),
                "approval record timestamp",
            ),
            summary=f"approval {record_type} {action_id}",
            reference=action_id,
        )
        records.append(
            EvidenceRecord(
                key=EvidenceCorrelationKey(
                    source="approval_log",
                    record_id=fingerprint,
                    mission_id=mission_id,
                    task_id=task_id,
                    domain_id=domain_id,
                ),
                reference=reference,
                payload=payload,
                metadata={
                    "record_type": record_type,
                    "action_id": action_id,
                    "record_index": index,
                },
            )
        )
    return tuple(records)


@dataclass(frozen=True, slots=True)
class ProviderBoundaryReconciliationEvidence:
    """Provider boundary reconciliation evidence for effect resolution.

    Records authoritative provider-boundary reconciliation outcomes that can
    resolve indeterminate effect states. Only evidence from this source can
    convert INDETERMINATE to NOTHING_LANDED or SOMETHING_LANDED.
    """
    reconciliation_id: str
    effect_intent_id: str
    dispatch_id: str | None
    idempotency_key: str
    provider_operation_id: str | None
    reconciliation_outcome: str  # "no_operation_committed" or "operation_committed"
    reconciled_at: datetime
    provider_scope: str
    reconciliation_method: str  # e.g., "idempotency_key_lookup", "provider_operation_lookup"

    def __post_init__(self) -> None:
        for field_name in ("reconciliation_id", "effect_intent_id", "idempotency_key", "provider_scope", "reconciliation_method"):
            object.__setattr__(self, field_name, _require_text(getattr(self, field_name), field_name))
        if self.dispatch_id is not None:
            object.__setattr__(self, "dispatch_id", _require_text(self.dispatch_id, "dispatch_id"))
        if self.provider_operation_id is not None:
            object.__setattr__(self, "provider_operation_id", _require_text(self.provider_operation_id, "provider_operation_id"))
        object.__setattr__(self, "reconciliation_outcome", _require_text(self.reconciliation_outcome, "reconciliation_outcome"))
        if self.reconciliation_outcome not in ("no_operation_committed", "operation_committed"):
            raise ValueError(f"reconciliation_outcome must be 'no_operation_committed' or 'operation_committed', got {self.reconciliation_outcome!r}")
        object.__setattr__(self, "reconciled_at", _require_timestamp(self.reconciled_at, "reconciled_at"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "reconciliation_id": self.reconciliation_id,
            "effect_intent_id": self.effect_intent_id,
            "dispatch_id": self.dispatch_id,
            "idempotency_key": self.idempotency_key,
            "provider_operation_id": self.provider_operation_id,
            "reconciliation_outcome": self.reconciliation_outcome,
            "reconciled_at": self.reconciled_at.isoformat(),
            "provider_scope": self.provider_scope,
            "reconciliation_method": self.reconciliation_method,
        }


def provider_boundary_reconciliation_record(
    evidence: ProviderBoundaryReconciliationEvidence,
    *,
    source_revision: str | None = None,
    domain_id: str | None = None,
    mission_id: str | None = None,
    task_id: str | None = None,
) -> EvidenceRecord:
    """Convert provider boundary reconciliation evidence to spine record."""
    if type(evidence) is not ProviderBoundaryReconciliationEvidence:
        raise TypeError("evidence must be a ProviderBoundaryReconciliationEvidence")
    source_revision = source_revision or evidence.reconciliation_id
    payload = evidence.to_dict()
    payload["source_revision"] = source_revision
    normalized_payload = _normalize_json(payload)
    reference = EvidenceReference(
        source_revision=source_revision,
        fingerprint=_fingerprint(normalized_payload),
        observed_at=evidence.reconciled_at,
        summary=f"provider boundary reconciliation {evidence.reconciliation_outcome}",
        reference=evidence.reconciliation_id,
    )
    return EvidenceRecord(
        key=EvidenceCorrelationKey(
            source="provider_boundary_reconciliation",
            record_id=evidence.reconciliation_id,
            mission_id=mission_id,
            task_id=task_id,
            domain_id=domain_id,
        ),
        reference=reference,
        payload=payload,
        metadata={
            "effect_intent_id": evidence.effect_intent_id,
            "dispatch_id": evidence.dispatch_id,
            "reconciliation_outcome": evidence.reconciliation_outcome,
            "provider_scope": evidence.provider_scope,
        },
    )


def worker_attempt_record(
    attempt: WorkerExecutionAttempt,
    *,
    domain_id: str | None = None,
) -> EvidenceRecord:
    if type(attempt) is not WorkerExecutionAttempt:
        raise TypeError("attempt must be a WorkerExecutionAttempt")
    payload = asdict(attempt)
    payload["status"] = attempt.status.value
    source_revision_dt = (
        attempt.terminal_at
        or attempt.started_at
        or attempt.claimed_at
        or attempt.request.created_at
    )
    source_revision = _require_timestamp(source_revision_dt, "execution timestamp").isoformat()
    normalized_payload = _normalize_json(payload)
    fingerprint = attempt.request.execution_fingerprint or _fingerprint(normalized_payload)
    record_id = _fingerprint(
        {
            "execution_attempt_id": attempt.request.execution_attempt_id,
            "status": attempt.status.value,
            "source_revision": source_revision,
        }
    )
    reference = EvidenceReference(
        source_revision=source_revision,
        fingerprint=fingerprint,
        observed_at=_require_timestamp(source_revision_dt, "execution timestamp"),
        summary=f"worker execution {attempt.status.value}",
        reference=attempt.request.execution_attempt_id,
    )
    return EvidenceRecord(
        key=EvidenceCorrelationKey(
            source="worker_execution",
            record_id=record_id,
            mission_id=attempt.request.mission_id,
            task_id=attempt.request.task_id,
            domain_id=domain_id,
        ),
        reference=reference,
        payload=payload,
        metadata={
            "dispatch_offer_id": attempt.request.dispatch_offer_id,
            "worker_node_id": attempt.request.worker_node_id,
            "coordinator_node_id": attempt.request.coordinator_node_id,
            "status": attempt.status.value,
        },
    )


__all__ = [
    "SCHEMA_VERSION",
    "EvidenceChain",
    "EvidenceCorrelationKey",
    "EvidenceExport",
    "EvidencePointer",
    "EvidenceRecord",
    "EvidenceReference",
    "EvidenceSpine",
    "EvidenceSpineConflictError",
    "EvidenceSpineCorruptionError",
    "EvidenceSpineError",
    "ProviderBoundaryReconciliationEvidence",
    "approval_records",
    "checkpoint_record",
    "event_records",
    "provider_boundary_reconciliation_record",
    "recovery_record",
    "research_evidence_record",
    "worker_attempt_record",
]
