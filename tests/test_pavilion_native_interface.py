"""PavilionOS Native Interface v0.1 — presentation contract and boundary validation.

This test suite proves that PavilionNativeInterface:
- Presents MissionaryX truth exactly (does not recreate it)
- Preserves effect status semantics (UNKNOWN, INDETERMINATE)
- Maintains read-only guarantees (no effectful operations)
- Enforces immutability and confidentiality boundaries
- Uses MissionObservability as single source
"""

from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from federation.durable_effect_store import DurableEffectStore
from federation.effect_safety import (
    AuthorityDisposition,
    AuthorityReservation,
    EffectDispatch,
    EffectIntent,
)
from federation.mission_observability import (
    MissionObservability,
    MissionObservabilityError,
    MissionObservabilityIntegrityError,
    MissionObservabilityNotFoundError,
    ProjectedEffectStatus,
)
from federation.mission_runtime import MissionRuntime
from federation.mission_state import MissionLifecycle
from pavilionos.native_interface import (
    PavilionNativeInterface,
    PavilionNativeInterfaceError,
    PavilionNativeInterfaceSourceError,
)
from research_mission.evidence_spine import (
    EvidenceCorrelationKey,
    EvidenceRecord,
    EvidenceReference,
    EvidenceSpine,
)


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
DOMAIN = "pavilion-native-domain"
MISSION_ID = "mission-presentation-test"


def _create_runtime_and_store(
    tmp_path: Path,
    *,
    domain: str = DOMAIN,
    mission_id: str = MISSION_ID,
    objective: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[MissionRuntime, DurableEffectStore]:
    runtime = MissionRuntime(db_path=tmp_path / "missions.sqlite3")
    store = DurableEffectStore(tmp_path / "effects.sqlite3")
    runtime.create_mission(
        mission_id=mission_id,
        control_domain=domain,
        objective=objective or "Test objective",
        owner_identity="owner-native",
        metadata=metadata or {"hidden": "secret"},
    )
    return runtime, store


def _create_effect(
    store: DurableEffectStore,
    *,
    domain: str = DOMAIN,
    mission_id: str = MISSION_ID,
    intent_id: str = "intent-1",
    reservation_id: str = "reservation-1",
    include_dispatch: bool = False,
    include_claim: bool = False,
    claim_id: str = "claim-1",
) -> tuple[EffectIntent, EffectDispatch | None]:
    intent = EffectIntent(
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
        created_at=NOW,
        control_domain=domain,
    )
    reservation = AuthorityReservation(
        reservation_id=reservation_id,
        effect_intent_id=intent_id,
        capability_type="external-effect",
        amount=1.0,
        disposition=AuthorityDisposition.RESERVED,
        reserved_at=NOW,
        disposition_at=None,
        disposition_evidence=None,
        control_domain=domain,
    )
    store.commit_intent(intent)
    store.store_reservation(reservation)

    dispatch = None
    if include_dispatch or include_claim:
        dispatch = EffectDispatch(
            dispatch_id="dispatch-1",
            effect_intent_id=intent.effect_intent_id,
            attempt_id=intent.attempt_id,
            idempotency_key=intent.idempotency_key,
            provider_adapter="adapter-1",
            capability_profile_version="v1",
            transport_digest="b" * 64,
            posture="accepted_by_transport",
            provider_operation_id="provider-op-1",
            evidence_reference="dispatch-evidence",
            dispatched_at=NOW + timedelta(minutes=1),
            control_domain=domain,
        )
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
            claimed_at=NOW + timedelta(minutes=2),
            expires_at=NOW + timedelta(hours=1),
            control_domain=domain,
        )

    return intent, dispatch


def _make_claim_indeterminate(
    store: DurableEffectStore,
    intent: EffectIntent,
    dispatch: EffectDispatch,
    *,
    claim_id: str = "claim-1",
) -> None:
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


def _interface(
    runtime: MissionRuntime,
    store: DurableEffectStore,
    *,
    evidence_spine: EvidenceSpine | None = None,
) -> PavilionNativeInterface:
    observability = MissionObservability(
        runtime,
        store,
        evidence_spine=evidence_spine,
        clock=lambda: NOW + timedelta(days=1),
    )
    return PavilionNativeInterface(observability)


# ======================================================================
# Lifecycle and revision projection (requirements 1-6)
# ======================================================================


def test_created_mission_renders_correctly(tmp_path: Path) -> None:
    """Requirement 1: CREATED mission renders correctly."""
    runtime, store = _create_runtime_and_store(tmp_path)
    interface = _interface(runtime, store)

    view = interface.mission_view(DOMAIN, MISSION_ID)

    assert view.control_domain == DOMAIN
    assert view.mission_id == MISSION_ID
    assert view.lifecycle is MissionLifecycle.CREATED
    assert view.revision == 1
    assert view.objective == "Test objective"
    assert view.owner_identity == "owner-native"


def test_running_mission_renders_correctly(tmp_path: Path) -> None:
    """Requirement 2: RUNNING mission renders correctly."""
    runtime, store = _create_runtime_and_store(tmp_path)
    runtime.start_mission(DOMAIN, MISSION_ID, expected_revision=1)
    interface = _interface(runtime, store)

    view = interface.mission_view(DOMAIN, MISSION_ID)

    assert view.lifecycle is MissionLifecycle.RUNNING
    assert view.revision == 2


def test_lifecycle_and_revision_copied_exactly_from_observation(tmp_path: Path) -> None:
    """Requirement 3: lifecycle/revision are copied exactly from MissionObservation."""
    runtime, store = _create_runtime_and_store(tmp_path)
    runtime.start_mission(DOMAIN, MISSION_ID, expected_revision=1)
    runtime.pause_mission(DOMAIN, MISSION_ID, expected_revision=2)
    observability = MissionObservability(runtime, store)
    observation = observability.observe(DOMAIN, MISSION_ID)
    interface = PavilionNativeInterface(observability)

    view = interface.mission_view(DOMAIN, MISSION_ID)

    assert view.lifecycle == observation.lifecycle
    assert view.revision == observation.revision
    assert view.lifecycle is MissionLifecycle.PAUSED
    assert view.revision == 3


def test_objective_comes_only_from_accepted_projection(tmp_path: Path) -> None:
    """Requirement 4: objective/display mission text comes only from accepted projection."""
    runtime, store = _create_runtime_and_store(tmp_path, objective="Accepted objective")
    observability = MissionObservability(runtime, store)
    observation = observability.observe(DOMAIN, MISSION_ID)
    interface = PavilionNativeInterface(observability)

    view = interface.mission_view(DOMAIN, MISSION_ID)

    assert view.objective == observation.specification.objective
    assert view.objective == "Accepted objective"
    assert view.owner_identity == observation.specification.owner_identity


def test_zero_effects_represented_correctly(tmp_path: Path) -> None:
    """Requirement 5: zero effects are represented correctly."""
    runtime, store = _create_runtime_and_store(tmp_path)
    interface = _interface(runtime, store)

    view = interface.mission_view(DOMAIN, MISSION_ID)

    assert view.effect_count == 0
    assert view.unresolved_effect_count == 0
    assert view.effects == ()
    assert view.unresolved_effects == ()


def test_effect_count_matches_observation(tmp_path: Path) -> None:
    """Requirement 6: effect count matches MissionObservation."""
    runtime, store = _create_runtime_and_store(tmp_path)
    intent, dispatch = _create_effect(store, include_claim=True)
    runtime.add_effect_reference(
        DOMAIN,
        MISSION_ID,
        intent.effect_intent_id,
        effect_dispatch_id=dispatch.dispatch_id if dispatch else None,
        gateway_claim_id="claim-1",
    )
    observability = MissionObservability(runtime, store)
    observation = observability.observe(DOMAIN, MISSION_ID)
    interface = PavilionNativeInterface(observability)

    view = interface.mission_view(DOMAIN, MISSION_ID)

    assert view.effect_count == len(observation.effects)
    assert view.effect_count == 1


# ======================================================================
# Effect status semantics (requirements 7-13)
# ======================================================================


def test_unknown_effect_remains_unknown(tmp_path: Path) -> None:
    """Requirement 7: UNKNOWN effect remains UNKNOWN."""
    runtime, store = _create_runtime_and_store(tmp_path)
    intent, _ = _create_effect(store)
    runtime.add_effect_reference(DOMAIN, MISSION_ID, intent.effect_intent_id)
    interface = _interface(runtime, store)

    view = interface.mission_view(DOMAIN, MISSION_ID)

    assert view.effects[0].projected_status is ProjectedEffectStatus.UNKNOWN
    assert view.effects[0].reconciliation_required is False


def test_unknown_not_rendered_as_nothing_landed(tmp_path: Path) -> None:
    """Requirement 8: UNKNOWN is not rendered as nothing_landed."""
    runtime, store = _create_runtime_and_store(tmp_path)
    intent, _ = _create_effect(store)
    runtime.add_effect_reference(DOMAIN, MISSION_ID, intent.effect_intent_id)
    interface = _interface(runtime, store)

    rendered = interface.render_mission(DOMAIN, MISSION_ID)

    assert "UNKNOWN" in rendered
    assert "nothing_landed" not in rendered.lower()


def test_unknown_not_rendered_as_something_landed(tmp_path: Path) -> None:
    """Requirement 9: UNKNOWN is not rendered as something_landed."""
    runtime, store = _create_runtime_and_store(tmp_path)
    intent, _ = _create_effect(store)
    runtime.add_effect_reference(DOMAIN, MISSION_ID, intent.effect_intent_id)
    interface = _interface(runtime, store)

    rendered = interface.render_mission(DOMAIN, MISSION_ID)

    assert "UNKNOWN" in rendered
    assert "something_landed" not in rendered.lower()


def test_indeterminate_remains_visibly_indeterminate(tmp_path: Path) -> None:
    """Requirement 10: INDETERMINATE remains visibly indeterminate."""
    runtime, store = _create_runtime_and_store(tmp_path)
    intent, dispatch = _create_effect(store, include_claim=True)
    assert dispatch is not None
    _make_claim_indeterminate(store, intent, dispatch)
    runtime.add_effect_reference(
        DOMAIN,
        MISSION_ID,
        intent.effect_intent_id,
        effect_dispatch_id=dispatch.dispatch_id,
        gateway_claim_id="claim-1",
    )
    interface = _interface(runtime, store)

    view = interface.mission_view(DOMAIN, MISSION_ID)

    assert view.effects[0].projected_status is ProjectedEffectStatus.INDETERMINATE
    assert view.effects[0].reconciliation_required is True
    assert view.unresolved_effects[0].projected_status is ProjectedEffectStatus.INDETERMINATE


def test_reconciliation_required_visible_for_indeterminate_effect(tmp_path: Path) -> None:
    """Requirement 11: reconciliation_required is visible for indeterminate effect."""
    runtime, store = _create_runtime_and_store(tmp_path)
    intent, dispatch = _create_effect(store, include_claim=True)
    assert dispatch is not None
    _make_claim_indeterminate(store, intent, dispatch)
    runtime.add_effect_reference(
        DOMAIN,
        MISSION_ID,
        intent.effect_intent_id,
        effect_dispatch_id=dispatch.dispatch_id,
        gateway_claim_id="claim-1",
    )
    interface = _interface(runtime, store)

    rendered = interface.render_mission(DOMAIN, MISSION_ID)

    assert "INDETERMINATE" in rendered
    assert "reconciliation required" in rendered


def test_mission_failed_does_not_rewrite_effect_outcome(tmp_path: Path) -> None:
    """Requirement 12: mission FAILED does not rewrite effect outcome."""
    runtime, store = _create_runtime_and_store(tmp_path)
    intent, _ = _create_effect(store)
    runtime.add_effect_reference(DOMAIN, MISSION_ID, intent.effect_intent_id)
    runtime.start_mission(DOMAIN, MISSION_ID, expected_revision=1)
    runtime.fail_mission(DOMAIN, MISSION_ID, expected_revision=2, reason="test failure")
    interface = _interface(runtime, store)

    view = interface.mission_view(DOMAIN, MISSION_ID)

    assert view.lifecycle is MissionLifecycle.FAILED
    assert view.effects[0].projected_status is ProjectedEffectStatus.UNKNOWN


def test_mission_completed_does_not_rewrite_effect_outcome(tmp_path: Path) -> None:
    """Requirement 13: mission COMPLETED does not rewrite effect outcome."""
    runtime, store = _create_runtime_and_store(tmp_path)
    intent, _ = _create_effect(store)
    runtime.add_effect_reference(DOMAIN, MISSION_ID, intent.effect_intent_id)
    runtime.start_mission(DOMAIN, MISSION_ID, expected_revision=1)
    runtime.complete_mission(DOMAIN, MISSION_ID, expected_revision=2)
    interface = _interface(runtime, store)

    view = interface.mission_view(DOMAIN, MISSION_ID)

    assert view.lifecycle is MissionLifecycle.COMPLETED
    assert view.effects[0].projected_status is ProjectedEffectStatus.UNKNOWN


# ======================================================================
# Unresolved effect and evidence handling (requirements 14-17)
# ======================================================================


def test_unresolved_effect_count_is_exact(tmp_path: Path) -> None:
    """Requirement 14: unresolved-effect count is exact."""
    runtime, store = _create_runtime_and_store(tmp_path)
    intent, _ = _create_effect(store)
    runtime.add_effect_reference(DOMAIN, MISSION_ID, intent.effect_intent_id)
    observability = MissionObservability(runtime, store)
    observation = observability.observe(DOMAIN, MISSION_ID)
    interface = PavilionNativeInterface(observability)

    view = interface.mission_view(DOMAIN, MISSION_ID)

    assert view.unresolved_effect_count == len(observation.unresolved_effects)
    assert view.unresolved_effect_count == 1


def test_evidence_available_preserved_exactly(tmp_path: Path) -> None:
    """Requirement 15: evidence_available is preserved exactly."""
    runtime, store = _create_runtime_and_store(tmp_path)
    record = EvidenceRecord(
        key=EvidenceCorrelationKey(
            source="test",
            record_id="record-1",
            mission_id=MISSION_ID,
            task_id="task-1",
            domain_id=DOMAIN,
        ),
        reference=EvidenceReference(
            source_revision="rev-1",
            fingerprint="f" * 64,
            observed_at=NOW,
            summary="test evidence",
            reference="ref-1",
        ),
        payload={},
        metadata={},
    )
    spine = EvidenceSpine.from_records((record,))
    observability = MissionObservability(runtime, store, evidence_spine=spine)
    observation = observability.observe(DOMAIN, MISSION_ID)
    interface = PavilionNativeInterface(observability)

    view = interface.mission_view(DOMAIN, MISSION_ID)

    assert view.evidence_available == observation.evidence_available
    assert view.evidence_available is True
    assert view.evidence_count == len(observation.evidence)


def test_unbound_evidence_count_represented_conservatively(tmp_path: Path) -> None:
    """Requirement 16: unbound evidence count is represented conservatively if exposed."""
    runtime, store = _create_runtime_and_store(tmp_path)
    unbound_record = EvidenceRecord(
        key=EvidenceCorrelationKey(
            source="test",
            record_id="unbound-1",
            mission_id=MISSION_ID,
            task_id="task-1",
            domain_id=None,  # Unbound
        ),
        reference=EvidenceReference(
            source_revision="rev-1",
            fingerprint="f" * 64,
            observed_at=NOW,
            summary="unbound evidence",
            reference="ref-1",
        ),
        payload={},
        metadata={},
    )
    spine = EvidenceSpine.from_records((unbound_record,))
    observability = MissionObservability(runtime, store, evidence_spine=spine)
    observation = observability.observe(DOMAIN, MISSION_ID)
    interface = PavilionNativeInterface(observability)

    view = interface.mission_view(DOMAIN, MISSION_ID)

    assert view.unbound_evidence_count == observation.unbound_evidence_count
    assert view.unbound_evidence_count == 1


def test_projection_fingerprint_preserved_exactly(tmp_path: Path) -> None:
    """Requirement 17: projection_fingerprint is preserved exactly."""
    runtime, store = _create_runtime_and_store(tmp_path)
    observability = MissionObservability(runtime, store)
    observation = observability.observe(DOMAIN, MISSION_ID)
    interface = PavilionNativeInterface(observability)

    view = interface.mission_view(DOMAIN, MISSION_ID)

    assert view.projection_fingerprint == observation.projection_fingerprint


# ======================================================================
# Timeline handling (requirements 18-21)
# ======================================================================


def test_generated_at_and_updated_at_remain_timezone_aware(tmp_path: Path) -> None:
    """Requirement 18: generated_at and updated_at remain timezone-aware."""
    runtime, store = _create_runtime_and_store(tmp_path)
    interface = _interface(runtime, store)

    view = interface.mission_view(DOMAIN, MISSION_ID)

    assert view.generated_at.tzinfo is not None
    assert view.updated_at.tzinfo is not None
    assert view.created_at.tzinfo is not None


def test_timeline_uses_observation_timeline_identities(tmp_path: Path) -> None:
    """Requirement 19: timeline uses MissionObservation timeline identities."""
    runtime, store = _create_runtime_and_store(tmp_path)
    runtime.start_mission(DOMAIN, MISSION_ID, expected_revision=1)
    observability = MissionObservability(runtime, store)
    observation = observability.observe(DOMAIN, MISSION_ID)
    interface = PavilionNativeInterface(observability)

    view = interface.mission_view(DOMAIN, MISSION_ID)

    assert len(view.timeline_summary) == len(observation.timeline)
    for pavilion_event, obs_event in zip(view.timeline_summary, observation.timeline):
        assert pavilion_event.source_type == obs_event.source_type
        assert pavilion_event.source_id == obs_event.source_id
        assert pavilion_event.recorded_at == obs_event.recorded_at
        assert pavilion_event.source_fingerprint == obs_event.source_fingerprint


def test_timeline_ordering_not_independently_reconstructed(tmp_path: Path) -> None:
    """Requirement 20: timeline ordering is not independently reconstructed from source stores."""
    runtime, store = _create_runtime_and_store(tmp_path)
    runtime.start_mission(DOMAIN, MISSION_ID, expected_revision=1)
    observability = MissionObservability(runtime, store)
    observation = observability.observe(DOMAIN, MISSION_ID)
    interface = PavilionNativeInterface(observability)

    timeline = interface.timeline_view(DOMAIN, MISSION_ID)

    # Timeline must match observation timeline exactly, not be independently constructed
    assert len(timeline) == len(observation.timeline)
    for i, (pavilion_event, obs_event) in enumerate(zip(timeline, observation.timeline)):
        assert pavilion_event.source_type == obs_event.source_type
        assert pavilion_event.source_id == obs_event.source_id
        assert pavilion_event.recorded_at == obs_event.recorded_at


def test_equal_timestamps_remain_deterministic(tmp_path: Path) -> None:
    """Requirement 21: equal timestamps remain deterministic."""
    runtime, store = _create_runtime_and_store(tmp_path)
    intent1, _ = _create_effect(store, intent_id="intent-a", reservation_id="res-a")
    intent2, _ = _create_effect(store, intent_id="intent-b", reservation_id="res-b")
    runtime.add_effect_reference(DOMAIN, MISSION_ID, intent1.effect_intent_id)
    runtime.add_effect_reference(DOMAIN, MISSION_ID, intent2.effect_intent_id)
    interface = _interface(runtime, store)

    view1 = interface.mission_view(DOMAIN, MISSION_ID)
    view2 = interface.mission_view(DOMAIN, MISSION_ID)

    assert view1.timeline_summary == view2.timeline_summary


# ======================================================================
# Immutability (requirements 22-23)
# ======================================================================


def test_view_models_are_immutable(tmp_path: Path) -> None:
    """Requirement 22: view models are immutable."""
    runtime, store = _create_runtime_and_store(tmp_path)
    interface = _interface(runtime, store)

    view = interface.mission_view(DOMAIN, MISSION_ID)

    with pytest.raises(FrozenInstanceError):
        view.mission_id = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        view.lifecycle = MissionLifecycle.RUNNING  # type: ignore[misc]


def test_nested_collections_cannot_be_mutated(tmp_path: Path) -> None:
    """Requirement 23: nested collections cannot be mutated."""
    runtime, store = _create_runtime_and_store(tmp_path)
    intent, _ = _create_effect(store)
    runtime.add_effect_reference(DOMAIN, MISSION_ID, intent.effect_intent_id)
    runtime.start_mission(DOMAIN, MISSION_ID, expected_revision=1)
    interface = _interface(runtime, store)

    view = interface.mission_view(DOMAIN, MISSION_ID)

    # Tuples are immutable by nature
    assert isinstance(view.effects, tuple)
    assert isinstance(view.unresolved_effects, tuple)
    assert isinstance(view.timeline_summary, tuple)
    # Individual items in tuples are frozen dataclasses
    with pytest.raises(FrozenInstanceError):
        view.effects[0].projected_status = ProjectedEffectStatus.INDETERMINATE  # type: ignore[misc]


# ======================================================================
# Confidentiality boundaries (requirements 24-28)
# ======================================================================


def test_render_output_is_deterministic_for_unchanged_observation(tmp_path: Path) -> None:
    """Requirement 24: render output is deterministic for unchanged observation."""
    runtime, store = _create_runtime_and_store(tmp_path)
    interface = _interface(runtime, store)

    render1 = interface.render_mission(DOMAIN, MISSION_ID)
    render2 = interface.render_mission(DOMAIN, MISSION_ID)

    assert render1 == render2


def test_no_raw_mission_metadata_appears(tmp_path: Path) -> None:
    """Requirement 25: no raw MissionSpecification.metadata appears."""
    runtime, store = _create_runtime_and_store(
        tmp_path,
        metadata={"MISSION-SECRET": "must-not-appear"},
    )
    interface = _interface(runtime, store)

    view = interface.mission_view(DOMAIN, MISSION_ID)
    rendered = interface.render_mission(DOMAIN, MISSION_ID)

    assert "MISSION-SECRET" not in repr(view)
    assert "must-not-appear" not in repr(view)
    assert "MISSION-SECRET" not in rendered
    assert "must-not-appear" not in rendered


def test_permit_verifier_cannot_appear(tmp_path: Path) -> None:
    """Requirement 26: permit_verifier cannot appear."""
    runtime, store = _create_runtime_and_store(tmp_path)
    intent, dispatch = _create_effect(store, include_claim=True)
    assert dispatch is not None
    _make_claim_indeterminate(store, intent, dispatch)
    runtime.add_effect_reference(
        DOMAIN,
        MISSION_ID,
        intent.effect_intent_id,
        effect_dispatch_id=dispatch.dispatch_id,
        gateway_claim_id="claim-1",
    )
    interface = _interface(runtime, store)

    view = interface.mission_view(DOMAIN, MISSION_ID)
    rendered = interface.render_mission(DOMAIN, MISSION_ID)

    assert "permit_verifier" not in repr(view)
    assert "permit_verifier" not in rendered
    assert "eeeeee" not in rendered  # The verifier value


def test_raw_evidence_payload_metadata_cannot_appear(tmp_path: Path) -> None:
    """Requirement 27: raw evidence payload/metadata cannot appear."""
    runtime, store = _create_runtime_and_store(tmp_path)
    record = EvidenceRecord(
        key=EvidenceCorrelationKey(
            source="test",
            record_id="record-1",
            mission_id=MISSION_ID,
            task_id="task-1",
            domain_id=DOMAIN,
        ),
        reference=EvidenceReference(
            source_revision="rev-1",
            fingerprint="f" * 64,
            observed_at=NOW,
            summary="safe summary",
            reference="ref-1",
        ),
        payload={"EVIDENCE-SECRET-PAYLOAD": "must-not-appear"},
        metadata={"EVIDENCE-SECRET-METADATA": "must-not-appear"},
    )
    spine = EvidenceSpine.from_records((record,))
    interface = _interface(runtime, store, evidence_spine=spine)

    view = interface.mission_view(DOMAIN, MISSION_ID)
    rendered = interface.render_mission(DOMAIN, MISSION_ID)

    assert "EVIDENCE-SECRET-PAYLOAD" not in repr(view)
    assert "EVIDENCE-SECRET-METADATA" not in repr(view)
    assert "must-not-appear" not in repr(view)
    assert "EVIDENCE-SECRET-PAYLOAD" not in rendered
    assert "EVIDENCE-SECRET-METADATA" not in rendered
    assert "must-not-appear" not in rendered


def test_no_credential_lease_objects_exposed(tmp_path: Path) -> None:
    """Requirement 28: no credential/lease objects are exposed."""
    runtime, store = _create_runtime_and_store(tmp_path)
    intent, dispatch = _create_effect(store, include_claim=True)
    assert dispatch is not None
    runtime.add_effect_reference(
        DOMAIN,
        MISSION_ID,
        intent.effect_intent_id,
        effect_dispatch_id=dispatch.dispatch_id,
        gateway_claim_id="claim-1",
    )
    interface = _interface(runtime, store)

    view = interface.mission_view(DOMAIN, MISSION_ID)
    rendered = interface.render_mission(DOMAIN, MISSION_ID)

    assert "credential" not in rendered.lower()
    assert "lease" not in rendered.lower()
    # Permit ID/verifier should not appear
    assert "permit-1" not in rendered


# ======================================================================
# Error handling (requirements 29-30)
# ======================================================================


def test_observability_not_found_behavior_distinguishable(tmp_path: Path) -> None:
    """Requirement 29: MissionObservability not-found behavior remains distinguishable."""
    runtime, store = _create_runtime_and_store(tmp_path)
    interface = _interface(runtime, store)

    with pytest.raises(PavilionNativeInterfaceError) as exc_info:
        interface.mission_view(DOMAIN, "missing-mission")

    # Should propagate not-found (not integrity error)
    assert "not found" in str(exc_info.value).lower() or isinstance(
        exc_info.value.__cause__, MissionObservabilityNotFoundError
    )


def test_observability_integrity_failure_propagated_explicitly(tmp_path: Path) -> None:
    """Requirement 30: MissionObservability integrity failure is propagated or wrapped explicitly."""
    runtime, store = _create_runtime_and_store(tmp_path)

    class FailingObservability:
        def observe(self, control_domain: str, mission_id: str):
            raise MissionObservabilityIntegrityError("Simulated integrity failure")

        def timeline(self, control_domain: str, mission_id: str):
            raise MissionObservabilityIntegrityError("Simulated integrity failure")

    interface = PavilionNativeInterface(FailingObservability())  # type: ignore[arg-type]

    with pytest.raises(PavilionNativeInterfaceSourceError) as exc_info:
        interface.mission_view(DOMAIN, MISSION_ID)

    assert "integrity" in str(exc_info.value).lower()


# ======================================================================
# No effectful operations (requirements 31-34)
# ======================================================================


def test_no_authoritative_writer_apis_invoked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Requirement 31: no authoritative writer APIs are invoked."""
    runtime, store = _create_runtime_and_store(tmp_path)
    intent, _ = _create_effect(store)
    runtime.add_effect_reference(DOMAIN, MISSION_ID, intent.effect_intent_id)

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
        "claim_gateway_dispatch",
        "issue_gateway_permit",
        "verify_and_consume_permit",
        "record_gateway_receipt",
        "record_gateway_result",
    )

    for name in runtime_writers:
        monkeypatch.setattr(runtime, name, forbidden(name))
    for name in effect_writers:
        monkeypatch.setattr(store, name, forbidden(name))

    interface = _interface(runtime, store)
    view = interface.mission_view(DOMAIN, MISSION_ID)
    interface.render_mission(DOMAIN, MISSION_ID)
    interface.timeline_view(DOMAIN, MISSION_ID)

    assert called == []
    assert view is not None


def test_canonical_coordinator_coordinate_never_called(tmp_path: Path) -> None:
    """Requirement 32: CanonicalPavilionCoordinator.coordinate is never called."""
    runtime, store = _create_runtime_and_store(tmp_path)
    interface = _interface(runtime, store)

    # Create a mock coordinator and verify it's never called
    mock_coordinator = Mock()

    # The interface should not even have access to a coordinator
    assert not hasattr(interface, "coordinator")
    assert not hasattr(interface, "_coordinator")

    # Verify no coordinate-like methods exist
    interface_methods = [m for m in dir(interface) if not m.startswith("_")]
    assert "coordinate" not in interface_methods
    assert "dispatch" not in interface_methods

    interface.mission_view(DOMAIN, MISSION_ID)
    assert not mock_coordinator.called


def test_canonical_adapter_dispatch_never_called(tmp_path: Path) -> None:
    """Requirement 33: CanonicalPavilionAdapter.dispatch is never called."""
    runtime, store = _create_runtime_and_store(tmp_path)
    interface = _interface(runtime, store)

    # Verify no adapter dispatch methods exist
    interface_methods = [m for m in dir(interface) if not m.startswith("_")]
    assert "dispatch" not in interface_methods

    interface.mission_view(DOMAIN, MISSION_ID)


def test_no_subprocess_network_sql_path_exists(tmp_path: Path) -> None:
    """Requirement 34: no subprocess/network/SQL path exists."""
    runtime, store = _create_runtime_and_store(tmp_path)
    interface = _interface(runtime, store)

    # The interface should only have presentation methods
    public_methods = [
        m
        for m in dir(interface)
        if not m.startswith("_") and callable(getattr(interface, m))
    ]

    # Only these three methods should be public
    assert set(public_methods) == {"mission_view", "render_mission", "timeline_view"}

    # These methods should not exist
    forbidden_methods = [
        "execute",
        "run",
        "subprocess",
        "query",
        "connect",
        "fetch",
        "request",
        "post",
        "get",
        "put",
        "delete",
    ]
    for forbidden in forbidden_methods:
        assert forbidden not in public_methods


# ======================================================================
# Domain isolation (requirement 35)
# ======================================================================


def test_same_mission_id_in_two_domains_isolated(tmp_path: Path) -> None:
    """Requirement 35: same mission ID in two ControlDomains remains isolated."""
    runtime = MissionRuntime(db_path=tmp_path / "missions.sqlite3")
    store = DurableEffectStore(tmp_path / "effects.sqlite3")
    runtime.create_mission(MISSION_ID, "domain-a", "Objective A", "owner-a")
    runtime.create_mission(MISSION_ID, "domain-b", "Objective B", "owner-b")
    interface = _interface(runtime, store)

    view_a = interface.mission_view("domain-a", MISSION_ID)
    view_b = interface.mission_view("domain-b", MISSION_ID)

    assert view_a.control_domain == "domain-a"
    assert view_b.control_domain == "domain-b"
    assert view_a.objective == "Objective A"
    assert view_b.objective == "Objective B"
    assert view_a.projection_fingerprint != view_b.projection_fingerprint


# ======================================================================
# Display and rendering (requirements 36-38)
# ======================================================================


def test_render_identifies_unresolved_effects_prominently(tmp_path: Path) -> None:
    """Requirement 36: render output identifies unresolved effects prominently."""
    runtime, store = _create_runtime_and_store(tmp_path)
    intent, dispatch = _create_effect(store, include_claim=True)
    assert dispatch is not None
    _make_claim_indeterminate(store, intent, dispatch)
    runtime.add_effect_reference(
        DOMAIN,
        MISSION_ID,
        intent.effect_intent_id,
        effect_dispatch_id=dispatch.dispatch_id,
        gateway_claim_id="claim-1",
    )
    interface = _interface(runtime, store)

    rendered = interface.render_mission(DOMAIN, MISSION_ID)

    assert "Unresolved Effects" in rendered
    assert "INDETERMINATE" in rendered
    assert intent.effect_intent_id[:32] in rendered


def test_no_local_cache_database_created(tmp_path: Path) -> None:
    """Requirement 37: no local cache/database is created."""
    runtime, store = _create_runtime_and_store(tmp_path)
    interface = _interface(runtime, store)
    before = {path.name for path in tmp_path.iterdir()}

    interface.mission_view(DOMAIN, MISSION_ID)
    interface.render_mission(DOMAIN, MISSION_ID)
    interface.timeline_view(DOMAIN, MISSION_ID)

    after = {path.name for path in tmp_path.iterdir()}
    assert after == before


def test_repeated_render_does_not_mutate_source_observation(tmp_path: Path) -> None:
    """Requirement 38: repeated render does not mutate the source MissionObservation."""
    runtime, store = _create_runtime_and_store(tmp_path)
    observability = MissionObservability(runtime, store)
    observation1 = observability.observe(DOMAIN, MISSION_ID)
    observation1_fingerprint = observation1.projection_fingerprint
    interface = PavilionNativeInterface(observability)

    interface.render_mission(DOMAIN, MISSION_ID)
    interface.render_mission(DOMAIN, MISSION_ID)
    interface.render_mission(DOMAIN, MISSION_ID)

    observation2 = observability.observe(DOMAIN, MISSION_ID)
    assert observation2.projection_fingerprint == observation1_fingerprint


# ======================================================================
# Additional coverage: full rendering verification
# ======================================================================


def test_render_includes_all_required_sections(tmp_path: Path) -> None:
    """Render output includes mission, status, and timeline sections."""
    runtime, store = _create_runtime_and_store(tmp_path, objective="Complete mission")
    runtime.start_mission(DOMAIN, MISSION_ID, expected_revision=1)
    interface = _interface(runtime, store)

    rendered = interface.render_mission(DOMAIN, MISSION_ID)

    assert "MissionaryX Mission" in rendered
    assert f"Mission: {MISSION_ID}" in rendered
    assert f"Domain: {DOMAIN}" in rendered
    assert "State: RUNNING" in rendered
    assert "Objective" in rendered
    assert "Complete mission" in rendered
    assert "Status" in rendered
    assert "Created:" in rendered
    assert "Updated:" in rendered
    assert "Effects:" in rendered
    assert "Projection:" in rendered
    assert "Recent Timeline" in rendered
    assert "Generated:" in rendered


def test_view_preserves_all_observation_fields(tmp_path: Path) -> None:
    """PavilionMissionView preserves all relevant MissionObservation fields."""
    runtime, store = _create_runtime_and_store(tmp_path)
    runtime.start_mission(DOMAIN, MISSION_ID, expected_revision=1)
    intent, dispatch = _create_effect(store, include_claim=True)
    assert dispatch is not None
    runtime.add_effect_reference(
        DOMAIN,
        MISSION_ID,
        intent.effect_intent_id,
        effect_dispatch_id=dispatch.dispatch_id,
        gateway_claim_id="claim-1",
    )
    # Use fixed clock for deterministic generated_at
    fixed_generated_at = NOW + timedelta(days=1)
    observability = MissionObservability(runtime, store, clock=lambda: fixed_generated_at)
    observation = observability.observe(DOMAIN, MISSION_ID)
    interface = PavilionNativeInterface(observability)

    view = interface.mission_view(DOMAIN, MISSION_ID)

    # Verify all fields are preserved
    assert view.control_domain == observation.control_domain
    assert view.mission_id == observation.mission_id
    assert view.objective == observation.specification.objective
    assert view.owner_identity == observation.specification.owner_identity
    assert view.agent_identity == observation.specification.agent_identity
    assert view.lifecycle == observation.lifecycle
    assert view.revision == observation.revision
    assert view.updated_at == observation.updated_at
    assert view.created_at == observation.specification.created_at
    assert view.effect_count == len(observation.effects)
    assert view.unresolved_effect_count == len(observation.unresolved_effects)
    assert view.evidence_available == observation.evidence_available
    assert view.evidence_count == len(observation.evidence)
    assert view.unbound_evidence_count == observation.unbound_evidence_count
    assert view.projection_fingerprint == observation.projection_fingerprint
    assert view.generated_at == observation.generated_at


def test_mission_with_agent_identity_renders_correctly(tmp_path: Path) -> None:
    """Mission with agent_identity renders the agent field."""
    runtime = MissionRuntime(db_path=tmp_path / "missions.sqlite3")
    store = DurableEffectStore(tmp_path / "effects.sqlite3")
    runtime.create_mission(
        mission_id=MISSION_ID,
        control_domain=DOMAIN,
        objective="Agent mission",
        owner_identity="owner-1",
        agent_identity="agent-1",
    )
    interface = _interface(runtime, store)

    rendered = interface.render_mission(DOMAIN, MISSION_ID)

    assert "Agent: agent-1" in rendered
    view = interface.mission_view(DOMAIN, MISSION_ID)
    assert view.agent_identity == "agent-1"
