from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import threading

import pytest

import tools.integrated_demonstrator.run_demo as demo


def _run(tmp_path: Path, run_id: str = "test-run") -> demo.DemonstratorRun:
    return demo.run_demo(
        artifacts_root=tmp_path / "artifacts",
        run_id=run_id,
        starting_repository_sha="test-starting-sha",
    )


def _file_digests(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.iterdir())
        if path.is_file()
    }


def _rewrite_manifest_artifact(run_directory: Path, artifact_name: str) -> None:
    manifest_path = run_directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact_path = run_directory / artifact_name
    for artifact in manifest["artifacts"]:
        if artifact["path"] == artifact_name:
            data = artifact_path.read_bytes()
            artifact["size_bytes"] = len(data)
            artifact["sha256"] = hashlib.sha256(data).hexdigest()
            break
    else:
        raise AssertionError(f"missing manifest artifact: {artifact_name}")
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )


def test_one_command_success_and_artifact_bundle(tmp_path: Path, capsys) -> None:
    artifacts_root = tmp_path / "cli-artifacts"

    exit_code = demo.main([
        "--artifacts-root", str(artifacts_root),
        "--run-id", "cli-run",
    ])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "MISSIONARYX INTEGRATED DEMONSTRATOR — PASS" in output
    run_directory = artifacts_root / "cli-run"
    report = json.loads((run_directory / "evidence-report.json").read_text())
    assert report["service_active_version"] == 2
    assert report["deployment_attempt_count"] == 1
    assert report["successful_transition_count"] == 1
    assert report["duplicate_deployment_count"] == 0
    assert report["unauthorized_operation_count"] == 0
    assert report["injected_failure_count"] == 1
    assert report["final_effect_posture"] == "something_landed"
    assert report["independent_verification_result"] == "v2_active"
    assert report["final_mission_state"] == "completed"
    assert {
        "effects.sqlite3",
        "evidence-report.json",
        "manifest.json",
        "mission-control.html",
        "mission-control.json",
        "mission.sqlite3",
        "run-summary.txt",
        "service.sqlite3",
    } == {path.name for path in run_directory.iterdir() if path.is_file()}
    manifest = json.loads((run_directory / "manifest.json").read_text())
    assert manifest["schema_version"] == "missionaryx.integrated-demonstrator-manifest.v0.2"
    assert manifest["run_id"] == "cli-run"
    assert manifest["verification_result"] == "pass"
    assert {item["path"] for item in manifest["artifacts"]} == {
        "effects.sqlite3",
        "evidence-report.json",
        "mission-control.html",
        "mission-control.json",
        "mission.sqlite3",
        "run-summary.txt",
        "service.sqlite3",
    }


def test_verify_mode_is_read_only_and_reconstructs_projection(tmp_path: Path) -> None:
    result = _run(tmp_path)
    before = _file_digests(result.run_directory)

    verified = demo.verify_run(result.run_directory)

    assert verified.run_id == result.run_id
    assert verified.report["final_mission_state"] == "completed"
    assert _file_digests(result.run_directory) == before


def test_verify_mode_detects_tampered_artifact(tmp_path: Path) -> None:
    result = _run(tmp_path)
    report_path = result.run_directory / "evidence-report.json"
    report_path.write_bytes(report_path.read_bytes() + b" ")

    with pytest.raises(demo.DemonstratorVerificationError, match="digest"):
        demo.verify_run(result.run_directory)


def test_two_runs_are_isolated_and_repeatable(tmp_path: Path) -> None:
    first = _run(tmp_path, "run-one")
    second = _run(tmp_path, "run-two")

    assert first.run_id != second.run_id
    assert first.run_directory != second.run_directory
    assert first.report["deployment_attempt_count"] == 1
    assert second.report["deployment_attempt_count"] == 1
    assert first.report["successful_transition_count"] == 1
    assert second.report["successful_transition_count"] == 1
    assert first.report["gateway_claim_id"] != second.report["gateway_claim_id"]
    assert (first.run_directory / "service.sqlite3").read_bytes() != b""
    assert (second.run_directory / "service.sqlite3").read_bytes() != b""
    demo.verify_run(first.run_directory)
    demo.verify_run(second.run_directory)


def test_verify_rejects_logically_inconsistent_resigned_durable_state(tmp_path: Path) -> None:
    result = _run(tmp_path)
    service_path = result.run_directory / "service.sqlite3"
    with sqlite3.connect(service_path) as connection:
        connection.execute(
            "UPDATE service_state SET deployment_attempt_count = 2 WHERE singleton = 1"
        )
        connection.commit()
        assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    _rewrite_manifest_artifact(result.run_directory, "service.sqlite3")

    with pytest.raises(demo.DemonstratorVerificationError, match="counter|attempt"):
        demo.verify_run(result.run_directory)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_missing_or_corrupt_durable_evidence_never_verifies(
    tmp_path: Path, damage: str
) -> None:
    result = _run(tmp_path)
    damaged = tmp_path / f"damaged-{damage}"
    shutil.copytree(result.run_directory, damaged)
    effects_path = damaged / "effects.sqlite3"
    if damage == "missing":
        effects_path.unlink()
    else:
        effects_path.write_bytes(b"not a sqlite database")

    with pytest.raises(demo.DemonstratorVerificationError):
        demo.verify_run(damaged)


def test_failure_preserves_evidence_and_leaves_no_service_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = {thread.ident for thread in threading.enumerate()}

    def fail_render(_view) -> str:
        raise RuntimeError("injected render failure")

    monkeypatch.setattr(demo, "render_mission_control", fail_render)
    with pytest.raises(RuntimeError, match="injected render failure"):
        _run(tmp_path, "failed-run")

    run_directory = tmp_path / "artifacts" / "failed-run"
    assert run_directory.is_dir()
    assert (run_directory / "effects.sqlite3").is_file()
    assert "FAIL" in (run_directory / "run-summary.txt").read_text(encoding="utf-8")
    leaked = [
        thread for thread in threading.enumerate()
        if thread.ident not in before and thread.is_alive()
    ]
    assert leaked == []
