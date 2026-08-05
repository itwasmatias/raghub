from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from flask import Blueprint, g, make_response, request
from werkzeug.exceptions import MethodNotAllowed, NotFound

from tools.ai_controller._locking import FileLock
from tools.ai_controller.mission.events import (
    EventLogCorruptionError,
    MissionEventLog,
    make_event,
)
from tools.ai_controller.mission.graph import DependencyGraph
from tools.ai_controller.mission.materializer import (
    TaskMaterializer,
    make_legacy_queue_task_id,
)
from tools.ai_controller.mission.models import (
    MissionStatus,
    MissionTaskStatus,
)
from tools.ai_controller.mission.repair import (
    RepairAction,
    RepairActionKind,
    RepairApplyStatus,
    RepairClassification,
    apply_mission_repair,
    plan_mission_repairs,
)
from tools.ai_controller.mission.scheduler import MissionScheduler
from tools.ai_controller.mission.store import (
    MissionStateConflictError,
    MissionStore,
)
from tools.ai_controller.queue import DurableQueue
from tools.ai_controller.reports import report_lock_path

from .approvals import ApprovalEvidenceCorrupt, ApprovalLog
from .auth import (
    Principal,
    authenticate,
    load_tokens_from_environment,
    validate_tokens,
)
from .errors import APIError, invalid
from .proposals import (
    SCHEMA_VERSION,
    fingerprint,
    lifecycle_action_id,
    parse_time,
    proposal_revision,
)
from .serialization import public_definition, public_state, public_value


API_PREFIX = "/api/controller/v1"
MAX_LIMIT = 100
MAX_REASON = 500
MAX_NOTE = 1000
MAX_BODY = 4096
QUEUE_STATES = ("pending", "running", "succeeded", "failed", "invalid")
CURSOR_SCHEMA = "controller-cursor-v1"
_PREFLIGHT_HEADERS = frozenset(
    {"authorization", "content-type", "x-request-id"}
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON value {value!r} is not allowed")


def _normalize_origin(origin: str) -> str:
    if (
        not isinstance(origin, str)
        or not origin
        or origin != origin.strip()
        or origin == "null"
        or "," in origin
    ):
        raise ValueError("CORS origin must be one exact HTTP(S) origin")
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.hostname.endswith(".")
    ):
        raise ValueError("CORS origin is malformed")
    try:
        port = parsed.port
        parsed.hostname.encode("ascii")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("CORS origin host or port is malformed") from exc
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    normalized = urlunsplit((parsed.scheme, netloc, "", "", ""))
    if normalized != origin:
        raise ValueError("CORS origin must already be normalized")
    return normalized


class ControllerOperations:
    def __init__(
        self,
        *,
        missions_root: Path,
        queue_root: Path,
        reports_root: Path,
        approvals_root: Path,
        tokens: dict[str, dict[str, Any]] | None = None,
        proposal_ttl_seconds: int = 900,
        now: Callable[[], datetime] = _utc_now,
        allowed_origins: tuple[str, ...] = (),
    ) -> None:
        self.missions_root = Path(missions_root)
        self.queue_root = Path(queue_root)
        self.reports_root = Path(reports_root)
        self.approvals_root = Path(approvals_root)
        self.controller_root = self.missions_root.parent
        self.tokens = validate_tokens(
            load_tokens_from_environment() if tokens is None else tokens
        )
        self.proposal_ttl_seconds = max(30, min(int(proposal_ttl_seconds), 3600))
        self.now = now
        self.allowed_origins = tuple(
            _normalize_origin(origin) for origin in allowed_origins
        )
        self.store = MissionStore(self.missions_root, create=False)
        self.queue = DurableQueue(self.queue_root, create=False)
        self.approvals = ApprovalLog(self.approvals_root, create=False)
        self.scheduler = MissionScheduler(
            self.store,
            self.queue,
            self.reports_root,
            self.missions_root,
        )

    @property
    def approval_log_path(self) -> Path:
        return self.approvals.path

    def restart(self) -> "ControllerOperations":
        return ControllerOperations(
            missions_root=self.missions_root,
            queue_root=self.queue_root,
            reports_root=self.reports_root,
            approvals_root=self.approvals_root,
            tokens=self.tokens,
            proposal_ttl_seconds=self.proposal_ttl_seconds,
            now=self.now,
            allowed_origins=self.allowed_origins,
        )

    def controller_state_available(self) -> bool:
        return all(
            path.is_dir()
            for path in (
                self.missions_root,
                self.queue_root,
                self.reports_root,
            )
        )

    def require_controller_state(self) -> None:
        if not self.controller_state_available():
            raise APIError(
                "CONTROLLER_UNAVAILABLE",
                "Authoritative controller state is unavailable.",
                503,
                retryable=True,
            )

    def _event_snapshot(self, mission_id: str):
        return MissionEventLog(
            self.missions_root / "events", mission_id
        ).read_snapshot()

    def _mission_snapshot(self, mission_id: str) -> tuple[Any, Any, str]:
        found = self.store.find_mission(mission_id)
        if found is None:
            raise APIError("NOT_FOUND", "Mission was not found.", 404)
        _, path = found
        try:
            raw = path.read_bytes()
            envelope = json.loads(raw)
            definition = self.store.load_definition(mission_id)
            state = self.store.load_state(mission_id)
        except MissionStateConflictError:
            raise
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise APIError(
                "CORRUPT_EVIDENCE",
                "Mission evidence is malformed.",
                409,
            ) from exc
        return definition, state, _sha(raw)

    def _queue_snapshot(self) -> tuple[list[dict[str, Any]], str]:
        records: list[dict[str, Any]] = []
        revision_input: list[dict[str, str]] = []
        for state in QUEUE_STATES:
            directory = self.queue_root / state
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.json")):
                try:
                    raw = path.read_bytes()
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        raise TypeError("queue record is not an object")
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    raise APIError(
                        "CORRUPT_EVIDENCE",
                        "Queue evidence is malformed.",
                        409,
                    ) from exc
                records.append(
                    {
                        "queue_id": path.stem,
                        "status": state,
                        "record": public_value(payload),
                    }
                )
                revision_input.append(
                    {"queue_id": path.stem, "status": state, "sha256": _sha(raw)}
                )
        records.sort(key=lambda item: (item["queue_id"], item["status"]))
        return records, fingerprint(revision_input)

    def _queue_record_evidence(self) -> list[dict[str, str]]:
        evidence: list[dict[str, str]] = []
        for state in QUEUE_STATES:
            directory = self.queue_root / state
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.json")):
                raw = path.read_bytes()
                evidence.append(
                    {
                        "queue_id": path.stem,
                        "status": state,
                        "sha256": _sha(raw),
                    }
                )
        return evidence

    def _report_revision(self) -> str:
        evidence: list[dict[str, str]] = []
        if self.reports_root.exists():
            for path in sorted(self.reports_root.glob("*.json")):
                evidence.append(
                    {
                        "name": path.name,
                        "sha256": _sha(path.read_bytes()),
                    }
                )
        return fingerprint(evidence)

    def _queue_bindings(
        self, mission_id: str, task_id: str | None, state: Any
    ) -> tuple[list[str], str]:
        records, queue_revision = self._queue_snapshot()
        if task_id is None:
            identities = sorted(
                {
                    item["queue_id"]
                    for item in records
                    if item.get("record", {}).get("metadata", {}).get("mission_id")
                    == mission_id
                }
            )
        else:
            materializer = TaskMaterializer(self.queue, self.reports_root)
            current = materializer.make_queue_task_id(mission_id, task_id)
            legacy = make_legacy_queue_task_id(mission_id, task_id)
            recorded = state.task_states.get(task_id)
            identities = sorted(
                {
                    current,
                    legacy,
                    *(
                        [recorded.queue_task_id]
                        if recorded is not None and recorded.queue_task_id
                        else []
                    ),
                }
            )
        relevant = [
            item
            for item in records
            if item["queue_id"] in identities
            or (
                task_id is None
                and item.get("record", {}).get("metadata", {}).get("mission_id")
                == mission_id
            )
        ]
        return identities, fingerprint(
            {"queue_revision": queue_revision, "records": relevant}
        )

    def _records(self) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        snapshot = self.approvals.snapshot()
        proposals: dict[str, dict[str, Any]] = {}
        later: list[dict[str, Any]] = []
        for record in snapshot.records:
            if record["record_type"] == "proposal":
                stored = dict(record["proposal"])
                proposals[stored["action_id"]] = stored
                later.extend(
                    {
                        "record_type": "supersession",
                        "action_id": superseded,
                        "superseded_by": stored["action_id"],
                    }
                    for superseded in stored.get("supersedes_action_ids", [])
                )
            else:
                later.append(dict(record))
        return proposals, later

    def _current_bindings(self, proposal: dict[str, Any]) -> dict[str, Any]:
        definition, state, mission_revision = self._mission_snapshot(
            proposal["mission_id"]
        )
        event_revision = self._event_snapshot(proposal["mission_id"]).revision
        queue_ids, queue_revision = self._queue_bindings(
            proposal["mission_id"], proposal.get("mission_task_id"), state
        )
        return {
            "definition": definition,
            "state": state,
            "mission_revision": mission_revision,
            "event_revision": event_revision,
            "queue_identities": queue_ids,
            "queue_revision": queue_revision,
            "report_revision": self._report_revision(),
        }

    def _is_stale(self, proposal: dict[str, Any]) -> bool:
        if proposal.get("recovery_action"):
            try:
                current_actions = plan_mission_repairs(
                    proposal["mission_id"],
                    self.store,
                    self.queue,
                    self.reports_root,
                )
                state = self.store.load_state(proposal["mission_id"])
                _, queue_revision = self._queue_bindings(
                    proposal["mission_id"],
                    proposal.get("mission_task_id"),
                    state,
                )
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                EventLogCorruptionError,
                MissionStateConflictError,
            ):
                return True
            return (
                queue_revision != proposal["expected_queue_revision"]
                or not any(
                    item.action_id == proposal["action_id"]
                    for item in current_actions
                )
            )
        try:
            current = self._current_bindings(proposal)
        except (APIError, EventLogCorruptionError, MissionStateConflictError):
            return True
        return any(
            (
                current["mission_revision"] != proposal["expected_mission_revision"],
                current["event_revision"] != proposal["expected_event_revision"],
                current["queue_revision"] != proposal["expected_queue_revision"],
                current["queue_identities"] != proposal["expected_queue_identities"],
                current["report_revision"]
                != proposal.get("expected_report_revision"),
            )
        )

    def _public_proposal(
        self,
        proposal: dict[str, Any],
        records: list[dict[str, Any]],
        *,
        evaluate_stale: bool = True,
    ) -> dict[str, Any]:
        action_id = proposal["action_id"]
        decisions = [
            record["decision"]
            for record in records
            if record["record_type"] == "decision"
            and record["decision"]["action_id"] == action_id
        ]
        applications = [
            record["application"]
            for record in records
            if record["record_type"] == "application"
            and record["application"]["action_id"] == action_id
        ]
        status = "pending"
        if decisions:
            status = (
                "approved"
                if decisions[-1]["decision"] == "approve"
                else "rejected"
            )
        if applications:
            status = (
                "applied"
                if applications[-1]["status"]
                in {"APPLIED", "ALREADY_APPLIED"}
                else applications[-1]["status"].lower()
            )
        if any(
            record["record_type"] == "supersession"
            and record["action_id"] == action_id
            for record in records
        ):
            status = "superseded"
        if status in {"pending", "approved"}:
            if self.now() >= parse_time(proposal["expires_at"]):
                status = "expired"
            elif evaluate_stale and self._is_stale(proposal):
                status = "stale"
        result = dict(proposal)
        result["status"] = status
        result["decision"] = decisions[-1] if decisions else None
        result["application"] = applications[-1] if applications else None
        result.pop("recovery_action", None)
        return public_value(result)

    def get_proposal(self, action_id: str) -> dict[str, Any]:
        try:
            proposals, records = self._records()
        except ApprovalEvidenceCorrupt as exc:
            raise APIError(
                "CORRUPT_EVIDENCE", "Approval evidence is corrupt.", 409
            ) from exc
        proposal = proposals.get(action_id)
        if proposal is None:
            raise APIError("NOT_FOUND", "Proposal was not found.", 404)
        return self._public_proposal(proposal, records)

    def _validate_lifecycle(
        self, action_type: str, definition: Any, state: Any, task_id: str | None
    ) -> None:
        allowed = {
            "MISSION_START": {MissionStatus.pending},
            "MISSION_PAUSE": {MissionStatus.running},
            "MISSION_RESUME": {MissionStatus.paused},
            "MISSION_CANCEL": {
                MissionStatus.pending,
                MissionStatus.running,
                MissionStatus.paused,
            },
        }
        if action_type in allowed and state.status not in allowed[action_type]:
            raise APIError(
                "INVALID_TRANSITION",
                "The requested mission transition is not currently allowed.",
                409,
            )
        if action_type == "TASK_RETRY":
            if state.status is not MissionStatus.running:
                raise APIError(
                    "INVALID_TRANSITION",
                    "The mission does not permit continued execution.",
                    409,
                )
            task_def = next(
                (item for item in definition.tasks if item.task_id == task_id), None
            )
            task_state = state.task_states.get(task_id or "")
            if task_def is None or task_state is None:
                raise APIError("NOT_FOUND", "Mission task was not found.", 404)
            if task_state.status is not MissionTaskStatus.failed:
                raise APIError(
                    "INVALID_TRANSITION",
                    "The mission task is not retryable.",
                    409,
                )
            if (
                task_state.attempt_count >= task_def.max_attempts
                or state.budget_usage.total_attempts
                >= definition.budgets.max_total_attempts
            ):
                raise APIError(
                    "BUDGET_EXHAUSTED",
                    "The retry budget is exhausted.",
                    409,
                )
            events = list(self._event_snapshot(definition.mission_id).events)
            for dependency in task_def.depends_on:
                dependency_state = state.task_states.get(dependency)
                if dependency_state is None or not any(
                    self.scheduler._is_exact_prerequisite_success(
                        event,
                        definition.mission_id,
                        dependency,
                        dependency_state,
                    )
                    for event in events
                ):
                    raise APIError(
                        "INVALID_TRANSITION",
                        "A retry prerequisite lacks exact durable success evidence.",
                        409,
                    )
            materializer = TaskMaterializer(self.queue, self.reports_root)
            accepted_ids = {
                materializer.make_queue_task_id(
                    definition.mission_id, task_id or ""
                ),
                make_legacy_queue_task_id(
                    definition.mission_id, task_id or ""
                ),
                *(
                    [task_state.queue_task_id]
                    if task_state.queue_task_id
                    else []
                ),
            }
            if any(
                (directory / f"{queue_id}.json").exists()
                for queue_id in accepted_ids
                for directory in (self.queue.pending, self.queue.running)
            ):
                raise APIError(
                    "INVALID_TRANSITION",
                    "The mission task already has active queue work.",
                    409,
                )
            ownership_error = self.scheduler.retry_queue_ownership_error(
                definition.mission_id, task_def, task_state
            )
            if ownership_error is not None:
                raise APIError(
                    "HUMAN_REVIEW_REQUIRED",
                    "Retry queue evidence is not exclusively controller-owned.",
                    409,
                )

    def _build_lifecycle_proposal(
        self,
        mission_id: str,
        action_type: str,
        *,
        task_id: str | None,
        finding: str,
        authority: str,
        created_at: str,
        expires_at: str,
    ) -> dict[str, Any]:
        definition, state, mission_revision = self._mission_snapshot(mission_id)
        self._validate_lifecycle(action_type, definition, state, task_id)
        event_snapshot = self._event_snapshot(mission_id)
        event_revision = event_snapshot.revision
        queue_ids, queue_revision = self._queue_bindings(
            mission_id, task_id, state
        )
        evidence = {
            "action_type": action_type,
            "mission_id": mission_id,
            "mission_task_id": task_id,
            "mission_revision": mission_revision,
            "event_revision": event_revision,
            "queue_identities": queue_ids,
            "queue_revision": queue_revision,
            "task_state": (
                state.task_states[task_id].to_dict() if task_id else None
            ),
            "budget_usage": state.budget_usage.to_dict(),
            "report_revision": self._report_revision(),
        }
        expected_queue_effects: list[dict[str, str]] = []
        if action_type in {"MISSION_START", "MISSION_RESUME"}:
            for ready_task_id in self.scheduler._compute_ready_tasks(
                definition, state, list(event_snapshot.events)
            ):
                expected_queue_effects.append(
                    {
                        "task_id": ready_task_id,
                        "queue_id": TaskMaterializer(
                            self.queue, self.reports_root
                        ).make_queue_task_id(mission_id, ready_task_id),
                    }
                )
        elif action_type == "TASK_RETRY" and task_id is not None:
            task_state = state.task_states[task_id]
            expected_queue_effects.append(
                {
                    "task_id": task_id,
                    "queue_id": (
                        task_state.queue_task_id
                        or TaskMaterializer(
                            self.queue, self.reports_root
                        ).make_queue_task_id(mission_id, task_id)
                    ),
                }
            )
        effect = {
            "action_type": action_type,
            "mission_id": mission_id,
            "mission_task_id": task_id,
            "target_status": {
                "MISSION_START": "running",
                "MISSION_PAUSE": "paused",
                "MISSION_RESUME": "running",
                "MISSION_CANCEL": "cancelled",
                "TASK_RETRY": "queued",
            }[action_type],
            "queue_identities": queue_ids,
            "queue_effects": expected_queue_effects,
        }
        binding = {
            **evidence,
            "evidence_fingerprint": fingerprint(evidence),
            "proposed_effect_fingerprint": fingerprint(effect),
            "authority": authority,
            "finding": finding,
            "created_at": created_at,
            "expires_at": expires_at,
        }
        proposal = {
            "schema_version": SCHEMA_VERSION,
            "action_id": lifecycle_action_id(binding),
            "action_type": action_type,
            "mission_id": mission_id,
            "mission_task_id": task_id,
            "expected_mission_revision": mission_revision,
            "expected_event_revision": event_revision,
            "expected_queue_identities": queue_ids,
            "expected_queue_revision": queue_revision,
            "expected_report_revision": evidence["report_revision"],
            "expected_queue_records": self._queue_record_evidence(),
            "expected_queue_effects": expected_queue_effects,
            "expected_budget_fingerprint": fingerprint(
                state.budget_usage.to_dict()
            ),
            "expected_budget_usage": state.budget_usage.to_dict(),
            "expected_task_state_fingerprints": {
                item_id: fingerprint(item_state.to_dict())
                for item_id, item_state in sorted(state.task_states.items())
            },
            "evidence_fingerprint": binding["evidence_fingerprint"],
            "proposed_effect_fingerprint": binding[
                "proposed_effect_fingerprint"
            ],
            "finding": finding,
            "created_at": created_at,
            "expires_at": expires_at,
            "status": "pending",
            "authority": authority,
        }
        proposal["proposal_revision"] = proposal_revision(proposal)
        return proposal

    def _verify_lifecycle_proposal(self, proposal: dict[str, Any]) -> None:
        """Recompute every immutable lifecycle field from current authority."""
        try:
            expected = self._build_lifecycle_proposal(
                proposal["mission_id"],
                proposal["action_type"],
                task_id=proposal.get("mission_task_id"),
                finding=proposal["finding"],
                authority=proposal["authority"],
                created_at=proposal["created_at"],
                expires_at=proposal["expires_at"],
            )
        except (
            APIError,
            EventLogCorruptionError,
            MissionStateConflictError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise APIError(
                "STALE_PROPOSAL",
                "Proposal cannot be recomputed from authoritative evidence.",
                409,
            ) from exc
        fields = set(expected) - {"status", "supersedes_action_ids"}
        source_fields = {
            "expected_mission_revision",
            "expected_event_revision",
            "expected_queue_identities",
            "expected_queue_revision",
            "expected_report_revision",
            "expected_queue_records",
            "expected_queue_effects",
            "expected_budget_fingerprint",
            "expected_budget_usage",
            "expected_task_state_fingerprints",
            "evidence_fingerprint",
            "proposed_effect_fingerprint",
        }
        if any(
            proposal.get(field) != expected[field] for field in source_fields
        ):
            raise APIError(
                "STALE_PROPOSAL",
                "Proposal evidence is stale.",
                409,
            )
        if any(proposal.get(field) != expected[field] for field in fields):
            raise APIError(
                "HUMAN_REVIEW_REQUIRED",
                "Proposal does not exactly bind authoritative evidence.",
                409,
            )

    def create_lifecycle_proposal(
        self,
        mission_id: str,
        action_type: str,
        *,
        task_id: str | None,
        reason: str | None,
        authority: str,
    ) -> tuple[dict[str, Any], bool]:
        try:
            created = self.now()
            proposal = self._build_lifecycle_proposal(
                mission_id,
                action_type,
                task_id=task_id,
                finding=reason or "Controller lifecycle action requested.",
                authority=authority,
                created_at=_iso(created),
                expires_at=_iso(
                    created + timedelta(seconds=self.proposal_ttl_seconds)
                ),
            )
        except MissionStateConflictError as exc:
            raise APIError(
                "CONFLICT", "Mission state has conflicting envelopes.", 409
            ) from exc
        except EventLogCorruptionError as exc:
            raise APIError(
                "CORRUPT_EVIDENCE", "Mission event evidence is corrupt.", 409
            ) from exc

        def write(snapshot):
            proposals = {
                item["proposal"]["action_id"]: item["proposal"]
                for item in snapshot.records
                if item["record_type"] == "proposal"
            }
            if proposal["action_id"] in proposals:
                return None, (dict(proposals[proposal["action_id"]]), False)
            decided = {
                item["decision"]["action_id"]
                for item in snapshot.records
                if item["record_type"] == "decision"
            }
            applied = {
                item["application"]["action_id"]
                for item in snapshot.records
                if item["record_type"] == "application"
            }
            stored = {
                **proposal,
                "supersedes_action_ids": sorted(
                    existing_id
                    for existing_id, existing in proposals.items()
                    if existing_id not in decided
                    and existing_id not in applied
                    and existing["mission_id"] == mission_id
                ),
            }
            return (
                {"record_type": "proposal", "proposal": stored},
                (stored, True),
            )

        try:
            return self.approvals.transact(write)
        except ApprovalEvidenceCorrupt as exc:
            raise APIError(
                "CORRUPT_EVIDENCE", "Approval evidence is corrupt.", 409
            ) from exc

    def recovery_proposals(self) -> list[dict[str, Any]]:
        generated: list[dict[str, Any]] = []
        for mission_id in self.store.list_missions():
            for action in plan_mission_repairs(
                mission_id, self.store, self.queue, self.reports_root
            ):
                evidence_revision = next(
                    (
                        item.split("=", 1)[1]
                        for item in action.evidence
                        if item.startswith("event_revision=")
                    ),
                    self._event_snapshot(mission_id).revision,
                )
                state = self.store.load_state(mission_id)
                queue_ids, queue_revision = self._queue_bindings(
                    mission_id, action.task_id, state
                )
                evidence_fingerprint = fingerprint(action.to_dict())
                proposal = {
                    "schema_version": SCHEMA_VERSION,
                    "action_id": action.action_id,
                    "action_type": action.proposed_action.value,
                    "mission_id": action.mission_id,
                    "mission_task_id": action.task_id,
                    "expected_mission_revision": action.revision,
                    "expected_event_revision": evidence_revision,
                    "expected_queue_identities": queue_ids,
                    "expected_queue_revision": queue_revision,
                    "evidence_fingerprint": evidence_fingerprint,
                    "proposed_effect_fingerprint": fingerprint(
                        {
                            "action": action.proposed_action.value,
                            "queue_id": action.expected_queue_id,
                        }
                    ),
                    "finding": action.finding,
                    "reason": action.reason,
                    "classification": action.classification.value,
                    "created_at": action.created_at,
                    "expires_at": _iso(
                        self.now()
                        + timedelta(seconds=self.proposal_ttl_seconds)
                    ),
                    "status": "pending",
                    "authority": "MissionSafeRecoveryPlanner",
                    "recovery_action": action.to_dict(),
                }
                proposal["proposal_revision"] = proposal_revision(proposal)
                generated.append(proposal)
        proposals, records = self._records()
        return [
            self._public_proposal(
                proposals.get(item["action_id"], item),
                records,
            )
            for item in generated
        ]

    def persist_recovery_proposal(
        self, action_id: str
    ) -> tuple[dict[str, Any], bool]:
        generated = {
            item["action_id"]: item
            for item in self.recovery_proposals()
        }
        public = generated.get(action_id)
        if public is None:
            raise APIError(
                "NOT_FOUND", "Recovery proposal was not found.", 404
            )
        raw_generated: dict[str, Any] | None = None
        for mission_id in self.store.list_missions():
            for action in plan_mission_repairs(
                mission_id, self.store, self.queue, self.reports_root
            ):
                if action.action_id != action_id:
                    continue
                state = self.store.load_state(mission_id)
                queue_ids, queue_revision = self._queue_bindings(
                    mission_id, action.task_id, state
                )
                evidence_revision = next(
                    (
                        item.split("=", 1)[1]
                        for item in action.evidence
                        if item.startswith("event_revision=")
                    ),
                    self._event_snapshot(mission_id).revision,
                )
                raw_generated = {
                    **public,
                    "expected_event_revision": evidence_revision,
                    "expected_queue_identities": queue_ids,
                    "expected_queue_revision": queue_revision,
                    "recovery_action": action.to_dict(),
                }
                raw_generated.pop("decision", None)
                raw_generated.pop("application", None)
                raw_generated["proposal_revision"] = proposal_revision(
                    raw_generated
                )
                break
            if raw_generated is not None:
                break
        if raw_generated is None:
            raise APIError(
                "STALE_PROPOSAL", "Recovery evidence became stale.", 409
            )

        def write(snapshot):
            existing = next(
                (
                    item["proposal"]
                    for item in snapshot.records
                    if item["record_type"] == "proposal"
                    and item["proposal"]["action_id"] == action_id
                ),
                None,
            )
            if existing is not None:
                return None, (dict(existing), False)
            return (
                {"record_type": "proposal", "proposal": raw_generated},
                (raw_generated, True),
            )

        return self.approvals.transact(write)

    def _verify_recovery_proposal(
        self, proposal: dict[str, Any], *, require_current_action: bool
    ) -> RepairAction:
        raw = proposal.get("recovery_action")
        if not isinstance(raw, dict):
            raise APIError(
                "HUMAN_REVIEW_REQUIRED",
                "Recovery proposal lacks its authoritative repair action.",
                409,
            )
        try:
            action = RepairAction(
                action_id=raw["action_id"],
                mission_id=raw["mission_id"],
                task_id=raw["task_id"],
                finding=raw["finding"],
                classification=RepairClassification(raw["classification"]),
                proposed_action=RepairActionKind(raw["proposed_action"]),
                reason=raw["reason"],
                revision=raw["revision"],
                expected_queue_id=raw["expected_queue_id"],
                evidence=tuple(raw["evidence"]),
                created_at=raw["created_at"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise APIError(
                "HUMAN_REVIEW_REQUIRED",
                "Recovery proposal action is malformed.",
                409,
            ) from exc
        canonical_action_id = "repair-" + fingerprint(
            {
                "version": "mission-safe-repair-v0.1",
                "mission_id": action.mission_id,
                "task_id": action.task_id,
                "finding": action.finding,
                "classification": action.classification.value,
                "proposed_action": action.proposed_action.value,
                "reason": action.reason,
                "revision": action.revision,
                "expected_queue_id": action.expected_queue_id,
                "evidence": list(action.evidence),
            }
        )[:24]
        if (
            action.to_dict() != raw
            or action.action_id != canonical_action_id
            or proposal["action_id"] != action.action_id
            or proposal["mission_id"] != action.mission_id
            or proposal.get("mission_task_id") != action.task_id
            or proposal["action_type"] != action.proposed_action.value
            or proposal["authority"] != "MissionSafeRecoveryPlanner"
            or proposal["evidence_fingerprint"] != fingerprint(action.to_dict())
            or proposal["expected_mission_revision"] != action.revision
            or proposal["proposed_effect_fingerprint"]
            != fingerprint(
                {
                    "action": action.proposed_action.value,
                    "queue_id": action.expected_queue_id,
                }
            )
            or proposal["proposal_revision"] != proposal_revision(proposal)
        ):
            raise APIError(
                "HUMAN_REVIEW_REQUIRED",
                "Recovery proposal does not exactly bind its repair authority.",
                409,
            )
        try:
            current = {
                item.action_id: item
                for item in plan_mission_repairs(
                    action.mission_id,
                    self.store,
                    self.queue,
                    self.reports_root,
                )
            }.get(action.action_id)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise APIError(
                "HUMAN_REVIEW_REQUIRED",
                "Recovery authority cannot be reconstructed.",
                409,
            ) from exc
        if current is not None and current.to_dict() != raw:
            raise APIError(
                "HUMAN_REVIEW_REQUIRED",
                "Recovery proposal differs from current planner authority.",
                409,
            )
        if require_current_action and current is None:
            raise APIError(
                "STALE_PROPOSAL",
                "Recovery repair evidence became stale.",
                409,
            )
        return action

    def decide(
        self,
        action_id: str,
        decision: str,
        body: dict[str, Any],
        principal: Principal,
    ) -> dict[str, Any]:
        expected = body["expected_proposal_revision"]
        key = body["idempotency_key"]
        note = body.get("note")

        def write(snapshot):
            proposals = {
                item["proposal"]["action_id"]: item["proposal"]
                for item in snapshot.records
                if item["record_type"] == "proposal"
            }
            proposal = proposals.get(action_id)
            if proposal is None:
                raise APIError("NOT_FOUND", "Proposal was not found.", 404)
            if not proposal.get("recovery_action"):
                self._verify_lifecycle_proposal(proposal)
            else:
                self._verify_recovery_proposal(
                    proposal, require_current_action=True
                )
            if proposal["proposal_revision"] != expected:
                raise APIError(
                    "CONFLICT",
                    "Proposal revision does not match.",
                    409,
                    current_revision=proposal["proposal_revision"],
                )
            request_binding = fingerprint(
                {
                    "action_id": action_id,
                    "decision": decision,
                    "expected_proposal_revision": expected,
                    "note": note,
                    "principal": principal.identity,
                }
            )
            for item in snapshot.records:
                if item["record_type"] != "decision":
                    continue
                existing = item["decision"]
                if existing["idempotency_key"] == key:
                    if existing["request_fingerprint"] == request_binding:
                        replay_proposal = self._public_proposal(
                            proposal,
                            list(snapshot.records),
                            evaluate_stale=False,
                        )
                        return None, {
                            "proposal": replay_proposal,
                            "decision": existing,
                        }
                    raise APIError(
                        "CONFLICT",
                        "Idempotency key was already used for another decision.",
                        409,
                    )
                if existing["action_id"] == action_id:
                    raise APIError(
                        "ALREADY_DECIDED",
                        "Proposal already has an authoritative decision.",
                        409,
                    )
            public = self._public_proposal(
                proposal, list(snapshot.records), evaluate_stale=True
            )
            if public["status"] == "expired":
                raise APIError(
                    "EXPIRED_PROPOSAL", "Proposal has expired.", 409
                )
            if public["status"] == "stale":
                raise APIError(
                    "STALE_PROPOSAL", "Proposal evidence is stale.", 409
                )
            if public["status"] == "superseded":
                raise APIError(
                    "CONFLICT",
                    "Proposal was superseded by a conflicting active proposal.",
                    409,
                )
            record = {
                "schema_version": SCHEMA_VERSION,
                "action_id": action_id,
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
                "approver_identity": principal.identity,
                "decision": decision,
                "decision_time": _iso(self.now()),
                "idempotency_key": key,
                "note": note,
                "request_fingerprint": request_binding,
            }
            record = self.approvals.bind_decision(record, snapshot.revision)
            public["status"] = (
                "approved" if decision == "approve" else "rejected"
            )
            public["decision"] = public_value(record)
            return (
                {"record_type": "decision", "decision": record},
                {
                    "proposal": public,
                    "decision": record,
                },
            )

        try:
            return self.approvals.transact(write)
        except ApprovalEvidenceCorrupt as exc:
            raise APIError(
                "CORRUPT_EVIDENCE", "Approval evidence is corrupt.", 409
            ) from exc

    def _exact_effect(
        self,
        proposal: dict[str, Any],
        events: list[Any] | None = None,
    ) -> bool:
        try:
            state = self.store.load_state(proposal["mission_id"])
            if events is None:
                events = list(
                    self._event_snapshot(proposal["mission_id"]).events
                )
        except (FileNotFoundError, ValueError):
            return False
        action = proposal["action_type"]
        lifecycle_event = {
            "MISSION_START": "mission_started",
            "MISSION_PAUSE": "mission_paused",
            "MISSION_RESUME": "mission_resumed",
            "MISSION_CANCEL": "mission_cancelled",
            "TASK_RETRY": "task_retry_queued",
        }.get(action)
        if lifecycle_event is None:
            return False
        intent_indexes = [
            index
            for index, event in enumerate(events)
            if event.event_type == "controller_action_started"
            and event.metadata.get("action_id") == proposal["action_id"]
            and event.metadata.get("proposal_revision")
            == proposal["proposal_revision"]
            and event.metadata.get("effect_fingerprint")
            == proposal["proposed_effect_fingerprint"]
        ]
        if len(intent_indexes) != 1:
            return False
        start_index = intent_indexes[0] + 1
        completion_indexes = [
            index
            for index, event in enumerate(events[start_index:], start=start_index)
            if event.event_type == "controller_action_applied"
            and event.metadata.get("action_id") == proposal["action_id"]
        ]
        end_index = completion_indexes[0] if completion_indexes else len(events)
        matching_events = [
            event
            for event in events[start_index:end_index]
            if event.event_type == lifecycle_event
            and event.mission_id == proposal["mission_id"]
            and (
                action != "TASK_RETRY"
                or event.task_id == proposal.get("mission_task_id")
            )
        ]
        if len(matching_events) != 1:
            return False
        effects = proposal.get("expected_queue_effects", [])
        if action == "MISSION_START":
            expected_status = MissionStatus.running
        if action == "MISSION_PAUSE":
            expected_status = MissionStatus.paused
        if action == "MISSION_RESUME":
            expected_status = MissionStatus.running
        if action == "MISSION_CANCEL":
            expected_status = MissionStatus.cancelled
        if action == "TASK_RETRY":
            task = state.task_states.get(proposal.get("mission_task_id") or "")
            if (
                task is None
                or task.status is not MissionTaskStatus.queued
                or len(effects) != 1
                or task.queue_task_id != effects[0]["queue_id"]
                or not self._retry_effect_payload_matches(
                    proposal, effects[0]["queue_id"]
                )
            ):
                return False
            expected_status = MissionStatus.running
        if state.status is not expected_status:
            return False
        if action in {"MISSION_PAUSE", "MISSION_CANCEL"}:
            return (
                self._queue_record_evidence()
                == proposal.get("expected_queue_records", [])
            )
        for effect in effects:
            task = state.task_states.get(effect["task_id"])
            queue_id = effect["queue_id"]
            if (
                task is None
                or task.queue_task_id != queue_id
                or task.status
                not in {MissionTaskStatus.queued, MissionTaskStatus.running}
            ):
                return False
            active_locations = [
                directory
                for directory in (self.queue.pending, self.queue.running)
                if (directory / f"{queue_id}.json").exists()
            ]
            if len(active_locations) != 1:
                return False
            if not any(
                event.event_type
                in {"task_enqueued", "task_retry_queued"}
                and event.task_id == effect["task_id"]
                and event.queue_task_id == queue_id
                for event in events
            ):
                return False
        return self._queue_recovery_matches(proposal)

    def _retry_effect_payload_matches(
        self, proposal: dict[str, Any], queue_id: str
    ) -> bool:
        try:
            definition = self.store.load_definition(proposal["mission_id"])
            state = self.store.load_state(proposal["mission_id"])
            task_def = next(
                task
                for task in definition.tasks
                if task.task_id == proposal["mission_task_id"]
            )
            task_state = state.task_states[proposal["mission_task_id"]]
            raw = (self.queue.pending / f"{queue_id}.json").read_text(
                encoding="utf-8"
            )
            payload = json.loads(raw)
        except (
            FileNotFoundError,
            OSError,
            ValueError,
            KeyError,
            StopIteration,
        ):
            return False
        return payload == self.scheduler._retry_payload(
            proposal["mission_id"],
            task_def,
            queue_id,
            attempt_number=task_state.attempt_count + 1,
        )

    def _queue_effect_payload_matches(
        self,
        proposal: dict[str, Any],
        task_id: str,
        queue_id: str,
        *,
        allow_previous_retry: bool = False,
    ) -> bool:
        locations = [
            directory / f"{queue_id}.json"
            for directory in (self.queue.pending, self.queue.running)
            if (directory / f"{queue_id}.json").exists()
        ]
        if len(locations) != 1:
            return False
        try:
            definition = self.store.load_definition(proposal["mission_id"])
            state = self.store.load_state(proposal["mission_id"])
            task_def = next(
                item for item in definition.tasks if item.task_id == task_id
            )
            payload = json.loads(locations[0].read_text(encoding="utf-8"))
            if proposal["action_type"] == "TASK_RETRY":
                task_state = state.task_states[task_id]
                expected = self.scheduler._retry_payload(
                    proposal["mission_id"],
                    task_def,
                    queue_id,
                    attempt_number=task_state.attempt_count + 1,
                )
                previous = self.scheduler._retry_payload(
                    proposal["mission_id"],
                    task_def,
                    queue_id,
                    attempt_number=task_state.attempt_count,
                )
                return payload == expected or (
                    allow_previous_retry and payload == previous
                )
            expected = TaskMaterializer(
                self.queue, self.reports_root
            )
            expected_id = expected.make_queue_task_id(
                proposal["mission_id"], task_id
            )
            legacy_id = make_legacy_queue_task_id(
                proposal["mission_id"], task_id
            )
            if queue_id not in {expected_id, legacy_id}:
                return False
            from tools.ai_controller.models import Task

            return payload == Task(
                id=queue_id,
                title=task_def.title,
                prompt=task_def.prompt,
                base_ref=task_def.base_ref,
                tests=list(task_def.tests),
                max_attempts=task_def.max_attempts,
                metadata={
                    **task_def.metadata,
                    "mission_id": proposal["mission_id"],
                    "mission_task_id": task_id,
                    "depends_on": list(task_def.depends_on),
                },
            ).to_dict()
        except (
            FileNotFoundError,
            OSError,
            ValueError,
            KeyError,
            StopIteration,
            TypeError,
        ):
            return False

    def _recover_lifecycle_effect_events(
        self,
        proposal: dict[str, Any],
        event_log: MissionEventLog,
        event_token: object,
    ) -> None:
        """Replay only missing audit events for a fully durable lifecycle effect."""
        action = proposal["action_type"]
        if action == "TASK_RETRY":
            return
        state = self.store.load_state(proposal["mission_id"])
        target = {
            "MISSION_START": MissionStatus.running,
            "MISSION_PAUSE": MissionStatus.paused,
            "MISSION_RESUME": MissionStatus.running,
            "MISSION_CANCEL": MissionStatus.cancelled,
        }[action]
        if state.status is not target:
            return
        effects = proposal.get("expected_queue_effects", [])
        if action in {"MISSION_PAUSE", "MISSION_CANCEL"}:
            if self._queue_record_evidence() != proposal.get(
                "expected_queue_records", []
            ):
                return
        else:
            for effect in effects:
                task = state.task_states.get(effect["task_id"])
                queue_id = effect["queue_id"]
                locations = [
                    directory
                    for directory in (self.queue.pending, self.queue.running)
                    if (directory / f"{queue_id}.json").exists()
                ]
                if (
                    task is None
                    or task.queue_task_id != queue_id
                    or task.status
                    not in {MissionTaskStatus.queued, MissionTaskStatus.running}
                    or len(locations) != 1
                ):
                    return

        events = list(event_log.read_snapshot_locked().events)
        intent_indexes = [
            index
            for index, event in enumerate(events)
            if event.event_type == "controller_action_started"
            and event.metadata.get("action_id") == proposal["action_id"]
        ]
        if len(intent_indexes) != 1:
            return
        post_events = events[intent_indexes[0] + 1 :]
        lifecycle_event = {
            "MISSION_START": "mission_started",
            "MISSION_PAUSE": "mission_paused",
            "MISSION_RESUME": "mission_resumed",
            "MISSION_CANCEL": "mission_cancelled",
        }[action]
        lifecycle_count = sum(
            event.event_type == lifecycle_event for event in post_events
        )
        if lifecycle_count > 1:
            raise APIError(
                "HUMAN_REVIEW_REQUIRED",
                "Lifecycle audit evidence is contradictory.",
                409,
            )
        if lifecycle_count == 0:
            event_log.append_locked(
                make_event(
                    lifecycle_event,
                    proposal["mission_id"],
                    metadata={"recovered_controller_action": proposal["action_id"]},
                ),
                event_token,
            )
        if action not in {"MISSION_START", "MISSION_RESUME"}:
            return
        for effect in effects:
            current = list(event_log.read_snapshot_locked().events)
            count = sum(
                event.event_type == "task_enqueued"
                and event.task_id == effect["task_id"]
                and event.queue_task_id == effect["queue_id"]
                for event in current[intent_indexes[0] + 1 :]
            )
            if count > 1:
                raise APIError(
                    "HUMAN_REVIEW_REQUIRED",
                    "Queue audit evidence is contradictory.",
                    409,
                )
            if count == 0:
                event_log.append_locked(
                    make_event(
                        "task_enqueued",
                        proposal["mission_id"],
                        task_id=effect["task_id"],
                        queue_task_id=effect["queue_id"],
                        reason="recovered controller lifecycle effect",
                    ),
                    event_token,
                )

    def _queue_recovery_matches(self, proposal: dict[str, Any]) -> bool:
        expected_records = proposal.get("expected_queue_records", [])
        current_records = self._queue_record_evidence()
        if (
            len({item["queue_id"] for item in expected_records})
            != len(expected_records)
            or len({item["queue_id"] for item in current_records})
            != len(current_records)
        ):
            return False
        original = {
            item["queue_id"]: (item["status"], item["sha256"])
            for item in expected_records
        }
        current = {
            item["queue_id"]: (item["status"], item["sha256"])
            for item in current_records
        }
        effects = {
            item["queue_id"]
            for item in proposal.get("expected_queue_effects", [])
        }
        if set(current) - set(original) - effects:
            return False
        if set(original) - set(current):
            return False
        action = proposal["action_type"]
        for queue_id, original_value in original.items():
            current_value = current.get(queue_id)
            if current_value == original_value:
                continue
            if (
                action == "TASK_RETRY"
                and queue_id in effects
                and original_value[0] in {"failed", "invalid"}
                and current_value is not None
                and current_value[0] == "pending"
                and (
                    self._retry_effect_payload_matches(proposal, queue_id)
                    or self._queue_effect_payload_matches(
                        proposal,
                        next(
                            item["task_id"]
                            for item in proposal.get(
                                "expected_queue_effects", []
                            )
                            if item["queue_id"] == queue_id
                        ),
                        queue_id,
                        allow_previous_retry=True,
                    )
                )
            ):
                continue
            return False
        for queue_id in set(current) - set(original):
            if (
                queue_id not in effects
                or current[queue_id][0] not in {"pending", "running"}
            ):
                return False
            effect = next(
                item
                for item in proposal.get("expected_queue_effects", [])
                if item["queue_id"] == queue_id
            )
            if not self._queue_effect_payload_matches(
                proposal,
                effect["task_id"],
                queue_id,
                allow_previous_retry=True,
            ):
                return False
        for effect in proposal.get("expected_queue_effects", []):
            task_id = effect["task_id"]
            current_id = effect["queue_id"]
            legacy_id = make_legacy_queue_task_id(
                proposal["mission_id"], task_id
            )
            if current_id != legacy_id and legacy_id in current:
                return False
            if current_id in current and not self._queue_effect_payload_matches(
                proposal,
                task_id,
                current_id,
                allow_previous_retry=True,
            ):
                return False
        return True

    def _intent_recovery_state(
        self,
        proposal: dict[str, Any],
        snapshot: Any,
        approval_revision: str,
    ) -> tuple[bool, bool]:
        action_id = proposal["action_id"]
        intent_indexes: list[int] = []
        for index, event in enumerate(snapshot.events):
            metadata = event.metadata
            if (
                event.event_type == "controller_action_started"
                and metadata.get("action_id") == action_id
                and metadata.get("action_type") == proposal["action_type"]
                and metadata.get("proposal_revision")
                == proposal["proposal_revision"]
                and metadata.get("mission_revision")
                == proposal["expected_mission_revision"]
                and metadata.get("event_revision")
                == proposal["expected_event_revision"]
                and metadata.get("queue_identities")
                == proposal["expected_queue_identities"]
                and metadata.get("evidence_fingerprint")
                == proposal["evidence_fingerprint"]
                and metadata.get("effect_fingerprint")
                == proposal["proposed_effect_fingerprint"]
                and metadata.get("proposal_envelope_fingerprint")
                == fingerprint(proposal)
                and metadata.get("canonical_proposal") == proposal
            ):
                intent_indexes.append(index)
        if not intent_indexes:
            return False, False
        if len(intent_indexes) != 1:
            raise APIError(
                "HUMAN_REVIEW_REQUIRED",
                "Controller intent evidence is contradictory.",
                409,
            )
        index = intent_indexes[0]
        prefix = b"".join(snapshot.raw.splitlines(keepends=True)[:index])
        intent = snapshot.events[index]
        if (
            _sha(prefix) != proposal["expected_event_revision"]
            or intent.metadata.get("approval_revision") != approval_revision
        ):
            raise APIError(
                "STALE_PROPOSAL",
                "Controller intent no longer matches authoritative evidence.",
                409,
            )
        allowed_events = {
            "MISSION_START": {
                "mission_started",
                "queue_created",
                "task_enqueued",
            },
            "MISSION_PAUSE": {"mission_paused"},
            "MISSION_RESUME": {
                "mission_resumed",
                "queue_created",
                "task_enqueued",
            },
            "MISSION_CANCEL": {"mission_cancelled"},
            "TASK_RETRY": {"task_retry_queued"},
        }[proposal["action_type"]] | {"controller_action_applied"}
        post_events = list(snapshot.events[index + 1 :])
        if any(event.event_type not in allowed_events for event in post_events):
            raise APIError(
                "STALE_PROPOSAL",
                "Evidence changed after controller intent.",
                409,
            )
        expected_effects = {
            (item["task_id"], item["queue_id"])
            for item in proposal.get("expected_queue_effects", [])
        }
        for event in post_events:
            if event.event_type in {"task_enqueued", "task_retry_queued"} and (
                event.task_id,
                event.queue_task_id,
            ) not in expected_effects:
                raise APIError(
                    "HUMAN_REVIEW_REQUIRED",
                    "Controller queue evidence is contradictory.",
                    409,
                )
            if event.event_type == "controller_action_applied" and (
                event.metadata.get("action_id") != action_id
                or event.metadata.get("effect_fingerprint")
                != proposal["proposed_effect_fingerprint"]
            ):
                raise APIError(
                    "HUMAN_REVIEW_REQUIRED",
                    "Controller completion evidence is contradictory.",
                    409,
                )
        current_revision = self._mission_snapshot(
            proposal["mission_id"]
        )[2]
        pristine = (
            current_revision == proposal["expected_mission_revision"]
            and self._queue_record_evidence()
            == proposal.get("expected_queue_records", [])
        )
        if not pristine:
            state = self.store.load_state(proposal["mission_id"])
            if (
                self._report_revision()
                != proposal.get("expected_report_revision")
                or not self._queue_recovery_matches(proposal)
            ):
                raise APIError(
                    "STALE_PROPOSAL",
                    "Queue or report evidence changed after controller intent.",
                    409,
                )
            original_budget = proposal.get("expected_budget_usage", {})
            current_budget = state.budget_usage.to_dict()
            if any(
                current_budget.get(field) != original_budget.get(field)
                for field in (
                    "total_attempts",
                    "failed_tasks",
                    "worktrees",
                )
            ):
                raise APIError(
                    "STALE_PROPOSAL",
                    "Mission budget evidence changed after intent.",
                    409,
                )
            targets = {
                "MISSION_START": MissionStatus.running,
                "MISSION_PAUSE": MissionStatus.paused,
                "MISSION_RESUME": MissionStatus.running,
                "MISSION_CANCEL": MissionStatus.cancelled,
                "TASK_RETRY": MissionStatus.running,
            }
            allowed_statuses = {targets[proposal["action_type"]]}
            if proposal["action_type"] == "MISSION_START":
                allowed_statuses.add(MissionStatus.pending)
            if state.status not in allowed_statuses:
                raise APIError(
                    "STALE_PROPOSAL",
                    "Mission evidence changed after controller intent.",
                    409,
                )
            expected_effects_by_task = {
                item["task_id"]: item["queue_id"]
                for item in proposal.get("expected_queue_effects", [])
            }
            for task_id, task_state in state.task_states.items():
                original = proposal.get(
                    "expected_task_state_fingerprints", {}
                ).get(task_id)
                if fingerprint(task_state.to_dict()) == original:
                    continue
                if (
                    task_id not in expected_effects_by_task
                    or task_state.queue_task_id
                    != expected_effects_by_task[task_id]
                    or task_state.status is not MissionTaskStatus.queued
                ):
                    raise APIError(
                        "STALE_PROPOSAL",
                        "Mission task evidence changed after controller intent.",
                        409,
                    )
        has_applied = any(
            event.event_type == "controller_action_applied"
            for event in post_events
        )
        return True, has_applied

    def _record_application(
        self,
        proposal: dict[str, Any],
        principal: Principal,
        status: str,
    ) -> dict[str, Any]:
        application = {
            "schema_version": SCHEMA_VERSION,
            "action_id": proposal["action_id"],
            "status": status,
            "applied_by": principal.identity,
            "applied_at": _iso(self.now()),
            "proposal_revision": proposal["proposal_revision"],
            "effect_fingerprint": proposal["proposed_effect_fingerprint"],
        }

        def write(snapshot):
            for item in snapshot.records:
                if (
                    item["record_type"] == "application"
                    and item["application"]["action_id"]
                    == proposal["action_id"]
                    and item["application"]["status"]
                    in {"APPLIED", "ALREADY_APPLIED"}
                ):
                    return None, dict(item["application"])
            return (
                {"record_type": "application", "application": application},
                application,
            )

        try:
            return self.approvals.transact(write)
        except ApprovalEvidenceCorrupt as exc:
            raise APIError(
                "CORRUPT_EVIDENCE",
                "The durable effect exists but approval evidence is corrupt.",
                409,
            ) from exc

    def _apply_lifecycle(
        self,
        proposal: dict[str, Any],
        principal: Principal,
        approval_revision: str,
    ) -> dict[str, Any]:
        action_id = proposal["action_id"]
        event_log = MissionEventLog(
            self.missions_root / "events", proposal["mission_id"]
        )
        with (
            self.store._lock,
            FileLock(self.queue.lock_path),
            FileLock(report_lock_path(self.reports_root)),
            event_log.locked() as event_token,
        ):
            snapshot = event_log.read_snapshot_locked()
            events = list(snapshot.events)
            has_intent, has_applied = self._intent_recovery_state(
                proposal, snapshot, approval_revision
            )
            if has_intent and not has_applied:
                self._recover_lifecycle_effect_events(
                    proposal, event_log, event_token
                )
                events = list(event_log.read_snapshot_locked().events)
                has_applied = any(
                    event.event_type == "controller_action_applied"
                    and event.metadata.get("action_id") == action_id
                    for event in events
                )
            exact_effect = self._exact_effect(proposal, events)
            if has_applied and not exact_effect:
                raise APIError(
                    "HUMAN_REVIEW_REQUIRED",
                    "The completed controller effect is no longer durable.",
                    409,
                )
            if exact_effect and has_intent:
                recovered = False
                if not has_applied:
                    event_log.append_locked(
                        make_event(
                            "controller_action_applied",
                            proposal["mission_id"],
                            task_id=proposal.get("mission_task_id"),
                            metadata={
                                "action_id": action_id,
                                "action_type": proposal["action_type"],
                                "proposal_revision": proposal[
                                    "proposal_revision"
                                ],
                                "evidence_fingerprint": proposal[
                                    "evidence_fingerprint"
                                ],
                                "effect_fingerprint": proposal[
                                    "proposed_effect_fingerprint"
                                ],
                                "recovered_interrupted_apply": True,
                            },
                        ),
                        event_token,
                    )
                    recovered = True
                status = "APPLIED" if recovered else "ALREADY_APPLIED"
            else:
                if self.now() >= parse_time(proposal["expires_at"]):
                    raise APIError(
                        "EXPIRED_PROPOSAL", "Proposal has expired.", 409
                    )
                if self._is_stale(proposal) and not has_intent:
                    raise APIError(
                        "STALE_PROPOSAL",
                        "Proposal evidence is stale.",
                        409,
                    )
                if not has_intent:
                    event_log.append_locked(
                        make_event(
                            "controller_action_started",
                            proposal["mission_id"],
                            task_id=proposal.get("mission_task_id"),
                            metadata={
                                "action_id": action_id,
                                "action_type": proposal["action_type"],
                                "proposal_revision": proposal[
                                    "proposal_revision"
                                ],
                                "mission_revision": proposal[
                                    "expected_mission_revision"
                                ],
                                "event_revision": proposal[
                                    "expected_event_revision"
                                ],
                                "queue_identities": proposal[
                                    "expected_queue_identities"
                                ],
                                "evidence_fingerprint": proposal[
                                    "evidence_fingerprint"
                                ],
                                "effect_fingerprint": proposal[
                                    "proposed_effect_fingerprint"
                                ],
                                "proposal_envelope_fingerprint": fingerprint(
                                    proposal
                                ),
                                "canonical_proposal": proposal,
                                "approval_revision": approval_revision,
                            },
                        ),
                        event_token,
                    )
                action = proposal["action_type"]
                if action == "MISSION_START":
                    self.scheduler.run_once(proposal["mission_id"])
                elif action == "MISSION_PAUSE":
                    self.scheduler.pause(proposal["mission_id"])
                elif action == "MISSION_RESUME":
                    self.scheduler.resume(proposal["mission_id"])
                    self.scheduler.run_once(proposal["mission_id"])
                elif action == "MISSION_CANCEL":
                    self.scheduler.cancel(proposal["mission_id"])
                elif action == "TASK_RETRY":
                    self.scheduler.retry_task(
                        proposal["mission_id"],
                        proposal["mission_task_id"],
                        recovery_action_id=action_id,
                    )
                else:
                    raise APIError(
                        "HUMAN_REVIEW_REQUIRED",
                        "Proposal action is not executable.",
                        409,
                    )
                if has_intent:
                    self._recover_lifecycle_effect_events(
                        proposal, event_log, event_token
                    )
                if not self._exact_effect(proposal):
                    raise APIError(
                        "INTERNAL_ERROR",
                        "The controller did not produce the exact proposed effect.",
                        500,
                        retryable=True,
                    )
                if not any(
                    event.event_type == "controller_action_applied"
                    and event.metadata.get("action_id") == action_id
                    for event in event_log.read_snapshot_locked().events
                ):
                    event_log.append_locked(
                        make_event(
                            "controller_action_applied",
                            proposal["mission_id"],
                            task_id=proposal.get("mission_task_id"),
                            metadata={
                                "action_id": action_id,
                                "action_type": proposal["action_type"],
                                "proposal_revision": proposal[
                                    "proposal_revision"
                                ],
                                "evidence_fingerprint": proposal[
                                    "evidence_fingerprint"
                                ],
                                "effect_fingerprint": proposal[
                                    "proposed_effect_fingerprint"
                                ],
                            },
                        ),
                        event_token,
                    )
                status = "APPLIED"
        return self._record_application(proposal, principal, status)

    def apply(self, action_id: str, principal: Principal) -> dict[str, Any]:
        try:
            proposals, records = self._records()
        except ApprovalEvidenceCorrupt as exc:
            raise APIError(
                "CORRUPT_EVIDENCE", "Approval evidence is corrupt.", 409
            ) from exc
        proposal = proposals.get(action_id)
        if proposal is None:
            raise APIError("NOT_FOUND", "Proposal was not found.", 404)
        if not proposal.get("recovery_action"):
            started = any(
                event.event_type == "controller_action_started"
                and event.metadata.get("action_id") == action_id
                for event in self._event_snapshot(
                    proposal["mission_id"]
                ).events
            )
            if not started:
                self._verify_lifecycle_proposal(proposal)
        else:
            self._verify_recovery_proposal(
                proposal, require_current_action=False
            )
        if any(
            item["record_type"] == "supersession"
            and item["action_id"] == action_id
            for item in records
        ):
            raise APIError(
                "STALE_PROPOSAL",
                "Proposal was superseded by a newer controller action.",
                409,
            )
        if any(
            item["record_type"] == "application"
            and item["application"]["action_id"] == action_id
            and item["application"]["status"] == "FAILED"
            for item in records
        ):
            raise APIError(
                "HUMAN_REVIEW_REQUIRED",
                "Proposal has a durable failed application.",
                409,
            )
        decisions = [
            item["decision"]
            for item in records
            if item["record_type"] == "decision"
            and item["decision"]["action_id"] == action_id
        ]
        if not decisions:
            raise APIError(
                "NOT_APPROVED", "Proposal is not approved.", 409
            )
        decision = decisions[-1]
        binding = {
            "schema_version": SCHEMA_VERSION,
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
            raise APIError(
                "HUMAN_REVIEW_REQUIRED",
                "Approval does not exactly bind the proposal evidence.",
                409,
            )
        if decision["decision"] != "approve":
            raise APIError(
                "NOT_APPROVED", "Proposal is not approved.", 409
            )
        if not proposal.get("recovery_action"):
            approval_revision = self.approvals.snapshot().revision
            completed = any(
                item["record_type"] == "application"
                and item["application"]["action_id"] == action_id
                for item in records
            )
            if completed:
                for event in self._event_snapshot(
                    proposal["mission_id"]
                ).events:
                    if (
                        event.event_type == "controller_action_started"
                        and event.metadata.get("action_id") == action_id
                        and isinstance(
                            event.metadata.get("approval_revision"), str
                        )
                    ):
                        approval_revision = event.metadata["approval_revision"]
                        break
            return self._apply_lifecycle(
                proposal, principal, approval_revision
            )
        action = self._verify_recovery_proposal(
            proposal, require_current_action=False
        )
        current_actions = {
            item.action_id: item
            for item in plan_mission_repairs(
                action.mission_id,
                self.store,
                self.queue,
                self.reports_root,
            )
        }
        if action.action_id not in current_actions:
            replay = apply_mission_repair(
                action,
                self.store,
                self.queue,
                self.reports_root,
                self.missions_root,
            )
            if replay.status is RepairApplyStatus.NO_LONGER_NEEDED:
                return self._record_application(
                    proposal, principal, "ALREADY_APPLIED"
                )
            raise APIError(
                "STALE_PROPOSAL", "Recovery evidence became stale.", 409
            )
        if current_actions[action.action_id].to_dict() != action.to_dict():
            raise APIError(
                "STALE_PROPOSAL", "Recovery evidence became stale.", 409
            )
        if self.now() >= parse_time(proposal["expires_at"]):
            raise APIError("EXPIRED_PROPOSAL", "Proposal has expired.", 409)
        if self._is_stale(proposal):
            raise APIError(
                "STALE_PROPOSAL", "Proposal evidence is stale.", 409
            )

        result = apply_mission_repair(
            action,
            self.store,
            self.queue,
            self.reports_root,
            self.missions_root,
        )
        status_map = {
            RepairApplyStatus.APPLIED: "APPLIED",
            RepairApplyStatus.NO_LONGER_NEEDED: "ALREADY_APPLIED",
            RepairApplyStatus.STALE: "STALE",
            RepairApplyStatus.HUMAN_REVIEW_REQUIRED: "FAILED",
            RepairApplyStatus.FAILED: "FAILED",
        }
        status = status_map[result.status]
        if status == "STALE":
            raise APIError(
                "STALE_PROPOSAL", "Recovery evidence became stale.", 409
            )
        if status == "FAILED":
            self._record_application(proposal, principal, status)
            raise APIError(
                "HUMAN_REVIEW_REQUIRED",
                "Recovery authority requires human review.",
                409,
            )
        return self._record_application(proposal, principal, status)


def _limit() -> int:
    raw = request.args.get("limit", "50")
    try:
        value = int(raw)
    except ValueError as exc:
        raise invalid("limit must be an integer") from exc
    if value < 1 or value > MAX_LIMIT:
        raise invalid(f"limit must be between 1 and {MAX_LIMIT}")
    return value


def _encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(raw: str) -> dict[str, Any]:
    try:
        padding = "=" * (-len(raw) % 4)
        decoded = base64.b64decode(
            raw + padding, altchars=b"-_", validate=True
        )
        payload = json.loads(
            decoded.decode("utf-8"), object_pairs_hook=_strict_json_object
        )
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise invalid("cursor is malformed") from exc
    if not isinstance(payload, dict):
        raise invalid("cursor is malformed")
    return payload


def _recovery_snapshot_revision(items: list[dict[str, Any]]) -> str:
    """Cursor evidence must not depend on presentation-time proposal expiry."""
    return fingerprint(
        [
            {
                "action_id": item["action_id"],
                "action_type": item["action_type"],
                "mission_id": item["mission_id"],
                "mission_task_id": item.get("mission_task_id"),
                "expected_mission_revision": item["expected_mission_revision"],
                "expected_event_revision": item["expected_event_revision"],
                "expected_queue_identities": item[
                    "expected_queue_identities"
                ],
                "expected_queue_revision": item["expected_queue_revision"],
                "evidence_fingerprint": item["evidence_fingerprint"],
                "proposed_effect_fingerprint": item[
                    "proposed_effect_fingerprint"
                ],
            }
            for item in items
        ]
    )


def _page(
    items: list[dict[str, Any]],
    key: str,
    *,
    resource: str,
    snapshot_revision: str,
    filters: dict[str, Any],
) -> dict[str, Any]:
    limit = _limit()
    offset = 0
    raw_cursor = request.args.get("cursor")
    if raw_cursor:
        cursor = _decode_cursor(raw_cursor)
        expected_fields = {
            "schema_version",
            "resource",
            "filters",
            "sort_key",
            "offset",
            "snapshot_revision",
            "limit",
        }
        if (
            set(cursor) != expected_fields
            or cursor["schema_version"] != CURSOR_SCHEMA
            or cursor["resource"] != resource
            or cursor["filters"] != filters
            or cursor["sort_key"] != key
            or cursor["snapshot_revision"] != snapshot_revision
            or not isinstance(cursor["offset"], int)
            or isinstance(cursor["offset"], bool)
            or cursor["offset"] <= 0
            or cursor["offset"] > len(items)
            or cursor["limit"] != limit
        ):
            code = (
                "CONFLICT"
                if cursor.get("snapshot_revision") != snapshot_revision
                else "INVALID_REQUEST"
            )
            raise APIError(
                code,
                "Cursor does not match this resource snapshot.",
                409 if code == "CONFLICT" else 400,
            )
        offset = cursor["offset"]
    selected = items[offset : offset + limit]
    next_offset = offset + len(selected)
    return {
        "items": selected,
        "next_cursor": (
            _encode_cursor(
                {
                    "schema_version": CURSOR_SCHEMA,
                    "resource": resource,
                    "filters": filters,
                    "sort_key": key,
                    "offset": next_offset,
                    "snapshot_revision": snapshot_revision,
                    "limit": limit,
                }
            )
            if next_offset < len(items) and selected
            else None
        ),
        "limit": limit,
    }


def _json_body(allowed: set[str], required: set[str] = set()) -> dict[str, Any]:
    if request.content_type != "application/json":
        raise APIError(
            "INVALID_REQUEST", "Mutation requests require application/json.", 415
        )
    if (request.content_length or 0) > MAX_BODY:
        raise APIError(
            "INVALID_REQUEST", "Request body exceeds the allowed size.", 413
        )
    raw = request.get_data(cache=True)
    if len(raw) > MAX_BODY:
        raise APIError(
            "INVALID_REQUEST", "Request body exceeds the allowed size.", 413
        )
    try:
        body = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_non_finite_json,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise invalid("Request body must be one unambiguous JSON object") from exc
    if not isinstance(body, dict):
        raise invalid("JSON body must be an object")
    unknown = sorted(set(body) - allowed)
    missing = sorted(required - set(body))
    if unknown:
        raise invalid("Unknown JSON fields are not allowed.")
    if missing:
        raise invalid("Required JSON fields are missing.")
    if "reason" in body and (
        not isinstance(body["reason"], str)
        or len(body["reason"].strip()) > MAX_REASON
    ):
        raise invalid("reason must be a bounded string")
    if "note" in body and (
        not isinstance(body["note"], str)
        or len(body["note"].strip()) > MAX_NOTE
    ):
        raise invalid("note must be a bounded string")
    if "expected_proposal_revision" in body and (
        not isinstance(body["expected_proposal_revision"], str)
        or not body["expected_proposal_revision"]
    ):
        raise invalid("expected_proposal_revision must be a non-empty string")
    if "idempotency_key" in required and (
        not isinstance(body.get("idempotency_key"), str)
        or not body["idempotency_key"]
        or body["idempotency_key"] != body["idempotency_key"].strip()
        or len(body["idempotency_key"]) > 200
    ):
        raise invalid("idempotency_key must be a bounded non-empty string")
    return body


def create_operations_blueprint(
    operations: ControllerOperations,
) -> Blueprint:
    blueprint = Blueprint("controller_operations_v1", __name__)

    capabilities = {
        "GET": "controller.read",
        "propose": "controller.propose",
        "approve": "controller.approve",
        "apply": "controller.apply",
    }

    @blueprint.before_request
    def controller_guard():
        g.controller_request_id = (
            request.headers.get("X-Request-ID", "").strip()[:128]
            or f"controller-{uuid4().hex}"
        )
        if request.method == "OPTIONS":
            origins = request.headers.getlist("Origin")
            origin = origins[0] if len(origins) == 1 else None
            requested_method = request.headers.get(
                "Access-Control-Request-Method", ""
            )
            requested_headers = [
                item.strip().lower()
                for item in request.headers.get(
                    "Access-Control-Request-Headers", ""
                ).split(",")
                if item.strip()
            ]
            rule_methods = (
                request.url_rule.methods if request.url_rule is not None else set()
            )
            if (
                origin not in operations.allowed_origins
                or requested_method not in {"GET", "POST"}
                or requested_method not in rule_methods
                or len(requested_headers) != len(set(requested_headers))
                or any(
                    item not in _PREFLIGHT_HEADERS
                    for item in requested_headers
                )
            ):
                raise APIError(
                    "INVALID_REQUEST",
                    "CORS preflight is not permitted for this controller route.",
                    400,
                )
            response = make_response("", 204)
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Methods"] = requested_method
            if requested_headers:
                response.headers["Access-Control-Allow-Headers"] = ", ".join(
                    requested_headers
                )
            response.headers["Access-Control-Max-Age"] = "600"
            response.headers["Vary"] = "Origin"
            return response
        endpoint = request.endpoint or ""
        if endpoint.endswith(".approve") or endpoint.endswith(".reject"):
            required = capabilities["approve"]
        elif endpoint.endswith(".apply_proposal"):
            required = capabilities["apply"]
        elif ".propose_" in endpoint:
            required = capabilities["propose"]
        else:
            required = capabilities["GET"]
        g.controller_principal = authenticate(operations.tokens, required)
        repeated = sorted(
            key for key in request.args if len(request.args.getlist(key)) != 1
        )
        if repeated:
            raise invalid("Query parameters must occur at most once.")
        if not endpoint.endswith(".health"):
            operations.require_controller_state()

    @blueprint.after_request
    def controller_headers(response):
        response.headers["X-Request-ID"] = g.get(
            "controller_request_id", "unavailable"
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        origins = request.headers.getlist("Origin")
        origin = origins[0] if len(origins) == 1 else None
        if origin and origin in operations.allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        return response

    @blueprint.errorhandler(APIError)
    def api_error(error: APIError):
        return error.response()

    @blueprint.errorhandler(EventLogCorruptionError)
    @blueprint.errorhandler(ApprovalEvidenceCorrupt)
    def corrupt_evidence(_error):
        return APIError(
            "CORRUPT_EVIDENCE", "Authoritative evidence is corrupt.", 409
        ).response()

    @blueprint.errorhandler(MissionStateConflictError)
    def split_state(_error):
        return APIError(
            "CONFLICT", "Mission state has conflicting envelopes.", 409
        ).response()

    @blueprint.errorhandler(Exception)
    def internal_error(_error):
        return APIError(
            "INTERNAL_ERROR",
            "The controller request could not be completed.",
            500,
            retryable=True,
        ).response()

    @blueprint.app_errorhandler(NotFound)
    def controller_not_found(error):
        if request.path.startswith(f"{API_PREFIX}/"):
            g.controller_request_id = g.get(
                "controller_request_id", f"controller-{uuid4().hex}"
            )
            return APIError(
                "NOT_FOUND", "Controller API resource was not found.", 404
            ).response()
        return error

    @blueprint.app_errorhandler(MethodNotAllowed)
    def controller_method_not_allowed(error):
        if request.path.startswith(f"{API_PREFIX}/"):
            g.controller_request_id = g.get(
                "controller_request_id", f"controller-{uuid4().hex}"
            )
            return APIError(
                "METHOD_NOT_ALLOWED",
                "The controller API method is not allowed.",
                405,
            ).response()
        return error

    @blueprint.get(f"{API_PREFIX}/health")
    def health():
        principal: Principal = g.controller_principal
        version_path = Path("VERSION")
        state_available = operations.controller_state_available()
        return {
            "service": "raghub-controller-operations-room",
            "api_version": "v1",
            "controller_version": (
                version_path.read_text(encoding="utf-8").strip()
                if version_path.exists()
                else None
            ),
            "server_time": _iso(operations.now()),
            "process_status": "alive",
            "controller_state": (
                "available" if state_available else "unavailable"
            ),
            "capabilities": {
                "read": "controller.read" in principal.capabilities,
                "approvals": "controller.approve" in principal.capabilities,
            },
        }

    @blueprint.get(f"{API_PREFIX}/overview")
    def overview():
        mission_counts: Counter[str] = Counter()
        task_counts: Counter[str] = Counter()
        blocked: list[str] = []
        conflicts: list[str] = []
        for mission_id in operations.store.list_missions():
            try:
                _, state, _ = operations._mission_snapshot(mission_id)
            except MissionStateConflictError:
                conflicts.append(mission_id)
                continue
            mission_counts[state.status.value] += 1
            task_counts.update(
                item.status.value for item in state.task_states.values()
            )
            if any(
                item.status
                in {MissionTaskStatus.blocked, MissionTaskStatus.approval_required}
                for item in state.task_states.values()
            ):
                blocked.append(mission_id)
        queue_records, queue_revision = operations._queue_snapshot()
        queue_counts = Counter(item["status"] for item in queue_records)
        proposals, records = operations._records()
        public = [
            operations._public_proposal(item, records)
            for item in proposals.values()
        ]
        return {
            "mission_counts": dict(sorted(mission_counts.items())),
            "task_counts": dict(sorted(task_counts.items())),
            "queue_counts": dict(sorted(queue_counts.items())),
            "blocked_missions": sorted(blocked),
            "stale_or_conflicting_missions": sorted(conflicts),
            "pending_recovery_proposals": sum(
                item["status"] == "pending"
                and item["authority"] == "MissionSafeRecoveryPlanner"
                for item in public
            ),
            "pending_approval_proposals": sum(
                item["status"] == "pending" for item in public
            ),
            "audit_revision": operations.approvals.snapshot().revision,
            "queue_revision": queue_revision,
        }

    @blueprint.get(f"{API_PREFIX}/missions")
    def missions():
        requested_status = request.args.get("status")
        if requested_status and requested_status not in {
            item.value for item in MissionStatus
        }:
            raise invalid("status is not a supported mission status")
        created_after = request.args.get("created_after")
        updated_after = request.args.get("updated_after")
        items: list[dict[str, Any]] = []
        for mission_id in operations.store.list_missions():
            try:
                definition, state, revision = operations._mission_snapshot(
                    mission_id
                )
            except MissionStateConflictError:
                items.append(
                    {
                        "mission_id": mission_id,
                        "status": "conflict",
                        "state_revision": None,
                    }
                )
                continue
            if requested_status and state.status.value != requested_status:
                continue
            found = operations.store.find_mission(mission_id)
            updated = (
                datetime.fromtimestamp(found[1].stat().st_mtime, timezone.utc)
                if found
                else operations.now()
            )
            if created_after and parse_time(definition.created_at) <= parse_time(
                created_after
            ):
                continue
            if updated_after and updated <= parse_time(updated_after):
                continue
            items.append(
                {
                    "mission_id": mission_id,
                    "title": (
                        public_value(definition.title, key="title")
                        or "[redacted]"
                    ),
                    "status": state.status.value,
                    "created_at": definition.created_at,
                    "updated_at": _iso(updated),
                    "state_revision": revision,
                }
            )
        items.sort(key=lambda item: item["mission_id"])
        filters = {
            "status": requested_status,
            "created_after": created_after,
            "updated_after": updated_after,
        }
        return _page(
            items,
            "mission_id",
            resource="missions",
            snapshot_revision=fingerprint(items),
            filters=filters,
        )

    def resolve_mission(mission_id: str) -> str:
        ids = operations.store.list_missions()
        if mission_id in ids:
            return mission_id
        matches = [item for item in ids if item.startswith(mission_id)]
        if not matches:
            raise APIError("NOT_FOUND", "Mission was not found.", 404)
        if len(matches) > 1:
            raise APIError(
                "CONFLICT", "Mission ID prefix is ambiguous.", 409
            )
        return matches[0]

    @blueprint.get(f"{API_PREFIX}/missions/<mission_id>")
    def mission_detail(mission_id: str):
        mission_id = resolve_mission(mission_id)
        definition, state, revision = operations._mission_snapshot(mission_id)
        graph = DependencyGraph(definition, state)
        queue_ids = {
            task.task_id: sorted(
                {
                    TaskMaterializer(
                        operations.queue, operations.reports_root
                    ).make_queue_task_id(mission_id, task.task_id),
                    make_legacy_queue_task_id(mission_id, task.task_id),
                    *(
                        [state.task_states[task.task_id].queue_task_id]
                        if task.task_id in state.task_states
                        and state.task_states[task.task_id].queue_task_id
                        else []
                    ),
                }
            )
            for task in definition.tasks
        }
        return {
            "definition": public_definition(definition),
            "state": public_state(state),
            "dependency_graph": {
                "topological_order": graph.topological_order(),
                "blocked_tasks": graph.blocked_tasks(),
                "running_tasks": graph.running_tasks(),
            },
            "runtime_and_retry_budgets": {
                "limits": definition.budgets.to_dict(),
                "usage": state.budget_usage.to_dict(),
                "task_max_attempts": {
                    task.task_id: task.max_attempts for task in definition.tasks
                },
            },
            "blockers": sorted(
                task_id
                for task_id, item in state.task_states.items()
                if item.status
                in {MissionTaskStatus.blocked, MissionTaskStatus.approval_required}
            ),
            "accepted_queue_identities": queue_ids,
            "state_revision": revision,
        }

    @blueprint.get(f"{API_PREFIX}/missions/<mission_id>/events")
    def mission_events(mission_id: str):
        mission_id = resolve_mission(mission_id)
        snapshot = operations._event_snapshot(mission_id)
        events = [
            {
                **public_value(event.to_dict()),
                "event_revision": index + 1,
            }
            for index, event in enumerate(snapshot.events)
            if event.mission_id == mission_id
        ]
        events.sort(key=lambda item: item["event_revision"])
        page = _page(
            events,
            "event_revision",
            resource="mission-events",
            snapshot_revision=snapshot.revision,
            filters={"mission_id": mission_id},
        )
        page["event_revision"] = snapshot.revision
        page["corruption_state"] = "valid"
        page["conflict_state"] = (
            "conflict"
            if len(operations.store.find_mission_candidates(mission_id)) > 1
            else "consistent"
        )
        return page

    @blueprint.get(f"{API_PREFIX}/queue")
    def queue():
        records, revision = operations._queue_snapshot()
        requested_status = request.args.get("status")
        mission_id = request.args.get("mission_id")
        if requested_status and requested_status not in QUEUE_STATES:
            raise invalid("status is not a supported queue status")
        if requested_status:
            records = [
                item for item in records if item["status"] == requested_status
            ]
        if mission_id:
            records = [
                item
                for item in records
                if item.get("record", {}).get("metadata", {}).get("mission_id")
                == mission_id
            ]
        page = _page(
            records,
            "queue_id",
            resource="queue",
            snapshot_revision=revision,
            filters={
                "status": requested_status,
                "mission_id": mission_id,
            },
        )
        page["queue_revision"] = revision
        return page

    @blueprint.get(f"{API_PREFIX}/recovery/proposals")
    def recovery_proposals():
        items = operations.recovery_proposals()
        items.sort(key=lambda item: item["action_id"])
        return _page(
            items,
            "action_id",
            resource="recovery-proposals",
            snapshot_revision=_recovery_snapshot_revision(items),
            filters={},
        )

    @blueprint.post(f"{API_PREFIX}/recovery/proposals/<action_id>")
    def propose_recovery(action_id: str):
        _json_body(set())
        value, created = operations.persist_recovery_proposal(action_id)
        return (
            operations._public_proposal(value, [], evaluate_stale=False),
            201 if created else 200,
        )

    @blueprint.get(f"{API_PREFIX}/proposals")
    def proposals():
        stored, records = operations._records()
        items = [
            operations._public_proposal(item, records)
            for item in stored.values()
        ]
        requested = request.args.get("status")
        if requested:
            items = [item for item in items if item["status"] == requested]
        items.sort(key=lambda item: item["action_id"])
        return _page(
            items,
            "action_id",
            resource="proposals",
            snapshot_revision=operations.approvals.snapshot().revision,
            filters={"status": requested},
        )

    @blueprint.get(f"{API_PREFIX}/proposals/<action_id>")
    def proposal(action_id: str):
        return operations.get_proposal(action_id)

    def propose(
        mission_id: str, action_type: str, task_id: str | None = None
    ):
        body = _json_body({"reason"})
        principal: Principal = g.controller_principal
        value, created = operations.create_lifecycle_proposal(
            mission_id,
            action_type,
            task_id=task_id,
            reason=(body.get("reason") or "").strip() or None,
            authority=principal.identity,
        )
        return value, (201 if created else 200)

    @blueprint.post(f"{API_PREFIX}/missions/<mission_id>/proposals/start")
    def propose_start(mission_id: str):
        return propose(mission_id, "MISSION_START")

    @blueprint.post(f"{API_PREFIX}/missions/<mission_id>/proposals/pause")
    def propose_pause(mission_id: str):
        return propose(mission_id, "MISSION_PAUSE")

    @blueprint.post(f"{API_PREFIX}/missions/<mission_id>/proposals/resume")
    def propose_resume(mission_id: str):
        return propose(mission_id, "MISSION_RESUME")

    @blueprint.post(f"{API_PREFIX}/missions/<mission_id>/proposals/cancel")
    def propose_cancel(mission_id: str):
        return propose(mission_id, "MISSION_CANCEL")

    @blueprint.post(
        f"{API_PREFIX}/missions/<mission_id>/tasks/<task_id>/proposals/retry"
    )
    def propose_retry(mission_id: str, task_id: str):
        return propose(mission_id, "TASK_RETRY", task_id)

    def decide(action_id: str, decision: str):
        body = _json_body(
            {"expected_proposal_revision", "note", "idempotency_key"},
            {"expected_proposal_revision", "idempotency_key"},
        )
        return operations.decide(
            action_id, decision, body, g.controller_principal
        )

    @blueprint.post(f"{API_PREFIX}/proposals/<action_id>/approve")
    def approve(action_id: str):
        return decide(action_id, "approve")

    @blueprint.post(f"{API_PREFIX}/proposals/<action_id>/reject")
    def reject(action_id: str):
        return decide(action_id, "reject")

    @blueprint.post(f"{API_PREFIX}/proposals/<action_id>/apply")
    def apply_proposal(action_id: str):
        _json_body(set())
        return operations.apply(action_id, g.controller_principal)

    return blueprint
