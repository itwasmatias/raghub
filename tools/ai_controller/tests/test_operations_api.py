from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tools.ai_controller.mission.events import MissionEventLog, make_event
from tools.ai_controller.mission.models import (
    BudgetUsage,
    MissionBudget,
    MissionDefinition,
    MissionState,
    MissionStatus,
    MissionTaskDefinition,
    MissionTaskState,
    MissionTaskStatus,
)
from tools.ai_controller.mission.store import MissionStore
from tools.ai_controller.mission.repair import plan_mission_repairs
from tools.ai_controller.operations_api import (
    ControllerOperations,
    create_operations_blueprint,
    validate_bind_address,
)
from tools.ai_controller.queue import DurableQueue


TOKENS = {
    "read-token": {
        "principal": "reader",
        "capabilities": ["controller.read"],
    },
    "propose-token": {
        "principal": "operator",
        "capabilities": ["controller.read", "controller.propose"],
    },
    "approve-token": {
        "principal": "approver",
        "capabilities": ["controller.read", "controller.approve"],
    },
    "apply-token": {
        "principal": "applier",
        "capabilities": ["controller.read", "controller.apply"],
    },
}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_mission(
    root: Path,
    *,
    mission_id: str = "mission-alpha",
    status: MissionStatus = MissionStatus.pending,
    task_status: MissionTaskStatus = MissionTaskStatus.pending,
    attempt_count: int = 0,
    max_attempts: int = 3,
) -> tuple[MissionStore, DurableQueue, Path, MissionDefinition]:
    missions = root / "missions"
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    store = MissionStore(missions)
    queue = DurableQueue(root / "queue")
    definition = MissionDefinition(
        mission_id=mission_id,
        title="Fixture mission",
        description="Offline fixture",
        repository_path="/private/host/repository",
        tasks=[
            MissionTaskDefinition(
                task_id="task-a",
                title="Fixture task",
                prompt="secret provider prompt",
                max_attempts=max_attempts,
            )
        ],
        budgets=MissionBudget(max_total_attempts=5),
        created_at="2026-08-04T12:00:00+00:00",
    )
    state = MissionState(
        mission_id=mission_id,
        status=status,
        task_states={
            "task-a": MissionTaskState(
                task_id="task-a",
                status=task_status,
                attempt_count=attempt_count,
            )
        },
        budget_usage=BudgetUsage(total_attempts=attempt_count),
        started_at=(
            "2026-08-04T12:00:00+00:00"
            if status is not MissionStatus.pending
            else None
        ),
    )
    store.create(definition, state)
    MissionEventLog(missions / "events", mission_id).append(
        make_event("mission_created", mission_id)
    )
    return store, queue, reports, definition


@pytest.fixture
def operations(tmp_path: Path) -> ControllerOperations:
    _create_mission(tmp_path / "controller")
    return ControllerOperations(
        missions_root=tmp_path / "controller" / "missions",
        queue_root=tmp_path / "controller" / "queue",
        reports_root=tmp_path / "controller" / "reports",
        approvals_root=tmp_path / "controller" / "approvals",
        tokens=TOKENS,
        proposal_ttl_seconds=600,
        now=lambda: datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def client(operations: ControllerOperations):
    from flask import Flask

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(create_operations_blueprint(operations))
    return app.test_client()


def test_all_controller_routes_require_bearer_authentication(client):
    response = client.get("/api/controller/v1/health")
    assert response.status_code == 401
    assert response.json["error"]["code"] == "UNAUTHENTICATED"
    assert "correlation_id" in response.json["error"]


def test_query_string_token_is_rejected(client):
    response = client.get("/api/controller/v1/health?token=read-token")
    assert response.status_code == 401


def test_read_capability_cannot_propose_approve_or_apply(client):
    paths = [
        "/api/controller/v1/missions/mission-alpha/proposals/start",
        "/api/controller/v1/proposals/not-found/approve",
        "/api/controller/v1/proposals/not-found/apply",
    ]
    for path in paths:
        response = client.post(path, json={}, headers=_auth("read-token"))
        assert response.status_code == 403
        assert response.json["error"]["code"] == "FORBIDDEN"


def test_health_and_overview_are_bounded_and_path_safe(client):
    health = client.get(
        "/api/controller/v1/health", headers=_auth("read-token")
    )
    overview = client.get(
        "/api/controller/v1/overview", headers=_auth("read-token")
    )
    assert health.status_code == 200
    assert health.json["service"] == "raghub-controller-operations-room"
    assert health.json["api_version"] == "v1"
    assert health.json["capabilities"] == {
        "read": True,
        "approvals": False,
    }
    assert overview.json["mission_counts"]["pending"] == 1
    assert "/private/host" not in json.dumps([health.json, overview.json])


def test_mission_listing_is_deterministic_filtered_and_bounded(
    client, operations: ControllerOperations
):
    _create_mission(
        operations.controller_root,
        mission_id="mission-beta",
        status=MissionStatus.paused,
    )
    response = client.get(
        "/api/controller/v1/missions?status=pending&limit=1",
        headers=_auth("read-token"),
    )
    assert response.status_code == 200
    assert [item["mission_id"] for item in response.json["items"]] == [
        "mission-alpha"
    ]
    assert len(response.json["items"]) <= 1

    too_large = client.get(
        "/api/controller/v1/missions?limit=10000",
        headers=_auth("read-token"),
    )
    assert too_large.status_code == 400
    assert too_large.json["error"]["code"] == "INVALID_REQUEST"


def test_mission_prefix_must_be_unambiguous(
    client, operations: ControllerOperations
):
    _create_mission(operations.controller_root, mission_id="mission-alpine")
    response = client.get(
        "/api/controller/v1/missions/mission-al",
        headers=_auth("read-token"),
    )
    assert response.status_code == 409
    assert response.json["error"]["code"] == "CONFLICT"


def test_mission_detail_does_not_expose_paths_prompts_or_secrets(client):
    response = client.get(
        "/api/controller/v1/missions/mission-alpha",
        headers=_auth("read-token"),
    )
    encoded = json.dumps(response.json)
    assert response.status_code == 200
    assert response.json["definition"]["mission_id"] == "mission-alpha"
    assert "secret provider prompt" not in encoded
    assert "/private/host/repository" not in encoded
    assert response.json["state_revision"]


def test_events_are_exact_revisioned_and_corrupt_tails_fail_closed(
    client, operations: ControllerOperations
):
    response = client.get(
        "/api/controller/v1/missions/mission-alpha/events",
        headers=_auth("read-token"),
    )
    assert response.status_code == 200
    assert response.json["event_revision"]
    assert {
        event["mission_id"] for event in response.json["items"]
    } == {"mission-alpha"}

    event_path = (
        operations.missions_root / "events" / "mission-alpha.jsonl"
    )
    with event_path.open("ab") as handle:
        handle.write(b'{"broken":')
    corrupt = client.get(
        "/api/controller/v1/missions/mission-alpha/events",
        headers=_auth("read-token"),
    )
    assert corrupt.status_code == 409
    assert corrupt.json["error"]["code"] == "CORRUPT_EVIDENCE"


@pytest.mark.parametrize(
    ("status", "suffix", "action_type"),
    [
        (MissionStatus.pending, "start", "MISSION_START"),
        (MissionStatus.running, "pause", "MISSION_PAUSE"),
        (MissionStatus.paused, "resume", "MISSION_RESUME"),
        (MissionStatus.running, "cancel", "MISSION_CANCEL"),
    ],
)
def test_lifecycle_proposals_are_deterministic_and_do_not_mutate(
    tmp_path: Path,
    status: MissionStatus,
    suffix: str,
    action_type: str,
):
    controller = tmp_path / suffix
    store, queue, reports, definition = _create_mission(
        controller, status=status
    )
    operations = ControllerOperations(
        missions_root=controller / "missions",
        queue_root=controller / "queue",
        reports_root=reports,
        approvals_root=controller / "approvals",
        tokens=TOKENS,
        now=lambda: datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc),
    )
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(create_operations_blueprint(operations))
    local_client = app.test_client()
    path = (
        f"/api/controller/v1/missions/{definition.mission_id}"
        f"/proposals/{suffix}"
    )
    first = local_client.post(
        path, json={"reason": "operator request"}, headers=_auth("propose-token")
    )
    second = local_client.post(
        path, json={"reason": "operator request"}, headers=_auth("propose-token")
    )
    assert first.status_code == 201
    assert first.json["action_type"] == action_type
    assert second.json["action_id"] == first.json["action_id"]
    assert store.load_state(definition.mission_id).status is status
    assert not any(queue.pending.glob("*.json"))


def test_invalid_transition_and_unknown_body_fields_are_rejected(client):
    invalid = client.post(
        "/api/controller/v1/missions/mission-alpha/proposals/resume",
        json={},
        headers=_auth("propose-token"),
    )
    unknown = client.post(
        "/api/controller/v1/missions/mission-alpha/proposals/start",
        json={"command": "rm -rf", "reason": "no"},
        headers=_auth("propose-token"),
    )
    assert invalid.status_code == 409
    assert invalid.json["error"]["code"] == "INVALID_TRANSITION"
    assert unknown.status_code == 400
    assert unknown.json["error"]["code"] == "INVALID_REQUEST"


def test_changed_event_revision_changes_proposal_identity(
    client, operations: ControllerOperations
):
    path = "/api/controller/v1/missions/mission-alpha/proposals/start"
    first = client.post(path, json={}, headers=_auth("propose-token"))
    MissionEventLog(
        operations.missions_root / "events", "mission-alpha"
    ).append(make_event("external_evidence", "mission-alpha"))
    second = client.post(path, json={}, headers=_auth("propose-token"))
    assert second.status_code == 201
    assert second.json["action_id"] != first.json["action_id"]


def test_retry_proposal_enforces_task_and_mission_budgets(tmp_path: Path):
    controller = tmp_path / "retry"
    _create_mission(
        controller,
        status=MissionStatus.running,
        task_status=MissionTaskStatus.failed,
        attempt_count=1,
        max_attempts=2,
    )
    operations = ControllerOperations(
        missions_root=controller / "missions",
        queue_root=controller / "queue",
        reports_root=controller / "reports",
        approvals_root=controller / "approvals",
        tokens=TOKENS,
        now=lambda: datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc),
    )
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(create_operations_blueprint(operations))
    response = app.test_client().post(
        "/api/controller/v1/missions/mission-alpha/tasks/task-a/proposals/retry",
        json={},
        headers=_auth("propose-token"),
    )
    assert response.status_code == 201
    assert response.json["action_type"] == "TASK_RETRY"


def _proposal(client) -> dict:
    response = client.post(
        "/api/controller/v1/missions/mission-alpha/proposals/start",
        json={},
        headers=_auth("propose-token"),
    )
    assert response.status_code == 201
    return response.json


def test_approval_is_separate_idempotent_bound_and_durable(
    client, operations: ControllerOperations
):
    proposal = _proposal(client)
    body = {
        "expected_proposal_revision": proposal["proposal_revision"],
        "note": "approved offline",
        "idempotency_key": "approval-key-1",
    }
    first = client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json=body,
        headers=_auth("approve-token"),
    )
    replay = client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json=body,
        headers=_auth("approve-token"),
    )
    assert first.status_code == 200
    assert replay.json == first.json
    assert first.json["decision"]["approver_identity"] == "approver"
    for field in (
        "action_id",
        "action_type",
        "mission_id",
        "proposal_revision",
        "mission_revision",
        "event_revision",
        "queue_identities",
        "evidence_fingerprint",
        "proposed_effect_fingerprint",
        "decision_time",
        "idempotency_key",
    ):
        assert field in first.json["decision"]
    assert (
        MissionStore(operations.missions_root).load_state("mission-alpha").status
        is MissionStatus.pending
    )

    restarted = operations.restart()
    assert restarted.get_proposal(proposal["action_id"])["status"] == "approved"


def test_conflicting_idempotency_key_and_concurrent_decisions_fail_closed(
    client,
):
    proposal = _proposal(client)
    approve_body = {
        "expected_proposal_revision": proposal["proposal_revision"],
        "idempotency_key": "same-key",
    }
    approved = client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json=approve_body,
        headers=_auth("approve-token"),
    )
    rejected = client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/reject",
        json=approve_body,
        headers=_auth("approve-token"),
    )
    assert approved.status_code == 200
    assert rejected.status_code == 409
    assert rejected.json["error"]["code"] in {
        "CONFLICT",
        "ALREADY_DECIDED",
    }


def test_expired_or_stale_proposal_cannot_be_approved(
    client, operations: ControllerOperations
):
    proposal = _proposal(client)
    event_log = MissionEventLog(
        operations.missions_root / "events", "mission-alpha"
    )
    event_log.append(make_event("changed", "mission-alpha"))
    response = client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": "stale-key",
        },
        headers=_auth("approve-token"),
    )
    assert response.status_code == 409
    assert response.json["error"]["code"] == "STALE_PROPOSAL"


def test_apply_requires_approval_and_uses_scheduler_authority(
    client, operations: ControllerOperations
):
    proposal = _proposal(client)
    path = f"/api/controller/v1/proposals/{proposal['action_id']}/apply"
    unapproved = client.post(path, json={}, headers=_auth("apply-token"))
    assert unapproved.status_code == 409
    assert unapproved.json["error"]["code"] == "NOT_APPROVED"

    client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": "apply-approval",
        },
        headers=_auth("approve-token"),
    )
    applied = client.post(path, json={}, headers=_auth("apply-token"))
    assert applied.status_code == 200
    assert applied.json["status"] == "APPLIED"
    assert (
        MissionStore(operations.missions_root).load_state("mission-alpha").status
        is MissionStatus.running
    )
    assert any(
        operations.queue_root.joinpath(state).glob("*.json")
        for state in ("pending", "running")
    )


def test_rejected_proposal_cannot_apply(client):
    proposal = _proposal(client)
    client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/reject",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": "reject-key",
        },
        headers=_auth("approve-token"),
    )
    response = client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/apply",
        json={},
        headers=_auth("apply-token"),
    )
    assert response.status_code == 409
    assert response.json["error"]["code"] == "NOT_APPROVED"


def test_corrupt_approval_log_blocks_decision_and_apply(
    client, operations: ControllerOperations
):
    proposal = _proposal(client)
    with operations.approval_log_path.open("ab") as handle:
        handle.write(b'{"broken":')
    decision = client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": "corrupt-key",
        },
        headers=_auth("approve-token"),
    )
    apply = client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/apply",
        json={},
        headers=_auth("apply-token"),
    )
    assert decision.status_code == 409
    assert apply.status_code == 409
    assert decision.json["error"]["code"] == "CORRUPT_EVIDENCE"


def test_mutations_require_json_and_enforce_body_size(client):
    content_type = client.post(
        "/api/controller/v1/missions/mission-alpha/proposals/start",
        data="{}",
        headers=_auth("propose-token"),
    )
    oversized = client.post(
        "/api/controller/v1/missions/mission-alpha/proposals/start",
        json={"reason": "x" * 5000},
        headers=_auth("propose-token"),
    )
    assert content_type.status_code == 415
    assert oversized.status_code in {400, 413}


def test_unconfigured_auth_fails_closed(tmp_path: Path):
    from flask import Flask

    operations = ControllerOperations(
        missions_root=tmp_path / "missions",
        queue_root=tmp_path / "queue",
        reports_root=tmp_path / "reports",
        approvals_root=tmp_path / "approvals",
        tokens={},
    )
    app = Flask(__name__)
    app.register_blueprint(create_operations_blueprint(operations))
    assert app.test_client().get(
        "/api/controller/v1/health",
        headers=_auth("anything"),
    ).status_code == 401


def test_constant_time_comparison_is_used(
    client, monkeypatch: pytest.MonkeyPatch
):
    calls: list[tuple[str, str]] = []

    def compared(left: str, right: str) -> bool:
        calls.append((left, right))
        return left == right

    monkeypatch.setattr("hmac.compare_digest", compared)
    response = client.get(
        "/api/controller/v1/health", headers=_auth("read-token")
    )
    assert response.status_code == 200
    assert calls


@pytest.mark.parametrize("address", ["0.0.0.0", "::", "192.168.1.10"])
def test_wildcard_and_public_bind_addresses_are_rejected(address: str):
    with pytest.raises(ValueError):
        validate_bind_address(address)


@pytest.mark.parametrize("address", ["127.0.0.1", "::1"])
def test_loopback_and_tailscale_bind_addresses_are_allowed(address: str):
    assert validate_bind_address(address) == address


def test_configured_tailscale_bind_address_is_allowed():
    assert validate_bind_address(
        "100.64.10.20", tailscale_address="100.64.10.20"
    ) == "100.64.10.20"


def test_missing_resource_uses_stable_error(client):
    response = client.get(
        "/api/controller/v1/missions/does-not-exist",
        headers=_auth("read-token"),
    )
    assert response.status_code == 404
    assert response.json["error"]["code"] == "NOT_FOUND"
    assert "Traceback" not in json.dumps(response.json)


def test_existing_flask_factory_registers_controller_blueprint(operations):
    from app import create_app

    flask_app = create_app(controller_operations=operations)
    response = flask_app.test_client().get(
        "/api/controller/v1/health", headers=_auth("read-token")
    )
    assert response.status_code == 200


def test_recovery_proposals_preserve_exact_planner_identity(tmp_path: Path):
    controller = tmp_path / "recovery"
    store, queue, reports, definition = _create_mission(
        controller,
        status=MissionStatus.running,
        task_status=MissionTaskStatus.pending,
    )
    planned = plan_mission_repairs(
        definition.mission_id, store, queue, reports
    )
    operations = ControllerOperations(
        missions_root=controller / "missions",
        queue_root=controller / "queue",
        reports_root=reports,
        approvals_root=controller / "approvals",
        tokens=TOKENS,
        now=lambda: datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc),
    )
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(create_operations_blueprint(operations))
    response = app.test_client().get(
        "/api/controller/v1/recovery/proposals",
        headers=_auth("read-token"),
    )
    assert response.status_code == 200
    assert [item["action_id"] for item in response.json["items"]] == [
        item.action_id for item in planned
    ]
    assert all(item["status"] == "pending" for item in response.json["items"])


def test_interrupted_lifecycle_apply_is_discoverable_and_retryable(
    client, operations: ControllerOperations, monkeypatch: pytest.MonkeyPatch
):
    proposal = _proposal(client)
    client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": "interrupt-approval",
        },
        headers=_auth("approve-token"),
    )
    original = operations.scheduler.run_once

    def interrupt_after_effect(mission_id: str):
        original(mission_id)
        raise RuntimeError("fixture interruption")

    monkeypatch.setattr(operations.scheduler, "run_once", interrupt_after_effect)
    path = f"/api/controller/v1/proposals/{proposal['action_id']}/apply"
    interrupted = client.post(path, json={}, headers=_auth("apply-token"))
    assert interrupted.status_code == 500

    monkeypatch.setattr(operations.scheduler, "run_once", original)
    recovered = client.post(path, json={}, headers=_auth("apply-token"))
    assert recovered.status_code == 200, recovered.json
    assert recovered.json["status"] == "APPLIED"


@pytest.mark.parametrize(
    ("initial", "suffix", "expected"),
    [
        (MissionStatus.running, "pause", MissionStatus.paused),
        (MissionStatus.paused, "resume", MissionStatus.running),
        (MissionStatus.running, "cancel", MissionStatus.cancelled),
    ],
)
def test_lifecycle_apply_uses_existing_scheduler_authority(
    tmp_path: Path,
    initial: MissionStatus,
    suffix: str,
    expected: MissionStatus,
):
    controller = tmp_path / f"apply-{suffix}"
    _create_mission(controller, status=initial)
    operations = ControllerOperations(
        missions_root=controller / "missions",
        queue_root=controller / "queue",
        reports_root=controller / "reports",
        approvals_root=controller / "approvals",
        tokens=TOKENS,
        now=lambda: datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc),
    )
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(create_operations_blueprint(operations))
    local_client = app.test_client()
    proposal = local_client.post(
        f"/api/controller/v1/missions/mission-alpha/proposals/{suffix}",
        json={},
        headers=_auth("propose-token"),
    ).json
    local_client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": f"approve-{suffix}",
        },
        headers=_auth("approve-token"),
    )
    applied = local_client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/apply",
        json={},
        headers=_auth("apply-token"),
    )
    assert applied.status_code == 200
    assert MissionStore(controller / "missions").load_state(
        "mission-alpha"
    ).status is expected


def test_retry_apply_reuses_deterministic_queue_identity(tmp_path: Path):
    controller = tmp_path / "retry-apply"
    store, queue, _, _ = _create_mission(
        controller,
        status=MissionStatus.running,
        task_status=MissionTaskStatus.failed,
        attempt_count=1,
        max_attempts=2,
    )
    operations = ControllerOperations(
        missions_root=controller / "missions",
        queue_root=controller / "queue",
        reports_root=controller / "reports",
        approvals_root=controller / "approvals",
        tokens=TOKENS,
        now=lambda: datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc),
    )
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(create_operations_blueprint(operations))
    local_client = app.test_client()
    proposal = local_client.post(
        "/api/controller/v1/missions/mission-alpha/tasks/task-a/proposals/retry",
        json={},
        headers=_auth("propose-token"),
    ).json
    local_client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": "retry-approval",
        },
        headers=_auth("approve-token"),
    )
    applied = local_client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/apply",
        json={},
        headers=_auth("apply-token"),
    )
    task_state = store.load_state("mission-alpha").task_states["task-a"]
    assert applied.status_code == 200
    assert task_state.status is MissionTaskStatus.queued
    assert task_state.queue_task_id in proposal["expected_queue_identities"]
    assert len(list(queue.pending.glob("*.json"))) == 1


def test_concurrent_apply_records_one_intent_and_one_effect(
    operations: ControllerOperations,
):
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(create_operations_blueprint(operations))
    setup_client = app.test_client()
    proposal = _proposal(setup_client)
    setup_client.post(
        f"/api/controller/v1/proposals/{proposal['action_id']}/approve",
        json={
            "expected_proposal_revision": proposal["proposal_revision"],
            "idempotency_key": "concurrent-approval",
        },
        headers=_auth("approve-token"),
    )
    barrier = threading.Barrier(2)
    path = f"/api/controller/v1/proposals/{proposal['action_id']}/apply"

    def apply_once(_index: int):
        barrier.wait(timeout=2)
        with app.test_client() as thread_client:
            return thread_client.post(
                path, json={}, headers=_auth("apply-token")
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(apply_once, range(2)))

    assert all(response.status_code == 200 for response in responses)
    events = MissionEventLog(
        operations.missions_root / "events", "mission-alpha"
    ).read_all()
    assert sum(
        event.event_type == "controller_action_started" for event in events
    ) == 1
    assert sum(
        event.event_type == "controller_action_applied" for event in events
    ) == 1


def test_conflicting_active_lifecycle_proposal_is_explicitly_superseded(
    tmp_path: Path,
):
    controller = tmp_path / "superseded"
    _create_mission(controller, status=MissionStatus.running)
    operations = ControllerOperations(
        missions_root=controller / "missions",
        queue_root=controller / "queue",
        reports_root=controller / "reports",
        approvals_root=controller / "approvals",
        tokens=TOKENS,
        now=lambda: datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc),
    )
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(create_operations_blueprint(operations))
    local_client = app.test_client()
    pause = local_client.post(
        "/api/controller/v1/missions/mission-alpha/proposals/pause",
        json={},
        headers=_auth("propose-token"),
    ).json
    cancel = local_client.post(
        "/api/controller/v1/missions/mission-alpha/proposals/cancel",
        json={},
        headers=_auth("propose-token"),
    ).json
    current = local_client.get(
        f"/api/controller/v1/proposals/{pause['action_id']}",
        headers=_auth("read-token"),
    )
    assert cancel["supersedes_action_ids"] == [pause["action_id"]]
    assert current.json["status"] == "superseded"
