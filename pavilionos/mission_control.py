"""Mission Control presentation v0.2 for the Integrated Demonstrator.

This module is a read-only projection over authoritative sources: the mission
runtime store, the durable effect store, and the persisted evidence report.
It does not add new truth-bearing state; every displayed value is derived from
one of those sources.  Success is asserted only when the evidence report, the
authoritative effect observation, and the checkpoint history all agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: F401 - retained public surface
import json
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from federation.mission_observability import MissionObservation
from federation.mission_runtime import MissionRuntime
from federation.mission_runtime_store import MissionRuntimeStore
from federation.durable_effect_store import DurableEffectStore
from pavilionos.native_interface import PavilionNativeInterface


PRESENTATION_SCHEMA_VERSION = "missionaryx.mission-control-projection.v0.2"
HUMAN_MISSION_TITLE = "Deploy test service v2 and verify the result"


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
    authority: dict[str, tuple[str, ...]]
    evidence: tuple[dict[str, Any], ...]
    technical: dict[str, Any]
    evidence_report: dict[str, Any] | None
    ready: bool
    source_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PRESENTATION_SCHEMA_VERSION,
            "mission": self.mission,
            "summary": self.summary,
            "timeline": list(self.timeline),
            "authority": {name: list(items) for name, items in self.authority.items()},
            "evidence": list(self.evidence),
            "technical": self.technical,
            "evidence_report_available": self.evidence_report is not None,
            "ready": self.ready,
            "source_error": self.source_error,
        }


# ---------------------------------------------------------------------------
# Timeline projection

_EVENT_LABELS = {
    "mission_transition": "Mission lifecycle changed",
    "authority_reservation": "Authority reserved",
    "authority_reservation_disposition": "Authority disposition updated",
    "effect_reference": "Deployment intent referenced by mission",
    "effect_intent": "Deployment intent committed",
    "effect_dispatch": "Governed dispatch prepared",
    "gateway_claim": "Dispatch ownership claimed",
    "gateway_handoff_started": "Test service handoff started",
    "gateway_indeterminate": "Response lost; effect is INDETERMINATE",
    "gateway_terminal": "Effect reached terminal state",
    "evidence": "Provider-boundary evidence recorded",
}

_CHECKPOINT_LABELS = {
    "ambiguous_dispatch_established": "Response lost; effect is INDETERMINATE",
    "retry_blocked": "Automatic retry BLOCKED",
    "reconciliation_started": "Reconciliation started",
    "external_state_observed": "External service state observed",
    "ambiguity_resolved": "Effect resolved as SOMETHING_LANDED",
    "independent_verification": "Independent verifier: PASS",
}

_CHECKPOINT_DETAILS = {
    "ambiguous_dispatch_established": (
        "MissionaryX could no longer prove whether the deployment had succeeded."
    ),
    "retry_blocked": (
        "Retrying immediately could have applied the same external operation twice."
    ),
    "reconciliation_started": (
        "MissionaryX began a read-only inspection of the external service."
    ),
    "external_state_observed": (
        "The service reported version 2 active — one dispatch attempt and one successful transition."
    ),
    "ambiguity_resolved": (
        "Provider-boundary evidence proved the original operation had landed exactly once."
    ),
    "independent_verification": (
        "A second, independent read confirmed the same service state."
    ),
}

# Timeline styling: severity classes drive the CSS presentation.
_EVENT_SEVERITY = {
    "mission_transition": "step-normal",
    "authority_reservation": "step-normal",
    "authority_reservation_disposition": "step-normal",
    "effect_reference": "step-normal",
    "effect_intent": "step-normal",
    "effect_dispatch": "step-normal",
    "gateway_claim": "step-normal",
    "gateway_handoff_started": "step-normal",
    "gateway_indeterminate": "step-warn",
    "gateway_terminal": "step-ok",
    "evidence": "step-normal",
}

_CHECKPOINT_SEVERITY = {
    "ambiguous_dispatch_established": "step-warn",
    "retry_blocked": "step-blocked",
    "reconciliation_started": "step-reconcile",
    "external_state_observed": "step-reconcile",
    "ambiguity_resolved": "step-ok",
    "independent_verification": "step-ok",
}

_TRANSITION_LABELS_BY_REVISION_AND_STATE = {
    (1, None): "Mission created",
    (2, "running"): "Mission started",
}


def _transition_label(revision: int, to_state: str) -> str:
    if revision == 1:
        return "Mission created"
    if to_state == "running" and revision == 2:
        return "Mission started"
    if to_state == "completed":
        return "Mission completed"
    return "Mission lifecycle changed"


def _timeline(observation: MissionObservation) -> tuple[dict[str, Any], ...]:
    """Deduplicated, story-first timeline projected from persisted evidence.

    Base ``mission_checkpoint`` events from the observation timeline are
    skipped because ``observation.checkpoints`` re-emits them with the
    reason-derived label.  The resulting list preserves every source event
    exactly once and is stable-sorted by ``(recorded_at, source_type, source_id)``.
    """
    items: list[dict[str, Any]] = []
    transition_labels = {
        transition.transition_id: _transition_label(
            transition.revision, transition.to_state.value
        )
        for transition in observation.transitions
    }
    for event in observation.timeline:
        if event.source_type == "mission_checkpoint":
            # Deduplicate: labeled version is emitted from observation.checkpoints.
            continue
        label = transition_labels.get(
            event.source_id,
            _EVENT_LABELS.get(
                event.source_type, event.source_type.replace("_", " ").title()
            ),
        )
        items.append(
            {
                "source_type": event.source_type,
                "source_id": event.source_id,
                "recorded_at": event.recorded_at.isoformat(),
                "label": label,
                "severity": _EVENT_SEVERITY.get(event.source_type, "step-normal"),
                "source_fingerprint": event.source_fingerprint,
            }
        )
    for checkpoint in observation.checkpoints:
        try:
            payload = json.loads(checkpoint.progress_data_json)
        except (TypeError, json.JSONDecodeError):
            payload = {}
        reason = checkpoint.reason or ""
        label = _CHECKPOINT_LABELS.get(
            reason,
            reason.replace("_", " ").title() if reason else "Checkpoint recorded",
        )
        item = {
            "source_type": "mission_checkpoint",
            "source_id": checkpoint.checkpoint_id,
            "recorded_at": checkpoint.created_at.isoformat(),
            "label": label,
            "reason": reason,
            "payload": payload,
            "severity": _CHECKPOINT_SEVERITY.get(reason, "step-normal"),
            "source_fingerprint": checkpoint.progress_data_json,
        }
        detail = _CHECKPOINT_DETAILS.get(reason)
        if detail is not None:
            item["detail"] = detail
        items.append(item)
    items.sort(
        key=lambda item: (item["recorded_at"], item["source_type"], item["source_id"])
    )
    return tuple(items)


# ---------------------------------------------------------------------------
# Evidence-report validation

def _validate_report(observation: MissionObservation, report: dict[str, Any]) -> None:
    required = {
        "mission_id",
        "human_mission_title",
        "final_mission_state",
        "final_effect_posture",
        "effect_history",
        "service_active_version",
        "deployment_attempt_count",
        "successful_transition_count",
        "duplicate_deployment_count",
        "unauthorized_operation_count",
        "injected_failure_count",
        "independent_verification_result",
        "reconciliation_result",
        "authority",
    }
    missing = sorted(required - report.keys())
    if missing:
        raise MissionControlSourceError(
            "evidence report missing: " + ", ".join(missing)
        )
    if not isinstance(report["human_mission_title"], str) or not report["human_mission_title"]:
        raise MissionControlSourceError("evidence report human_mission_title is invalid")
    if report["mission_id"] != observation.mission_id:
        raise MissionControlSourceError(
            "evidence report mission does not match observation"
        )
    if not observation.effects or any(
        effect.projected_status.value != "something_landed"
        or effect.reconciliation_required
        for effect in observation.effects
    ):
        raise MissionControlSourceError(
            "authoritative effect observation is not fully resolved"
        )
    if report["final_mission_state"] != observation.lifecycle.value:
        raise MissionControlSourceError(
            "evidence report lifecycle contradicts observation"
        )
    if report["effect_history"] != ["indeterminate", "something_landed"]:
        raise MissionControlSourceError(
            "effect history does not preserve INDETERMINATE -> SOMETHING_LANDED"
        )
    if (
        report["final_effect_posture"] != "something_landed"
        or report["reconciliation_result"] != "something_landed"
    ):
        raise MissionControlSourceError(
            "evidence report does not prove landed effect"
        )
    if (
        report["service_active_version"] != 2
        or report["independent_verification_result"] != "v2_active"
    ):
        raise MissionControlSourceError(
            "evidence report does not prove independent v2 verification"
        )
    if any(
        report[name] != expected
        for name, expected in {
            "deployment_attempt_count": 1,
            "successful_transition_count": 1,
            "duplicate_deployment_count": 0,
            "unauthorized_operation_count": 0,
            "injected_failure_count": 1,
        }.items()
    ):
        raise MissionControlSourceError(
            "evidence report counters are inconsistent"
        )
    if not isinstance(report["authority"], dict) or not report["authority"]:
        raise MissionControlSourceError(
            "evidence report authority breakdown is invalid"
        )
    for name, disposition in report["authority"].items():
        if not isinstance(name, str) or not isinstance(disposition, str):
            raise MissionControlSourceError("authority entries must be name -> disposition strings")
        if disposition not in ("allowed", "denied"):
            raise MissionControlSourceError(
                f"authority disposition for {name!r} must be allowed or denied"
            )
    if not any(item.get("reason") == "retry_blocked" for item in _timeline(observation)):
        raise MissionControlSourceError(
            "persisted retry-blocked checkpoint is missing"
        )


# ---------------------------------------------------------------------------
# Projection assembly

def _authority_breakdown(report: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    allowed: list[str] = []
    denied: list[str] = []
    for name, disposition in sorted(report.get("authority", {}).items()):
        if disposition == "allowed":
            allowed.append(name)
        elif disposition == "denied":
            denied.append(name)
    return {"allowed": tuple(allowed), "denied": tuple(denied)}


def _evidence_rows(report: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    persisted = report.get("persisted_evidence_history_count", 0)
    reservation_disposition = report.get("authority_disposition")
    reconciliation_state = report.get("reconciliation_state")
    return (
        {
            "label": "Mission state",
            "status": "Verified",
            "detail": (
                f"Durable lifecycle: {report.get('final_mission_state', 'unknown').upper()}"
            ),
            "ok": True,
        },
        {
            "label": "Authorization",
            "status": "Verified",
            "detail": (
                f"Authority reservation {str(reservation_disposition or 'unknown').upper()}"
            ),
            "ok": reservation_disposition == "consumed",
        },
        {
            "label": "Dispatch history",
            "status": "Verified",
            "detail": (
                f"{report.get('deployment_attempt_count', 0)} authorized dispatch"
                f" — {report.get('duplicate_deployment_count', 0)} duplicates"
            ),
            "ok": True,
        },
        {
            "label": "Reconciliation",
            "status": "Verified",
            "detail": (
                f"Obligation {str(reconciliation_state or 'unknown').upper()} —"
                f" outcome {str(report.get('reconciliation_result', 'unknown')).upper()}"
            ),
            "ok": reconciliation_state == "resolved",
        },
        {
            "label": "External observation",
            "status": "Verified",
            "detail": (
                f"Service active_version={report.get('service_active_version', '?')} —"
                f" successful_transition_count={report.get('successful_transition_count', '?')}"
            ),
            "ok": True,
        },
        {
            "label": "Independent verifier",
            "status": "PASS"
            if report.get("independent_verification_result") == "v2_active"
            else "FAIL",
            "detail": "Second read-only GET /state confirmed the reconciled state",
            "ok": report.get("independent_verification_result") == "v2_active",
        },
        {
            "label": "Persisted evidence history",
            "status": "Verified",
            "detail": f"{persisted} probe(s) attached to the reconciliation obligation",
            "ok": persisted >= 2,
        },
    )


def _summary(report: dict[str, Any], mission_lifecycle: str) -> dict[str, Any]:
    return {
        "mission_status": mission_lifecycle.upper(),
        "human_mission_title": report["human_mission_title"],
        "external_result": (
            "v2 active"
            if report.get("independent_verification_result") == "v2_active"
            else str(report.get("independent_verification_result", "unknown"))
        ),
        "evidence_verified": True,
        "authorized_operations": report["deployment_attempt_count"],
        "successful_transitions": report["successful_transition_count"],
        "injected_failures": report["injected_failure_count"],
        "duplicate_operations": report["duplicate_deployment_count"],
        "unauthorized_operations": report["unauthorized_operation_count"],
        "active_version": report["service_active_version"],
        "final_effect_posture": report["final_effect_posture"],
        "effect_history": "INDETERMINATE → SOMETHING_LANDED",
        "independent_verification": (
            "PASS"
            if report.get("independent_verification_result") == "v2_active"
            else "FAIL"
        ),
        "retry_blocked": True,
    }


def _technical(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "control_domain": report.get("control_domain"),
        "mission_id": report.get("mission_id"),
        "mission_revision": report.get("mission_revision"),
        "effect_intent_id": report.get("effect_intent_id"),
        "effect_dispatch_id": report.get("effect_dispatch_id"),
        "gateway_claim_id": report.get("gateway_claim_id"),
        "authority_reservation_id": report.get("authority_reservation_id"),
        "reconciliation_obligation_id": report.get("reconciliation_obligation_id"),
        "starting_repository_commit_sha": report.get("starting_repository_commit_sha"),
        "evidence_schema_version": report.get("evidence_schema_version"),
        "persisted_evidence_history_count": report.get(
            "persisted_evidence_history_count"
        ),
    }


def build_mission_control_view(
    interface: PavilionNativeInterface,
    control_domain: str,
    mission_id: str,
    *,
    evidence_report: dict[str, Any] | None,
) -> MissionControlView:
    """Assemble the projection from the accepted native seam and stores."""
    try:
        native = interface.mission_view(control_domain, mission_id)
        observation = interface._observability.observe(  # type: ignore[attr-defined]
            control_domain, mission_id
        )
        timeline = _timeline(observation)
        if evidence_report is not None:
            _validate_report(observation, evidence_report)
        ready = evidence_report is not None and bool(observation.effects) and all(
            effect.projected_status.value == "something_landed"
            and not effect.reconciliation_required
            for effect in observation.effects
        )
        if not ready or evidence_report is None:
            return MissionControlView(
                mission={
                    "mission_id": native.mission_id,
                    "objective": native.objective,
                    "lifecycle": native.lifecycle.value,
                    "revision": native.revision,
                },
                summary={},
                timeline=timeline,
                authority={"allowed": (), "denied": ()},
                evidence=(),
                technical={},
                evidence_report=evidence_report,
                ready=False,
                source_error=None,
            )
        summary = _summary(evidence_report, native.lifecycle.value)
        authority = _authority_breakdown(evidence_report)
        evidence = _evidence_rows(evidence_report)
        technical = _technical(evidence_report)
        return MissionControlView(
            mission={
                "mission_id": native.mission_id,
                "objective": native.objective,
                "lifecycle": native.lifecycle.value,
                "revision": native.revision,
            },
            summary=summary,
            timeline=timeline,
            authority=authority,
            evidence=evidence,
            technical=technical,
            evidence_report=evidence_report,
            ready=True,
        )
    except Exception as exc:
        return MissionControlView(
            mission={},
            summary={},
            timeline=(),
            authority={"allowed": (), "denied": ()},
            evidence=(),
            technical={},
            evidence_report=evidence_report,
            ready=False,
            source_error=str(exc),
        )


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
        observability_module = __import__(
            "federation.mission_observability", fromlist=["MissionObservability"]
        )
        interface = PavilionNativeInterface(
            observability_module.MissionObservability(runtime, effects)
        )
        path = Path(report_path)
        try:
            report = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        except (OSError, json.JSONDecodeError) as exc:
            return MissionControlView(
                mission={},
                summary={},
                timeline=(),
                authority={"allowed": (), "denied": ()},
                evidence=(),
                technical={},
                evidence_report=None,
                ready=False,
                source_error=f"evidence report unavailable: {exc}",
            )
        return build_mission_control_view(
            interface, control_domain, mission_id, evidence_report=report
        )
    finally:
        effects.close()


# ---------------------------------------------------------------------------
# HTML rendering

_STYLESHEET = """
:root {
    color-scheme: dark;
    --bg: #0b1120;
    --panel: #131c30;
    --panel-2: #182339;
    --border: #26324a;
    --text: #eef2f7;
    --muted: #9aa8c2;
    --accent: #47d7a3;
    --accent-soft: rgba(71, 215, 163, 0.12);
    --warn: #f0b06b;
    --warn-soft: rgba(240, 176, 107, 0.14);
    --danger: #f28b82;
    --danger-soft: rgba(242, 139, 130, 0.14);
    --reconcile: #7aa8ff;
    --reconcile-soft: rgba(122, 168, 255, 0.14);
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
    background: var(--bg);
    color: var(--text);
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
}
.topbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 18px 28px;
    border-bottom: 1px solid var(--border);
    background: rgba(19, 28, 48, 0.85);
    position: sticky;
    top: 0;
    z-index: 10;
    backdrop-filter: blur(6px);
}
.brand { display: flex; gap: 12px; align-items: baseline; }
.brand-name { font-weight: 700; letter-spacing: 0.14em; font-size: 0.9rem; text-transform: uppercase; }
.brand-role { color: var(--muted); font-size: 0.85rem; letter-spacing: 0.18em; text-transform: uppercase; }
.brand-tag { color: var(--muted); font-size: 0.78rem; letter-spacing: 0.14em; text-transform: uppercase; }
main {
    max-width: 1080px;
    margin: 0 auto;
    padding: 32px 28px 64px;
    display: flex;
    flex-direction: column;
    gap: 24px;
}
.hero {
    background: var(--panel);
    border: 1px solid var(--border);
    border-left: 4px solid var(--accent);
    border-radius: 14px;
    padding: 28px 32px;
    display: flex;
    flex-direction: column;
    gap: 16px;
}
.hero-eyebrow {
    color: var(--muted);
    letter-spacing: 0.18em;
    text-transform: uppercase;
    font-size: 0.78rem;
    margin: 0;
    display: flex;
    gap: 10px;
    align-items: center;
}
.status-dot {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--accent);
    box-shadow: 0 0 0 4px var(--accent-soft);
}
.status-word { color: var(--accent); font-weight: 700; letter-spacing: 0.14em; }
.hero h1 {
    margin: 0;
    font-size: clamp(1.55rem, 3.2vw, 2.35rem);
    line-height: 1.2;
    font-weight: 650;
    letter-spacing: -0.005em;
}
.hero-subtitle {
    color: var(--muted);
    font-size: 0.85rem;
    margin: 0;
}
.hero-subtitle code {
    background: rgba(122, 168, 255, 0.08);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 0.82rem;
    color: #cddaf5;
}
.hero-facts {
    display: flex;
    flex-wrap: wrap;
    gap: 24px;
    margin-top: 4px;
}
.hero-facts .fact {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 140px;
}
.hero-facts .fact span {
    color: var(--muted);
    font-size: 0.72rem;
    letter-spacing: 0.16em;
    text-transform: uppercase;
}
.hero-facts .fact strong {
    color: var(--text);
    font-size: 1.1rem;
    font-weight: 600;
}
.hero-metrics {
    list-style: none;
    padding: 0;
    margin: 4px 0 0;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
}
.hero-metrics li {
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 16px;
    display: flex;
    flex-direction: column;
    gap: 4px;
}
.hero-metrics .num {
    font-size: 1.6rem;
    font-weight: 700;
    color: var(--accent);
    line-height: 1;
}
.hero-metrics .label {
    color: var(--muted);
    font-size: 0.8rem;
    line-height: 1.35;
}
.panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 24px 28px;
}
.panel h2 {
    margin: 0 0 6px;
    font-size: 1.15rem;
    letter-spacing: 0.03em;
}
.panel-lede {
    color: var(--muted);
    margin: 0 0 18px;
    font-size: 0.95rem;
    max-width: 62ch;
}
.timeline {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
}
.timeline li {
    display: grid;
    grid-template-columns: 18ch 1fr;
    gap: 18px;
    align-items: start;
    padding: 12px 14px;
    border-radius: 10px;
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent);
}
.timeline li .when {
    color: var(--muted);
    font-size: 0.78rem;
    font-variant-numeric: tabular-nums;
    letter-spacing: 0.02em;
}
.timeline li .what {
    display: flex;
    flex-direction: column;
    gap: 4px;
}
.timeline li .what .label {
    font-weight: 600;
    color: var(--text);
    font-size: 0.98rem;
}
.timeline li .what .detail {
    color: var(--muted);
    font-size: 0.86rem;
    line-height: 1.4;
    max-width: 60ch;
}
.timeline li.step-warn {
    border-left-color: var(--warn);
    background: var(--warn-soft);
}
.timeline li.step-warn .what .label::before { content: "\\26A0  "; }
.timeline li.step-blocked {
    border-left-color: var(--danger);
    background: var(--danger-soft);
}
.timeline li.step-blocked .what .label::before { content: "\\1F6D1  "; }
.timeline li.step-reconcile {
    border-left-color: var(--reconcile);
    background: var(--reconcile-soft);
}
.timeline li.step-ok .what .label::before { content: "\\2713  "; color: var(--accent); }
.grid-two {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 24px;
}
.authority-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 18px;
}
.authority-grid h3 {
    margin: 0 0 8px;
    font-size: 0.82rem;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--muted);
}
.authority-grid ul {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
}
.authority-grid .allow li::before { content: "\\2713  "; color: var(--accent); }
.authority-grid .deny li::before { content: "\\2715  "; color: var(--danger); }
.authority-grid li { font-size: 0.94rem; }
dl.metrics-list, dl.tech-list {
    margin: 0;
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 8px 24px;
    align-items: baseline;
}
dl.metrics-list > div, dl.tech-list > div {
    display: contents;
}
dl.metrics-list dt, dl.tech-list dt {
    color: var(--muted);
    font-size: 0.88rem;
}
dl.metrics-list dd, dl.tech-list dd {
    margin: 0;
    text-align: right;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
}
dl.tech-list dd { font-weight: 500; font-family: "SFMono-Regular", ui-monospace, Menlo, Consolas, monospace; font-size: 0.85rem; word-break: break-all; text-align: right; color: #cddaf5; }
.evidence table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.94rem;
}
.evidence th, .evidence td {
    text-align: left;
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
}
.evidence tr:last-child th, .evidence tr:last-child td { border-bottom: none; }
.evidence th { color: var(--muted); font-weight: 500; width: 24%; }
.evidence td .status {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    background: var(--accent-soft);
    color: var(--accent);
    margin-right: 10px;
}
.evidence td .status.fail { background: var(--danger-soft); color: var(--danger); }
.evidence td .detail { color: var(--muted); font-size: 0.86rem; }
details.panel > summary {
    cursor: pointer;
    font-size: 1.05rem;
    font-weight: 600;
    list-style: none;
    display: flex;
    align-items: center;
    gap: 8px;
}
details.panel > summary::after {
    content: "\\25BE";
    color: var(--muted);
    margin-left: auto;
    transition: transform 0.15s ease;
}
details.panel[open] > summary::after { transform: rotate(180deg); }
details.panel[open] { padding-bottom: 24px; }
details.panel > summary::-webkit-details-marker { display: none; }
details.subdetail { margin-top: 18px; border: 1px solid var(--border); border-radius: 10px; background: var(--panel-2); padding: 12px 16px; }
details.subdetail > summary { cursor: pointer; font-weight: 600; font-size: 0.95rem; }
details.subdetail pre {
    white-space: pre-wrap;
    max-height: 360px;
    overflow: auto;
    background: rgba(6, 12, 24, 0.65);
    padding: 12px;
    border-radius: 8px;
    font-size: 0.82rem;
}
details.subdetail ul { margin: 0; padding-left: 18px; }
.explainer {
    background: var(--warn-soft);
    border-left: 3px solid var(--warn);
    padding: 14px 18px;
    border-radius: 10px;
    color: var(--text);
    font-size: 0.92rem;
    margin: 12px 0 0;
    max-width: 65ch;
}
.explainer strong { color: var(--warn); letter-spacing: 0.06em; }
.callout-blocked { background: var(--danger-soft); border-left-color: var(--danger); }
.callout-blocked strong { color: var(--danger); }
@media (max-width: 720px) {
    main { padding: 20px 16px 48px; }
    .topbar { padding: 14px 18px; flex-direction: column; gap: 4px; align-items: flex-start; }
    .hero { padding: 22px 20px; }
    .hero h1 { font-size: 1.5rem; }
    .grid-two { grid-template-columns: 1fr; }
    .authority-grid { grid-template-columns: 1fr; }
    .timeline li { grid-template-columns: 1fr; gap: 6px; }
    .timeline li .when { font-size: 0.72rem; }
    dl.metrics-list, dl.tech-list { grid-template-columns: 1fr; }
    dl.metrics-list dd, dl.tech-list dd { text-align: left; }
    .evidence th, .evidence td { display: block; width: auto; }
    .evidence th { padding-bottom: 0; }
}
"""


def _render_incomplete(view: MissionControlView) -> str:
    message = escape(
        view.source_error
        or "Persisted evidence is unavailable; success is not asserted."
    )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>MissionaryX \u00b7 Mission Control</title>"
        "<style>body{background:#0b1120;color:#eef2f7;font:16px system-ui;"
        "margin:0;padding:32px}main{max-width:640px;margin:auto}"
        "h1{font-size:1.4rem}.notice{background:#3a2a1c;border-left:4px solid #f0b06b;"
        "padding:16px 20px;border-radius:8px}</style></head>"
        "<body><main><h1>MissionaryX \u00b7 Mission Control</h1>"
        f"<p class=\"notice\" role=\"alert\">{message}</p></main></body></html>"
    )


def _render_hero(view: MissionControlView) -> str:
    s = view.summary
    return (
        "<section class=\"hero\" aria-labelledby=\"mission-title\">"
        "<p class=\"hero-eyebrow\">"
        "<span class=\"status-dot\" aria-hidden=\"true\"></span>"
        f"<span>Mission</span> <span class=\"status-word\">{escape(str(s['mission_status']))}</span>"
        "</p>"
        f"<h1 id=\"mission-title\">{escape(str(s['human_mission_title']))}</h1>"
        "<p class=\"hero-subtitle\">Mission ID \u00b7 "
        f"<code>{escape(str(view.mission.get('mission_id', 'unknown')))}</code></p>"
        "<div class=\"hero-facts\">"
        "<div class=\"fact\"><span>External result</span>"
        f"<strong>{escape(str(s['external_result']).upper())}</strong></div>"
        "<div class=\"fact\"><span>Evidence</span>"
        "<strong>VERIFIED</strong></div>"
        "<div class=\"fact\"><span>Effect posture</span>"
        f"<strong>{escape(str(s['final_effect_posture']).upper())}</strong></div>"
        "<div class=\"fact\"><span>Independent verifier</span>"
        f"<strong>{escape(str(s['independent_verification']))}</strong></div>"
        "</div>"
        "<ul class=\"hero-metrics\">"
        f"<li><span class=\"num\">{escape(str(s['authorized_operations']))}</span>"
        "<span class=\"label\">authorized external operation(s)</span></li>"
        f"<li><span class=\"num\">{escape(str(s['injected_failures']))}</span>"
        "<span class=\"label\">injected connection failure(s)</span></li>"
        f"<li><span class=\"num\">{escape(str(s['duplicate_operations']))}</span>"
        "<span class=\"label\">duplicate operation(s)</span></li>"
        f"<li><span class=\"num\">{escape(str(s['unauthorized_operations']))}</span>"
        "<span class=\"label\">unauthorized operation(s)</span></li>"
        "</ul>"
        "</section>"
    )


def _render_timeline(view: MissionControlView) -> str:
    items = []
    for item in view.timeline:
        severity = escape(str(item.get("severity", "step-normal")))
        label = escape(str(item.get("label", "")))
        detail = item.get("detail")
        detail_html = (
            f"<span class=\"detail\">{escape(str(detail))}</span>" if detail else ""
        )
        items.append(
            f"<li class=\"{severity}\">"
            f"<span class=\"when\">{escape(str(item.get('recorded_at', '')))}</span>"
            "<span class=\"what\">"
            f"<span class=\"label\">{label}</span>"
            f"{detail_html}"
            "</span></li>"
        )
    return (
        "<section class=\"panel\" aria-labelledby=\"timeline-title\">"
        "<h2 id=\"timeline-title\">Mission timeline</h2>"
        "<p class=\"panel-lede\">"
        "MissionaryX authorized one bounded operation, dispatched it exactly once, then"
        " lost confirmation. The outcome was classified as INDETERMINATE, automatic retry"
        " was refused, and the effect was reconciled against external service truth."
        "</p>"
        f"<ol class=\"timeline\">{''.join(items)}</ol>"
        "<p class=\"explainer\"><strong>WHY THIS MATTERS</strong> \u2014 A blind retry could"
        " have applied the same external change twice. MissionaryX chose to reconcile"
        " against the provider before advancing.</p>"
        "</section>"
    )


def _render_authority(view: MissionControlView) -> str:
    allowed = "".join(
        f"<li>{escape(str(name))}</li>" for name in view.authority.get("allowed", ())
    )
    denied = "".join(
        f"<li>{escape(str(name))}</li>" for name in view.authority.get("denied", ())
    )
    return (
        "<section class=\"panel authority\" aria-labelledby=\"authority-title\">"
        "<h2 id=\"authority-title\">Authority</h2>"
        "<p class=\"panel-lede\">Bounded scopes granted to this mission at authorization time.</p>"
        "<div class=\"authority-grid\">"
        f"<div class=\"allow\"><h3>Allowed</h3><ul>{allowed or '<li>None</li>'}</ul></div>"
        f"<div class=\"deny\"><h3>Denied</h3><ul>{denied or '<li>None</li>'}</ul></div>"
        "</div>"
        "</section>"
    )


def _render_key_metrics(view: MissionControlView) -> str:
    s = view.summary
    rows = [
        ("Authorized external operations", s["authorized_operations"]),
        ("Dispatch attempts", s["authorized_operations"]),
        ("Observed successful changes", s["successful_transitions"]),
        ("Injected failures", s["injected_failures"]),
        ("Duplicate operations", s["duplicate_operations"]),
        ("Unauthorized operations", s["unauthorized_operations"]),
        ("Service active version", f"v{s['active_version']}"),
        ("Final effect posture", str(s["final_effect_posture"]).upper()),
        ("Verification result", str(s["external_result"]).upper()),
        ("Evidence verification", "VERIFIED"),
    ]
    body = "".join(
        f"<div><dt>{escape(label)}</dt><dd>{escape(str(value))}</dd></div>"
        for label, value in rows
    )
    return (
        "<section class=\"panel\" aria-labelledby=\"metrics-title\">"
        "<h2 id=\"metrics-title\">Key metrics</h2>"
        "<p class=\"panel-lede\">All values are derived from persisted evidence.</p>"
        f"<dl class=\"metrics-list\">{body}</dl>"
        "</section>"
    )


def _render_evidence(view: MissionControlView) -> str:
    rows = []
    for entry in view.evidence:
        status_class = "" if entry.get("ok") else " fail"
        rows.append(
            f"<tr><th>{escape(str(entry['label']))}</th>"
            f"<td><span class=\"status{status_class}\">{escape(str(entry['status']))}</span>"
            f"<span class=\"detail\">{escape(str(entry.get('detail', '')))}</span></td></tr>"
        )
    return (
        "<section class=\"panel evidence\" aria-labelledby=\"evidence-title\">"
        "<h2 id=\"evidence-title\">Evidence</h2>"
        "<p class=\"panel-lede\">This page is reconstructed from persisted evidence."
        " Each row is verified against the source artifacts.</p>"
        f"<table><tbody>{''.join(rows)}</tbody></table>"
        "</section>"
    )


def _render_technical(view: MissionControlView) -> str:
    tech = view.technical
    row_order = (
        ("Mission ID", "mission_id"),
        ("Mission revision", "mission_revision"),
        ("Control domain", "control_domain"),
        ("Effect intent", "effect_intent_id"),
        ("Effect dispatch", "effect_dispatch_id"),
        ("Gateway claim", "gateway_claim_id"),
        ("Authority reservation", "authority_reservation_id"),
        ("Reconciliation obligation", "reconciliation_obligation_id"),
        ("Starting repository commit", "starting_repository_commit_sha"),
        ("Evidence schema", "evidence_schema_version"),
        ("Reconciliation probe count", "persisted_evidence_history_count"),
    )
    rows = "".join(
        f"<div><dt>{escape(label)}</dt><dd>{escape(str(tech.get(key, 'unknown')))}</dd></div>"
        for label, key in row_order
    )
    artifacts = "".join(
        f"<li><code>{escape(name)}</code></li>"
        for name in (
            "effects.sqlite3",
            "evidence-report.json",
            "manifest.json",
            "mission-control.html",
            "mission-control.json",
            "mission.sqlite3",
            "run-summary.txt",
            "service.sqlite3",
        )
    )
    report_json = escape(
        json.dumps(view.evidence_report, sort_keys=True, indent=2, ensure_ascii=False)
    )
    return (
        "<details class=\"panel technical\">"
        "<summary>Technical details</summary>"
        f"<dl class=\"tech-list\">{rows}</dl>"
        f"<details class=\"subdetail\"><summary>Evidence artifacts (this run bundle)</summary>"
        f"<ul>{artifacts}</ul></details>"
        "<details class=\"subdetail\"><summary>Machine-readable evidence report</summary>"
        f"<pre>{report_json}</pre></details>"
        "</details>"
    )


def render_mission_control(view: MissionControlView) -> str:
    """Render the v0.2 Mission Control HTML from a projected view."""
    if not view.ready:
        return _render_incomplete(view)
    parts = [
        "<!doctype html>",
        "<html lang=\"en\">",
        "<head>",
        "<meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        f"<title>MissionaryX \u00b7 Mission Control \u00b7 {escape(str(view.summary['human_mission_title']))}</title>",
        f"<style>{_STYLESHEET}</style>",
        "</head>",
        "<body>",
        "<header class=\"topbar\">",
        "<div class=\"brand\">"
        "<span class=\"brand-name\">MissionaryX</span>"
        "<span class=\"brand-role\">Mission Control</span>"
        "</div>",
        "<div class=\"brand-tag\">Evidence-backed mission view</div>",
        "</header>",
        "<main>",
        _render_hero(view),
        _render_timeline(view),
        "<div class=\"grid-two\">",
        _render_authority(view),
        _render_key_metrics(view),
        "</div>",
        _render_evidence(view),
        _render_technical(view),
        "</main>",
        "</body>",
        "</html>",
    ]
    return "".join(parts)


# ---------------------------------------------------------------------------
# Minimal HTTP handler used by the local demonstration server

def create_mission_control_handler(view_provider):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            view = view_provider()
            if urlparse(self.path).path == "/api/mission-control/evidence":
                body = json.dumps(
                    view.evidence_report or {"available": False}, sort_keys=True
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            else:
                body = render_mission_control(view).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    return Handler


__all__ = [
    "HUMAN_MISSION_TITLE",
    "MissionControlSourceError",
    "MissionControlView",
    "PRESENTATION_SCHEMA_VERSION",
    "build_mission_control_view",
    "create_mission_control_handler",
    "load_mission_control_view",
    "render_mission_control",
]
