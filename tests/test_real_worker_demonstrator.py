from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from federation.mission_runtime import MissionRuntime
from federation.mission_runtime_store import MissionRuntimeStore
from pavilionos.mission_control import load_mission_control_view, render_mission_control
from tools.integrated_demonstrator.governed_ambiguous_dispatch import (
    run_governed_ambiguous_dispatch,
)
from tools.integrated_demonstrator.real_worker import (
    PROPOSAL_SCHEMA_VERSION,
    ProposalAuthorityDenied,
    ProposalRequest,
    ProposalSchemaError,
    WorkerProposalResult,
    WorkerReadiness,
    WorkerReadinessCategory,
    WorkerNotReadyError,
    parse_worker_proposal,
)
from tools.integrated_demonstrator.run_demo import (
    DemonstratorVerificationError,
    main,
    run_demo,
    verify_run,
)


class _ScriptedWorker:
    worker_identity = "scripted-test-worker"
    provider_identity = "scripted-test-provider"
    model_identity = "scripted-test-model"
    live_external = False

    def __init__(
        self,
        *,
        target: str = "test-service",
        malformed: bool = False,
        readiness: WorkerReadinessCategory = WorkerReadinessCategory.WORKER_READY,
        attempt_count: int = 1,
    ) -> None:
        self.target = target
        self.malformed = malformed
        self.readiness = readiness
        self.attempt_count = attempt_count

    def check_readiness(self) -> WorkerReadiness:
        status = 200 if self.readiness is WorkerReadinessCategory.WORKER_READY else 503
        return WorkerReadiness(self.readiness, status)

    def propose(self, request: ProposalRequest) -> WorkerProposalResult:
        if self.malformed:
            raise ProposalSchemaError("proposal content is malformed JSON")
        proposal = parse_worker_proposal(
            json.dumps(
                {
                    "schema_version": PROPOSAL_SCHEMA_VERSION,
                    "request_id": request.request_id,
                    "worker_identity": self.worker_identity,
                    "provider_identity": self.provider_identity,
                    "model_identity": self.model_identity,
                    "proposed_action": {
                        "type": "deploy_service_version",
                        "arguments": {"target": self.target, "version": "v2"},
                    },
                    "rationale": "Scripted test-only proposal.",
                }
            ),
            expected_request_id=request.request_id,
            expected_worker_identity=self.worker_identity,
            expected_provider_identity=self.provider_identity,
            expected_model_identity=self.model_identity,
        )
        return WorkerProposalResult(
            proposal=proposal,
            attempt_count=self.attempt_count,
            live_external=False,
            attempt_failures=("worker_transport_failure",)
            if self.attempt_count == 2
            else (),
        )


def _checkpoint_reasons(root: Path) -> set[str]:
    runtime = MissionRuntime(store=MissionRuntimeStore(root / "mission.sqlite3"))
    checkpoints = runtime.list_checkpoints(
        "integrated-demonstrator",
        "mission-integrated-demonstrator-ambiguous-v1",
    )
    return {checkpoint.reason for checkpoint in checkpoints}


def _resign_report_artifact(run_directory: Path) -> None:
    report_path = run_directory / "evidence-report.json"
    manifest_path = run_directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    data = report_path.read_bytes()
    for artifact in manifest["artifacts"]:
        if artifact["path"] == "evidence-report.json":
            artifact["size_bytes"] = len(data)
            artifact["sha256"] = hashlib.sha256(data).hexdigest()
            break
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def test_no_execution_occurs_without_a_valid_typed_proposal(tmp_path: Path) -> None:
    with pytest.raises(ProposalSchemaError):
        run_governed_ambiguous_dispatch(
            tmp_path,
            proposal_worker=_ScriptedWorker(malformed=True),
        )

    assert not (tmp_path / "service.sqlite3").exists()
    assert not (tmp_path / "effects.sqlite3").exists()
    assert "worker_proposal_rejected" in _checkpoint_reasons(tmp_path)


def test_denied_proposal_is_persisted_and_cannot_execute(tmp_path: Path) -> None:
    with pytest.raises(ProposalAuthorityDenied):
        run_governed_ambiguous_dispatch(
            tmp_path,
            proposal_worker=_ScriptedWorker(target="production-service"),
        )

    assert not (tmp_path / "service.sqlite3").exists()
    assert not (tmp_path / "effects.sqlite3").exists()
    assert "worker_authority_denied" in _checkpoint_reasons(tmp_path)


def test_not_ready_worker_is_persisted_and_cannot_request_or_execute_proposal(
    tmp_path: Path,
) -> None:
    with pytest.raises(WorkerNotReadyError, match="model_loading"):
        run_governed_ambiguous_dispatch(
            tmp_path,
            proposal_worker=_ScriptedWorker(
                readiness=WorkerReadinessCategory.MODEL_LOADING
            ),
        )

    assert not (tmp_path / "service.sqlite3").exists()
    assert not (tmp_path / "effects.sqlite3").exists()
    assert "worker_not_ready" in _checkpoint_reasons(tmp_path)


def test_deterministic_worker_participates_in_full_verified_demo(tmp_path: Path) -> None:
    result = run_demo(
        artifacts_root=tmp_path,
        run_id="deterministic-worker-e2e",
        starting_repository_sha="2c12307b4d076aaf73617fe95cd4892569f84164",
    )
    report = result.report
    worker = report["worker"]

    assert worker["worker_identity"] == "deterministic-test-worker"
    assert worker["provider_identity"] == "deterministic-test-provider"
    assert worker["model_identity"] == "deterministic-proposal-model-v0.1"
    assert worker["live_external"] is False
    assert worker["external_endpoint_contacted"] is False
    assert worker["proposal_schema_valid"] is True
    assert worker["authority_decision"]["authorized"] is True
    assert worker["execution_permitted"] is True
    assert worker["proposal"]["proposed_action"] == {
        "type": "deploy_service_version",
        "arguments": {"target": "test-service", "version": "v2"},
    }
    assert worker["execution_binding"]["arguments"] == {
        "target": "test-service",
        "version": "v2",
    }
    assert worker["execution_binding"]["operation_digest"] == report["executed_operation"][
        "operation_digest"
    ]
    assert report["deployment_attempt_count"] == 1
    assert report["effect_history"] == ["indeterminate", "something_landed"]
    assert report["final_effect_posture"] == "something_landed"
    assert report["independent_verification_result"] == "v2_active"
    assert report["final_mission_state"] == "completed"

    replayed = verify_run(result.run_directory)
    assert replayed.report == report
    assert replayed.report["worker"]["live_external"] is False


def test_retried_worker_response_can_create_only_one_controlled_effect(
    tmp_path: Path,
) -> None:
    result = run_demo(
        artifacts_root=tmp_path,
        run_id="bounded-worker-retry",
        starting_repository_sha="start-sha",
        proposal_worker=_ScriptedWorker(attempt_count=2),
    )
    assert result.report["worker"]["attempt_count"] == 2
    assert result.report["worker"]["retry_count"] == 1
    assert result.report["worker"]["attempt_failures"] == [
        "worker_transport_failure"
    ]
    assert result.report["deployment_attempt_count"] == 1
    assert result.report["successful_transition_count"] == 1
    assert result.report["duplicate_deployment_count"] == 0


def test_worker_evidence_is_secret_free_and_mission_control_bound(tmp_path: Path) -> None:
    result = run_demo(
        artifacts_root=tmp_path,
        run_id="worker-evidence-bound",
        starting_repository_sha="start-sha",
    )
    serialized = json.dumps(result.report, sort_keys=True)
    assert "Authorization" not in serialized
    assert "Bearer " not in serialized
    assert "top-secret-token" not in serialized

    worker = result.report["worker"]
    view = load_mission_control_view(
        result.run_directory / "mission.sqlite3",
        result.run_directory / "effects.sqlite3",
        result.run_directory / "evidence-report.json",
        "integrated-demonstrator",
        result.report["mission_id"],
    )
    assert view.ready is True
    assert view.worker is not None
    assert view.worker["provider"] == worker["provider_identity"]
    assert view.worker["model"] == worker["model_identity"]
    assert view.worker["role"] == "Proposal / Planner"
    assert view.worker["proposal"] == "Deploy test service v2"
    html = render_mission_control(view)
    assert worker["provider_identity"] in html
    assert worker["model_identity"] in html
    assert "Deterministic test infrastructure" in html
    assert "Proposal validated" in html
    assert "Operation authorized" in html


def test_mission_control_refuses_worker_evidence_drift(tmp_path: Path) -> None:
    result = run_demo(
        artifacts_root=tmp_path,
        run_id="worker-evidence-drift",
        starting_repository_sha="start-sha",
    )
    report_path = result.run_directory / "evidence-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["worker"]["model_identity"] = "forged-model"
    report_path.write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    view = load_mission_control_view(
        result.run_directory / "mission.sqlite3",
        result.run_directory / "effects.sqlite3",
        report_path,
        "integrated-demonstrator",
        report["mission_id"],
    )
    assert view.ready is False
    assert "worker" in (view.source_error or "").lower()
    _resign_report_artifact(result.run_directory)
    with pytest.raises(DemonstratorVerificationError, match="worker|Mission Control"):
        verify_run(result.run_directory)


def test_live_mode_refuses_missing_configuration_without_creating_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in (
        "MISSIONARYX_WORKER_BASE_URL",
        "MISSIONARYX_WORKER_API_KEY",
        "MISSIONARYX_WORKER_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    exit_code = main(["--worker-mode", "live", "--artifacts-root", str(tmp_path)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "LIVE WORKER MODE REQUESTED" in captured.out
    assert "MISSIONARYX_WORKER_BASE_URL is required" in captured.err
    assert list(tmp_path.iterdir()) == []


def test_deterministic_creator_output_never_claims_live_verification(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "--worker-mode",
            "deterministic",
            "--artifacts-root",
            str(tmp_path),
            "--run-id",
            "creator-offline",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Worker mode:          DETERMINISTIC TEST INFRASTRUCTURE" in captured.out
    assert "LIVE EXTERNAL WORKER VERIFIED: NO" in captured.out
