from __future__ import annotations

import json
from pathlib import Path

from pavilionos.mission_control import (
    PRESENTATION_SCHEMA_VERSION,
    load_mission_control_view,
    render_mission_control,
)
from tools.integrated_demonstrator.governed_ambiguous_dispatch import (
    reconcile_and_verify_governed_dispatch,
)


def _run(tmp_path: Path):
    result = reconcile_and_verify_governed_dispatch(
        tmp_path, starting_repository_sha="1794aaf739324a7f79532b0c84891b4891cc0639"
    )
    return result, load_mission_control_view(
        tmp_path / "mission.sqlite3",
        tmp_path / "effects.sqlite3",
        tmp_path / "evidence-report.json",
        "integrated-demonstrator",
        result.ambiguous.mission_id,
    )


def test_mission_control_projects_persisted_story_and_counters(tmp_path: Path):
    result, view = _run(tmp_path)
    assert view.ready is True
    assert view.summary == {
        "mission_status": "COMPLETED",
        "human_mission_title": "Deploy test service v2 and verify the result",
        "external_result": "v2 active",
        "evidence_verified": True,
        "authorized_operations": 1,
        "successful_transitions": 1,
        "injected_failures": 1,
        "duplicate_operations": 0,
        "unauthorized_operations": 0,
        "active_version": 2,
        "final_effect_posture": "something_landed",
        "effect_history": "INDETERMINATE \u2192 SOMETHING_LANDED",
        "independent_verification": "PASS",
        "retry_blocked": True,
    }
    labels = [item["label"] for item in view.timeline]
    assert "Response lost; effect is INDETERMINATE" in labels
    assert "Automatic retry BLOCKED" in labels
    assert "Effect resolved as SOMETHING_LANDED" in labels
    assert "Independent verifier: PASS" in labels
    assert result.ambiguous.retry_blocked is True
    # The observation timeline no longer emits duplicate "Mission checkpoint recorded"
    # rows; each labeled checkpoint appears exactly once.
    assert labels.count("Mission checkpoint recorded") == 0

    html = render_mission_control(view)
    assert "Deploy test service v2 and verify the result" in html
    assert "INDETERMINATE" in html
    assert "Automatic retry BLOCKED" in html
    assert "Machine-readable evidence report" in html
    assert "Technical details" in html
    assert "@media" in html
    projected = view.to_dict()
    assert projected["schema_version"] == PRESENTATION_SCHEMA_VERSION
    assert projected["authority"]["allowed"] == [
        "test service deployment",
        "test service state read",
    ]
    assert projected["authority"]["denied"] == ["production", "spending"]


def test_missing_or_inconsistent_report_never_renders_success(tmp_path: Path):
    result = reconcile_and_verify_governed_dispatch(tmp_path, starting_repository_sha="sha")
    report_path = tmp_path / "evidence-report.json"
    report = json.loads(report_path.read_text())
    report["service_active_version"] = 1
    report_path.write_text(json.dumps(report))
    bad = load_mission_control_view(
        result.ambiguous.mission_database_path,
        result.ambiguous.effect_database_path,
        report_path,
        "integrated-demonstrator",
        result.ambiguous.mission_id,
    )
    assert bad.ready is False
    assert "does not prove" in (bad.source_error or "")

    report_path.unlink()
    missing = load_mission_control_view(
        result.ambiguous.mission_database_path,
        result.ambiguous.effect_database_path,
        report_path,
        "integrated-demonstrator",
        result.ambiguous.mission_id,
    )
    assert missing.ready is False
    assert "unavailable" in render_mission_control(missing).lower()


def test_machine_readable_report_is_deterministic_and_accessible(tmp_path: Path):
    result, view = _run(tmp_path)
    report = json.loads((tmp_path / "evidence-report.json").read_text())
    assert report["mission_id"] == result.ambiguous.mission_id
    assert report["effect_history"] == ["indeterminate", "something_landed"]
    assert report["human_mission_title"] == "Deploy test service v2 and verify the result"
    assert json.loads(json.dumps(view.evidence_report, sort_keys=True)) == report
