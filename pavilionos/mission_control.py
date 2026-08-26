"""Demo-grade Mission Control presentation for the Integrated Demonstrator.

This module is deliberately read-only.  It projects the accepted
MissionObservability/native-interface records and the durable evidence report;
it does not infer a successful mission from a mission lifecycle alone.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Protocol
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from federation.mission_observability import MissionObservation
from federation.mission_runtime import MissionRuntime
from federation.mission_runtime_store import MissionRuntimeStore
from federation.durable_effect_store import DurableEffectStore
from pavilionos.native_interface import PavilionNativeInterface


class MissionControlSourceError(Exception):
    """Persisted source data is absent or contradictory."""


class _ReadOnlyObservability(Protocol):
    def observe(self, control_domain: str, mission_id: str) -> MissionObservation: ...


@dataclass(frozen=True, slots=True)
class MissionControlView:
    """All display fields are derived from persisted sources."""

    mission: dict[str, Any]
    summary: dict[str, Any]
    timeline: tuple[dict[str, Any], ...]
    evidence_report: dict[str, Any] | None
    ready: bool
    source_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission": self.mission,
            "summary": self.summary,
            "timeline": list(self.timeline),
            "evidence_report_available": self.evidence_report is not None,
            "ready": self.ready,
            "source_error": self.source_error,
        }


_EVENT_LABELS = {
    "mission_transition": "Mission lifecycle changed",
    "mission_checkpoint": "Mission checkpoint recorded",
    "authority_reservation": "Authority established",
    "authority_reservation_disposition": "Authority disposition updated",
    "effect_reference": "Deployment intent referenced",
    "effect_intent": "Deployment intent created",
    "effect_dispatch": "Governed dispatch sent",
    "gateway_claim": "Dispatch ownership claimed",
    "gateway_handoff_started": "Test service handoff started",
    "gateway_indeterminate": "Response lost; effect is INDETERMINATE",
    "gateway_terminal": "Effect reached terminal state",
    "evidence": "Provider-boundary evidence recorded",
}


def _timeline(observation: MissionObservation) -> tuple[dict[str, Any], ...]:
    """Use the canonical timeline; checkpoint payloads add no new truth."""
    items = []
    transition_labels = {
        transition.transition_id: (
            "Mission created" if transition.revision == 1 else
            "Mission started" if transition.to_state.value == "running" and transition.revision == 2 else
            "Mission completed" if transition.to_state.value == "completed" else
            "Mission lifecycle changed"
        )
        for transition in observation.transitions
    }
    for event in observation.timeline:
        items.append({
            "source_type": event.source_type,
            "source_id": event.source_id,
            "recorded_at": event.recorded_at.isoformat(),
            "label": transition_labels.get(event.source_id, _EVENT_LABELS.get(event.source_type, event.source_type.replace("_", " ").title())),
            "source_fingerprint": event.source_fingerprint,
        })
    for checkpoint in observation.checkpoints:
        try:
            payload = json.loads(checkpoint.progress_data_json)
        except (TypeError, json.JSONDecodeError):
            payload = {}
        reason = checkpoint.reason or ""
        label = {
            "ambiguous_dispatch_established": "Response lost; effect is INDETERMINATE",
            "retry_blocked": "Automatic retry BLOCKED",
            "reconciliation_started": "Reconciliation begins",
            "external_state_observed": "v2 observed: external service state read",
            "ambiguity_resolved": "Effect resolved as SOMETHING_LANDED",
            "independent_verification": "Independent verifier: PASS",
        }.get(reason, reason.replace("_", " ").title() if reason else "Checkpoint recorded")
        items.append({
            "source_type": "mission_checkpoint",
            "source_id": checkpoint.checkpoint_id,
            "recorded_at": checkpoint.created_at.isoformat(),
            "label": label,
            "reason": reason,
            "payload": payload,
            "source_fingerprint": checkpoint.progress_data_json,
        })
    return tuple(sorted(items, key=lambda item: (item["recorded_at"], item["source_type"], item["source_id"])))


def _validate_report(observation: MissionObservation, report: dict[str, Any]) -> None:
    required = {
        "mission_id", "final_mission_state", "final_effect_posture", "effect_history",
        "service_active_version", "deployment_attempt_count", "successful_transition_count",
        "duplicate_deployment_count", "unauthorized_operation_count", "injected_failure_count",
        "independent_verification_result", "reconciliation_result",
    }
    missing = sorted(required - report.keys())
    if missing:
        raise MissionControlSourceError("evidence report missing: " + ", ".join(missing))
    if report["mission_id"] != observation.mission_id:
        raise MissionControlSourceError("evidence report mission does not match observation")
    if not observation.effects or any(
        effect.projected_status.value != "something_landed"
        or effect.reconciliation_required
        for effect in observation.effects
    ):
        raise MissionControlSourceError("authoritative effect observation is not fully resolved")
    if report["final_mission_state"] != observation.lifecycle.value:
        raise MissionControlSourceError("evidence report lifecycle contradicts observation")
    if report["effect_history"] != ["indeterminate", "something_landed"]:
        raise MissionControlSourceError("effect history does not preserve INDETERMINATE -> SOMETHING_LANDED")
    if report["final_effect_posture"] != "something_landed" or report["reconciliation_result"] != "something_landed":
        raise MissionControlSourceError("evidence report does not prove landed effect")
    if report["service_active_version"] != 2 or report["independent_verification_result"] != "v2_active":
        raise MissionControlSourceError("evidence report does not prove independent v2 verification")
    if any(report[name] != expected for name, expected in {
        "deployment_attempt_count": 1, "successful_transition_count": 1,
        "duplicate_deployment_count": 0, "unauthorized_operation_count": 0,
        "injected_failure_count": 1,
    }.items()):
        raise MissionControlSourceError("evidence report counters are inconsistent")
    if not any(item.get("reason") == "retry_blocked" for item in _timeline(observation)):
        raise MissionControlSourceError("persisted retry-blocked checkpoint is missing")


def build_mission_control_view(
    interface: PavilionNativeInterface,
    control_domain: str,
    mission_id: str,
    *,
    evidence_report: dict[str, Any] | None,
) -> MissionControlView:
    """Build a conservative view from the existing native presentation seam."""
    try:
        native = interface.mission_view(control_domain, mission_id)
        observation = interface._observability.observe(control_domain, mission_id)  # type: ignore[attr-defined]
        timeline = _timeline(observation)
        if evidence_report is not None:
            _validate_report(observation, evidence_report)
        summary = {
            "mission_status": native.lifecycle.value.upper(),
            "active_version": evidence_report.get("service_active_version") if evidence_report else None,
            "deployment_attempts": evidence_report.get("deployment_attempt_count") if evidence_report else None,
            "successful_transitions": evidence_report.get("successful_transition_count") if evidence_report else None,
            "duplicate_deployments": evidence_report.get("duplicate_deployment_count") if evidence_report else None,
            "unauthorized_operations": evidence_report.get("unauthorized_operation_count") if evidence_report else None,
            "injected_failures": evidence_report.get("injected_failure_count") if evidence_report else None,
            "final_effect_posture": evidence_report.get("final_effect_posture") if evidence_report else None,
            "effect_history": "INDETERMINATE → SOMETHING_LANDED" if evidence_report else None,
            "independent_verification": "PASS" if evidence_report and evidence_report.get("independent_verification_result") == "v2_active" else None,
        }
        # The integrated runner persists its deterministic report separately from
        # the in-memory EvidenceSpine.  The report is accepted only after the
        # authoritative effect observation and report cross-check above.
        ready = evidence_report is not None and bool(observation.effects) and all(
            effect.projected_status.value == "something_landed"
            and not effect.reconciliation_required
            for effect in observation.effects
        )
        return MissionControlView(native.__dict__ if hasattr(native, "__dict__") else {
            "mission_id": native.mission_id, "objective": native.objective,
            "lifecycle": native.lifecycle.value, "revision": native.revision,
        }, summary, timeline, evidence_report, ready)
    except Exception as exc:
        return MissionControlView({}, {}, (), evidence_report, False, str(exc))


def load_mission_control_view(
    mission_database_path: str | Path,
    effect_database_path: str | Path,
    report_path: str | Path,
    control_domain: str,
    mission_id: str,
) -> MissionControlView:
    """Load persisted stores for the read-only display boundary."""
    runtime = MissionRuntime(store=MissionRuntimeStore(mission_database_path))
    effects = DurableEffectStore(effect_database_path)
    try:
        interface = PavilionNativeInterface(__import__("federation.mission_observability", fromlist=["MissionObservability"]).MissionObservability(runtime, effects))
        path = Path(report_path)
        try:
            report = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        except (OSError, json.JSONDecodeError) as exc:
            report = None
            return MissionControlView({}, {}, (), None, False, f"evidence report unavailable: {exc}")
        return build_mission_control_view(interface, control_domain, mission_id, evidence_report=report)
    finally:
        effects.close()


def render_mission_control(view: MissionControlView) -> str:
    """Render HTML with an explicit incomplete/error state."""
    if not view.ready:
        message = escape(view.source_error or "Persisted evidence is unavailable; success is not asserted.")
        return f"<!doctype html><title>Mission Control</title><main><h1>Mission Control</h1><p role='alert'>{message}</p></main>"
    s = view.summary
    cards = "".join(f"<div class='metric'><small>{escape(k.replace('_', ' ').title())}</small><strong>{escape(str(v).upper())}</strong></div>" for k, v in s.items() if k not in {"effect_history"})
    timeline = "".join(f"<li><time>{escape(item['recorded_at'])}</time><span>{escape(item['label'])}</span></li>" for item in view.timeline)
    report = escape(json.dumps(view.evidence_report, sort_keys=True, indent=2))
    authority = view.evidence_report.get("authority", {}) if view.evidence_report else {}
    authority_html = "".join(
        f"<li>{escape(str(name))}: <b>{escape(str(disposition).upper())}</b></li>"
        for name, disposition in authority.items()
    )
    participants = ", ".join(str(item) for item in (view.evidence_report.get("participants", []) if view.evidence_report else []))
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>Mission Control</title>
<style>body{{font:16px system-ui;margin:0;background:#101827;color:#edf2f7}}main{{max-width:1100px;margin:auto;padding:32px}}.hero,.panel{{background:#182337;border:1px solid #33445e;border-radius:16px;padding:24px;margin:16px 0}}.hero{{border-left:6px solid #47d7a3}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}.metric{{background:#101827;padding:14px;border-radius:10px}}small{{display:block;color:#aab8cc}}strong{{display:block;margin-top:7px;font-size:1.25rem}}li{{display:flex;gap:18px;padding:11px;border-left:3px solid #47d7a3;margin:7px 0;list-style:none}}time{{color:#aab8cc;font-size:.85rem}}.warning{{background:#49351c;border-color:#c8923e}}pre{{white-space:pre-wrap;max-height:420px;overflow:auto}}</style></head>
<body><main><section class='hero'><small>MISSION CONTROL · EVIDENCE-BACKED VIEW</small><h1>{escape(str(view.mission.get('mission_id', 'Mission')))}</h1><p>{escape(str(view.mission.get('objective', '')))}</p><h2>COMPLETED · v2 active</h2><p>Participants: {escape(participants)}</p></section>
<section class='panel'><h2>Authority disposition</h2><ul>{authority_html}</ul></section>
<section class='metrics'>{cards}</section><section class='panel warning'><h2>Ambiguous dispatch preserved</h2><p>The response was deliberately lost. The effect became <b>INDETERMINATE</b>, so automatic retry was <b>BLOCKED</b>. Reconciliation later established <b>SOMETHING_LANDED</b>.</p><p>{escape(str(s['effect_history']))}</p></section>
<section class='panel'><h2>Mission timeline</h2><ol>{timeline}</ol></section><section class='panel'><h2>Machine-readable evidence report</h2><pre>{report}</pre></section></main></body></html>"""


def create_mission_control_handler(view_provider):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            view = view_provider()
            if urlparse(self.path).path == "/api/mission-control/evidence":
                body = json.dumps(view.evidence_report or {"available": False}, sort_keys=True).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
            else:
                body = render_mission_control(view).encode()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self, *_args):
            return
    return Handler


__all__ = ["MissionControlSourceError", "MissionControlView", "build_mission_control_view", "load_mission_control_view", "render_mission_control", "create_mission_control_handler"]
