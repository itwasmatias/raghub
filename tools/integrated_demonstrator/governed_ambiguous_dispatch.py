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
    )


__all__ = ["AmbiguousDispatchRun", "run_governed_ambiguous_dispatch"]
