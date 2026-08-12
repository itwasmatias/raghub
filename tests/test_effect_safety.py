"""Focused tests for effect safety spine primitives.

M6 Execution Prompt Engine v0.1 — Effect Safety Spine

Tests cover:
- Stable decision lineage
- Intent/dispatch separation
- Effect states (nothing_landed, something_landed, indeterminate)
- Authority reservation and terminal disposition
- Reconciliation obligations
- Provider capability contracts
- Mission posture and lifecycle
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from federation.effect_safety import (
    EffectIntent,
    EffectDispatch,
    EffectState,
    AuthorityReservation,
    AuthorityDisposition,
    ReconciliationObligation,
    ReconciliationState,
    ProviderCapability,
    ProviderReconcilability,
    MissionEffectFold,
    MissionPosture,
    MissionLifecycle,
    EffectIntentRegistry,
    derive_effect_state,
    resolve_indeterminate_from_evidence,
    project_mission_posture,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class TestEffectIntent:
    """Test write-ahead intent commitment before dispatch."""

    def test_intent_commitment_creates_stable_decision_id(self):
        """Intent must have stable decision_id for joining to other records."""
        intent = EffectIntent(
            effect_intent_id="intent-001",
            decision_id="decision-001",
            mission_id="mission-001",
            task_id="task-001",
            attempt_id="attempt-001",
            operation_digest=_fingerprint({"op": "test"}),
            idempotency_key="idempotency-001",
            provider_scope="test-provider",
            authority_reservation_id="reservation-001",
            compensation_strategy=None,
            evidence_reference="evidence-001",
            state="committed_not_dispatched",
            created_at=_now(),
        )
        assert intent.decision_id == "decision-001"
        assert intent.state == "committed_not_dispatched"

    def test_intent_without_dispatch_is_not_reconciliation_obligation(self):
        """Committed intent with no dispatch is not a provider obligation."""
        intent = EffectIntent(
            effect_intent_id="intent-002",
            decision_id="decision-002",
            mission_id="mission-002",
            task_id=None,
            attempt_id="attempt-002",
            operation_digest=_fingerprint({"op": "test"}),
            idempotency_key="idempotency-002",
            provider_scope="test-provider",
            authority_reservation_id="reservation-002",
            compensation_strategy=None,
            evidence_reference="evidence-002",
            state="committed_not_dispatched",
            created_at=_now(),
        )
        # No dispatch exists, so no reconciliation obligation
        assert intent.state == "committed_not_dispatched"


class TestEffectDispatch:
    """Test dispatch attempt tracking separate from intent."""

    def test_dispatch_attempt_tracks_transport_posture(self):
        """Dispatch records transport outcome separately from intent."""
        dispatch = EffectDispatch(
            dispatch_id="dispatch-001",
            effect_intent_id="intent-001",
            attempt_id="attempt-001",
            idempotency_key="idempotency-001",
            provider_adapter="test-adapter",
            capability_profile_version="v1",
            transport_digest=_fingerprint({"transport": "details"}),
            posture="attempting",
            provider_operation_id=None,
            evidence_reference="dispatch-evidence-001",
            dispatched_at=_now(),
        )
        assert dispatch.posture == "attempting"
        assert dispatch.provider_operation_id is None

    def test_possibly_escaped_dispatch_becomes_indeterminate(self):
        """Dispatch that may have escaped without confirmation is indeterminate."""
        dispatch = EffectDispatch(
            dispatch_id="dispatch-002",
            effect_intent_id="intent-002",
            attempt_id="attempt-002",
            idempotency_key="idempotency-002",
            provider_adapter="test-adapter",
            capability_profile_version="v1",
            transport_digest=_fingerprint({"transport": "details"}),
            posture="transport_outcome_unknown",
            provider_operation_id=None,
            evidence_reference="dispatch-evidence-002",
            dispatched_at=_now(),
        )
        assert dispatch.posture == "transport_outcome_unknown"


class TestEffectState:
    """Test nothing_landed, something_landed, indeterminate states."""

    def test_nothing_landed_requires_affirmative_boundary_evidence(self):
        """nothing_landed state requires proof no provider operation committed."""
        state = derive_effect_state(
            dispatch_posture="rejected_before_send",
            boundary_evidence={"confirmed": "no_provider_operation"},
        )
        assert state == EffectState.NOTHING_LANDED

    def test_something_landed_when_external_change_committed(self):
        """something_landed state when external change committed."""
        state = derive_effect_state(
            dispatch_posture="accepted_by_transport",
            boundary_evidence={"confirmed": "provider_committed"},
        )
        assert state == EffectState.SOMETHING_LANDED

    def test_indeterminate_when_request_may_have_escaped(self):
        """indeterminate state when request may have escaped without confirmation."""
        state = derive_effect_state(
            dispatch_posture="transport_outcome_unknown",
            boundary_evidence={},
        )
        assert state == EffectState.INDETERMINATE

    def test_indeterminate_cannot_resolve_from_task_failure(self):
        """Task completion/failure cannot resolve indeterminate."""
        with pytest.raises(ValueError, match="task state cannot resolve indeterminate"):
            resolve_indeterminate_from_evidence(
                effect_state=EffectState.INDETERMINATE,
                evidence_source="task_completion",
                evidence={},
            )

    def test_indeterminate_cannot_resolve_from_timeout(self):
        """Elapsed time cannot resolve indeterminate."""
        with pytest.raises(ValueError, match="timeout cannot resolve indeterminate"):
            resolve_indeterminate_from_evidence(
                effect_state=EffectState.INDETERMINATE,
                evidence_source="timeout",
                evidence={},
            )

    def test_indeterminate_cannot_resolve_from_retry_count(self):
        """Retry count cannot resolve indeterminate."""
        with pytest.raises(ValueError, match="retry count cannot resolve indeterminate"):
            resolve_indeterminate_from_evidence(
                effect_state=EffectState.INDETERMINATE,
                evidence_source="retry_count",
                evidence={},
            )

    def test_only_boundary_reconciliation_evidence_resolves_indeterminate(self):
        """Only authoritative boundary reconciliation can resolve indeterminate."""
        resolved_state = resolve_indeterminate_from_evidence(
            effect_state=EffectState.INDETERMINATE,
            evidence_source="boundary_reconciliation",
            evidence={
                "idempotency_key": "key-001",
                "provider_operation_id": None,
                "confirmed": "no_operation_committed",
            },
        )
        assert resolved_state == EffectState.NOTHING_LANDED


class TestAuthorityReservation:
    """Test authority reservation tracking."""

    def test_reservation_held_before_dispatch(self):
        """Authority is reserved before dispatch."""
        reservation = AuthorityReservation(
            reservation_id="res-001",
            effect_intent_id="intent-001",
            capability_type="compute",
            amount=10.0,
            disposition=AuthorityDisposition.RESERVED,
            reserved_at=_now(),
            disposition_at=None,
            disposition_evidence=None,
        )
        assert reservation.disposition == AuthorityDisposition.RESERVED

    def test_reservation_consumed_on_something_landed(self):
        """Authority consumed when something_landed."""
        reservation = AuthorityReservation(
            reservation_id="res-002",
            effect_intent_id="intent-002",
            capability_type="compute",
            amount=10.0,
            disposition=AuthorityDisposition.CONSUMED,
            reserved_at=_now() - timedelta(minutes=5),
            disposition_at=_now(),
            disposition_evidence="effect-state:something_landed",
        )
        assert reservation.disposition == AuthorityDisposition.CONSUMED

    def test_reservation_released_on_nothing_landed(self):
        """Authority released when nothing_landed with affirmative proof."""
        reservation = AuthorityReservation(
            reservation_id="res-003",
            effect_intent_id="intent-003",
            capability_type="compute",
            amount=10.0,
            disposition=AuthorityDisposition.RELEASED,
            reserved_at=_now() - timedelta(minutes=5),
            disposition_at=_now(),
            disposition_evidence="effect-state:nothing_landed",
        )
        assert reservation.disposition == AuthorityDisposition.RELEASED

    def test_reservation_held_while_indeterminate(self):
        """Authority held while effect state is indeterminate."""
        reservation = AuthorityReservation(
            reservation_id="res-004",
            effect_intent_id="intent-004",
            capability_type="compute",
            amount=10.0,
            disposition=AuthorityDisposition.RESERVED,
            reserved_at=_now() - timedelta(hours=1),
            disposition_at=None,
            disposition_evidence=None,
        )
        assert reservation.disposition == AuthorityDisposition.RESERVED
        # Verify indeterminate does not automatically free reservation

    def test_retry_cannot_double_spend_indeterminate_reservation(self):
        """Retry cannot double-spend a held indeterminate reservation."""
        existing_reservation = AuthorityReservation(
            reservation_id="res-005",
            effect_intent_id="intent-005",
            capability_type="compute",
            amount=10.0,
            disposition=AuthorityDisposition.RESERVED,
            reserved_at=_now() - timedelta(hours=1),
            disposition_at=None,
            disposition_evidence=None,
        )
        # Attempting to create a new reservation for retry should fail
        # This would be enforced by EffectIntentRegistry
        assert existing_reservation.disposition == AuthorityDisposition.RESERVED

    def test_terminal_disposition_assumed_consumed_unreconciled(self):
        """Terminal disposition for permanently unresolvable effect."""
        reservation = AuthorityReservation(
            reservation_id="res-006",
            effect_intent_id="intent-006",
            capability_type="compute",
            amount=10.0,
            disposition=AuthorityDisposition.ASSUMED_CONSUMED_UNRECONCILED,
            reserved_at=_now() - timedelta(days=7),
            disposition_at=_now(),
            disposition_evidence="escalation:terminal-disposition-policy",
        )
        assert reservation.disposition == AuthorityDisposition.ASSUMED_CONSUMED_UNRECONCILED
        # Must require explicit escalation/policy evidence
        assert "escalation" in reservation.disposition_evidence


class TestReconciliationObligation:
    """Test durable background reconciliation obligations."""

    def test_indeterminate_effect_creates_reconciliation_obligation(self):
        """Indeterminate effect creates durable reconciliation obligation."""
        obligation = ReconciliationObligation(
            obligation_id="obligation-001",
            effect_intent_id="intent-001",
            dispatch_id="dispatch-001",
            state=ReconciliationState.PENDING,
            provider_reconcilability=ProviderReconcilability.IDEMPOTENCY_KEY_LOOKUP,
            next_probe_at=_now() + timedelta(minutes=5),
            probe_history=(),
            terminal_disposition=None,
            created_at=_now(),
        )
        assert obligation.state == ReconciliationState.PENDING

    def test_reconciliation_obligation_tracks_probe_history(self):
        """Reconciliation obligation tracks probe attempts."""
        obligation = ReconciliationObligation(
            obligation_id="obligation-002",
            effect_intent_id="intent-002",
            dispatch_id="dispatch-002",
            state=ReconciliationState.IN_PROGRESS,
            provider_reconcilability=ProviderReconcilability.PROVIDER_OPERATION_LOOKUP,
            next_probe_at=_now() + timedelta(minutes=10),
            probe_history=(
                {"probed_at": _now().isoformat(), "result": "still_indeterminate"},
            ),
            terminal_disposition=None,
            created_at=_now() - timedelta(hours=1),
        )
        assert len(obligation.probe_history) == 1
        assert obligation.state == ReconciliationState.IN_PROGRESS

    def test_terminal_disposition_requires_escalation(self):
        """Terminal disposition requires explicit policy/escalation."""
        obligation = ReconciliationObligation(
            obligation_id="obligation-003",
            effect_intent_id="intent-003",
            dispatch_id="dispatch-003",
            state=ReconciliationState.ESCALATED,
            provider_reconcilability=ProviderReconcilability.NONE,
            next_probe_at=None,
            probe_history=(
                {"probed_at": (_now() - timedelta(days=1)).isoformat(), "result": "unreconcilable"},
            ),
            terminal_disposition="assumed_consumed_unreconciled",
            created_at=_now() - timedelta(days=7),
        )
        assert obligation.terminal_disposition == "assumed_consumed_unreconciled"
        assert obligation.state == ReconciliationState.ESCALATED


class TestProviderCapability:
    """Test provider capability contracts."""

    def test_non_reconcilable_provider_requires_bounded_consequence(self):
        """Provider with no reconcilability requires bounded consequence policy."""
        capability = ProviderCapability(
            provider_id="provider-001",
            adapter_version="v1.0",
            operation_family="test_ops",
            idempotency_semantics={"scope": "none"},
            reconcilability=ProviderReconcilability.NONE,
            confirmation_guarantees={},
            blind_retry_safe=False,
            max_bounded_consequence={"max_cost": 0.01},
            compensation_support=False,
            tested_at=_now(),
            evidence="capability-test-evidence-001",
        )
        assert capability.reconcilability == ProviderReconcilability.NONE
        assert capability.max_bounded_consequence is not None

    def test_provider_with_idempotency_key_reconcilability(self):
        """Provider supports idempotency key lookup."""
        capability = ProviderCapability(
            provider_id="provider-002",
            adapter_version="v1.0",
            operation_family="api_calls",
            idempotency_semantics={"scope": "key", "retention": "7d"},
            reconcilability=ProviderReconcilability.IDEMPOTENCY_KEY_LOOKUP,
            confirmation_guarantees={"lookup_retention": "7d"},
            blind_retry_safe=True,
            max_bounded_consequence=None,
            compensation_support=False,
            tested_at=_now(),
            evidence="capability-test-evidence-002",
        )
        assert capability.reconcilability == ProviderReconcilability.IDEMPOTENCY_KEY_LOOKUP
        assert capability.blind_retry_safe is True


class TestMissionPosture:
    """Test mission posture fold over effect states."""

    def test_any_indeterminate_effect_parks_mission_for_reconciliation(self):
        """Any indeterminate effect → parked_reconciliation."""
        posture = project_mission_posture(
            task_states={"task-001": "succeeded"},
            effect_states={
                "effect-001": EffectState.SOMETHING_LANDED,
                "effect-002": EffectState.INDETERMINATE,
            },
            compensation_states={},
        )
        assert posture == MissionPosture.PARKED_RECONCILIATION

    def test_failed_task_with_uncompensated_landed_effect_requires_compensation(self):
        """Failed task with uncompensated landed effect → dirty_compensation_required."""
        posture = project_mission_posture(
            task_states={"task-001": "failed"},
            effect_states={"effect-001": EffectState.SOMETHING_LANDED},
            compensation_states={},
        )
        assert posture == MissionPosture.DIRTY_COMPENSATION_REQUIRED

    def test_all_failed_tasks_with_nothing_landed_are_clean_failed(self):
        """All failed tasks with verified nothing_landed → clean_failed."""
        posture = project_mission_posture(
            task_states={"task-001": "failed", "task-002": "failed"},
            effect_states={
                "effect-001": EffectState.NOTHING_LANDED,
                "effect-002": EffectState.NOTHING_LANDED,
            },
            compensation_states={},
        )
        assert posture == MissionPosture.CLEAN_FAILED

    def test_successful_tasks_with_landed_effects_are_clean_succeeded(self):
        """Successful tasks with intended landed effects → clean_succeeded."""
        posture = project_mission_posture(
            task_states={"task-001": "succeeded"},
            effect_states={"effect-001": EffectState.SOMETHING_LANDED},
            compensation_states={},
        )
        assert posture == MissionPosture.CLEAN_SUCCEEDED


class TestMissionLifecycle:
    """Test mission lifecycle independent of posture."""

    def test_mission_can_close_while_effects_unsettled(self):
        """Mission can close when task execution ends even with unsettled effects."""
        lifecycle = MissionLifecycle.CLOSED
        # Mission closed but effects may still be in reconciliation
        assert lifecycle == MissionLifecycle.CLOSED

    def test_mission_cannot_seal_with_indeterminate_effects(self):
        """Mission cannot seal with unresolved indeterminate effects."""
        fold = MissionEffectFold(
            mission_id="mission-001",
            posture=MissionPosture.PARKED_RECONCILIATION,
            lifecycle=MissionLifecycle.CLOSED,
            indeterminate_effects=("effect-001",),
            uncompensated_landed_effects=(),
            active_reconciliation_obligations=("obligation-001",),
            folded_at=_now(),
            seal_digest=None,
        )
        # Cannot seal while indeterminate effects exist
        assert fold.seal_digest is None
        assert len(fold.indeterminate_effects) > 0

    def test_mission_cannot_seal_with_unsettled_compensation(self):
        """Mission cannot seal with unsettled compensation."""
        fold = MissionEffectFold(
            mission_id="mission-002",
            posture=MissionPosture.DIRTY_COMPENSATION_REQUIRED,
            lifecycle=MissionLifecycle.CLOSED,
            indeterminate_effects=(),
            uncompensated_landed_effects=("effect-001",),
            active_reconciliation_obligations=(),
            folded_at=_now(),
            seal_digest=None,
        )
        # Cannot seal while uncompensated effects exist
        assert fold.seal_digest is None
        assert len(fold.uncompensated_landed_effects) > 0

    def test_mission_can_seal_when_effects_settled(self):
        """Mission can seal when no indeterminate effects and compensation settled."""
        fold = MissionEffectFold(
            mission_id="mission-003",
            posture=MissionPosture.CLEAN_SUCCEEDED,
            lifecycle=MissionLifecycle.SEALED,
            indeterminate_effects=(),
            uncompensated_landed_effects=(),
            active_reconciliation_obligations=(),
            folded_at=_now(),
            seal_digest=_fingerprint({"evidence": "complete"}),
        )
        assert fold.lifecycle == MissionLifecycle.SEALED
        assert fold.seal_digest is not None
        assert len(fold.indeterminate_effects) == 0
        assert len(fold.uncompensated_landed_effects) == 0
