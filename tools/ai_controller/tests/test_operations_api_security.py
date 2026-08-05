from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from flask import Flask

from tools.ai_controller.mission.events import (
    EventLogCorruptionError,
    MissionEventLog,
    make_event,
)
from tools.ai_controller.mission.models import MissionStatus, MissionTaskStatus
from tools.ai_controller.mission.models import MissionState, MissionTaskState
from tools.ai_controller.mission.repair import plan_mission_repairs
from tools.ai_controller.models import Task
from tools.ai_controller.operations_api import (
    ControllerOperations,
    create_operations_blueprint,
    validate_bind_address,
)
from tools.ai_controller.operations_api.approvals import (
    ApprovalEvidenceCorrupt,
    ApprovalLog,
)
from tools.ai_controller.operations_api.auth import load_tokens_from_environment
from tools.ai_controller.operations_api.errors import APIError
from tools.ai_controller.operations_api.proposals import (
    fingerprint,
    proposal_revision,
)
from tools.ai_controller.operations_api.serialization import public_value
from tools.ai_controller.tests.test_operations_api import (
    TOKENS,
    _auth,
    _create_mission,
    _proposal,
    client,
    operations,
)


def _app(operations: ControllerOperations) -> Flask:
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(create_operations_blueprint(operations))
    return app


def _rewrite_decision(
    operations: ControllerOperations,
    mutate,
) -> None:
    records = [
        json.loads(line)
        for line in operations.approval_log_path.read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    decision = next(
        item["decision"]
        for item in records
        if item["record_type"] == "decision"
    )
    mutate(decision)
    operations.approval_log_path.write_text(
        "".join(
            json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n"
            for item in records
        ),
        encoding="utf-8",
    )


def _rewrite_proposal(operations: ControllerOperations, mutate) -> dict:
    records = [
        json.loads(line)
        for line in operations.approval_log_path.read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    proposal = next(
        item["proposal"]
        for item in records
        if item["record_type"] == "proposal"
    )
    mutate(proposal)
    from tools.ai_controller.operations_api.proposals import proposal_revision

    proposal["proposal_revision"] = proposal_revision(proposal)
    operations.approval_log_path.write_text(
        "".join(
            json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n"
            for item in records
        ),
        encoding="utf-8",
    )
    return proposal


def _rewrite_records(
    operations: ControllerOperations,
    mutate,
) -> None:
    records = [
        json.loads(line)
        for line in operations.approval_log_path.read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    mutate(records)
    operations.approval_log_path.write_text(
        "".join(
            json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n"
            for item in records
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("action_id", "controller-" + "f" * 64),
        ("expected_mission_revision", "a" * 64),
        ("expected_event_revision", "b" * 64),
        ("expected_queue_identities", ["forged-queue"]),
        ("expected_queue_revision", "c" * 64),
        ("evidence_fingerprint", "d" * 64),
        ("proposed_effect_fingerprint", "e" * 64),
        ("authority", "forged-authority"),
        ("expires_at", "2026-08-04T19:00:00+00:00"),
    ],
)
@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_decision_recomputes_every_lifecycle_proposal_binding(
    client,
    operations: ControllerOperations,
    field: str,
    value,
    decision: str,
):
    proposal = _proposal(client)
    stored = _rewrite_proposal(
        operations, lambda item: item.__setitem__(field, value)
    )

    response = client.post(
        f"/api/controller/v1/proposals/{stored['action_id']}/{decision}",
        json={
            "expected_proposal_revision": stored["proposal_revision"],
            "idempotency_key": f"recompute-{field}-{decision}",
        },
        headers=_auth("approve-token"),
    )

    assert response.status_code == 409
    assert response.json["error"]["code"] in {
        "CORRUPT_EVIDENCE",
        "STALE_PROPOSAL",
        "HUMAN_REVIEW_REQUIRED",
    }


def test_apply_recomputes_bound_effect_before_scheduler_mutation(
    client,
    operations: ControllerOperations,
):
    proposal = _proposal(client)
    client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": "effect-binding-approval",
        },
        headers=_auth("approve-token"),
    )
    _rewrite_proposal(
        operations,
        lambda item: item.__setitem__(
            "expected_queue_effects",
            [{"task_id": "task-a", "queue_id": "forged-queue"}],
        ),
    )

    response = client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/apply",
        json={},
        headers=_auth("apply-token"),
    )

    assert response.status_code == 409
    assert response.json["error"]["code"] in {
        "CORRUPT_EVIDENCE",
        "STALE_PROPOSAL",
        "HUMAN_REVIEW_REQUIRED",
    }
    assert operations.store.load_state("mission-alpha").status is MissionStatus.pending


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "action_type",
        "mission_id",
        "proposal_revision",
        "mission_revision",
        "event_revision",
        "queue_identities",
        "evidence_fingerprint",
        "proposed_effect_fingerprint",
        "approver_identity",
        "decision_time",
        "idempotency_key",
    ],
)
def test_incomplete_decision_record_never_authorizes_apply(
    client,
    operations: ControllerOperations,
    field: str,
):
    proposal = _proposal(client)
    client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": "strict-decision",
        },
        headers=_auth("approve-token"),
    )
    _rewrite_decision(operations, lambda decision: decision.pop(field))

    response = client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/apply",
        json={},
        headers=_auth("apply-token"),
    )

    assert response.status_code == 409
    assert response.json["error"]["code"] == "CORRUPT_EVIDENCE"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("action_type", "MISSION_CANCEL"),
        ("mission_id", "another-mission"),
        ("mission_task_id", "another-task"),
        ("proposal_revision", "0" * 64),
        ("mission_revision", "1" * 64),
        ("event_revision", "2" * 64),
        ("queue_identities", ["forged-queue"]),
        ("evidence_fingerprint", "3" * 64),
        ("proposed_effect_fingerprint", "4" * 64),
        ("decision", "maybe"),
    ],
)
def test_forged_decision_binding_never_authorizes_apply(
    client,
    operations: ControllerOperations,
    field: str,
    value,
):
    proposal = _proposal(client)
    client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": "forged-decision",
        },
        headers=_auth("approve-token"),
    )
    _rewrite_decision(
        operations, lambda decision: decision.__setitem__(field, value)
    )

    response = client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/apply",
        json={},
        headers=_auth("apply-token"),
    )

    assert response.status_code == 409
    assert response.json["error"]["code"] in {
        "CORRUPT_EVIDENCE",
        "HUMAN_REVIEW_REQUIRED",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("decision", "approve"),
        ("approver_identity", "different-approver"),
        ("proposal_revision", "0" * 64),
        ("action_type", "MISSION_CANCEL"),
        ("mission_id", "mission-beta"),
        ("mission_task_id", "task-beta"),
        ("evidence_fingerprint", "1" * 64),
        ("proposed_effect_fingerprint", "2" * 64),
        ("idempotency_key", "different-key"),
        ("decision_time", "2026-08-04T18:01:00+00:00"),
        ("previous_revision", "3" * 64),
        ("record_integrity", "4" * 64),
    ],
)
def test_schema_valid_durable_decision_field_tampering_is_detected(
    client,
    operations: ControllerOperations,
    field: str,
    value,
):
    proposal = _proposal(client)
    rejected = client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/reject",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": "durable-rejection",
        },
        headers=_auth("approve-token"),
    )
    assert rejected.status_code == 200
    _rewrite_decision(
        operations, lambda decision: decision.__setitem__(field, value)
    )

    response = client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/apply",
        json={},
        headers=_auth("apply-token"),
    )

    assert response.status_code == 409
    assert response.json["error"]["code"] in {
        "CORRUPT_EVIDENCE",
        "HUMAN_REVIEW_REQUIRED",
    }
    assert operations.store.load_state("mission-alpha").status is MissionStatus.pending


@pytest.mark.parametrize(
    ("replacement_action", "replacement_task"),
    [
        ("MISSION_CANCEL", None),
        ("MISSION_PAUSE", None),
        ("MISSION_RESUME", None),
        ("TASK_RETRY", "task-a"),
    ],
)
def test_post_intent_action_substitution_never_mutates(
    client,
    operations: ControllerOperations,
    monkeypatch: pytest.MonkeyPatch,
    replacement_action: str,
    replacement_task: str | None,
):
    proposal = _approved_start(client)
    path = f"/api/controller/v1/proposals/{proposal['action_id']}/apply"
    original = operations.scheduler.run_once
    monkeypatch.setattr(
        operations.scheduler,
        "run_once",
        lambda _mission_id: (_ for _ in ()).throw(RuntimeError("interrupt")),
    )
    assert client.post(path, json={}, headers=_auth("apply-token")).status_code == 500
    monkeypatch.setattr(operations.scheduler, "run_once", original)

    def substitute(records):
        stored = next(
            item["proposal"] for item in records if item["record_type"] == "proposal"
        )
        decision = next(
            item["decision"] for item in records if item["record_type"] == "decision"
        )
        stored["action_type"] = replacement_action
        stored["mission_task_id"] = replacement_task
        stored["proposed_effect_fingerprint"] = fingerprint(
            {
                "action_type": replacement_action,
                "mission_id": stored["mission_id"],
                "mission_task_id": replacement_task,
            }
        )
        stored["proposal_revision"] = proposal_revision(stored)
        for field in (
            "action_type",
            "mission_task_id",
            "proposal_revision",
            "proposed_effect_fingerprint",
        ):
            decision[field] = stored[field]

    _rewrite_records(operations, substitute)
    response = client.post(path, json={}, headers=_auth("apply-token"))

    assert response.status_code == 409
    assert response.json["error"]["code"] in {
        "CORRUPT_EVIDENCE",
        "HUMAN_REVIEW_REQUIRED",
        "STALE_PROPOSAL",
    }
    assert operations.store.load_state("mission-alpha").status is MissionStatus.pending


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("action_id", "repair-" + "f" * 24),
        ("classification", "HUMAN_REVIEW_REQUIRED"),
        ("mission_id", "mission-beta"),
        ("task_id", "task-beta"),
        ("revision", "1" * 64),
        ("expected_queue_id", "forged-queue"),
        ("evidence", ["event_revision=" + "2" * 64]),
        ("proposed_action", "NONE"),
        ("created_at", "2026-08-04T18:01:00+00:00"),
    ],
)
def test_recovery_verification_reconstructs_exact_current_planner_action(
    tmp_path: Path,
    field: str,
    value,
):
    root = tmp_path / f"recovery-forge-{field}"
    _create_mission(
        root,
        status=MissionStatus.running,
        task_status=MissionTaskStatus.pending,
    )
    operations = ControllerOperations(
        missions_root=root / "missions",
        queue_root=root / "queue",
        reports_root=root / "reports",
        approvals_root=root / "approvals",
        tokens=TOKENS,
    )
    action = plan_mission_repairs(
        "mission-alpha",
        operations.store,
        operations.queue,
        operations.reports_root,
    )[0]
    proposal = operations.recovery_proposals()[0]
    raw = action.to_dict()
    raw[field] = value
    proposal["recovery_action"] = raw
    proposal["action_id"] = raw["action_id"]
    proposal["mission_id"] = raw["mission_id"]
    proposal["mission_task_id"] = raw["task_id"]
    proposal["action_type"] = raw["proposed_action"]
    proposal["classification"] = raw["classification"]
    proposal["expected_mission_revision"] = raw["revision"]
    proposal["evidence_fingerprint"] = fingerprint(raw)
    proposal["proposed_effect_fingerprint"] = fingerprint(
        {"action": raw["proposed_action"], "queue_id": raw["expected_queue_id"]}
    )
    proposal["proposal_revision"] = proposal_revision(proposal)

    with pytest.raises(APIError):
        operations._verify_recovery_proposal(
            proposal, require_current_action=False
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda decision: decision.__setitem__("unexpected", True),
        lambda decision: decision.__setitem__("decision_time", 7),
        lambda decision: decision.__setitem__("queue_identities", "queue"),
    ],
)
def test_unknown_or_malformed_decision_fields_corrupt_snapshot(
    client,
    operations: ControllerOperations,
    mutate,
):
    proposal = _proposal(client)
    client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": "malformed-decision",
        },
        headers=_auth("approve-token"),
    )
    _rewrite_decision(operations, mutate)
    with pytest.raises(ApprovalEvidenceCorrupt):
        operations.approvals.snapshot()


def test_conflicting_duplicate_decisions_corrupt_snapshot(
    client,
    operations: ControllerOperations,
):
    proposal = _proposal(client)
    client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": "duplicate-decision",
        },
        headers=_auth("approve-token"),
    )
    records = operations.approval_log_path.read_text(encoding="utf-8").splitlines()
    duplicate = json.loads(records[-1])
    duplicate["decision"]["decision"] = "reject"
    with operations.approval_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(duplicate) + "\n")
    with pytest.raises(ApprovalEvidenceCorrupt):
        operations.approvals.snapshot()


def test_approval_append_revision_is_exact_file_digest(tmp_path: Path):
    log = ApprovalLog(tmp_path / "approvals")
    records = [
        {
            "record_type": "proposal",
            "proposal": {
                "schema_version": "controller-proposal-v0.1",
                "action_id": "controller-" + "a" * 64,
                "action_type": "MISSION_START",
                "mission_id": "m",
                "mission_task_id": None,
                "expected_mission_revision": "b" * 64,
                "expected_event_revision": "c" * 64,
                "expected_queue_identities": [],
                "expected_queue_revision": "d" * 64,
                "evidence_fingerprint": "e" * 64,
                "proposed_effect_fingerprint": "f" * 64,
                "finding": "Unicode \u2713",
                "created_at": "2026-08-04T18:00:00+00:00",
                "expires_at": "2026-08-04T18:10:00+00:00",
                "status": "pending",
                "authority": "operator",
                "proposal_revision": "1" * 64,
                "supersedes_action_ids": [],
            },
        }
    ]
    snapshot = log.append(records[0])
    assert snapshot.revision == hashlib.sha256(log.path.read_bytes()).hexdigest()
    assert snapshot.revision == log.snapshot().revision


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b'{"reason":"x"',
        b'{"reason":"a","reason":"b"}',
        b"[]",
        b"null",
        b"{} trailing",
        b"\xff",
    ],
)
def test_mutation_json_is_strict_and_side_effect_free(
    operations: ControllerOperations,
    raw: bytes,
):
    app = _app(operations)
    before = (
        operations.approval_log_path.read_bytes()
        if operations.approval_log_path.exists()
        else None
    )
    response = app.test_client().post(
        "/api/controller/v1/missions/mission-alpha/proposals/start",
        data=raw,
        content_type="application/json",
        headers=_auth("propose-token"),
    )
    after = (
        operations.approval_log_path.read_bytes()
        if operations.approval_log_path.exists()
        else None
    )
    assert response.status_code == 400
    assert response.json["error"]["code"] == "INVALID_REQUEST"
    assert after == before


@pytest.mark.parametrize(
    "query",
    [
        "limit=1&limit=1",
        "status=pending&status=pending",
        "cursor=x&cursor=x",
        "mission_id=mission-alpha&mission_id=mission-alpha",
    ],
)
def test_repeated_single_value_query_parameters_are_rejected(client, query: str):
    response = client.get(
        f"/api/controller/v1/queue?{query}",
        headers=_auth("read-token"),
    )
    assert response.status_code == 400
    assert response.json["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize(
    "configuration",
    [
        {"token": {"principal": "p", "capabilities": {"controller.read": True}}},
        {"token": {"principal": "p", "capabilities": "controller.read"}},
        {"token": {"principal": "p", "capabilities": [True]}},
        {"token": {"principal": "p", "capabilities": ["Controller.Read"]}},
        {"token": {"principal": "p", "capabilities": [" controller.read"]}},
        {"token": {"principal": "p", "capabilities": ["controller.read"] * 2}},
        {"token": {"principal": "p", "capabilities": ["controller.unknown"]}},
        {"token": {"principal": "p", "capabilities": ["*"]}},
        {"token": {"principal": "p", "capabilities": [""]}},
        {"token": {"capabilities": ["controller.read"]}},
        {"token": {"principal": "", "capabilities": ["controller.read"]}},
        {
            "one": {"principal": "same", "capabilities": ["controller.read"]},
            "two": {"principal": "same", "capabilities": ["controller.read"]},
        },
    ],
)
def test_auth_configuration_has_one_strict_schema(
    tmp_path: Path, configuration: dict
):
    with pytest.raises(ValueError):
        ControllerOperations(
            missions_root=tmp_path / "missions",
            queue_root=tmp_path / "queue",
            reports_root=tmp_path / "reports",
            approvals_root=tmp_path / "approvals",
            tokens=configuration,
        )


@pytest.mark.parametrize(
    "raw",
    [
        '{"t":{"principal":"a","capabilities":["controller.read"]},'
        '"t":{"principal":"b","capabilities":["controller.read","controller.apply"]}}',
        '{"t":{"principal":"a","principal":"b","capabilities":["controller.read"]}}',
        "[]",
        "{",
    ],
)
def test_environment_auth_json_rejects_duplicates_and_malformed_roots(
    monkeypatch: pytest.MonkeyPatch, raw: str
):
    monkeypatch.setenv("RAGHUB_CONTROLLER_TOKENS", raw)
    monkeypatch.delenv("RAGHUB_CONTROLLER_TOKEN_FILE", raising=False)
    with pytest.raises((ValueError, json.JSONDecodeError)):
        load_tokens_from_environment()


@pytest.mark.parametrize(
    "address",
    [
        "100.64.10.21",
        "8.8.8.8",
        "192.168.1.2",
        "0.0.0.0",
        "::",
        "localhost",
        " 100.64.10.20",
        "100.64.10.20,127.0.0.1",
        "::ffff:100.64.10.20",
    ],
)
def test_bind_address_requires_exact_configured_tailscale_address(address: str):
    with pytest.raises(ValueError):
        validate_bind_address(address, tailscale_address="100.64.10.20")


def test_exact_configured_tailscale_and_loopback_are_allowed():
    assert (
        validate_bind_address(
            "100.64.10.20", tailscale_address="100.64.10.20"
        )
        == "100.64.10.20"
    )
    assert validate_bind_address("127.0.0.1") == "127.0.0.1"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"event_type": "", "timestamp": "x", "mission_id": "m"},
        {"event_type": "x", "timestamp": "", "mission_id": "m"},
        {"event_type": "x", "timestamp": "not-time", "mission_id": "m"},
        {"event_type": "x", "timestamp": "2026-08-04T18:00:00+00:00"},
        {
            "event_type": "task_succeeded",
            "timestamp": "2026-08-04T18:00:00+00:00",
            "mission_id": "m",
            "metadata": [],
        },
        {
            "event_type": "task_succeeded",
            "timestamp": "2026-08-04T18:00:00+00:00",
            "mission_id": "m",
            "metadata": {},
        },
    ],
)
def test_event_snapshot_rejects_missing_or_malformed_fields(
    tmp_path: Path, payload: dict
):
    event_log = MissionEventLog(tmp_path, "m")
    event_log._log_path.parent.mkdir(parents=True, exist_ok=True)
    event_log._log_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(EventLogCorruptionError):
        event_log.read_snapshot()


def test_recovery_get_is_physically_read_only(tmp_path: Path):
    root = tmp_path / "absent-controller"
    operations = ControllerOperations(
        missions_root=root / "missions",
        queue_root=root / "queue",
        reports_root=root / "reports",
        approvals_root=root / "approvals",
        tokens=TOKENS,
    )
    response = _app(operations).test_client().get(
        "/api/controller/v1/recovery/proposals",
        headers=_auth("read-token"),
    )
    assert response.status_code == 503
    assert response.json["error"]["code"] == "CONTROLLER_UNAVAILABLE"
    assert not root.exists()


def test_recovery_proposal_persists_only_through_authenticated_post(
    tmp_path: Path,
):
    root = tmp_path / "recovery-post"
    store, queue, reports, definition = _create_mission(
        root,
        status=MissionStatus.running,
        task_status=MissionTaskStatus.pending,
    )
    operations = ControllerOperations(
        missions_root=root / "missions",
        queue_root=root / "queue",
        reports_root=reports,
        approvals_root=root / "approvals",
        tokens=TOKENS,
    )
    local_client = _app(operations).test_client()
    discovered = local_client.get(
        "/api/controller/v1/recovery/proposals",
        headers=_auth("read-token"),
    )
    action_id = discovered.json["items"][0]["action_id"]
    assert not operations.approval_log_path.exists()

    persisted = local_client.post(
        f"/api/controller/v1/recovery/proposals/{action_id}",
        json={},
        headers=_auth("propose-token"),
    )

    assert persisted.status_code == 201
    assert persisted.json["action_id"] == action_id
    assert operations.approval_log_path.exists()


def test_unknown_controller_routes_use_stable_json_errors(client):
    missing = client.get(
        "/api/controller/v1/not-a-route", headers=_auth("read-token")
    )
    method = client.put(
        "/api/controller/v1/health",
        json={},
        headers=_auth("read-token"),
    )
    assert missing.status_code == 404
    assert method.status_code == 405
    assert missing.json["error"]["code"] == "NOT_FOUND"
    assert method.json["error"]["code"] == "METHOD_NOT_ALLOWED"


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "https://good.example.evil",
        "http://good.example",
        "https://good.example:444",
        "https://user@good.example",
        "https://good.example.",
        "https://good.example,https://evil.example",
    ],
)
def test_cors_rejects_non_exact_or_malformed_origins(
    operations: ControllerOperations, origin: str
):
    operations.allowed_origins = ("https://good.example",)
    response = _app(operations).test_client().get(
        "/api/controller/v1/health",
        headers={**_auth("read-token"), "Origin": origin},
    )
    assert "Access-Control-Allow-Origin" not in response.headers


def test_configured_null_cors_origin_is_rejected(
    tmp_path: Path,
):
    with pytest.raises(ValueError):
        ControllerOperations(
            missions_root=tmp_path / "missions",
            queue_root=tmp_path / "queue",
            reports_root=tmp_path / "reports",
            approvals_root=tmp_path / "approvals",
            tokens=TOKENS,
            allowed_origins=("null",),
        )


def test_nested_queue_secrets_and_paths_are_redacted(
    client,
    operations: ControllerOperations,
):
    path = operations.queue_root / "pending" / "untrusted.json"
    path.write_text(
        json.dumps(
            {
                "id": "untrusted",
                "status": "pending",
                "metadata": {
                    "mission_id": "mission-alpha",
                    "api_key": "leak",
                    "nested": [
                        {
                            "credentials": "leak",
                            "provider_prompt": "leak",
                            "unknown_private_material": "-----BEGIN OPENSSH PRIVATE KEY-----",
                            "safe_looking": "/home/person/repository/file",
                        }
                    ],
                },
                "provider": {"password": "leak", "prompt": "leak"},
                "report_contents": "leak",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    response = client.get(
        "/api/controller/v1/queue", headers=_auth("read-token")
    )
    encoded = json.dumps(response.json).lower()
    for forbidden in (
        "leak",
        "provider_prompt",
        "report_contents",
        "/home/",
        "openssh private key",
    ):
        assert forbidden not in encoded


@pytest.mark.parametrize(
    "status",
    [
        MissionStatus.failed,
        MissionStatus.cancelled,
        MissionStatus.succeeded,
        MissionStatus.budget_exhausted,
        MissionStatus.paused,
    ],
)
def test_retry_is_rejected_when_mission_cannot_execute(
    tmp_path: Path, status: MissionStatus
):
    root = tmp_path / status.value
    _create_mission(
        root,
        status=status,
        task_status=MissionTaskStatus.failed,
        attempt_count=1,
    )
    operations = ControllerOperations(
        missions_root=root / "missions",
        queue_root=root / "queue",
        reports_root=root / "reports",
        approvals_root=root / "approvals",
        tokens=TOKENS,
    )
    response = _app(operations).test_client().post(
        "/api/controller/v1/missions/mission-alpha/tasks/task-a/proposals/retry",
        json={},
        headers=_auth("propose-token"),
    )
    assert response.status_code == 409
    assert response.json["error"]["code"] == "INVALID_TRANSITION"


def test_event_cursor_is_snapshot_bound_and_numeric(
    client, operations: ControllerOperations
):
    event_log = MissionEventLog(
        operations.missions_root / "events", "mission-alpha"
    )
    for index in range(12):
        event_log.append(make_event(f"fixture_{index}", "mission-alpha"))
    first = client.get(
        "/api/controller/v1/missions/mission-alpha/events?limit=10",
        headers=_auth("read-token"),
    )
    assert first.status_code == 200
    assert [item["event_revision"] for item in first.json["items"]] == list(
        range(1, 11)
    )
    second = client.get(
        "/api/controller/v1/missions/mission-alpha/events"
        f"?limit=10&cursor={first.json['next_cursor']}",
        headers=_auth("read-token"),
    )
    assert [item["event_revision"] for item in second.json["items"]] == [
        11,
        12,
        13,
    ]
    event_log.append(make_event("changed_snapshot", "mission-alpha"))
    stale = client.get(
        "/api/controller/v1/missions/mission-alpha/events"
        f"?limit=10&cursor={first.json['next_cursor']}",
        headers=_auth("read-token"),
    )
    assert stale.status_code == 409
    assert stale.json["error"]["code"] == "CONFLICT"


def _approved_start(client) -> dict:
    proposal = _proposal(client)
    approved = client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": "interruption-approval",
        },
        headers=_auth("approve-token"),
    )
    assert approved.status_code == 200
    return proposal


def test_durable_intent_does_not_bypass_changed_event_evidence(
    client,
    operations: ControllerOperations,
    monkeypatch: pytest.MonkeyPatch,
):
    proposal = _approved_start(client)
    path = f"/api/controller/v1/proposals/{proposal['action_id']}/apply"
    original = operations.scheduler.run_once
    monkeypatch.setattr(
        operations.scheduler,
        "run_once",
        lambda _mission_id: (_ for _ in ()).throw(RuntimeError("interrupt")),
    )
    assert client.post(
        path, json={}, headers=_auth("apply-token")
    ).status_code == 500
    MissionEventLog(
        operations.missions_root / "events", "mission-alpha"
    ).append(make_event("external_change", "mission-alpha"))
    monkeypatch.setattr(operations.scheduler, "run_once", original)

    response = client.post(path, json={}, headers=_auth("apply-token"))

    assert response.status_code == 409
    assert response.json["error"]["code"] == "STALE_PROPOSAL"
    assert (
        operations.store.load_state("mission-alpha").status
        is MissionStatus.pending
    )


def test_durable_intent_does_not_bypass_changed_mission_budget(
    client,
    operations: ControllerOperations,
    monkeypatch: pytest.MonkeyPatch,
):
    proposal = _approved_start(client)
    path = f"/api/controller/v1/proposals/{proposal['action_id']}/apply"
    original = operations.scheduler.run_once
    monkeypatch.setattr(
        operations.scheduler,
        "run_once",
        lambda _mission_id: (_ for _ in ()).throw(RuntimeError("interrupt")),
    )
    assert client.post(
        path, json={}, headers=_auth("apply-token")
    ).status_code == 500
    state = operations.store.load_state("mission-alpha")
    changed = MissionState.from_dict(state.to_dict())
    changed.budget_usage.total_attempts += 1
    operations.store.update_state("mission-alpha", changed)
    monkeypatch.setattr(operations.scheduler, "run_once", original)

    response = client.post(path, json={}, headers=_auth("apply-token"))

    assert response.status_code == 409
    assert response.json["error"]["code"] == "STALE_PROPOSAL"


def test_start_recovers_state_before_queue_interruption(
    client,
    operations: ControllerOperations,
    monkeypatch: pytest.MonkeyPatch,
):
    proposal = _approved_start(client)
    path = f"/api/controller/v1/proposals/{proposal['action_id']}/apply"
    original = operations.scheduler.run_once

    def state_only(mission_id: str):
        state = operations.store.load_state(mission_id)
        operations.store.update_state(
            mission_id,
            MissionState(
                mission_id=state.mission_id,
                status=MissionStatus.running,
                task_states=state.task_states,
                budget_usage=state.budget_usage,
                started_at="2026-08-04T18:00:00+00:00",
                finished_at=state.finished_at,
                paused_at=state.paused_at,
                failure_reason=state.failure_reason,
                root_cause_task_ids=state.root_cause_task_ids,
                events_path=state.events_path,
                report_path=state.report_path,
            ),
        )
        raise RuntimeError("interrupt after state")

    monkeypatch.setattr(operations.scheduler, "run_once", state_only)
    assert client.post(
        path, json={}, headers=_auth("apply-token")
    ).status_code == 500
    monkeypatch.setattr(operations.scheduler, "run_once", original)

    response = client.post(path, json={}, headers=_auth("apply-token"))

    assert response.status_code == 200
    assert response.json["status"] == "APPLIED"
    state = operations.store.load_state("mission-alpha")
    assert state.task_states["task-a"].status is MissionTaskStatus.queued
    assert (
        operations.queue_root
        / "pending"
        / f"{state.task_states['task-a'].queue_task_id}.json"
    ).exists()


def test_start_recovers_queue_before_state_and_linkage_interruption(
    client,
    operations: ControllerOperations,
    monkeypatch: pytest.MonkeyPatch,
):
    proposal = _approved_start(client)
    path = f"/api/controller/v1/proposals/{proposal['action_id']}/apply"
    original = operations.scheduler.run_once

    def queue_only(mission_id: str):
        definition = operations.store.load_definition(mission_id)
        state = operations.store.load_state(mission_id)
        operations.scheduler._materializer.materialize(
            mission_id, definition.tasks[0], state
        )
        raise RuntimeError("interrupt after queue before mission state")

    monkeypatch.setattr(operations.scheduler, "run_once", queue_only)
    assert client.post(path, json={}, headers=_auth("apply-token")).status_code == 500
    monkeypatch.setattr(operations.scheduler, "run_once", original)

    restarted = _app(operations.restart()).test_client()
    response = restarted.post(path, json={}, headers=_auth("apply-token"))

    assert response.status_code == 200, response.json
    assert response.json["status"] == "APPLIED"
    state = operations.store.load_state("mission-alpha")
    assert state.status is MissionStatus.running
    assert state.task_states["task-a"].status is MissionTaskStatus.queued
    assert len(list(operations.queue.pending.glob("*.json"))) == 1


def test_retry_recovers_queue_before_state_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "retry-interruption"
    store, queue, _, _ = _create_mission(
        root,
        status=MissionStatus.running,
        task_status=MissionTaskStatus.failed,
        attempt_count=1,
    )
    operations = ControllerOperations(
        missions_root=root / "missions",
        queue_root=root / "queue",
        reports_root=root / "reports",
        approvals_root=root / "approvals",
        tokens=TOKENS,
    )
    failed_id = operations.scheduler._materializer.make_queue_task_id(
        "mission-alpha", "task-a"
    )
    (queue.failed / f"{failed_id}.json").write_text(
        json.dumps(
            Task(
                id=failed_id,
                title="Fixture task",
                prompt="secret provider prompt",
                base_ref="HEAD",
                tests=[],
                max_attempts=3,
                metadata={
                    "mission_id": "mission-alpha",
                    "mission_task_id": "task-a",
                    "depends_on": [],
                },
            ).to_dict()
        )
        + "\n",
        encoding="utf-8",
    )
    state = store.load_state("mission-alpha")
    state.task_states["task-a"].queue_task_id = failed_id
    store.update_state("mission-alpha", state)
    local_client = _app(operations).test_client()
    proposal = local_client.post(
        "/api/controller/v1/missions/mission-alpha/tasks/task-a/proposals/retry",
        json={},
        headers=_auth("propose-token"),
    ).json
    local_client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": "retry-interruption",
        },
        headers=_auth("approve-token"),
    )
    original = operations.scheduler.retry_task

    def queue_only(
        _mission_id: str,
        _task_id: str,
        *,
        recovery_action_id: str | None = None,
    ):
        (queue.failed / f"{failed_id}.json").replace(
            queue.pending / f"{failed_id}.json"
        )
        payload = json.loads(
            (queue.pending / f"{failed_id}.json").read_text(
                encoding="utf-8"
            )
        )
        payload["metadata"]["mission_attempt"] = 2
        (queue.pending / f"{failed_id}.json").write_text(
            json.dumps(payload) + "\n", encoding="utf-8"
        )
        raise RuntimeError("interrupt after queue")

    monkeypatch.setattr(operations.scheduler, "retry_task", queue_only)
    path = f"/api/controller/v1/proposals/{proposal['action_id']}/apply"
    assert local_client.post(
        path, json={}, headers=_auth("apply-token")
    ).status_code == 500
    monkeypatch.setattr(operations.scheduler, "retry_task", original)

    recovered = local_client.post(
        path, json={}, headers=_auth("apply-token")
    )

    assert recovered.status_code == 200, recovered.json
    assert recovered.json["status"] == "APPLIED"
    assert (
        store.load_state("mission-alpha").task_states["task-a"].status
        is MissionTaskStatus.queued
    )


def test_retry_recovers_move_before_payload_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "retry-move-before-rewrite"
    store, queue, _, _ = _create_mission(
        root,
        status=MissionStatus.running,
        task_status=MissionTaskStatus.failed,
        attempt_count=1,
    )
    operations = ControllerOperations(
        missions_root=root / "missions",
        queue_root=root / "queue",
        reports_root=root / "reports",
        approvals_root=root / "approvals",
        tokens=TOKENS,
    )
    queue_id = operations.scheduler._materializer.make_queue_task_id(
        "mission-alpha", "task-a"
    )
    old_payload = operations.scheduler._retry_payload(
        "mission-alpha",
        operations.store.load_definition("mission-alpha").tasks[0],
        queue_id,
        attempt_number=1,
    )
    (queue.failed / f"{queue_id}.json").write_text(
        json.dumps(old_payload) + "\n", encoding="utf-8"
    )
    state = store.load_state("mission-alpha")
    state.task_states["task-a"].queue_task_id = queue_id
    store.update_state("mission-alpha", state)
    local_client = _app(operations).test_client()
    proposal = local_client.post(
        "/api/controller/v1/missions/mission-alpha/tasks/task-a/proposals/retry",
        json={},
        headers=_auth("propose-token"),
    ).json
    assert local_client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": "move-before-rewrite",
        },
        headers=_auth("approve-token"),
    ).status_code == 200
    original = operations.scheduler.retry_task

    def move_only(
        _mission_id: str,
        _task_id: str,
        *,
        recovery_action_id: str | None = None,
    ):
        (queue.failed / f"{queue_id}.json").replace(
            queue.pending / f"{queue_id}.json"
        )
        raise RuntimeError("interrupt after move")

    monkeypatch.setattr(operations.scheduler, "retry_task", move_only)
    path = f"/api/controller/v1/proposals/{proposal['action_id']}/apply"
    assert local_client.post(
        path, json={}, headers=_auth("apply-token")
    ).status_code == 500
    monkeypatch.setattr(operations.scheduler, "retry_task", original)

    response = _app(operations.restart()).test_client().post(
        path, json={}, headers=_auth("apply-token")
    )
    assert response.status_code == 200, response.json
    payload = json.loads(
        (queue.pending / f"{queue_id}.json").read_text(encoding="utf-8")
    )
    assert payload["metadata"]["mission_attempt"] == 2


def test_historical_retry_event_never_suppresses_current_retry_occurrence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "historical-retry"
    store, queue, _, _ = _create_mission(
        root,
        status=MissionStatus.running,
        task_status=MissionTaskStatus.failed,
        attempt_count=2,
    )
    operations = ControllerOperations(
        missions_root=root / "missions",
        queue_root=root / "queue",
        reports_root=root / "reports",
        approvals_root=root / "approvals",
        tokens=TOKENS,
    )
    queue_id = operations.scheduler._materializer.make_queue_task_id(
        "mission-alpha", "task-a"
    )
    task_def = operations.store.load_definition("mission-alpha").tasks[0]
    (queue.failed / f"{queue_id}.json").write_text(
        json.dumps(
            operations.scheduler._retry_payload(
                "mission-alpha", task_def, queue_id, attempt_number=2
            )
        )
        + "\n",
        encoding="utf-8",
    )
    state = store.load_state("mission-alpha")
    state.task_states["task-a"].queue_task_id = queue_id
    store.update_state("mission-alpha", state)
    MissionEventLog(root / "missions" / "events", "mission-alpha").append(
        make_event(
            "task_retry_queued",
            "mission-alpha",
            task_id="task-a",
            queue_task_id=queue_id,
            metadata={
                "attempt_number": 2,
                "controller_action_id": "prior-retry",
            },
        )
    )
    local_client = _app(operations).test_client()
    proposal = local_client.post(
        "/api/controller/v1/missions/mission-alpha/tasks/task-a/proposals/retry",
        json={},
        headers=_auth("propose-token"),
    ).json
    local_client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": "later-retry",
        },
        headers=_auth("approve-token"),
    )
    original = operations.scheduler.retry_task

    def effect_without_event(
        mission_id: str,
        task_id: str,
        *,
        recovery_action_id: str | None = None,
    ):
        original(
            mission_id,
            task_id,
            recovery_action_id=recovery_action_id,
        )
        log = MissionEventLog(root / "missions" / "events", mission_id)
        lines = log._log_path.read_text(encoding="utf-8").splitlines()
        log._log_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        raise RuntimeError("interrupt before retry event")

    monkeypatch.setattr(operations.scheduler, "retry_task", effect_without_event)
    path = f"/api/controller/v1/proposals/{proposal['action_id']}/apply"
    assert local_client.post(
        path, json={}, headers=_auth("apply-token")
    ).status_code == 500
    monkeypatch.setattr(operations.scheduler, "retry_task", original)

    response = _app(operations.restart()).test_client().post(
        path, json={}, headers=_auth("apply-token")
    )
    assert response.status_code == 200, response.json
    current = [
        event
        for event in MissionEventLog(
            root / "missions" / "events", "mission-alpha"
        ).read_all()
        if event.event_type == "task_retry_queued"
        and event.metadata.get("attempt_number") == 3
    ]
    assert len(current) == 1
    assert current[0].metadata["controller_action_id"] == proposal["action_id"]


def test_three_retry_occurrences_remain_distinct_across_restarts(
    tmp_path: Path,
):
    root = tmp_path / "three-retries"
    store, queue, _, _ = _create_mission(
        root,
        status=MissionStatus.running,
        task_status=MissionTaskStatus.failed,
        attempt_count=0,
    )
    operations = ControllerOperations(
        missions_root=root / "missions",
        queue_root=root / "queue",
        reports_root=root / "reports",
        approvals_root=root / "approvals",
        tokens=TOKENS,
    )
    action_ids: list[str] = []
    queue_id = operations.scheduler._materializer.make_queue_task_id(
        "mission-alpha", "task-a"
    )

    for attempt in (1, 2, 3):
        current = operations.restart()
        local_client = _app(current).test_client()
        proposal = local_client.post(
            "/api/controller/v1/missions/mission-alpha/tasks/task-a/proposals/retry",
            json={},
            headers=_auth("propose-token"),
        ).json
        action_ids.append(proposal["action_id"])
        assert local_client.post(
            f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
            json={
                "expected_proposal_revision": proposal["proposal_revision"],
                "idempotency_key": f"retry-cycle-{attempt}",
            },
            headers=_auth("approve-token"),
        ).status_code == 200
        applied = local_client.post(
            f"/api/controller/v1/proposals/{proposal['action_id']}/apply",
            json={},
            headers=_auth("apply-token"),
        )
        assert applied.status_code == 200, applied.json
        if attempt == 3:
            break
        (queue.pending / f"{queue_id}.json").replace(
            queue.failed / f"{queue_id}.json"
        )
        state = store.load_state("mission-alpha")
        prior = state.task_states["task-a"]
        state.task_states["task-a"] = MissionTaskState(
            task_id=prior.task_id,
            status=MissionTaskStatus.failed,
            queue_task_id=queue_id,
            attempt_count=attempt,
            failure_reason=f"attempt {attempt} failed",
        )
        state.budget_usage.total_attempts = attempt
        store.update_state("mission-alpha", state)

    retry_events = [
        event
        for event in MissionEventLog(
            root / "missions" / "events", "mission-alpha"
        ).read_all()
        if event.event_type == "task_retry_queued"
    ]
    assert [event.metadata["attempt_number"] for event in retry_events] == [
        1,
        2,
        3,
    ]
    assert [
        event.metadata["controller_action_id"] for event in retry_events
    ] == action_ids
    assert len(set(action_ids)) == 3


@pytest.mark.parametrize(
    "sentinel",
    [
        "Bearer fake-access-value",
        "api_key=fake-key",
        "PASSWORD=fake-password",
        "provider system prompt: do private work",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "/home/operator/private/repository",
        "https://user:password@example.test/resource",
        "report contents: private output",
    ],
)
@pytest.mark.parametrize(
    "innocent_key",
    [
        "name",
        "label",
        "description",
        "value",
        "note",
        "reason",
        "detail",
        "message",
        "status_text",
    ],
)
def test_public_serialization_redacts_sensitive_values_under_innocent_keys(
    sentinel: str,
    innocent_key: str,
):
    payload = {
        "metadata": {
            innocent_key: {
                "nested": [sentinel, {"again": sentinel}],
            }
        }
    }
    assert sentinel not in json.dumps(public_value(payload))


def test_public_task_state_map_preserves_safe_dynamic_task_ids():
    payload = {
        "task_states": {
            "task-a": {
                "task_id": "task-a",
                "status": "failed",
                "queue_task_id": "queue-a",
                "attempt_count": 2,
                "failure_reason": "bounded blocker",
                "report_path": "/private/report.json",
                "test_results": [{"status": "failed", "message": "safe result"}],
            },
            "task-b": {
                "task_id": "task-b",
                "status": "blocked",
                "queue_task_id": None,
                "attempt_count": 0,
                "failure_reason": "waiting for task-a",
            },
        }
    }

    result = public_value(payload)

    assert set(result["task_states"]) == {"task-a", "task-b"}
    assert result["task_states"]["task-a"]["queue_task_id"] == "queue-a"
    assert result["task_states"]["task-a"]["attempt_count"] == 2
    assert "report_path" not in result["task_states"]["task-a"]


@pytest.mark.parametrize(
    "key",
    [
        " padded",
        "padded ",
        "\tpadded",
        "padded\n",
        "\u2003padded",
        "padded\u2003",
        " \t\n",
        "x" * 201,
    ],
)
@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_invalid_idempotency_keys_fail_before_durable_access(
    client,
    operations: ControllerOperations,
    key: str,
    decision: str,
):
    proposal = _proposal(client)
    before = operations.approval_log_path.read_bytes()
    response = client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/{decision}",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": key,
        },
        headers=_auth("approve-token"),
    )

    assert response.status_code == 400
    assert response.json["error"]["code"] == "INVALID_REQUEST"
    assert operations.approval_log_path.read_bytes() == before


@pytest.mark.parametrize(
    "duplicate_state",
    [None, "invalid"],
)
def test_retry_rejects_foreign_and_duplicate_queue_locations(
    tmp_path: Path,
    duplicate_state: str | None,
):
    root = tmp_path / "retry-ownership"
    store, queue, reports, _ = _create_mission(
        root,
        status=MissionStatus.running,
        task_status=MissionTaskStatus.failed,
        attempt_count=1,
    )
    operations = ControllerOperations(
        missions_root=root / "missions",
        queue_root=root / "queue",
        reports_root=reports,
        approvals_root=root / "approvals",
        tokens=TOKENS,
    )
    queue_id = operations.scheduler._materializer.make_queue_task_id(
        "mission-alpha", "task-a"
    )
    payload = Task(
        id=queue_id,
        title="Fixture task",
        prompt="secret provider prompt",
        base_ref="HEAD",
        tests=[],
        max_attempts=3,
        metadata={
            "mission_id": (
                "mission-alpha" if duplicate_state else "foreign-mission"
            ),
            "mission_task_id": "task-a",
            "depends_on": [],
        },
    ).to_dict()
    (queue.failed / f"{queue_id}.json").write_text(
        json.dumps(payload) + "\n", encoding="utf-8"
    )
    if duplicate_state:
        (getattr(queue, duplicate_state) / f"{queue_id}.json").write_text(
            json.dumps(payload) + "\n", encoding="utf-8"
        )
    state = store.load_state("mission-alpha")
    state.task_states["task-a"].queue_task_id = queue_id
    store.update_state("mission-alpha", state)

    response = _app(operations).test_client().post(
        "/api/controller/v1/missions/mission-alpha/tasks/task-a/proposals/retry",
        json={},
        headers=_auth("propose-token"),
    )

    assert response.status_code == 409
    assert response.json["error"]["code"] in {
        "HUMAN_REVIEW_REQUIRED",
        "INVALID_TRANSITION",
    }
    assert (queue.failed / f"{queue_id}.json").exists()
    if duplicate_state:
        assert (getattr(queue, duplicate_state) / f"{queue_id}.json").exists()


@pytest.mark.parametrize("with_terminal_queue", [True, False])
def test_retry_archives_old_report_and_requires_matching_attempt(
    tmp_path: Path,
    with_terminal_queue: bool,
):
    root = tmp_path / "retry-report-attempt"
    store, queue, reports, _ = _create_mission(
        root,
        status=MissionStatus.running,
        task_status=MissionTaskStatus.failed,
        attempt_count=1,
    )
    operations = ControllerOperations(
        missions_root=root / "missions",
        queue_root=root / "queue",
        reports_root=reports,
        approvals_root=root / "approvals",
        tokens=TOKENS,
    )
    queue_id = operations.scheduler._materializer.make_queue_task_id(
        "mission-alpha", "task-a"
    )
    payload = Task(
        id=queue_id,
        title="Fixture task",
        prompt="secret provider prompt",
        base_ref="HEAD",
        tests=[],
        max_attempts=3,
        metadata={
            "mission_id": "mission-alpha",
            "mission_task_id": "task-a",
            "depends_on": [],
        },
    ).to_dict()
    if with_terminal_queue:
        (queue.failed / f"{queue_id}.json").write_text(
            json.dumps(payload) + "\n", encoding="utf-8"
        )
    (reports / f"{queue_id}.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "task_id": queue_id,
                "mission_id": "mission-alpha",
                "mission_task_id": "task-a",
                "mission_attempt": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = store.load_state("mission-alpha")
    state.task_states["task-a"].queue_task_id = (
        queue_id if with_terminal_queue else None
    )
    store.update_state("mission-alpha", state)
    local_client = _app(operations).test_client()
    proposal = local_client.post(
        "/api/controller/v1/missions/mission-alpha/tasks/task-a/proposals/retry",
        json={},
        headers=_auth("propose-token"),
    ).json
    approved = local_client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": "retry-report-binding",
        },
        headers=_auth("approve-token"),
    )
    assert approved.status_code == 200
    applied = local_client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/apply",
        json={},
        headers=_auth("apply-token"),
    )

    assert applied.status_code == 200, applied.json
    operations.scheduler.run_once("mission-alpha")
    task = store.load_state("mission-alpha").task_states["task-a"]
    assert task.status is MissionTaskStatus.queued
    assert not (reports / f"{queue_id}.json").exists()
    assert (reports / "history" / f"{queue_id}.attempt-1.json").exists()


def test_recovery_cursor_uses_only_durable_snapshot_evidence(tmp_path: Path):
    root = tmp_path / "recovery-cursor"
    _create_mission(
        root,
        status=MissionStatus.running,
        task_status=MissionTaskStatus.pending,
    )
    _create_mission(
        root,
        mission_id="mission-beta",
        status=MissionStatus.running,
        task_status=MissionTaskStatus.pending,
    )
    current = [datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc)]
    operations = ControllerOperations(
        missions_root=root / "missions",
        queue_root=root / "queue",
        reports_root=root / "reports",
        approvals_root=root / "approvals",
        tokens=TOKENS,
        now=lambda: current[0],
    )
    local_client = _app(operations).test_client()
    first = local_client.get(
        "/api/controller/v1/recovery/proposals?limit=1",
        headers=_auth("read-token"),
    )
    current[0] = datetime(2026, 8, 4, 19, 0, tzinfo=timezone.utc)
    second = local_client.get(
        "/api/controller/v1/recovery/proposals"
        f"?limit=1&cursor={first.json['next_cursor']}",
        headers=_auth("read-token"),
    )

    assert first.status_code == 200
    assert second.status_code == 200


def test_public_read_responses_keep_safe_task_data_without_private_values(
    client,
    operations: ControllerOperations,
):
    definition, state, _ = operations._mission_snapshot("mission-alpha")
    definition.tasks[0].depends_on = ["safe-dependency"]
    definition.tasks[0].metadata = {
        "safe_note": "visible to operators",
        "api_token": "must-not-leak",
        "apiKey": "also-must-not-leak",
        "nested": {
            "safe_flag": True,
            "private_path": "/home/private",
            "windows_location": r"C:\Users\private\repository",
        },
    }
    operations.store.update_state("mission-alpha", state)
    mission_path = operations.store.find_mission("mission-alpha")[1]
    envelope = json.loads(mission_path.read_text(encoding="utf-8"))
    envelope["definition"] = definition.to_dict()
    mission_path.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
    queue_id = operations.scheduler._materializer.make_queue_task_id(
        "mission-alpha", "task-a"
    )
    (operations.queue_root / "pending" / f"{queue_id}.json").write_text(
        json.dumps(
            Task(
                id=queue_id,
                title="Fixture task",
                prompt="secret provider prompt",
                base_ref="HEAD",
                tests=[],
                metadata={
                    "mission_id": "mission-alpha",
                    "mission_task_id": "task-a",
                    "safe_note": "visible to operators",
                    "api_token": "must-not-leak",
                    "apiKey": "also-must-not-leak",
                    "nested": {
                        "safe_flag": True,
                        "private_path": "/home/private",
                        "windows_location": r"C:\Users\private\repository",
                    },
                },
            ).to_dict()
        )
        + "\n",
        encoding="utf-8",
    )

    endpoints = [
        "/api/controller/v1/health",
        "/api/controller/v1/overview",
        "/api/controller/v1/missions",
        "/api/controller/v1/missions/mission-alpha",
        "/api/controller/v1/missions/mission-alpha/events",
        "/api/controller/v1/queue",
        "/api/controller/v1/recovery/proposals",
        "/api/controller/v1/proposals",
    ]
    responses = [
        client.get(endpoint, headers=_auth("read-token")) for endpoint in endpoints
    ]
    assert all(response.status_code == 200 for response in responses)
    mission = responses[3].json
    queue = responses[5].json
    assert mission["definition"]["tasks"][0]["depends_on"] == [
        "safe-dependency"
    ]
    assert mission["definition"]["tasks"][0]["metadata"]["safe_note"] == (
        "visible to operators"
    )
    assert queue["items"][0]["record"]["metadata"]["safe_note"] == (
        "visible to operators"
    )
    encoded = json.dumps([response.json for response in responses]).lower()
    assert "must-not-leak" not in encoded
    assert "also-must-not-leak" not in encoded
    assert "/home/private" not in encoded
    assert r"c:\users\private" not in encoded


def test_valid_cors_preflight_bypasses_auth_without_mutating_state(
    operations: ControllerOperations,
):
    operations.allowed_origins = ("https://good.example",)
    app = _app(operations)
    before = [
        path.read_bytes()
        for path in sorted(operations.controller_root.rglob("*"))
        if path.is_file()
    ]
    preflight = app.test_client().open(
        "/api/controller/v1/missions/mission-alpha/proposals/start",
        method="OPTIONS",
        headers={
            "Origin": "https://good.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )
    after = [
        path.read_bytes()
        for path in sorted(operations.controller_root.rglob("*"))
        if path.is_file()
    ]
    unauthenticated = app.test_client().post(
        "/api/controller/v1/missions/mission-alpha/proposals/start",
        json={},
    )

    assert preflight.status_code == 204
    assert preflight.headers["Access-Control-Allow-Origin"] == "https://good.example"
    assert preflight.headers["Access-Control-Allow-Methods"] == "POST"
    assert preflight.headers["Access-Control-Allow-Headers"] == (
        "authorization, content-type"
    )
    assert after == before
    assert unauthenticated.status_code == 401


def test_invalid_event_is_never_written_and_non_finite_json_is_rejected(
    tmp_path: Path,
):
    event_log = MissionEventLog(tmp_path / "events", "mission-alpha")
    with pytest.raises(EventLogCorruptionError):
        event_log.append(
            make_event(
                "external_evidence",
                "mission-alpha",
                metadata={"non_finite": float("nan")},
            )
        )
    assert not event_log._log_path.exists()


def _approve_and_apply(client, proposal: dict, key: str):
    approved = client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": key,
        },
        headers=_auth("approve-token"),
    )
    assert approved.status_code == 200, approved.json
    return client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/apply",
        json={},
        headers=_auth("apply-token"),
    )


def test_lifecycle_effects_bind_one_occurrence_for_each_repeated_cycle(
    tmp_path: Path,
):
    root = tmp_path / "repeated-lifecycle"
    _create_mission(root, status=MissionStatus.running)
    operations = ControllerOperations(
        missions_root=root / "missions",
        queue_root=root / "queue",
        reports_root=root / "reports",
        approvals_root=root / "approvals",
        tokens=TOKENS,
    )
    local_client = _app(operations).test_client()
    first_pause = local_client.post(
        "/api/controller/v1/missions/mission-alpha/proposals/pause",
        json={},
        headers=_auth("propose-token"),
    ).json
    assert _approve_and_apply(local_client, first_pause, "cycle-pause-1").status_code == 200
    resume = local_client.post(
        "/api/controller/v1/missions/mission-alpha/proposals/resume",
        json={},
        headers=_auth("propose-token"),
    ).json
    assert _approve_and_apply(local_client, resume, "cycle-resume").status_code == 200
    second_pause = local_client.post(
        "/api/controller/v1/missions/mission-alpha/proposals/pause",
        json={},
        headers=_auth("propose-token"),
    ).json
    response = _approve_and_apply(
        local_client, second_pause, "cycle-pause-2"
    )

    assert response.status_code == 200, response.json
    events = MissionEventLog(
        operations.missions_root / "events", "mission-alpha"
    ).read_all()
    assert sum(event.event_type == "mission_paused" for event in events) == 2


def test_resume_recovers_state_and_queue_before_task_audit_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "resume-boundaries"
    store, queue, reports, _ = _create_mission(
        root, status=MissionStatus.paused
    )
    operations = ControllerOperations(
        missions_root=root / "missions",
        queue_root=root / "queue",
        reports_root=reports,
        approvals_root=root / "approvals",
        tokens=TOKENS,
    )
    local_client = _app(operations).test_client()
    proposal = local_client.post(
        "/api/controller/v1/missions/mission-alpha/proposals/resume",
        json={},
        headers=_auth("propose-token"),
    ).json
    approved = local_client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": "resume-state-queue",
        },
        headers=_auth("approve-token"),
    )
    assert approved.status_code == 200
    original = operations.scheduler.run_once

    def persist_queue_without_task_event(mission_id: str):
        state = store.load_state(mission_id)
        task = next(
            item
            for item in store.load_definition(mission_id).tasks
            if item.task_id == "task-a"
        )
        queue_id = operations.scheduler._materializer.materialize(
            mission_id, task, state
        )
        prior = state.task_states["task-a"]
        state.task_states["task-a"] = MissionTaskState(
            task_id="task-a",
            status=MissionTaskStatus.queued,
            queue_task_id=queue_id,
            attempt_count=prior.attempt_count,
            queued_at="2026-08-04T18:00:00+00:00",
        )
        store.update_state(mission_id, state)
        raise RuntimeError("interrupt after queue and state")

    monkeypatch.setattr(
        operations.scheduler, "run_once", persist_queue_without_task_event
    )
    path = f"/api/controller/v1/proposals/{proposal['action_id']}/apply"
    assert local_client.post(
        path, json={}, headers=_auth("apply-token")
    ).status_code == 500
    monkeypatch.setattr(operations.scheduler, "run_once", original)

    recovered = local_client.post(
        path, json={}, headers=_auth("apply-token")
    )
    assert recovered.status_code == 200, recovered.json
    events = MissionEventLog(
        operations.missions_root / "events", "mission-alpha"
    ).read_all()
    queue_id = store.load_state("mission-alpha").task_states[
        "task-a"
    ].queue_task_id
    assert sum(
        event.event_type == "task_enqueued"
        and event.queue_task_id == queue_id
        for event in events
    ) == 1


@pytest.mark.parametrize(
    ("status", "path", "task_status"),
    [
        (
            MissionStatus.pending,
            "/api/controller/v1/missions/mission-alpha/proposals/start",
            MissionTaskStatus.pending,
        ),
        (
            MissionStatus.running,
            "/api/controller/v1/missions/mission-alpha/proposals/pause",
            MissionTaskStatus.pending,
        ),
        (
            MissionStatus.paused,
            "/api/controller/v1/missions/mission-alpha/proposals/resume",
            MissionTaskStatus.pending,
        ),
        (
            MissionStatus.running,
            "/api/controller/v1/missions/mission-alpha/proposals/cancel",
            MissionTaskStatus.pending,
        ),
        (
            MissionStatus.running,
            "/api/controller/v1/missions/mission-alpha/tasks/task-a/proposals/retry",
            MissionTaskStatus.failed,
        ),
    ],
)
def test_every_lifecycle_action_rejects_authority_substitution(
    tmp_path: Path,
    status: MissionStatus,
    path: str,
    task_status: MissionTaskStatus,
):
    root = tmp_path / path.rsplit("/", 1)[-1]
    _create_mission(root, status=status, task_status=task_status)
    operations = ControllerOperations(
        missions_root=root / "missions",
        queue_root=root / "queue",
        reports_root=root / "reports",
        approvals_root=root / "approvals",
        tokens=TOKENS,
    )
    local_client = _app(operations).test_client()
    proposal = local_client.post(
        path, json={}, headers=_auth("propose-token")
    ).json
    altered = _rewrite_proposal(
        operations,
        lambda item: item.__setitem__("authority", "substituted-principal"),
    )
    response = local_client.post(
        f"/api/controller/v1/proposals/{altered['action_id']}/approve",
        json={
            "expected_proposal_revision": altered["proposal_revision"],
            "idempotency_key": f"authority-{status.value}-{task_status.value}",
        },
        headers=_auth("approve-token"),
    )

    assert response.status_code == 409
    assert response.json["error"]["code"] == "HUMAN_REVIEW_REQUIRED"


@pytest.mark.parametrize("other_identity", ["legacy", "current"])
@pytest.mark.parametrize("location", ["pending", "running", "succeeded", "invalid"])
def test_retry_rejects_current_legacy_duplicate_locations(
    tmp_path: Path,
    other_identity: str,
    location: str,
):
    root = tmp_path / f"queue-location-{other_identity}-{location}"
    store, queue, reports, _ = _create_mission(
        root,
        status=MissionStatus.running,
        task_status=MissionTaskStatus.failed,
        attempt_count=1,
    )
    operations = ControllerOperations(
        missions_root=root / "missions",
        queue_root=root / "queue",
        reports_root=reports,
        approvals_root=root / "approvals",
        tokens=TOKENS,
    )
    current = operations.scheduler._materializer.make_queue_task_id(
        "mission-alpha", "task-a"
    )
    legacy = operations.scheduler._materializer.make_queue_task_id(
        "mission-alpha", "task-a"
    )
    if other_identity == "legacy":
        from tools.ai_controller.mission.materializer import (
            make_legacy_queue_task_id,
        )

        legacy = make_legacy_queue_task_id("mission-alpha", "task-a")
    payload = lambda queue_id: Task(
        id=queue_id,
        title="Fixture task",
        prompt="secret provider prompt",
        base_ref="HEAD",
        tests=[],
        max_attempts=3,
        metadata={
            "mission_id": "mission-alpha",
            "mission_task_id": "task-a",
            "depends_on": [],
        },
    ).to_dict()
    (queue.failed / f"{current}.json").write_text(
        json.dumps(payload(current)) + "\n", encoding="utf-8"
    )
    (getattr(queue, location) / f"{legacy}.json").write_text(
        json.dumps(payload(legacy)) + "\n", encoding="utf-8"
    )
    state = store.load_state("mission-alpha")
    state.task_states["task-a"].queue_task_id = current
    store.update_state("mission-alpha", state)

    response = _app(operations).test_client().post(
        "/api/controller/v1/missions/mission-alpha/tasks/task-a/proposals/retry",
        json={},
        headers=_auth("propose-token"),
    )
    assert response.status_code == 409
    assert response.json["error"]["code"] in {
        "HUMAN_REVIEW_REQUIRED",
        "INVALID_TRANSITION",
    }


def test_recovery_application_replays_only_existing_repair_authority(
    tmp_path: Path,
):
    root = tmp_path / "recovery-authority"
    _create_mission(
        root,
        status=MissionStatus.running,
        task_status=MissionTaskStatus.pending,
    )
    operations = ControllerOperations(
        missions_root=root / "missions",
        queue_root=root / "queue",
        reports_root=root / "reports",
        approvals_root=root / "approvals",
        tokens=TOKENS,
    )
    local_client = _app(operations).test_client()
    discovered = local_client.get(
        "/api/controller/v1/recovery/proposals",
        headers=_auth("read-token"),
    ).json["items"][0]
    persisted = local_client.post(
        f"/api/controller/v1/recovery/proposals/{discovered['action_id']}",
        json={},
        headers=_auth("propose-token"),
    ).json
    approved = local_client.post(
        f"/api/controller/v1/proposals/{persisted['action_id']}/approve",
        json={
            "expected_proposal_revision": persisted["proposal_revision"],
            "idempotency_key": "recovery-existing-authority",
        },
        headers=_auth("approve-token"),
    )
    assert approved.status_code == 200, approved.json
    first = local_client.post(
        f"/api/controller/v1/proposals/{persisted['action_id']}/apply",
        json={},
        headers=_auth("apply-token"),
    )
    second = local_client.post(
        f"/api/controller/v1/proposals/{persisted['action_id']}/apply",
        json={},
        headers=_auth("apply-token"),
    )

    assert first.status_code == 200, first.json
    assert second.status_code == 200, second.json
    events = MissionEventLog(
        operations.missions_root / "events", "mission-alpha"
    ).read_all()
    assert sum(event.event_type == "repair_applied" for event in events) == 1
