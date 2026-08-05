from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.ai_controller._locking import FileLock

from .proposals import SCHEMA_VERSION


class ApprovalEvidenceCorrupt(ValueError):
    pass


@dataclass(frozen=True)
class ApprovalSnapshot:
    records: tuple[dict[str, Any], ...]
    revision: str


class ApprovalLog:
    """Append-only proposal/decision/effect evidence.

    Lock order places this log after mission, queue, report, and mission-event
    evidence whenever those authorities are held together.
    """

    def __init__(self, root: Path, *, create: bool = False) -> None:
        self.root = Path(root)
        self.path = self.root / "controller-proposals.jsonl"
        self.lock_path = self.root / ".controller-proposals.lock"
        self.create = create

    @staticmethod
    def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ApprovalEvidenceCorrupt(
                    f"approval evidence contains duplicate field {key!r}"
                )
            result[key] = value
        return result

    @staticmethod
    def _require_exact_fields(
        value: dict[str, Any],
        *,
        required: set[str],
        allowed: set[str],
        context: str,
    ) -> None:
        missing = required - set(value)
        unknown = set(value) - allowed
        if missing or unknown:
            raise ApprovalEvidenceCorrupt(
                f"{context} has missing or unknown fields"
            )

    @staticmethod
    def _nonempty_string(value: Any) -> bool:
        return isinstance(value, str) and bool(value) and value == value.strip()

    @classmethod
    def _validate_proposal(cls, proposal: Any) -> None:
        if not isinstance(proposal, dict):
            raise ApprovalEvidenceCorrupt("proposal record is not an object")
        required = {
            "schema_version",
            "action_id",
            "action_type",
            "mission_id",
            "mission_task_id",
            "expected_mission_revision",
            "expected_event_revision",
            "expected_queue_identities",
            "expected_queue_revision",
            "evidence_fingerprint",
            "proposed_effect_fingerprint",
            "finding",
            "created_at",
            "expires_at",
            "status",
            "authority",
            "proposal_revision",
        }
        allowed = required | {
            "expected_budget_fingerprint",
            "expected_budget_usage",
            "expected_queue_effects",
            "expected_queue_records",
            "expected_report_revision",
            "expected_task_state_fingerprints",
            "supersedes_action_ids",
            "reason",
            "classification",
            "recovery_action",
        }
        cls._require_exact_fields(
            proposal,
            required=required,
            allowed=allowed,
            context="proposal",
        )
        string_fields = required - {
            "mission_task_id",
            "expected_queue_identities",
        }
        if (
            proposal["schema_version"] != SCHEMA_VERSION
            or any(
                not cls._nonempty_string(proposal[field])
                for field in string_fields
            )
            or (
                proposal["mission_task_id"] is not None
                and not cls._nonempty_string(proposal["mission_task_id"])
            )
            or not isinstance(proposal["expected_queue_identities"], list)
            or not all(
                cls._nonempty_string(item)
                for item in proposal["expected_queue_identities"]
            )
            or len(set(proposal["expected_queue_identities"]))
            != len(proposal["expected_queue_identities"])
            or (
                "supersedes_action_ids" in proposal
                and (
                    not isinstance(proposal["supersedes_action_ids"], list)
                    or not all(
                        cls._nonempty_string(item)
                        for item in proposal["supersedes_action_ids"]
                    )
                )
            )
        ):
            raise ApprovalEvidenceCorrupt("proposal record has invalid fields")

    @classmethod
    def _validate_decision(cls, decision: Any) -> None:
        if not isinstance(decision, dict):
            raise ApprovalEvidenceCorrupt("decision record is not an object")
        fields = {
            "schema_version",
            "action_id",
            "action_type",
            "mission_id",
            "mission_task_id",
            "proposal_revision",
            "mission_revision",
            "event_revision",
            "queue_identities",
            "evidence_fingerprint",
            "proposed_effect_fingerprint",
            "approver_identity",
            "decision",
            "decision_time",
            "idempotency_key",
            "note",
            "request_fingerprint",
            "previous_revision",
            "record_integrity",
        }
        cls._require_exact_fields(
            decision,
            required=fields,
            allowed=fields,
            context="decision",
        )
        string_fields = fields - {"mission_task_id", "queue_identities", "note"}
        if (
            decision["schema_version"] != SCHEMA_VERSION
            or decision["decision"] not in {"approve", "reject"}
            or any(
                not cls._nonempty_string(decision[field])
                for field in string_fields
            )
            or (
                decision["mission_task_id"] is not None
                and not cls._nonempty_string(decision["mission_task_id"])
            )
            or not isinstance(decision["queue_identities"], list)
            or not all(
                cls._nonempty_string(item)
                for item in decision["queue_identities"]
            )
            or len(set(decision["queue_identities"]))
            != len(decision["queue_identities"])
            or (
                decision["note"] is not None
                and not isinstance(decision["note"], str)
            )
        ):
            raise ApprovalEvidenceCorrupt("decision record has invalid fields")

    @classmethod
    def _decision_integrity(
        cls,
        decision: dict[str, Any],
    ) -> str:
        bound = {
            key: value
            for key, value in decision.items()
            if key != "record_integrity"
        }
        serialized = json.dumps(
            {"record_type": "decision", "decision": bound},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(
            decision["previous_revision"].encode("ascii")
            + b"\x00"
            + serialized
        ).hexdigest()

    @classmethod
    def bind_decision(
        cls,
        decision: dict[str, Any],
        previous_revision: str,
    ) -> dict[str, Any]:
        bound = {
            **decision,
            "previous_revision": previous_revision,
        }
        bound["record_integrity"] = cls._decision_integrity(bound)
        return bound

    @classmethod
    def _validate_application(cls, application: Any) -> None:
        if not isinstance(application, dict):
            raise ApprovalEvidenceCorrupt("application record is not an object")
        fields = {
            "schema_version",
            "action_id",
            "status",
            "applied_by",
            "applied_at",
            "proposal_revision",
            "effect_fingerprint",
        }
        cls._require_exact_fields(
            application,
            required=fields,
            allowed=fields,
            context="application",
        )
        if (
            application["schema_version"] != SCHEMA_VERSION
            or application["status"] not in {
                "APPLIED",
                "ALREADY_APPLIED",
                "FAILED",
            }
            or any(
                not cls._nonempty_string(application[field])
                for field in fields - {"schema_version", "status"}
            )
        ):
            raise ApprovalEvidenceCorrupt("application record has invalid fields")

    @classmethod
    def _validate_records(cls, records: list[dict[str, Any]]) -> None:
        decisions_by_action: dict[str, dict[str, Any]] = {}
        decisions_by_key: dict[str, dict[str, Any]] = {}
        applications: set[str] = set()
        proposals: dict[str, dict[str, Any]] = {}
        for record in records:
            if set(record) != {"record_type", record.get("record_type", "")}:
                raise ApprovalEvidenceCorrupt(
                    "approval record wrapper has invalid fields"
                )
            record_type = record.get("record_type")
            if record_type == "proposal":
                cls._validate_proposal(record["proposal"])
                action_id = record["proposal"]["action_id"]
                if action_id in proposals:
                    raise ApprovalEvidenceCorrupt("duplicate proposal record")
                proposals[action_id] = record["proposal"]
            elif record_type == "decision":
                cls._validate_decision(record["decision"])
                decision = record["decision"]
                action_id = decision["action_id"]
                key = decision["idempotency_key"]
                if action_id in decisions_by_action or key in decisions_by_key:
                    raise ApprovalEvidenceCorrupt(
                        "conflicting duplicate approval records"
                    )
                decisions_by_action[action_id] = decision
                decisions_by_key[key] = decision
            elif record_type == "application":
                cls._validate_application(record["application"])
                action_id = record["application"]["action_id"]
                if action_id in applications:
                    raise ApprovalEvidenceCorrupt(
                        "duplicate application record"
                    )
                applications.add(action_id)
            else:
                raise ApprovalEvidenceCorrupt("unknown approval record type")
        if any(action_id not in proposals for action_id in decisions_by_action):
            raise ApprovalEvidenceCorrupt(
                "decision belongs to an unknown proposal"
            )
        for action_id, decision in decisions_by_action.items():
            proposal = proposals[action_id]
            binding = {
                "schema_version": proposal["schema_version"],
                "action_id": proposal["action_id"],
                "action_type": proposal["action_type"],
                "mission_id": proposal["mission_id"],
                "mission_task_id": proposal.get("mission_task_id"),
                "proposal_revision": proposal["proposal_revision"],
                "mission_revision": proposal["expected_mission_revision"],
                "event_revision": proposal["expected_event_revision"],
                "queue_identities": proposal["expected_queue_identities"],
                "evidence_fingerprint": proposal["evidence_fingerprint"],
                "proposed_effect_fingerprint": proposal[
                    "proposed_effect_fingerprint"
                ],
            }
            if any(decision.get(key) != value for key, value in binding.items()):
                raise ApprovalEvidenceCorrupt(
                    "decision does not bind its complete proposal revision"
                )
            request = {
                "action_id": action_id,
                "decision": decision["decision"],
                "expected_proposal_revision": decision["proposal_revision"],
                "note": decision["note"],
                "principal": decision["approver_identity"],
            }
            expected_request = hashlib.sha256(
                json.dumps(
                    request,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if decision["request_fingerprint"] != expected_request:
                raise ApprovalEvidenceCorrupt(
                    "decision request binding is invalid"
                )
            if decision["record_integrity"] != cls._decision_integrity(decision):
                raise ApprovalEvidenceCorrupt(
                    "decision record integrity is invalid"
                )

    def _snapshot_unlocked(self) -> ApprovalSnapshot:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            raw = b""
        revision = hashlib.sha256(raw).hexdigest()
        if raw and not raw.endswith(b"\n"):
            raise ApprovalEvidenceCorrupt("approval evidence has an incomplete tail")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ApprovalEvidenceCorrupt(
                "approval evidence is not valid UTF-8"
            ) from exc
        records: list[dict[str, Any]] = []
        prefix = b""
        raw_lines = raw.splitlines(keepends=True)
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line:
                raise ApprovalEvidenceCorrupt(
                    f"approval evidence has an empty record at line {line_number}"
                )
            try:
                record = json.loads(
                    line,
                    object_pairs_hook=self._strict_object,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ApprovalEvidenceCorrupt(
                            f"approval evidence has non-finite JSON {value!r}"
                        )
                    ),
                )
            except (json.JSONDecodeError, ApprovalEvidenceCorrupt) as exc:
                raise ApprovalEvidenceCorrupt(
                    f"approval evidence is malformed at line {line_number}"
                ) from exc
            if not isinstance(record, dict):
                raise ApprovalEvidenceCorrupt(
                    f"approval evidence is invalid at line {line_number}"
                )
            encoded_line = raw_lines[line_number - 1]
            if encoded_line != self._line(record):
                raise ApprovalEvidenceCorrupt(
                    f"approval evidence is not canonically serialized at line {line_number}"
                )
            if record.get("record_type") == "decision":
                decision = record.get("decision")
                if (
                    not isinstance(decision, dict)
                    or decision.get("previous_revision")
                    != hashlib.sha256(prefix).hexdigest()
                ):
                    raise ApprovalEvidenceCorrupt(
                        "decision predecessor revision is invalid"
                    )
            records.append(record)
            prefix += encoded_line
        self._validate_records(records)
        return ApprovalSnapshot(tuple(records), revision)

    def snapshot(self) -> ApprovalSnapshot:
        if not self.create or not self.lock_path.exists():
            return self._snapshot_unlocked()
        with FileLock(self.lock_path):
            return self._snapshot_unlocked()

    @staticmethod
    def _line(record: dict[str, Any]) -> bytes:
        return json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8") + b"\n"

    def append(self, record: dict[str, Any]) -> ApprovalSnapshot:
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path):
            snapshot = self._snapshot_unlocked()
            self._validate_records([*snapshot.records, record])
            line = self._line(record)
            with self.path.open("ab") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            return self._snapshot_unlocked()

    def transact(self, callback):
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path):
            snapshot = self._snapshot_unlocked()
            record, result = callback(snapshot)
            if record is not None:
                self._validate_records([*snapshot.records, record])
                line = self._line(record)
                with self.path.open("ab") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            return result
