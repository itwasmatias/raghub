"""Deterministic independent replication contract tests."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

import pytest

from research_mission import (
    JudgeEligibilityDecision, JudgeOutcome, ReplicationAttempt,
    ReplicationAttemptOutcome, ReplicationClaimStatus, ResearchChallenge,
    ResearchClaim, ResearchEvaluation, ResearchEvaluationStatus,
    ResearchReplicationContractError, ResearchReplicationRuntime,
    SynthesisResult,
)

NOW = datetime(2026, 8, 9, tzinfo=timezone.utc)


def evaluation():
    claim = ResearchClaim("claim-1", "mission-1", "research-task", "bounded claim", ("source-1",))
    synthesis = SynthesisResult("synthesis-1", "mission-1", ResearchEvaluationStatus.ELIGIBLE,
        (claim,), ("source-1",), (), ())
    return ResearchEvaluation("evaluation-1", "mission-1", ("research-task",),
        "challenge-task", "judge-task", (claim,), ResearchChallenge(
            "challenge-task", (), (), (), (), (), ("challenge-evidence",)), (),
        JudgeEligibilityDecision("judge-task", JudgeOutcome.ACCEPTED, "accepted",
            ("claim-1",), (), ("source-1",), (), ("judge-evidence",)), synthesis,
        ("research-task", "challenge-task", "judge-task"),
        ("source-1", "challenge-evidence", "judge-evidence"))


def attempt(outcome=ReplicationAttemptOutcome.SUCCESSFUL, *, replicator="replicator-a",
            attempt_id="replication-1", evidence_id="replication-evidence-1"):
    return ReplicationAttempt(attempt_id, "mission-1", "evaluation-1", "claim-1",
        ("source-1",), replicator, outcome, (evidence_id,), "independent attempt", NOW)


def test_successful_independent_replication_is_replicated_not_proof():
    result = ResearchReplicationRuntime(evaluation()).evaluate((attempt(),))
    assert result.claims[0].status is ReplicationClaimStatus.REPLICATED
    assert "established" not in result.claims[0].status.value
    assert result.claims[0].original_evidence_ids == ("source-1",)
    assert result.claims[0].replication_evidence_ids == ("replication-evidence-1",)


def test_no_attempt_or_insufficient_attempt_remains_insufficient():
    runtime = ResearchReplicationRuntime(evaluation(), minimum_successful_replications=2)
    assert runtime.evaluate(()).claims[0].status is ReplicationClaimStatus.INSUFFICIENT
    assert runtime.evaluate((attempt(),)).claims[0].status is ReplicationClaimStatus.INSUFFICIENT


def test_failed_and_explicitly_insufficient_attempts_are_distinct():
    failed = ResearchReplicationRuntime(evaluation()).evaluate((attempt(
        ReplicationAttemptOutcome.FAILED),))
    insufficient = ResearchReplicationRuntime(evaluation()).evaluate((attempt(
        ReplicationAttemptOutcome.INSUFFICIENT),))
    assert failed.claims[0].status is ReplicationClaimStatus.FAILED
    assert insufficient.claims[0].status is ReplicationClaimStatus.INSUFFICIENT


def test_contradiction_dominates_success_and_preserves_disagreement():
    attempts = (attempt(), attempt(ReplicationAttemptOutcome.CONTRADICTORY,
        replicator="replicator-b", attempt_id="replication-2", evidence_id="replication-evidence-2"))
    result = ResearchReplicationRuntime(evaluation()).evaluate(attempts)
    assert result.claims[0].status is ReplicationClaimStatus.CONTRADICTORY
    assert tuple(item.outcome for item in result.claims[0].attempts) == (
        ReplicationAttemptOutcome.SUCCESSFUL, ReplicationAttemptOutcome.CONTRADICTORY)


@pytest.mark.parametrize("change", [
    {"mission_id": "foreign"}, {"evaluation_id": "foreign"},
    {"claim_id": "foreign"}, {"original_evidence_ids": ("foreign",)},
])
def test_foreign_or_composed_authority_is_rejected(change):
    with pytest.raises(ResearchReplicationContractError):
        ResearchReplicationRuntime(evaluation()).evaluate((replace(attempt(), **change),))


def test_replication_evidence_cannot_reuse_original_evidence():
    with pytest.raises(ResearchReplicationContractError):
        ResearchReplicationRuntime(evaluation()).evaluate((attempt(evidence_id="source-1"),))


@pytest.mark.parametrize("source_role", ["research-task", "challenge-task", "judge-task"])
def test_source_role_cannot_count_as_independent_replicator(source_role):
    with pytest.raises(ResearchReplicationContractError):
        ResearchReplicationRuntime(evaluation()).evaluate((attempt(replicator=source_role),))


def test_duplicate_attempt_or_replicator_is_rejected():
    one = attempt()
    with pytest.raises(ResearchReplicationContractError):
        ResearchReplicationRuntime(evaluation()).evaluate((one, one))
    with pytest.raises(ResearchReplicationContractError):
        ResearchReplicationRuntime(evaluation()).evaluate((one, replace(one,
            attempt_id="replication-2", replication_evidence_ids=("replication-evidence-2",)),))


def test_ineligible_evaluation_cannot_be_replicated_as_eligible():
    source = evaluation()
    source = replace(source, synthesis=replace(source.synthesis,
        status=ResearchEvaluationStatus.INELIGIBLE, eligible_claims=(), included_evidence_ids=()))
    with pytest.raises(ResearchReplicationContractError): ResearchReplicationRuntime(source)


def test_deterministic_idempotent_ordering_and_thread_safety():
    attempts = (attempt(ReplicationAttemptOutcome.CONTRADICTORY, replicator="b",
        attempt_id="z", evidence_id="z-evidence"), attempt(replicator="a",
        attempt_id="a", evidence_id="a-evidence"))
    runtime = ResearchReplicationRuntime(evaluation())
    expected = runtime.evaluate(attempts)
    assert runtime.evaluate(reversed(attempts)) == expected
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(item == expected for item in pool.map(lambda _: runtime.evaluate(attempts), range(32)))


def test_outputs_are_immutable_snapshots():
    result = ResearchReplicationRuntime(evaluation()).evaluate((attempt(),))
    with pytest.raises(FrozenInstanceError): result.claims[0].status = ReplicationClaimStatus.FAILED


def test_evaluation_mutation_after_runtime_capture_is_rejected():
    source = evaluation(); runtime = ResearchReplicationRuntime(source)
    object.__setattr__(source, "mission_id", "mutated")
    with pytest.raises(ResearchReplicationContractError): runtime.evaluate((attempt(),))


def test_malformed_threshold_and_attempt_payload_fail_closed():
    with pytest.raises(ValueError): ResearchReplicationRuntime(evaluation(), minimum_successful_replications=0)
    with pytest.raises(ValueError): ReplicationAttempt("", "mission-1", "evaluation-1", "claim-1",
        ("source-1",), "replicator", ReplicationAttemptOutcome.SUCCESSFUL,
        ("replication-evidence",), "summary", NOW)


def test_result_identity_binds_complete_replication_attempt_payload():
    runtime = ResearchReplicationRuntime(evaluation())
    first = runtime.evaluate((attempt(),))
    changed = runtime.evaluate((replace(attempt(), summary="materially changed summary"),))
    assert first.replication_id != changed.replication_id
