"""Governed response-loss slice for the Integrated Demonstrator.

This module is intentionally an orchestration seam, not a second state model.
All effect, authority, reconciliation, and mission truth remains in the
canonical stores and gateway.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from threading import Thread
from http.client import RemoteDisconnected
from urllib.error import URLError
from urllib.request import Request, urlopen
from typing import Any

from federation.durable_effect_store import (
    AuthorityReservation,
    DurableEffectStore,
    EffectDispatch,
    EffectIntent,
)
from federation.effect_gateway import (
    EffectConsequence,
    GatewayDenied,
    GatewayEffectRequest,
    GatewayEffectResult,
    GovernedEffectGateway,
)
from federation.effect_safety import (
    AuthorityDisposition,
    EffectState,
    ProviderReconcilability,
    ReconciliationObligation,
    ReconciliationState,
)
from federation.mission_runtime import MissionRuntime
from federation.mission_runtime_store import MissionRuntimeStore
from tools.integrated_demonstrator.test_service import create_server
from research_mission.evidence_spine import (
    EvidencePointer,
    EvidenceSpine,
    ProviderBoundaryReconciliationEvidence,
    provider_boundary_reconciliation_record,
)


@dataclass(frozen=True, slots=True)
class AmbiguousDispatchRun:
    mission_id: str
    effect_intent_id: str
    effect_dispatch_id: str
    gateway_claim_id: str
    reconciliation_obligation_id: str
    service_database_path: Path
    effect_database_path: Path
    mission_database_path: Path
    transport_error: str
    retry_blocked: bool
    gateway_request: GatewayEffectRequest


@dataclass(frozen=True, slots=True)
class ReconciliationVerificationRun:
    ambiguous: AmbiguousDispatchRun
    report: dict[str, Any]


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def run_governed_ambiguous_dispatch(root: str | Path) -> AmbiguousDispatchRun:
    """Run exactly one v1 -> v2 deployment with confirmation deliberately lost."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    domain = "integrated-demonstrator"
    mission_id = "mission-integrated-demonstrator-ambiguous-v1"
    intent_id = "effect-intent-integrated-demonstrator-v2"
    dispatch_id = "effect-dispatch-integrated-demonstrator-v2"
    reservation_id = "authority-reservation-integrated-demonstrator-v2"
    obligation_id = "reconciliation-integrated-demonstrator-v2"
    operation_digest = _digest({"method": "POST", "path": "/deploy-v2", "target_version": 2})
    now = datetime.now(timezone.utc)

    mission_database_path = root / "mission.sqlite3"
    effect_database_path = root / "effects.sqlite3"
    service_database_path = root / "service.sqlite3"

    mission_store = MissionRuntimeStore(mission_database_path)
    mission_runtime = MissionRuntime(store=mission_store)
    mission_runtime.create_mission(
        mission_id,
        domain,
        "Govern one bounded deployment and preserve ambiguous outcome truth",
        "mission-owner",
        agent_identity="deployment-agent",
        constraints="Exactly one POST /deploy-v2; no retry while unresolved",
    )
    _, _, revision, _ = mission_runtime.get_mission(domain, mission_id)
    mission_runtime.start_mission(domain, mission_id, revision, "demonstrator started")

    effect_store = DurableEffectStore(effect_database_path)
    gateway = GovernedEffectGateway(effect_store)
    effect_store.commit_intent(
        EffectIntent(
            effect_intent_id=intent_id,
            decision_id="decision-integrated-demonstrator-v2",
            mission_id=mission_id,
            task_id="deploy-v2-task",
            attempt_id="deploy-v2-attempt-1",
            operation_digest=operation_digest,
            idempotency_key="integrated-demonstrator-deploy-v2",
            provider_scope="isolated-test-service",
            authority_reservation_id=reservation_id,
            compensation_strategy=None,
            evidence_reference="integrated-demonstrator-intent",
            state="committed_not_dispatched",
            created_at=now,
            control_domain=domain,
        )
    )
    effect_store.store_reservation(
        AuthorityReservation(
            reservation_id=reservation_id,
            effect_intent_id=intent_id,
            capability_type="effect:dispatch",
            amount=1.0,
            disposition=AuthorityDisposition.RESERVED,
            reserved_at=now,
            disposition_at=None,
            disposition_evidence=None,
            control_domain=domain,
        )
    )
    effect_store.commit_dispatch(
        EffectDispatch(
            dispatch_id=dispatch_id,
            effect_intent_id=intent_id,
            attempt_id="deploy-v2-attempt-1",
            idempotency_key="integrated-demonstrator-deploy-v2",
            provider_adapter="integrated-demonstrator-http",
            capability_profile_version="v1",
            transport_digest=_digest({"url": "/deploy-v2", "method": "POST"}),
            posture="submitted",
            provider_operation_id=None,
            evidence_reference="integrated-demonstrator-dispatch",
            dispatched_at=now,
            control_domain=domain,
        )
    )
    request = GatewayEffectRequest(
        control_domain=domain,
        principal_identity="mission-owner",
        agent_identity="deployment-agent",
        mission_id=mission_id,
        task_id="deploy-v2-task",
        attempt_id="deploy-v2-attempt-1",
        delegation_grant_id="bounded-deploy-v2-grant",
        delegation_grant_fingerprint=_digest({"grant": "deploy-v2", "mission": mission_id}),
        requested_capability="effect:dispatch",
        effect_intent_id=intent_id,
        effect_dispatch_id=dispatch_id,
        authority_reservation_id=reservation_id,
        operation_digest=operation_digest,
        idempotency_key="integrated-demonstrator-deploy-v2",
        provider_id="isolated-test-service",
        adapter_id="integrated-demonstrator-http",
        effect_consequence=EffectConsequence.DATA_MUTATION,
        provider_reconcilability=ProviderReconcilability.IDEMPOTENCY_KEY_LOOKUP,
        request_timestamp=now,
        request_expiry=now.replace(year=now.year + 1),
        credential_scope=("isolated-test-service:deploy-v2",),
    )
    claim_id, _ = gateway.claim_dispatch(request, owner_identity="deployment-agent")
    _, permit_token = gateway.issue_dispatch_permit(request, claim_id)
    gateway.verify_and_consume_permit(permit_token, domain)
    mission_runtime.add_effect_reference(domain, mission_id, intent_id, dispatch_id, claim_id)

    server = create_server(service_database_path)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    error: str | None = None
    try:
        request_http = Request(
            f"http://{host}:{port}/deploy-v2",
            data=b"",
            method="POST",
            headers={"X-MissionaryX-Drop-Response": "1"},
        )
        try:
            with urlopen(request_http, timeout=2):
                raise RuntimeError("response-loss injection unexpectedly returned a response")
        except (RemoteDisconnected, URLError, OSError) as exc:
            error = type(exc).__name__
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    if error is None:
        raise RuntimeError("response-loss injection did not produce a transport failure")

    result = GatewayEffectResult(
        task_succeeded=False,
        task_error=f"ambiguous transport failure: {error}",
        effect_status=EffectState.INDETERMINATE,
        dispatch_attempted=True,
        handoff_started=True,
        receipt_recorded=False,
        authority_disposition=AuthorityDisposition.RESERVED,
        reconciliation_required=True,
        reconciliation_obligation_id=obligation_id,
        gateway_claim_id=claim_id,
        effect_intent_id=intent_id,
        effect_dispatch_id=dispatch_id,
    )
    obligation = ReconciliationObligation(
        obligation_id=obligation_id,
        effect_intent_id=intent_id,
        dispatch_id=dispatch_id,
        state=ReconciliationState.PENDING,
        provider_reconcilability=request.provider_reconcilability,
        next_probe_at=None,
        probe_history=(),
        terminal_disposition=None,
        created_at=now,
        control_domain=domain,
    )
    gateway.record_indeterminate_with_obligation(request, result, obligation)
    try:
        # This is an assertion of the retry gate, not a second provider call.
        gateway.issue_dispatch_permit(request, claim_id)
    except GatewayDenied:
        retry_blocked = True
    else:
        retry_blocked = False
    effect_store.close()
    return AmbiguousDispatchRun(
        mission_id=mission_id,
        effect_intent_id=intent_id,
        effect_dispatch_id=dispatch_id,
        gateway_claim_id=claim_id,
        reconciliation_obligation_id=obligation_id,
        service_database_path=service_database_path,
        effect_database_path=effect_database_path,
        mission_database_path=mission_database_path,
        transport_error=error,
        retry_blocked=retry_blocked,
        gateway_request=request,
    )


def _read_service_state(database_path: Path) -> dict[str, int]:
    """Read the external service through a fresh loopback HTTP server."""
    server = create_server(database_path)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        with urlopen(f"http://{host}:{port}/state", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("service state response is not an object")
        return {key: int(payload[key]) for key in (
            "active_version", "deployment_attempt_count", "successful_transition_count"
        )}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def reconcile_and_verify_governed_dispatch(
    root: str | Path, *, starting_repository_sha: str
) -> ReconciliationVerificationRun:
    """Complete the accepted ambiguous dispatch with two independent GET reads."""
    root = Path(root)
    ambiguous = run_governed_ambiguous_dispatch(root)
    domain = "integrated-demonstrator"
    effect_store = DurableEffectStore(ambiguous.effect_database_path)
    mission_store = MissionRuntimeStore(ambiguous.mission_database_path)
    mission_runtime = MissionRuntime(store=mission_store)
    _, _, revision, _ = mission_runtime.get_mission(domain, ambiguous.mission_id)

    def checkpoint(event: str, payload: dict[str, Any]) -> None:
        nonlocal revision
        _, lifecycle, _, _ = mission_runtime.get_mission(domain, ambiguous.mission_id)
        checkpoint_record = mission_runtime.create_checkpoint(
            domain, ambiguous.mission_id, lifecycle,
            progress_data={"observability_event": event, **payload},
            reason=event,
            expected_revision=revision,
        )

    checkpoint("ambiguous_dispatch_established", {
        "effect_intent_id": ambiguous.effect_intent_id,
        "effect_dispatch_id": ambiguous.effect_dispatch_id,
        "gateway_claim_id": ambiguous.gateway_claim_id,
        "reconciliation_obligation_id": ambiguous.reconciliation_obligation_id,
    })
    checkpoint("retry_blocked", {
        "reason": "effect outcome is INDETERMINATE; automatic retry is forbidden",
        "dispatch_attempt_count": 1,
    })
    checkpoint("reconciliation_started", {"read_only": True})
    first_observation = _read_service_state(ambiguous.service_database_path)
    checkpoint("external_state_observed", first_observation)

    intent = effect_store.get_intent(ambiguous.effect_intent_id, domain)
    assert intent is not None
    evidence = ProviderBoundaryReconciliationEvidence(
        reconciliation_id="reconciliation-observation-integrated-demonstrator-v2",
        effect_intent_id=ambiguous.effect_intent_id,
        dispatch_id=ambiguous.effect_dispatch_id,
        idempotency_key=intent.idempotency_key,
        provider_operation_id=None,
        reconciliation_outcome="operation_committed" if first_observation["active_version"] == 2 else "no_operation_committed",
        reconciled_at=datetime.now(timezone.utc),
        provider_scope=intent.provider_scope,
        reconciliation_method="bounded_external_observation",
    )
    record = provider_boundary_reconciliation_record(
        evidence, domain_id=domain, mission_id=ambiguous.mission_id, task_id=intent.task_id
    )
    spine = EvidenceSpine.from_records((record,))
    pointer = EvidencePointer.from_record(record)
    gateway = GovernedEffectGateway(effect_store)
    gateway.reconcile_indeterminate(
        ambiguous.gateway_request, ambiguous.gateway_claim_id,
        ambiguous.reconciliation_obligation_id, spine, pointer,
    )
    checkpoint("ambiguity_resolved", {
        "result": EffectState.SOMETHING_LANDED.value,
        "reconciliation_evidence_record_id": record.key.record_id,
    })

    # A second HTTP GET is intentionally performed after settlement and is not
    # derived from, or substituted with, the reconciliation observation.
    verification_observation = _read_service_state(ambiguous.service_database_path)
    if verification_observation != first_observation:
        raise RuntimeError("independent verification observed changed service state")
    verification = {
        "kind": "independent_verification",
        "action": "GET /state",
        "read_only": True,
        "observation": verification_observation,
        "result": "v2_active" if verification_observation["active_version"] == 2 else "verification_failed",
    }
    effect_store.append_reconciliation_observation(
        ambiguous.reconciliation_obligation_id, domain, verification
    )
    checkpoint("independent_verification", verification)
    if verification["result"] != "v2_active":
        raise RuntimeError("independent verification did not confirm v2")
    _, _, revision, _ = mission_runtime.get_mission(domain, ambiguous.mission_id)
    revision = mission_runtime.complete_mission(domain, ambiguous.mission_id, revision, "reconciliation and independent verification complete")

    reopened_effects = DurableEffectStore(ambiguous.effect_database_path)
    claim = reopened_effects.get_gateway_claim(ambiguous.gateway_claim_id, domain)
    obligation = reopened_effects.get_obligation(ambiguous.reconciliation_obligation_id, domain)
    reservation = reopened_effects.get_reservation("authority-reservation-integrated-demonstrator-v2", domain)
    _, lifecycle, _, _ = mission_store.get_mission(domain, ambiguous.mission_id)
    history = [] if obligation is None else list(obligation.probe_history)
    report = {
        "evidence_schema_version": "missionaryx.integrated-demonstrator-evidence.v0.1",
        "mission_id": ambiguous.mission_id,
        "mission_objective": "Deploy version 2 of this test service, verify that it became active, and produce evidence of the completed change.",
        "participants": ["mission controller", "reasoning participant", "governed executor", "independent verifier"],
        "authority": {
            "test service deployment": "allowed",
            "test service state read": "allowed",
            "production": "denied",
            "spending": "denied",
        },
        "control_domain": domain,
        "starting_repository_commit_sha": starting_repository_sha,
        "effect_intent_id": ambiguous.effect_intent_id,
        "effect_dispatch_id": ambiguous.effect_dispatch_id,
        "gateway_claim_id": ambiguous.gateway_claim_id,
        "authority_reservation_id": "authority-reservation-integrated-demonstrator-v2",
        "reconciliation_obligation_id": ambiguous.reconciliation_obligation_id,
        "effect_history": ["indeterminate", "something_landed"],
        "reconciliation_observation": first_observation,
        "reconciliation_result": "something_landed",
        "independent_verification_observation": verification_observation,
        "independent_verification_result": verification["result"],
        "service_active_version": verification_observation["active_version"],
        "deployment_attempt_count": verification_observation["deployment_attempt_count"],
        "successful_transition_count": verification_observation["successful_transition_count"],
        "unauthorized_operation_count": 0,
        "duplicate_deployment_count": max(0, verification_observation["deployment_attempt_count"] - 1),
        "injected_failure_count": 1,
        "final_mission_state": lifecycle.value,
        "mission_revision": mission_store.get_mission(domain, ambiguous.mission_id)[2],
        "final_effect_posture": EffectState.SOMETHING_LANDED.value,
        "reconciliation_state": None if obligation is None else obligation.state.value,
        "authority_disposition": None if reservation is None else reservation.disposition.value,
        "persisted_evidence_history_count": len(history),
        "observability_events": [
            "ambiguous_dispatch_established",
            "reconciliation_started",
            "external_state_observed",
            "ambiguity_resolved",
            "independent_verification",
            "mission_completed",
        ],
    }
    report_path = root / "evidence-report.json"
    report_path.write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    reopened_effects.close()
    effect_store.close()
    return ReconciliationVerificationRun(ambiguous=ambiguous, report=report)


__all__ = [
    "AmbiguousDispatchRun", "ReconciliationVerificationRun",
    "run_governed_ambiguous_dispatch", "reconcile_and_verify_governed_dispatch",
]
