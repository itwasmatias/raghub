"""Mission Observability v0.1 contract, isolation, and read-only attacks."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from federation.durable_effect_store import DurableEffectStore, StorageIntegrityError
from federation.effect_safety import (
    AuthorityDisposition,
    AuthorityReservation,
    EffectDispatch,
    EffectIntent,
)
from federation.mission_observability import (
    MissionObservability,
    MissionObservabilityConcurrentChangeError,
    MissionObservabilityIntegrityError,
    MissionObservabilityNotFoundError,
    ProjectedEffectStatus,
)
from federation.mission_runtime import MissionRuntime
from federation.mission_state import MissionLifecycle
from research_mission.evidence_spine import (
    EvidenceCorrelationKey,
    EvidenceRecord,
    EvidenceReference,
    EvidenceSpine,
)


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
DOMAIN_A = "observability-domain-a"
DOMAIN_B = "observability-domain-b"
MISSION_ID = "mission-shared"


def _runtime_and_store(
    tmp_path: Path,
    *,
    domain: str = DOMAIN_A,
    mission_id: str = MISSION_ID,
    suffix: str = "default",
    objective: str | None = None,
) -> tuple[MissionRuntime, DurableEffectStore]:
    runtime = MissionRuntime(db_path=tmp_path / f"missions-{suffix}.sqlite3")
    store = DurableEffectStore(tmp_path / f"effects-{suffix}.sqlite3")
    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain,
        objective=objective or f"Observe {domain}",
        owner_identity=f"owner-{domain}",
        metadata={"immutable": {"value": 1}},
    )
    return runtime, store


def _intent(
    *,
    domain: str = DOMAIN_A,
    mission_id: str = MISSION_ID,
    intent_id: str = "intent-1",
    reservation_id: str = "reservation-1",
    at: datetime = NOW,
) -> EffectIntent:
    return EffectIntent(
        effect_intent_id=intent_id,
        decision_id=f"decision-{intent_id}",
        mission_id=mission_id,
        task_id="task-1",
        attempt_id=f"attempt-{intent_id}",
        operation_digest="a" * 64,
        idempotency_key=f"idempotency-{intent_id}",
        provider_scope="provider-scope",
        authority_reservation_id=reservation_id,
        compensation_strategy=None,
        evidence_reference=f"evidence-{intent_id}",
        state="committed_not_dispatched",
        created_at=at,
        control_domain=domain,
    )


def _dispatch(
    intent: EffectIntent,
    *,
    dispatch_id: str = "dispatch-1",
    at: datetime = NOW + timedelta(minutes=1),
) -> EffectDispatch:
    return EffectDispatch(
        dispatch_id=dispatch_id,
        effect_intent_id=intent.effect_intent_id,
        attempt_id=intent.attempt_id,
        idempotency_key=intent.idempotency_key,
        provider_adapter="adapter-1",
        capability_profile_version="v1",
        transport_digest="b" * 64,
        posture="accepted_by_transport",
        provider_operation_id="provider-operation-1",
        evidence_reference="dispatch-evidence-1",
        dispatched_at=at,
        control_domain=intent.control_domain,
    )


def _reservation(
    intent: EffectIntent,
    *,
    at: datetime = NOW,
) -> AuthorityReservation:
    return AuthorityReservation(
        reservation_id=intent.authority_reservation_id,
        effect_intent_id=intent.effect_intent_id,
        capability_type="external-effect",
        amount=1.0,
        disposition=AuthorityDisposition.RESERVED,
        reserved_at=at,
        disposition_at=None,
        disposition_evidence=None,
        control_domain=intent.control_domain,
    )


def _commit_effect(
    store: DurableEffectStore,
    *,
    domain: str = DOMAIN_A,
    mission_id: str = MISSION_ID,
    intent_id: str = "intent-1",
    dispatch_id: str = "dispatch-1",
    reservation_id: str = "reservation-1",
    include_dispatch: bool = False,
    include_claim: bool = False,
    claim_id: str = "claim-1",
    at: datetime = NOW,
) -> tuple[EffectIntent, EffectDispatch | None]:
    intent = _intent(
        domain=domain,
        mission_id=mission_id,
        intent_id=intent_id,
        reservation_id=reservation_id,
        at=at,
    )
    store.commit_intent(intent)
    store.store_reservation(_reservation(intent, at=at))
    dispatch = None
    if include_dispatch or include_claim:
        dispatch = _dispatch(intent, dispatch_id=dispatch_id, at=at + timedelta(minutes=1))
        store.commit_dispatch(dispatch)
    if include_claim:
        assert dispatch is not None
        store.claim_gateway_dispatch(
            gateway_claim_id=claim_id,
            request_fingerprint="c" * 64,
            effect_intent_id=intent.effect_intent_id,
            effect_dispatch_id=dispatch.dispatch_id,
            authority_reservation_id=intent.authority_reservation_id,
            delegation_grant_id="delegation-1",
            delegation_grant_fingerprint="d" * 64,
            requested_capability="effect:dispatch",
            idempotency_key=intent.idempotency_key,
            operation_digest=intent.operation_digest,
            provider_id="provider-1",
            adapter_id=dispatch.provider_adapter,
            owner_identity="owner-1",
            claimed_at=at + timedelta(minutes=2),
            expires_at=at + timedelta(hours=1),
            control_domain=domain,
        )
    return intent, dispatch


def _make_claim_indeterminate(
    store: DurableEffectStore,
    intent: EffectIntent,
    dispatch: EffectDispatch,
    *,
    claim_id: str = "claim-1",
) -> str:
    verifier = "e" * 64
    store.issue_gateway_permit(
        permit_id="permit-1",
        permit_verifier=verifier,
        request_fingerprint="c" * 64,
        effect_intent_id=intent.effect_intent_id,
        effect_dispatch_id=dispatch.dispatch_id,
        authority_reservation_id=intent.authority_reservation_id,
        gateway_claim_id=claim_id,
        delegation_grant_id="delegation-1",
        delegation_grant_fingerprint="d" * 64,
        requested_capability="effect:dispatch",
        operation_digest=intent.operation_digest,
        idempotency_key=intent.idempotency_key,
        provider_id="provider-1",
        adapter_id=dispatch.provider_adapter,
        credential_scope=("scope-a",),
        owner_identity="owner-1",
        issued_at=NOW + timedelta(minutes=2),
        expires_at=NOW + timedelta(hours=1),
        control_domain=intent.control_domain,
    )
    store.verify_and_consume_permit(
        permit_verifier=verifier,
        control_domain=intent.control_domain,
        now=NOW + timedelta(minutes=3),
    )
    store.record_gateway_result(
        gateway_claim_id=claim_id,
        control_domain=intent.control_domain,
        effect_status="indeterminate",
        now=NOW + timedelta(minutes=4),
    )
    return verifier


def _make_claim_terminal(
    store: DurableEffectStore,
    intent: EffectIntent,
    dispatch: EffectDispatch,
    *,
    effect_status: str,
    claim_id: str = "claim-1",
) -> str:
    verifier = "9" * 64
    store.issue_gateway_permit(
        permit_id="permit-terminal",
        permit_verifier=verifier,
        request_fingerprint="c" * 64,
        effect_intent_id=intent.effect_intent_id,
        effect_dispatch_id=dispatch.dispatch_id,
        authority_reservation_id=intent.authority_reservation_id,
        gateway_claim_id=claim_id,
        delegation_grant_id="delegation-1",
        delegation_grant_fingerprint="d" * 64,
        requested_capability="effect:dispatch",
        operation_digest=intent.operation_digest,
        idempotency_key=intent.idempotency_key,
        provider_id="provider-1",
        adapter_id=dispatch.provider_adapter,
        credential_scope=("scope-a",),
        owner_identity="owner-1",
        issued_at=NOW + timedelta(minutes=2),
        expires_at=NOW + timedelta(hours=1),
        control_domain=intent.control_domain,
    )
    store.verify_and_consume_permit(
        permit_verifier=verifier,
        control_domain=intent.control_domain,
        now=NOW + timedelta(minutes=3),
    )
    store.record_gateway_receipt(
        gateway_claim_id=claim_id,
        control_domain=intent.control_domain,
        now=NOW + timedelta(minutes=4),
    )
    store.record_gateway_result(
        gateway_claim_id=claim_id,
        control_domain=intent.control_domain,
        effect_status=effect_status,
        now=NOW + timedelta(minutes=5),
    )
    return verifier


def _add_reference(
    runtime: MissionRuntime,
    intent: EffectIntent,
    dispatch: EffectDispatch | None = None,
    *,
    claim_id: str | None = None,
) -> None:
    runtime.add_effect_reference(
        control_domain=intent.control_domain,
        mission_id=intent.mission_id,
        effect_intent_id=intent.effect_intent_id,
        effect_dispatch_id=None if dispatch is None else dispatch.dispatch_id,
        gateway_claim_id=claim_id,
    )


def _evidence_record(
    *,
    domain: str | None,
    mission_id: str = MISSION_ID,
    record_id: str,
    source: str = "research_evidence",
    at: datetime = NOW,
    fingerprint_char: str = "f",
) -> EvidenceRecord:
    return EvidenceRecord(
        key=EvidenceCorrelationKey(
            source=source,
            record_id=record_id,
            mission_id=mission_id,
            task_id="task-1",
            domain_id=domain,
        ),
        reference=EvidenceReference(
            source_revision=f"revision-{record_id}",
            fingerprint=fingerprint_char * 64,
            observed_at=at,
            summary=f"summary-{record_id}",
            reference=f"reference-{record_id}",
        ),
        payload={"raw_credential": "must-not-be-projected"},
        metadata={"private": "must-not-be-projected"},
    )


class _EffectReadOverride:
    """Delegate public reads except explicitly malformed authoritative results."""

    _BASE = object()

    def __init__(
        self,
        base: DurableEffectStore,
        *,
        intent: Any = _BASE,
        dispatch: Any = _BASE,
        claim: Any = _BASE,
        reservation: Any = _BASE,
    ) -> None:
        self.base = base
        self.intent = intent
        self.dispatch = dispatch
        self.claim = claim
        self.reservation = reservation

    def get_intent(self, effect_intent_id: str, control_domain: str):
        if self.intent is not self._BASE:
            return self.intent
        return self.base.get_intent(effect_intent_id, control_domain)

    def get_dispatch(self, dispatch_id: str, control_domain: str):
        if self.dispatch is not self._BASE:
            return self.dispatch
        return self.base.get_dispatch(dispatch_id, control_domain)

    def get_gateway_claim(self, gateway_claim_id: str, control_domain: str):
        if self.claim is not self._BASE:
            return self.claim
        return self.base.get_gateway_claim(gateway_claim_id, control_domain)

    def get_reservation(
        self,
        reservation_id: str,
        control_domain: str,
        evidence_spine: EvidenceSpine | None = None,
    ):
        if self.reservation is not self._BASE:
            return self.reservation
        return self.base.get_reservation(reservation_id, control_domain, evidence_spine)


def _observe(
    runtime: Any,
    store: Any,
    *,
    domain: str = DOMAIN_A,
    mission_id: str = MISSION_ID,
    evidence_spine: EvidenceSpine | None = None,
    generated_at: datetime = NOW + timedelta(days=1),
):
    return MissionObservability(
        runtime,
        store,
        evidence_spine=evidence_spine,
        clock=lambda: generated_at,
    ).observe(domain, mission_id)


def test_basic_observation_of_created_mission(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)

    observation = _observe(runtime, store)

    assert observation.control_domain == DOMAIN_A
    assert observation.mission_id == MISSION_ID
    assert observation.lifecycle is MissionLifecycle.CREATED
    assert observation.revision == 1
    assert observation.specification.objective == f"Observe {DOMAIN_A}"
    assert observation.specification.specification_fingerprint is not None
    assert observation.effects == ()
    assert observation.evidence_available is False
    assert len(observation.projection_fingerprint) == 64


def test_missing_mission_has_typed_not_found_error(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)

    with pytest.raises(MissionObservabilityNotFoundError):
        _observe(runtime, store, mission_id="missing-mission")


def test_mission_transitions_are_projected(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    runtime.start_mission(DOMAIN_A, MISSION_ID, expected_revision=1, reason="Begin")
    runtime.pause_mission(DOMAIN_A, MISSION_ID, expected_revision=2, reason="Hold")

    observation = _observe(runtime, store)

    assert observation.lifecycle is MissionLifecycle.PAUSED
    assert observation.revision == 3
    assert tuple(item.to_state for item in observation.transitions) == (
        MissionLifecycle.CREATED,
        MissionLifecycle.RUNNING,
        MissionLifecycle.PAUSED,
    )
    assert tuple(item.revision for item in observation.transitions) == (1, 2, 3)


def test_mission_checkpoints_are_projected_immutably(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    runtime.create_checkpoint(
        DOMAIN_A,
        MISSION_ID,
        MissionLifecycle.CREATED,
        checkpoint_id="checkpoint-1",
        sequence=0,
        progress_data={"nested": {"percent": 20}},
        reason="Recorded progress",
    )

    observation = _observe(runtime, store)

    assert len(observation.checkpoints) == 1
    checkpoint = observation.checkpoints[0]
    assert checkpoint.checkpoint_id == "checkpoint-1"
    assert checkpoint.progress_data_json == '{"nested":{"percent":20}}'


def test_effect_reference_alone_never_invents_effect_outcome(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, _ = _commit_effect(store)
    _add_reference(runtime, intent)

    observation = _observe(runtime, store)

    effect = observation.effects[0]
    assert effect.projected_status is ProjectedEffectStatus.UNKNOWN
    assert effect.gateway_claim is None
    assert effect.reconciliation_required is False
    assert observation.unresolved_effects[0].reason == "effect_outcome_unavailable"


def test_intent_resolves_by_exact_domain_and_id(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, _ = _commit_effect(store, intent_id="intent-exact")
    _add_reference(runtime, intent)

    effect = _observe(runtime, store).effects[0]

    assert effect.intent.effect_intent_id == "intent-exact"
    assert effect.intent.control_domain == DOMAIN_A
    assert effect.intent.mission_id == MISSION_ID


def test_dispatch_resolves_and_binds_exact_intent(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_dispatch=True)
    assert dispatch is not None
    _add_reference(runtime, intent, dispatch)

    effect = _observe(runtime, store).effects[0]

    assert effect.dispatch is not None
    assert effect.dispatch.dispatch_id == dispatch.dispatch_id
    assert effect.dispatch.effect_intent_id == intent.effect_intent_id


def test_gateway_claim_resolves_and_binds_intent_dispatch(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    _add_reference(runtime, intent, dispatch, claim_id="claim-1")

    effect = _observe(runtime, store).effects[0]

    assert effect.gateway_claim is not None
    assert effect.gateway_claim.gateway_claim_id == "claim-1"
    assert effect.gateway_claim.control_domain == DOMAIN_A
    assert effect.gateway_claim.effect_intent_id == intent.effect_intent_id
    assert effect.gateway_claim.effect_dispatch_id == dispatch.dispatch_id
    assert effect.gateway_claim.authority_reservation_id == intent.authority_reservation_id
    assert effect.gateway_claim.requested_capability == "effect:dispatch"


def test_claim_authoritative_dispatch_is_resolved_when_reference_omits_it(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    _add_reference(runtime, intent, claim_id="claim-1")

    effect = _observe(runtime, store).effects[0]

    assert effect.reference.effect_dispatch_id is None
    assert effect.dispatch is not None
    assert effect.dispatch.dispatch_id == dispatch.dispatch_id


def test_reservation_resolves_and_binds_exact_intent(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, _ = _commit_effect(store)
    _add_reference(runtime, intent)

    reservation = _observe(runtime, store).effects[0].reservation

    assert reservation.reservation_id == intent.authority_reservation_id
    assert reservation.effect_intent_id == intent.effect_intent_id
    assert reservation.disposition is AuthorityDisposition.RESERVED


def test_indeterminate_claim_remains_visible_and_unresolved(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    _make_claim_indeterminate(store, intent, dispatch)
    _add_reference(runtime, intent, dispatch, claim_id="claim-1")

    observation = _observe(runtime, store)
    effect = observation.effects[0]

    assert effect.gateway_claim is not None
    assert effect.gateway_claim.state == "indeterminate"
    assert effect.projected_status is ProjectedEffectStatus.INDETERMINATE
    assert effect.reconciliation_required is True
    assert observation.unresolved_effects[0].reconciliation_required is True


@pytest.mark.parametrize("effect_status", ["nothing_landed", "something_landed"])
def test_terminal_claim_does_not_reconstruct_unexposed_effect_result(
    tmp_path: Path,
    effect_status: str,
) -> None:
    runtime, store = _runtime_and_store(tmp_path, suffix=effect_status)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    _make_claim_terminal(store, intent, dispatch, effect_status=effect_status)
    _add_reference(runtime, intent, dispatch, claim_id="claim-1")

    effect = _observe(runtime, store).effects[0]

    assert effect.gateway_claim is not None
    assert effect.gateway_claim.state == "terminal"
    assert effect.projected_status is ProjectedEffectStatus.UNKNOWN
    assert effect.status_basis == "effect_outcome_unavailable_from_public_reads"


@pytest.mark.parametrize(
    ("terminal_state", "transition"),
    [
        (MissionLifecycle.FAILED, "fail_mission"),
        (MissionLifecycle.COMPLETED, "complete_mission"),
    ],
)
def test_terminal_mission_state_never_implies_effect_outcome(
    tmp_path: Path,
    terminal_state: MissionLifecycle,
    transition: str,
) -> None:
    runtime, store = _runtime_and_store(tmp_path, suffix=terminal_state.value)
    intent, _ = _commit_effect(store)
    _add_reference(runtime, intent)
    revision = runtime.start_mission(DOMAIN_A, MISSION_ID, 1)
    if transition == "fail_mission":
        runtime.fail_mission(DOMAIN_A, MISSION_ID, revision, "mission failed")
    else:
        runtime.complete_mission(DOMAIN_A, MISSION_ID, revision)

    observation = _observe(runtime, store)

    assert observation.lifecycle is terminal_state
    assert observation.effects[0].projected_status is ProjectedEffectStatus.UNKNOWN


def test_missing_effect_intent_fails_closed(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    runtime.add_effect_reference(DOMAIN_A, MISSION_ID, "missing-intent")

    with pytest.raises(MissionObservabilityIntegrityError, match="intent"):
        _observe(runtime, store)


def test_missing_referenced_dispatch_fails_closed(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, _ = _commit_effect(store)
    runtime.add_effect_reference(
        DOMAIN_A,
        MISSION_ID,
        intent.effect_intent_id,
        effect_dispatch_id="missing-dispatch",
    )

    with pytest.raises(MissionObservabilityIntegrityError, match="dispatch"):
        _observe(runtime, store)


def test_missing_referenced_gateway_claim_fails_closed(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_dispatch=True)
    assert dispatch is not None
    _add_reference(runtime, intent, dispatch, claim_id="missing-claim")

    with pytest.raises(MissionObservabilityIntegrityError, match="claim"):
        _observe(runtime, store)


def test_missing_authoritative_reservation_fails_closed(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent = _intent()
    store.commit_intent(intent)
    _add_reference(runtime, intent)

    with pytest.raises(MissionObservabilityIntegrityError, match="reservation"):
        _observe(runtime, store)


def test_cross_intent_dispatch_mismatch_fails_closed(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_dispatch=True)
    assert dispatch is not None
    _add_reference(runtime, intent, dispatch)
    malformed = replace(dispatch, effect_intent_id="other-intent")

    with pytest.raises(MissionObservabilityIntegrityError, match="dispatch.*intent"):
        _observe(runtime, _EffectReadOverride(store, dispatch=malformed))


def test_cross_intent_gateway_claim_mismatch_fails_closed(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    _add_reference(runtime, intent, dispatch, claim_id="claim-1")
    claim = store.get_gateway_claim("claim-1", DOMAIN_A)
    assert claim is not None
    malformed = {**claim, "effect_intent_id": "other-intent"}

    with pytest.raises(MissionObservabilityIntegrityError, match="claim.*intent"):
        _observe(runtime, _EffectReadOverride(store, claim=malformed))


def test_cross_dispatch_gateway_claim_mismatch_fails_closed(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    _add_reference(runtime, intent, dispatch, claim_id="claim-1")
    claim = store.get_gateway_claim("claim-1", DOMAIN_A)
    assert claim is not None
    malformed = {**claim, "effect_dispatch_id": "other-dispatch"}

    with pytest.raises(MissionObservabilityIntegrityError, match="claim.*dispatch"):
        _observe(runtime, _EffectReadOverride(store, claim=malformed))


def test_cross_intent_reservation_mismatch_fails_closed(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, _ = _commit_effect(store)
    _add_reference(runtime, intent)
    malformed = replace(_reservation(intent), effect_intent_id="other-intent")

    with pytest.raises(MissionObservabilityIntegrityError, match="reservation.*intent"):
        _observe(runtime, _EffectReadOverride(store, reservation=malformed))


def test_intent_mission_mismatch_fails_closed(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, _ = _commit_effect(store, mission_id="other-mission")
    runtime.add_effect_reference(DOMAIN_A, MISSION_ID, intent.effect_intent_id)

    with pytest.raises(MissionObservabilityIntegrityError, match="mission"):
        _observe(runtime, store)


def test_cross_domain_effect_ids_cannot_leak(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent_a, _ = _commit_effect(
        store,
        domain=DOMAIN_A,
        intent_id="shared-intent",
        reservation_id="shared-reservation",
    )
    _commit_effect(
        store,
        domain=DOMAIN_B,
        mission_id=MISSION_ID,
        intent_id="shared-intent",
        reservation_id="shared-reservation",
    )
    _add_reference(runtime, intent_a)

    effect = _observe(runtime, store).effects[0]

    assert effect.intent.control_domain == DOMAIN_A
    assert effect.reservation.control_domain == DOMAIN_A


def test_same_mission_id_in_two_domains_remains_isolated(tmp_path: Path) -> None:
    runtime = MissionRuntime(db_path=tmp_path / "missions.sqlite3")
    store = DurableEffectStore(tmp_path / "effects.sqlite3")
    runtime.create_mission(MISSION_ID, DOMAIN_A, "Objective A", "owner-a")
    runtime.create_mission(MISSION_ID, DOMAIN_B, "Objective B", "owner-b")

    observation_a = _observe(runtime, store, domain=DOMAIN_A)
    observation_b = _observe(runtime, store, domain=DOMAIN_B)

    assert observation_a.specification.objective == "Objective A"
    assert observation_b.specification.objective == "Objective B"
    assert observation_a.projection_fingerprint != observation_b.projection_fingerprint


def test_same_effect_and_claim_ids_in_two_domains_remain_isolated(tmp_path: Path) -> None:
    runtime = MissionRuntime(db_path=tmp_path / "missions.sqlite3")
    store = DurableEffectStore(tmp_path / "effects.sqlite3")
    runtime.create_mission(MISSION_ID, DOMAIN_A, "Objective A", "owner-a")
    runtime.create_mission(MISSION_ID, DOMAIN_B, "Objective B", "owner-b")
    intent_a, dispatch_a = _commit_effect(
        store,
        domain=DOMAIN_A,
        intent_id="shared-intent",
        dispatch_id="shared-dispatch",
        reservation_id="shared-reservation",
        include_claim=True,
        claim_id="shared-claim",
    )
    intent_b, dispatch_b = _commit_effect(
        store,
        domain=DOMAIN_B,
        intent_id="shared-intent",
        dispatch_id="shared-dispatch",
        reservation_id="shared-reservation",
        include_claim=True,
        claim_id="shared-claim",
    )
    assert dispatch_a is not None and dispatch_b is not None
    _add_reference(runtime, intent_a, dispatch_a, claim_id="shared-claim")
    _add_reference(runtime, intent_b, dispatch_b, claim_id="shared-claim")

    effect_a = _observe(runtime, store, domain=DOMAIN_A).effects[0]
    effect_b = _observe(runtime, store, domain=DOMAIN_B).effects[0]

    assert effect_a.intent.control_domain == DOMAIN_A
    assert effect_b.intent.control_domain == DOMAIN_B
    assert effect_a.gateway_claim is not None
    assert effect_b.gateway_claim is not None
    assert effect_a.gateway_claim.control_domain == DOMAIN_A
    assert effect_b.gateway_claim.control_domain == DOMAIN_B
    assert effect_a.reservation.control_domain == DOMAIN_A
    assert effect_b.reservation.control_domain == DOMAIN_B


def test_evidence_for_same_mission_in_other_domain_is_excluded(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    record_a = _evidence_record(domain=DOMAIN_A, record_id="record-a")
    record_b = _evidence_record(domain=DOMAIN_B, record_id="record-b", fingerprint_char="1")
    spine = EvidenceSpine.from_records((record_b, record_a))

    observation = _observe(runtime, store, evidence_spine=spine)
    domain_only = _observe(
        runtime,
        store,
        evidence_spine=EvidenceSpine.from_records((record_a,)),
    )

    assert tuple(item.record_id for item in observation.evidence) == ("record-a",)
    assert DOMAIN_B not in observation.to_json()
    assert "record-b" not in observation.to_json()
    assert observation.projection_fingerprint == domain_only.projection_fingerprint


def test_unbound_evidence_is_not_attributed_and_is_accounted(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    unbound = _evidence_record(domain=None, record_id="unbound")
    spine = EvidenceSpine.from_records((unbound,))

    observation = _observe(runtime, store, evidence_spine=spine)

    assert observation.evidence == ()
    assert observation.unbound_evidence_count == 1
    assert '"record_id":"unbound"' not in observation.to_json()


def test_verified_domain_matching_evidence_appears_without_raw_payload(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    record = _evidence_record(domain=DOMAIN_A, record_id="verified")
    spine = EvidenceSpine.from_records((record,))

    evidence = _observe(runtime, store, evidence_spine=spine).evidence[0]

    assert evidence.record_id == "verified"
    assert evidence.record_fingerprint == record.record_fingerprint
    assert evidence.reference_fingerprint == record.reference.fingerprint
    serialized = evidence.to_json()
    assert "must-not-be-projected" not in serialized
    assert "raw_credential" not in serialized


def test_timeline_ordering_is_deterministic(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    _add_reference(runtime, intent, dispatch, claim_id="claim-1")
    spine = EvidenceSpine.from_records(
        (_evidence_record(domain=DOMAIN_A, record_id="record-1", at=NOW),)
    )
    observability = MissionObservability(runtime, store, evidence_spine=spine, clock=lambda: NOW)

    first = observability.observe(DOMAIN_A, MISSION_ID)
    second = observability.observe(DOMAIN_A, MISSION_ID)

    assert first.timeline == second.timeline
    assert observability.timeline(DOMAIN_A, MISSION_ID) == first.timeline
    assert tuple(
        (event.recorded_at, event.source_type, event.source_id, event.source_fingerprint)
        for event in first.timeline
    ) == tuple(
        sorted(
            (
                event.recorded_at,
                event.source_type,
                event.source_id,
                event.source_fingerprint,
            )
            for event in first.timeline
        )
    )


def test_equal_timestamps_use_stable_tie_breaker(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    first_intent, _ = _commit_effect(
        store,
        intent_id="intent-b",
        reservation_id="reservation-b",
        at=NOW,
    )
    second_intent, _ = _commit_effect(
        store,
        intent_id="intent-a",
        reservation_id="reservation-a",
        at=NOW,
    )
    _add_reference(runtime, first_intent)
    _add_reference(runtime, second_intent)

    events = [
        event
        for event in _observe(runtime, store).timeline
        if event.source_type == "effect_intent" and event.recorded_at == NOW
    ]

    assert [event.source_id for event in events] == ["intent-a", "intent-b"]


def test_projection_fingerprint_stable_over_unchanged_sources(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)

    first = _observe(runtime, store, generated_at=NOW)
    second = _observe(runtime, store, generated_at=NOW + timedelta(hours=1))

    assert first.projection_fingerprint == second.projection_fingerprint
    assert first.generated_at != second.generated_at


def test_authoritative_source_change_changes_projection_fingerprint(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    before = _observe(runtime, store)
    runtime.create_checkpoint(
        DOMAIN_A,
        MISSION_ID,
        MissionLifecycle.CREATED,
        checkpoint_id="checkpoint-change",
        sequence=0,
    )

    after = _observe(runtime, store)

    assert before.projection_fingerprint != after.projection_fingerprint


def test_generated_at_is_excluded_from_source_content_fingerprint(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    first = _observe(runtime, store, generated_at=NOW)
    second = _observe(runtime, store, generated_at=NOW + timedelta(days=10))

    first_payload = first.to_dict()
    second_payload = second.to_dict()
    assert first_payload["generated_at"] != second_payload["generated_at"]
    assert first_payload["projection_fingerprint"] == second_payload["projection_fingerprint"]


def test_permit_verifier_never_appears_in_projection_or_serialization(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    verifier = _make_claim_indeterminate(store, intent, dispatch)
    _add_reference(runtime, intent, dispatch, claim_id="claim-1")

    observation = _observe(runtime, store)
    serialized = observation.to_json()

    assert "permit_verifier" not in serialized
    assert verifier not in serialized
    assert not hasattr(observation.effects[0].gateway_claim, "permit_verifier")


def test_projection_models_and_nested_content_are_immutable(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    runtime.create_checkpoint(
        DOMAIN_A,
        MISSION_ID,
        MissionLifecycle.CREATED,
        checkpoint_id="checkpoint-1",
        sequence=0,
        progress_data={"list": [1, 2]},
    )
    observation = _observe(runtime, store)

    with pytest.raises(FrozenInstanceError):
        observation.revision = 99  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        observation.checkpoints[0].reason = "changed"  # type: ignore[misc]
    payload = observation.to_dict()
    payload["specification"]["objective"] = "mutated copy"
    assert observation.specification.objective != "mutated copy"


def test_observation_calls_no_authoritative_writer_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    _add_reference(runtime, intent, dispatch, claim_id="claim-1")
    called: list[str] = []

    def forbidden(name: str):
        def _call(*args: Any, **kwargs: Any) -> None:
            called.append(name)
            raise AssertionError(f"writer called: {name}")

        return _call

    runtime_writers = (
        "create_mission",
        "start_mission",
        "pause_mission",
        "resume_mission",
        "complete_mission",
        "fail_mission",
        "cancel_mission",
        "create_checkpoint",
        "add_effect_reference",
    )
    effect_writers = (
        "commit_intent",
        "commit_dispatch",
        "store_reservation",
        "release_reservation",
        "store_obligation",
        "claim_gateway_dispatch",
        "issue_gateway_permit",
        "verify_and_consume_permit",
        "record_gateway_receipt",
        "record_gateway_result",
        "revoke_permit",
    )
    for name in runtime_writers:
        monkeypatch.setattr(runtime, name, forbidden(name))
    for name in effect_writers:
        monkeypatch.setattr(store, name, forbidden(name))
    monkeypatch.setattr(store, "get_obligation", forbidden("get_obligation"))

    _observe(runtime, store)

    assert called == []


def test_observation_creates_no_database_or_file_persistence(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, _ = _commit_effect(store)
    _add_reference(runtime, intent)
    store.get_intent(intent.effect_intent_id, DOMAIN_A)
    runtime.get_mission(DOMAIN_A, MISSION_ID)
    before = {path.name for path in tmp_path.iterdir()}

    _observe(runtime, store)

    after = {path.name for path in tmp_path.iterdir()}
    assert after == before


def test_mission_revision_change_during_projection_fails_closed(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)

    class ChangingRuntime:
        def __init__(self, delegate: MissionRuntime) -> None:
            self.delegate = delegate
            self.reads = 0

        def get_mission(self, control_domain: str, mission_id: str):
            self.reads += 1
            result = self.delegate.get_mission(control_domain, mission_id)
            if self.reads == 1:
                return result
            specification, _state, revision, updated_at = result
            return (
                specification,
                MissionLifecycle.RUNNING,
                revision + 1,
                updated_at + timedelta(seconds=1),
            )

        def list_transitions(self, control_domain: str, mission_id: str):
            return self.delegate.list_transitions(control_domain, mission_id)

        def list_checkpoints(self, control_domain: str, mission_id: str):
            return self.delegate.list_checkpoints(control_domain, mission_id)

        def list_effect_references(self, control_domain: str, mission_id: str):
            return self.delegate.list_effect_references(control_domain, mission_id)

    changing = ChangingRuntime(runtime)

    with pytest.raises(MissionObservabilityConcurrentChangeError):
        _observe(changing, store)
    assert changing.reads == 2


def test_revision_change_during_transition_read_reports_concurrent_change(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)

    class MutatingRuntime:
        def __init__(self, delegate: MissionRuntime) -> None:
            self.delegate = delegate
            self.mutated = False

        def get_mission(self, control_domain: str, mission_id: str):
            return self.delegate.get_mission(control_domain, mission_id)

        def list_transitions(self, control_domain: str, mission_id: str):
            if not self.mutated:
                self.delegate.start_mission(control_domain, mission_id, expected_revision=1)
                self.mutated = True
            return self.delegate.list_transitions(control_domain, mission_id)

        def list_checkpoints(self, control_domain: str, mission_id: str):
            return self.delegate.list_checkpoints(control_domain, mission_id)

        def list_effect_references(self, control_domain: str, mission_id: str):
            return self.delegate.list_effect_references(control_domain, mission_id)

    with pytest.raises(MissionObservabilityConcurrentChangeError):
        _observe(MutatingRuntime(runtime), store)


def test_cross_domain_object_returned_by_reader_fails_closed(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, _ = _commit_effect(store)
    _add_reference(runtime, intent)
    wrong_domain_intent = replace(intent, control_domain=DOMAIN_B)

    with pytest.raises(MissionObservabilityIntegrityError, match="domain"):
        _observe(runtime, _EffectReadOverride(store, intent=wrong_domain_intent))


def test_contradictory_claim_binding_fields_fail_closed(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    _add_reference(runtime, intent, dispatch, claim_id="claim-1")
    claim = store.get_gateway_claim("claim-1", DOMAIN_A)
    assert claim is not None
    malformed = {**claim, "operation_digest": "f" * 64}

    with pytest.raises(MissionObservabilityIntegrityError, match="operation_digest"):
        _observe(runtime, _EffectReadOverride(store, claim=malformed))


def test_authoritative_store_integrity_error_is_not_swallowed(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    runtime.add_effect_reference(DOMAIN_A, MISSION_ID, "intent-1")

    class BrokenStore(_EffectReadOverride):
        def get_intent(self, effect_intent_id: str, control_domain: str):
            raise StorageIntegrityError("authoritative store corrupt")

    with pytest.raises(StorageIntegrityError, match="authoritative store corrupt"):
        _observe(runtime, BrokenStore(store))


# ======================================================================
# BLOCKER 1 REGRESSION: Gateway claim state/timestamp validation
# ======================================================================


def test_gateway_claim_terminal_at_before_receipt_recorded_at_fails(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    _add_reference(runtime, intent, dispatch, claim_id="claim-1")
    claim = store.get_gateway_claim("claim-1", DOMAIN_A)
    assert claim is not None
    claimed_at_ts = datetime.fromisoformat(claim["claimed_at"])
    malformed = {
        **claim,
        "state": "terminal",
        "handoff_started_at": (claimed_at_ts + timedelta(minutes=1)).isoformat(),
        "receipt_recorded_at": (claimed_at_ts + timedelta(minutes=5)).isoformat(),
        "terminal_at": (claimed_at_ts + timedelta(minutes=4)).isoformat(),  # Before receipt!
    }

    with pytest.raises(MissionObservabilityIntegrityError, match="terminal_at must follow receipt_recorded_at"):
        _observe(runtime, _EffectReadOverride(store, claim=malformed))


def test_gateway_claim_receipt_recorded_at_before_handoff_started_at_fails(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    _add_reference(runtime, intent, dispatch, claim_id="claim-1")
    claim = store.get_gateway_claim("claim-1", DOMAIN_A)
    assert claim is not None
    malformed = {
        **claim,
        "state": "receipt_recorded",
        "handoff_started_at": (NOW + timedelta(minutes=5)).isoformat(),
        "receipt_recorded_at": (NOW + timedelta(minutes=4)).isoformat(),  # Before handoff!
    }

    with pytest.raises(MissionObservabilityIntegrityError, match="receipt_recorded_at must follow handoff_started_at"):
        _observe(runtime, _EffectReadOverride(store, claim=malformed))


def test_gateway_claim_handoff_started_at_before_claimed_at_fails(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    _add_reference(runtime, intent, dispatch, claim_id="claim-1")
    claim = store.get_gateway_claim("claim-1", DOMAIN_A)
    assert claim is not None
    claimed_at_ts = datetime.fromisoformat(claim["claimed_at"])
    malformed = {
        **claim,
        "state": "handoff_started",
        "handoff_started_at": (claimed_at_ts - timedelta(seconds=1)).isoformat(),  # Before claimed!
    }

    with pytest.raises(MissionObservabilityIntegrityError, match="handoff_started_at must follow claimed_at"):
        _observe(runtime, _EffectReadOverride(store, claim=malformed))


def test_gateway_claim_handoff_started_with_forbidden_receipt_timestamp_fails(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    _add_reference(runtime, intent, dispatch, claim_id="claim-1")
    claim = store.get_gateway_claim("claim-1", DOMAIN_A)
    assert claim is not None
    malformed = {
        **claim,
        "state": "handoff_started",
        "handoff_started_at": (NOW + timedelta(minutes=3)).isoformat(),
        "receipt_recorded_at": (NOW + timedelta(minutes=4)).isoformat(),  # Forbidden for handoff_started!
    }

    with pytest.raises(MissionObservabilityIntegrityError, match="handoff_started.*must not have receipt_recorded_at"):
        _observe(runtime, _EffectReadOverride(store, claim=malformed))


def test_gateway_claim_claimed_with_forbidden_terminal_timestamp_fails(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    _add_reference(runtime, intent, dispatch, claim_id="claim-1")
    claim = store.get_gateway_claim("claim-1", DOMAIN_A)
    assert claim is not None
    malformed = {
        **claim,
        "state": "claimed",
        "terminal_at": (NOW + timedelta(minutes=10)).isoformat(),  # Forbidden for claimed!
    }

    with pytest.raises(MissionObservabilityIntegrityError, match="claimed.*must not have terminal_at"):
        _observe(runtime, _EffectReadOverride(store, claim=malformed))


def test_gateway_claim_terminal_missing_required_receipt_timestamp_fails(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    _add_reference(runtime, intent, dispatch, claim_id="claim-1")
    claim = store.get_gateway_claim("claim-1", DOMAIN_A)
    assert claim is not None
    malformed = {
        **claim,
        "state": "terminal",
        "handoff_started_at": (NOW + timedelta(minutes=3)).isoformat(),
        "terminal_at": (NOW + timedelta(minutes=5)).isoformat(),
        # Missing receipt_recorded_at!
    }

    with pytest.raises(MissionObservabilityIntegrityError, match="terminal.*requires receipt_recorded_at"):
        _observe(runtime, _EffectReadOverride(store, claim=malformed))


def test_gateway_claim_indeterminate_with_forbidden_receipt_timestamp_fails(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    _add_reference(runtime, intent, dispatch, claim_id="claim-1")
    claim = store.get_gateway_claim("claim-1", DOMAIN_A)
    assert claim is not None
    malformed = {
        **claim,
        "state": "indeterminate",
        "handoff_started_at": (NOW + timedelta(minutes=3)).isoformat(),
        "receipt_recorded_at": (NOW + timedelta(minutes=4)).isoformat(),  # Forbidden for indeterminate!
        "terminal_at": (NOW + timedelta(minutes=5)).isoformat(),
    }

    with pytest.raises(MissionObservabilityIntegrityError, match="indeterminate.*must not have receipt_recorded_at"):
        _observe(runtime, _EffectReadOverride(store, claim=malformed))


def test_gateway_claim_valid_terminal_chronology_succeeds(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    _make_claim_terminal(store, intent, dispatch, effect_status="nothing_landed")
    _add_reference(runtime, intent, dispatch, claim_id="claim-1")

    observation = _observe(runtime, store)

    assert observation.effects[0].gateway_claim is not None
    assert observation.effects[0].gateway_claim.state == "terminal"


def test_gateway_claim_valid_indeterminate_chronology_succeeds(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    _make_claim_indeterminate(store, intent, dispatch)
    _add_reference(runtime, intent, dispatch, claim_id="claim-1")

    observation = _observe(runtime, store)

    assert observation.effects[0].gateway_claim is not None
    assert observation.effects[0].gateway_claim.state == "indeterminate"


# ======================================================================
# BLOCKER 2 REGRESSION: Mission transition chain integrity
# ======================================================================


def test_mission_transition_broken_from_to_state_chain_fails(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    runtime.start_mission(DOMAIN_A, MISSION_ID, expected_revision=1)
    runtime.pause_mission(DOMAIN_A, MISSION_ID, expected_revision=2)

    class BrokenTransitionRuntime:
        def __init__(self, delegate):
            self.delegate = delegate

        def get_mission(self, control_domain: str, mission_id: str):
            return self.delegate.get_mission(control_domain, mission_id)

        def list_transitions(self, control_domain: str, mission_id: str):
            transitions = list(self.delegate.list_transitions(control_domain, mission_id))
            # Break the chain: make second transition start from COMPLETED instead of RUNNING
            broken = replace(transitions[1], from_state=MissionLifecycle.COMPLETED)
            transitions[1] = broken
            return transitions

        def list_checkpoints(self, control_domain: str, mission_id: str):
            return self.delegate.list_checkpoints(control_domain, mission_id)

        def list_effect_references(self, control_domain: str, mission_id: str):
            return self.delegate.list_effect_references(control_domain, mission_id)

    with pytest.raises(MissionObservabilityIntegrityError, match="transition chain broken"):
        _observe(BrokenTransitionRuntime(runtime), store)


def test_mission_transition_timestamp_regression_fails(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    runtime.start_mission(DOMAIN_A, MISSION_ID, expected_revision=1)
    runtime.pause_mission(DOMAIN_A, MISSION_ID, expected_revision=2)

    class RegressingTransitionRuntime:
        def __init__(self, delegate):
            self.delegate = delegate

        def get_mission(self, control_domain: str, mission_id: str):
            return self.delegate.get_mission(control_domain, mission_id)

        def list_transitions(self, control_domain: str, mission_id: str):
            transitions = list(self.delegate.list_transitions(control_domain, mission_id))
            # Make second transition earlier than first
            regressed = replace(
                transitions[1],
                transitioned_at=transitions[0].transitioned_at - timedelta(seconds=1)
            )
            transitions[1] = regressed
            return transitions

        def list_checkpoints(self, control_domain: str, mission_id: str):
            return self.delegate.list_checkpoints(control_domain, mission_id)

        def list_effect_references(self, control_domain: str, mission_id: str):
            return self.delegate.list_effect_references(control_domain, mission_id)

    with pytest.raises(MissionObservabilityIntegrityError, match="timestamp regression"):
        _observe(RegressingTransitionRuntime(runtime), store)


def test_mission_valid_pause_resume_complete_chain_succeeds(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    runtime.start_mission(DOMAIN_A, MISSION_ID, expected_revision=1)
    runtime.pause_mission(DOMAIN_A, MISSION_ID, expected_revision=2)
    runtime.resume_mission(DOMAIN_A, MISSION_ID, expected_revision=3)
    runtime.complete_mission(DOMAIN_A, MISSION_ID, expected_revision=4)

    observation = _observe(runtime, store)

    assert observation.lifecycle is MissionLifecycle.COMPLETED
    assert tuple(t.to_state for t in observation.transitions) == (
        MissionLifecycle.CREATED,
        MissionLifecycle.RUNNING,
        MissionLifecycle.PAUSED,
        MissionLifecycle.RUNNING,
        MissionLifecycle.COMPLETED,
    )


# ======================================================================
# BLOCKER 3 REGRESSION: Exact claim/dispatch correlation
# ======================================================================


def test_gateway_claim_adapter_id_mismatch_with_dispatch_fails(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    _add_reference(runtime, intent, dispatch, claim_id="claim-1")
    claim = store.get_gateway_claim("claim-1", DOMAIN_A)
    assert claim is not None
    malformed_claim = {**claim, "adapter_id": "different-adapter"}

    with pytest.raises(MissionObservabilityIntegrityError, match="adapter_id.*contradicts.*provider_adapter"):
        _observe(runtime, _EffectReadOverride(store, claim=malformed_claim))


def test_gateway_claim_adapter_id_exact_binding_succeeds(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    _add_reference(runtime, intent, dispatch, claim_id="claim-1")

    observation = _observe(runtime, store)

    assert observation.effects[0].dispatch is not None
    assert observation.effects[0].gateway_claim is not None
    assert observation.effects[0].gateway_claim.adapter_id == observation.effects[0].dispatch.provider_adapter


# ======================================================================
# BLOCKER 4 REGRESSION: Mission metadata exposure
# ======================================================================


def test_mission_metadata_absent_from_to_dict(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path, objective="Test metadata exclusion")

    observation = _observe(runtime, store)
    projected = observation.to_dict()

    assert "MISSION-METADATA-SECRET" not in json.dumps(projected)
    assert "metadata" not in projected["specification"]
    assert "metadata_json" not in projected["specification"]
    assert projected["specification"]["specification_fingerprint"] is not None


def test_mission_metadata_absent_from_to_json(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)

    observation = _observe(runtime, store)
    serialized = observation.to_json()

    assert "metadata" not in serialized or '"metadata":' not in serialized
    assert "metadata_json" not in serialized
    assert '"immutable"' not in serialized  # From test metadata
    assert "specification_fingerprint" in serialized


def test_mission_metadata_absent_from_repr(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)

    observation = _observe(runtime, store)
    representation = repr(observation.specification)

    assert "immutable" not in representation
    assert "metadata" not in representation or "metadata_json" not in representation


def test_mission_metadata_absent_from_timeline(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)

    timeline = _observe(runtime, store).timeline
    timeline_json = json.dumps([event.to_dict() for event in timeline])

    assert "metadata" not in timeline_json or '"metadata":' not in timeline_json
    assert "immutable" not in timeline_json


def test_mission_metadata_change_alters_projection_fingerprint(tmp_path: Path) -> None:
    runtime_a, store_a = _runtime_and_store(
        tmp_path,
        suffix="a",
        mission_id="mission-a",
        objective="Objective A",
    )
    runtime_b, store_b = _runtime_and_store(
        tmp_path,
        suffix="b",
        mission_id="mission-b",
        objective="Objective A",  # Same objective
    )
    # Create mission-b with different metadata
    runtime_b_alt = MissionRuntime(db_path=tmp_path / "missions-b.sqlite3")
    runtime_b_alt.create_mission(
        mission_id="mission-c",
        control_domain=DOMAIN_A,
        objective="Objective A",
        owner_identity=f"owner-{DOMAIN_A}",
        metadata={"different": {"value": 2}},  # Different metadata
    )

    observation_a = _observe(runtime_a, store_a, mission_id="mission-a")
    observation_c = _observe(runtime_b_alt, store_b, mission_id="mission-c")

    # Different metadata should produce different specification fingerprints
    assert observation_a.specification.specification_fingerprint != observation_c.specification.specification_fingerprint
    # Which should produce different projection fingerprints
    assert observation_a.projection_fingerprint != observation_c.projection_fingerprint


def test_serialization_is_deterministic_and_contains_no_secret_material(tmp_path: Path) -> None:
    runtime, store = _runtime_and_store(tmp_path)
    intent, dispatch = _commit_effect(store, include_claim=True)
    assert dispatch is not None
    verifier = _make_claim_indeterminate(store, intent, dispatch)
    _add_reference(runtime, intent, dispatch, claim_id="claim-1")
    spine = EvidenceSpine.from_records(
        (_evidence_record(domain=DOMAIN_A, record_id="verified"),)
    )

    first = _observe(runtime, store, evidence_spine=spine, generated_at=NOW)
    second = _observe(runtime, store, evidence_spine=spine, generated_at=NOW)

    assert first.to_json() == second.to_json()
    assert json.loads(first.to_json()) == first.to_dict()
    assert verifier not in first.to_json()
    assert "must-not-be-projected" not in first.to_json()
    assert "permit_token" not in first.to_json()
    assert "credential" not in first.to_json()
    assert "metadata_json" not in first.to_json()
    assert '"immutable"' not in first.to_json()
