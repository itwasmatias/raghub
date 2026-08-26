from __future__ import annotations

import json
from pathlib import Path

import pytest

from federation.durable_effect_store import DurableEffectStore
from federation.effect_gateway import GatewayStateError
from federation.mission_observability import MissionObservability, ProjectedEffectStatus
from federation.mission_runtime import MissionRuntime
from federation.mission_runtime_store import MissionRuntimeStore
from tools.integrated_demonstrator.governed_ambiguous_dispatch import (
    reconcile_and_verify_governed_dispatch,
)


def test_reconciliation_verification_completion_and_report_survive_reopen(tmp_path: Path):
    result = reconcile_and_verify_governed_dispatch(tmp_path, starting_repository_sha="start-sha")
    report = result.report
    assert report["effect_history"] == ["indeterminate", "something_landed"]
    assert report["reconciliation_state"] == "resolved"
    assert report["authority_disposition"] == "consumed"
    assert report["final_mission_state"] == "completed"
    assert report["final_effect_posture"] == "something_landed"
    assert report["service_active_version"] == 2
    assert report["deployment_attempt_count"] == 1
    assert report["successful_transition_count"] == 1
    assert report["duplicate_deployment_count"] == 0
    assert report["independent_verification_result"] == "v2_active"
    assert report["persisted_evidence_history_count"] == 2
    json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False)

    effects = DurableEffectStore(result.ambiguous.effect_database_path)
    try:
        assert effects.get_obligation(
            result.ambiguous.reconciliation_obligation_id, "integrated-demonstrator"
        ).state.value == "resolved"
        assert effects.get_gateway_claim(
            result.ambiguous.gateway_claim_id, "integrated-demonstrator"
        )["state"] == "reconciled"
        assert effects.get_reservation(
            "authority-reservation-integrated-demonstrator-v2", "integrated-demonstrator"
        ).disposition.value == "consumed"
        observation = MissionObservability(
            MissionRuntime(MissionRuntimeStore(result.ambiguous.mission_database_path)), effects
        ).observe("integrated-demonstrator", result.ambiguous.mission_id)
        assert observation.lifecycle.value == "completed"
        assert observation.effects[0].projected_status is ProjectedEffectStatus.SOMETHING_LANDED
        assert any(item.reason == "independent_verification" for item in observation.checkpoints)
    finally:
        effects.close()


def test_reconciliation_rejects_mismatched_obligation_without_mutation(tmp_path: Path):
    # The integrated runner is intentionally the only path that can complete;
    # a mismatched obligation is rejected by the canonical gateway/store seam.
    ambiguous = __import__(
        "tools.integrated_demonstrator.governed_ambiguous_dispatch",
        fromlist=["run_governed_ambiguous_dispatch"],
    ).run_governed_ambiguous_dispatch(tmp_path)
    effects = DurableEffectStore(ambiguous.effect_database_path)
    try:
        with pytest.raises(GatewayStateError):
            from federation.effect_gateway import GovernedEffectGateway
            GovernedEffectGateway(effects).reconcile_indeterminate(
                ambiguous.gateway_request,
                ambiguous.gateway_claim_id,
                "wrong-obligation",
                object(),
                object(),
            )
        assert effects.get_gateway_claim(
            ambiguous.gateway_claim_id, "integrated-demonstrator"
        )["state"] == "indeterminate"
    finally:
        effects.close()
