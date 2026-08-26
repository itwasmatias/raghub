from __future__ import annotations

from federation.durable_effect_store import DurableEffectStore
from federation.effect_safety import AuthorityDisposition, EffectState, ReconciliationState
from federation.mission_runtime_store import MissionRuntimeStore
from tools.integrated_demonstrator.governed_ambiguous_dispatch import (
    run_governed_ambiguous_dispatch,
)
from tools.integrated_demonstrator.test_service import DemoServiceStore


def test_governed_dispatch_response_loss_is_durable_and_unresolved(tmp_path):
    run = run_governed_ambiguous_dispatch(tmp_path)

    service = DemoServiceStore(run.service_database_path)
    state = service.read_state()
    assert state.active_version == 2
    assert state.deployment_attempt_count == 1
    assert state.successful_transition_count == 1

    effects = DurableEffectStore(run.effect_database_path)
    claim = effects.get_gateway_claim(run.gateway_claim_id, "integrated-demonstrator")
    assert claim["state"] == EffectState.INDETERMINATE.value
    assert claim["receipt_recorded_at"] is None
    assert effects.get_obligation(
        run.reconciliation_obligation_id, "integrated-demonstrator"
    ).state is ReconciliationState.PENDING
    assert effects.get_reservation(
        "authority-reservation-integrated-demonstrator-v2", "integrated-demonstrator"
    ).disposition is AuthorityDisposition.RESERVED
    assert effects.get_intent(
        run.effect_intent_id, "integrated-demonstrator"
    ).mission_id == run.mission_id
    assert effects.get_dispatch(
        run.effect_dispatch_id, "integrated-demonstrator"
    ).effect_intent_id == run.effect_intent_id
    effects.close()

    reopened_effects = DurableEffectStore(run.effect_database_path)
    assert reopened_effects.get_gateway_claim(
        run.gateway_claim_id, "integrated-demonstrator"
    )["state"] == "indeterminate"
    assert reopened_effects.get_obligation(
        run.reconciliation_obligation_id, "integrated-demonstrator"
    ).state is ReconciliationState.PENDING
    reopened_effects.close()

    mission = MissionRuntimeStore(run.mission_database_path)
    _, lifecycle, _, _ = mission.get_mission("integrated-demonstrator", run.mission_id)
    assert lifecycle.value == "running"
    assert len(mission.list_effect_references("integrated-demonstrator", run.mission_id)) == 1


def test_blind_retry_is_blocked_without_second_http_request(tmp_path):
    run = run_governed_ambiguous_dispatch(tmp_path)

    assert run.transport_error in {"RemoteDisconnected", "URLError", "ConnectionResetError"}
    assert run.retry_blocked is True
    state = DemoServiceStore(run.service_database_path).read_state()
    assert state.deployment_attempt_count == 1
    assert state.successful_transition_count == 1
