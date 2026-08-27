"""Presentation-layer consistency tests for Mission Control v0.2.

Every human-facing claim on the rendered page must be traceable to persisted
evidence.  These tests guard against the failure mode where the UI silently
drifts away from the authoritative run outputs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from pavilionos.mission_control import (
    HUMAN_MISSION_TITLE,
    MissionControlView,
    PRESENTATION_SCHEMA_VERSION,
    load_mission_control_view,
    render_mission_control,
)
from tools.integrated_demonstrator.governed_ambiguous_dispatch import (
    reconcile_and_verify_governed_dispatch,
)


@pytest.fixture
def view_and_report(tmp_path: Path) -> tuple[MissionControlView, dict]:
    result = reconcile_and_verify_governed_dispatch(
        tmp_path, starting_repository_sha="presentation-test-sha"
    )
    view = load_mission_control_view(
        tmp_path / "mission.sqlite3",
        tmp_path / "effects.sqlite3",
        tmp_path / "evidence-report.json",
        "integrated-demonstrator",
        result.ambiguous.mission_id,
    )
    return view, result.report


def test_human_title_and_internal_id_are_both_present(view_and_report):
    view, report = view_and_report
    html = render_mission_control(view)
    assert HUMAN_MISSION_TITLE == "Deploy test service v2 and verify the result"
    assert view.summary["human_mission_title"] == HUMAN_MISSION_TITLE
    assert HUMAN_MISSION_TITLE in html
    # Internal identifier is still available as secondary technical metadata.
    assert report["mission_id"] in html
    # But the human title outranks the internal id in the document order.
    assert html.index(HUMAN_MISSION_TITLE) < html.index(report["mission_id"])


def test_mission_status_and_counters_are_evidence_derived(view_and_report):
    view, report = view_and_report
    assert view.summary["mission_status"] == report["final_mission_state"].upper()
    assert view.summary["authorized_operations"] == report["deployment_attempt_count"]
    assert view.summary["successful_transitions"] == report["successful_transition_count"]
    assert view.summary["duplicate_operations"] == report["duplicate_deployment_count"]
    assert view.summary["unauthorized_operations"] == report["unauthorized_operation_count"]
    assert view.summary["injected_failures"] == report["injected_failure_count"]
    assert view.summary["active_version"] == report["service_active_version"]
    assert view.summary["final_effect_posture"] == report["final_effect_posture"]


def test_html_never_hard_codes_operational_numbers(view_and_report):
    """Substituting an alternate report must change the rendered digits."""
    view, report = view_and_report
    original_html = render_mission_control(view)
    forged_report = dict(report)
    forged_report["deployment_attempt_count"] = 7
    forged_report["injected_failure_count"] = 3
    forged_report["duplicate_deployment_count"] = 5
    forged_view = MissionControlView(
        mission=view.mission,
        summary={
            **view.summary,
            "authorized_operations": 7,
            "injected_failures": 3,
            "duplicate_operations": 5,
        },
        timeline=view.timeline,
        authority=view.authority,
        evidence=view.evidence,
        technical=view.technical,
        evidence_report=forged_report,
        ready=True,
    )
    forged_html = render_mission_control(forged_view)
    # Real evidence renders 1/1/0; forged evidence renders 7/3/5. If the numbers
    # were hard-coded, the HTMLs would be identical.
    assert original_html != forged_html
    assert '<span class="num">7</span>' in forged_html
    assert '<span class="num">3</span>' in forged_html
    assert '<span class="num">5</span>' in forged_html


def test_authority_partition_matches_evidence(view_and_report):
    view, report = view_and_report
    expected_allowed = tuple(
        sorted(name for name, disp in report["authority"].items() if disp == "allowed")
    )
    expected_denied = tuple(
        sorted(name for name, disp in report["authority"].items() if disp == "denied")
    )
    assert view.authority["allowed"] == expected_allowed
    assert view.authority["denied"] == expected_denied
    html = render_mission_control(view)
    # Each allowed and denied scope is projected verbatim into the HTML.
    for name in expected_allowed + expected_denied:
        assert name in html


def test_timeline_projects_uncertainty_and_reconciliation_story(view_and_report):
    view, _ = view_and_report
    reasons = [item.get("reason") for item in view.timeline if item.get("reason")]
    for expected in (
        "ambiguous_dispatch_established",
        "retry_blocked",
        "reconciliation_started",
        "external_state_observed",
        "ambiguity_resolved",
        "independent_verification",
    ):
        assert expected in reasons, f"missing {expected} in timeline"
    # Uncertainty must appear before reconciliation, which must appear before
    # SOMETHING_LANDED and the independent verification.
    order = {reason: reasons.index(reason) for reason in reasons if reason}
    assert order["ambiguous_dispatch_established"] < order["retry_blocked"]
    assert order["retry_blocked"] < order["reconciliation_started"]
    assert order["reconciliation_started"] < order["ambiguity_resolved"]
    assert order["ambiguity_resolved"] < order["independent_verification"]


def test_timeline_severity_classes_light_up_the_narrative(view_and_report):
    view, _ = view_and_report
    severities = {item.get("reason"): item.get("severity") for item in view.timeline if item.get("reason")}
    assert severities["ambiguous_dispatch_established"] == "step-warn"
    assert severities["retry_blocked"] == "step-blocked"
    assert severities["reconciliation_started"] == "step-reconcile"
    assert severities["ambiguity_resolved"] == "step-ok"
    assert severities["independent_verification"] == "step-ok"
    html = render_mission_control(view)
    assert "step-warn" in html
    assert "step-blocked" in html
    assert "step-reconcile" in html


def test_retry_blocked_statement_is_bound_to_authoritative_state(view_and_report):
    view, _ = view_and_report
    assert view.summary["retry_blocked"] is True
    html = render_mission_control(view)
    assert "Automatic retry BLOCKED" in html
    # The plain-language explanation is present for the retry-blocked step.
    assert "duplicate operations" in html.lower() or "same external" in html.lower()


def test_something_landed_only_appears_after_reconciliation(view_and_report):
    view, _ = view_and_report
    html = render_mission_control(view)
    # Both phrases must be present.
    assert "INDETERMINATE" in html
    assert "SOMETHING_LANDED" in html
    # Within the mission timeline section, INDETERMINATE precedes SOMETHING_LANDED
    # so the reconciliation story is presented in causal order.
    timeline_open = html.index("id=\"timeline-title\"")
    timeline_close = html.index("</ol>", timeline_open)
    timeline_html = html[timeline_open:timeline_close]
    assert "INDETERMINATE" in timeline_html
    assert "SOMETHING_LANDED" in timeline_html
    assert timeline_html.index("INDETERMINATE") < timeline_html.index("SOMETHING_LANDED")


def test_evidence_section_reports_verified_status(view_and_report):
    view, _ = view_and_report
    labels = {row["label"] for row in view.evidence}
    assert {
        "Mission state",
        "Authorization",
        "Dispatch history",
        "Reconciliation",
        "External observation",
        "Independent verifier",
        "Persisted evidence history",
    }.issubset(labels)
    assert all(row["ok"] for row in view.evidence)
    html = render_mission_control(view)
    assert "class=\"status\">Verified" in html
    assert "class=\"status\">PASS" in html


def test_technical_details_are_available_but_progressively_disclosed(view_and_report):
    view, report = view_and_report
    html = render_mission_control(view)
    # Every technical identifier is present so reviewers can audit the run.
    for key in (
        "effect_intent_id",
        "effect_dispatch_id",
        "gateway_claim_id",
        "authority_reservation_id",
        "reconciliation_obligation_id",
    ):
        value = report[key]
        assert value in html, f"missing {key} in technical details"
    # But the technical details block is inside a collapsed <details> panel.
    assert re.search(r"<details[^>]*>\s*<summary>Technical details</summary>", html)


def test_responsive_and_semantic_markup_is_present(view_and_report):
    view, _ = view_and_report
    html = render_mission_control(view)
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert "@media (max-width: 720px)" in html
    assert '<html lang="en">' in html
    # Header/main/section semantics.
    assert "<header class=\"topbar\">" in html
    assert "<main>" in html
    # ARIA-labelled sections improve accessibility.
    assert "aria-labelledby=\"mission-title\"" in html
    assert "aria-labelledby=\"timeline-title\"" in html


def test_projection_is_deterministic_and_stable(view_and_report):
    view, _ = view_and_report
    first = view.to_dict()
    second = view.to_dict()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["schema_version"] == PRESENTATION_SCHEMA_VERSION
    assert first["ready"] is True
    assert first["mission"]["mission_id"] == view.mission["mission_id"]
    assert first["evidence_report_available"] is True


def test_render_is_deterministic(view_and_report):
    view, _ = view_and_report
    assert render_mission_control(view) == render_mission_control(view)


def test_incomplete_state_never_renders_success(view_and_report):
    view, _ = view_and_report
    incomplete = MissionControlView(
        mission=view.mission,
        summary={},
        timeline=(),
        authority={"allowed": (), "denied": ()},
        evidence=(),
        technical={},
        evidence_report=None,
        ready=False,
        source_error="evidence not available",
    )
    html = render_mission_control(incomplete)
    assert "MISSION COMPLETE" not in html.upper()
    assert "SOMETHING_LANDED" not in html
    assert "evidence not available" in html
