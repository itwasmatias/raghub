"""Deterministic confidence synthesis over judge and replication evidence."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import hashlib
import json

import pytest

from research_mission import (
    ConfidenceClassification, JudgeEligibilityDecision, JudgeOutcome,
    ReplicationAttempt, ReplicationAttemptOutcome, ReplicationClaimEvaluation,
    ReplicationClaimStatus, ResearchChallenge, ResearchClaim, ResearchConfidenceContractError,
    ResearchConfidenceSynthesisRuntime, ResearchEvaluation, ResearchEvaluationStatus,
    ResearchReplicationResult, SynthesisResult,
)
from datetime import datetime, timezone

NOW = datetime(2026, 8, 9, tzinfo=timezone.utc)


def evaluation(*, eligible=True):
    claim = ResearchClaim("claim-1", "mission-1", "research-task", "bounded claim", ("source-1",))
    eligible_claims = (claim,) if eligible else ()
    synthesis = SynthesisResult("synthesis-1", "mission-1",
        ResearchEvaluationStatus.ELIGIBLE if eligible else ResearchEvaluationStatus.INELIGIBLE,
        eligible_claims, ("source-1",) if eligible else (),
        () if eligible else ("source-1",), ("contradiction-1",))
    return ResearchEvaluation("evaluation-1", "mission-1", ("research-task",),
        "challenge-task", "judge-task", (claim,), ResearchChallenge(
            "challenge-task", (), (), (), (), (), ("challenge-evidence",)), (),
        JudgeEligibilityDecision("judge-task", JudgeOutcome.ACCEPTED if eligible else JudgeOutcome.REJECTED,
            "bounded judgment", ("claim-1",) if eligible else (),
            () if eligible else ("claim-1",), ("source-1",) if eligible else (),
            () if eligible else ("source-1",), ("judge-evidence",)), synthesis,
        ("research-task", "challenge-task", "judge-task"),
        ("source-1", "challenge-evidence", "judge-evidence"))


def replication(status, *, attempts=()):
    claim = ReplicationClaimEvaluation("claim-1", "bounded claim", status,
        ("source-1",), tuple(sorted(
            evidence for item in attempts for evidence in item.replication_evidence_ids)),
        tuple(attempts))
    payload = {"evaluation_id": "evaluation-1", "claims": [(claim.claim_id,
        claim.status.value, [(item.attempt_id, item.mission_id, item.evaluation_id,
        item.claim_id, item.original_evidence_ids, item.replicator_id,
        item.outcome.value, item.replication_evidence_ids, item.summary,
        item.observed_at.isoformat()) for item in claim.attempts])]}
    replication_id = "replication-" + hashlib.sha256(json.dumps(payload,
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return ResearchReplicationResult(replication_id, "mission-1", "evaluation-1",
        (claim,), ("source-1", "challenge-evidence", "judge-evidence"),
        claim.replication_evidence_ids)


def attempt(outcome, attempt_id="attempt-1", evidence="replication-evidence"):
    return ReplicationAttempt(attempt_id, "mission-1", "evaluation-1", "claim-1",
        ("source-1",), "independent-worker", outcome, (evidence,), "attempt", NOW)


@pytest.mark.parametrize(("status", "expected"), [
    (ReplicationClaimStatus.REPLICATED, ConfidenceClassification.REPLICATED),
    (ReplicationClaimStatus.CONTRADICTORY, ConfidenceClassification.CONTRADICTED),
    (ReplicationClaimStatus.FAILED, ConfidenceClassification.INSUFFICIENT_EVIDENCE),
    (ReplicationClaimStatus.INSUFFICIENT, ConfidenceClassification.INSUFFICIENT_EVIDENCE),
])
def test_replication_status_maps_without_claiming_proof(status, expected):
    support = {
        ReplicationClaimStatus.REPLICATED: (attempt(ReplicationAttemptOutcome.SUCCESSFUL),),
        ReplicationClaimStatus.CONTRADICTORY: (attempt(ReplicationAttemptOutcome.CONTRADICTORY),),
        ReplicationClaimStatus.FAILED: (attempt(ReplicationAttemptOutcome.FAILED),),
        ReplicationClaimStatus.INSUFFICIENT: (attempt(ReplicationAttemptOutcome.INSUFFICIENT),),
    }[status]
    result = ResearchConfidenceSynthesisRuntime(evaluation()).synthesize(
        replication(status, attempts=support))
    assert result.claims[0].classification is expected
    assert "proof" not in result.claims[0].classification.value
    assert "established" not in result.claims[0].classification.value


def test_missing_replication_is_explicitly_unreplicated():
    result = ResearchConfidenceSynthesisRuntime(evaluation()).synthesize(None)
    assert result.claims[0].classification is ConfidenceClassification.UNREPLICATED


def test_some_success_below_threshold_is_partially_replicated():
    successful = attempt(ReplicationAttemptOutcome.SUCCESSFUL)
    source = replication(ReplicationClaimStatus.INSUFFICIENT, attempts=(successful,))
    result = ResearchConfidenceSynthesisRuntime(evaluation()).synthesize(source)
    assert result.claims[0].classification is ConfidenceClassification.PARTIALLY_REPLICATED


def test_contradiction_dominates_successful_attempts():
    attempts = (attempt(ReplicationAttemptOutcome.SUCCESSFUL),
        replace(attempt(ReplicationAttemptOutcome.CONTRADICTORY, "attempt-2",
            "contradiction-evidence"), replicator_id="independent-worker-2"))
    result = ResearchConfidenceSynthesisRuntime(evaluation()).synthesize(
        replication(ReplicationClaimStatus.CONTRADICTORY, attempts=attempts))
    assert result.claims[0].classification is ConfidenceClassification.CONTRADICTED
    assert result.claims[0].replication_evidence_ids == (
        "contradiction-evidence", "replication-evidence")


def test_judge_ineligible_claim_remains_insufficient_even_without_replication():
    result = ResearchConfidenceSynthesisRuntime(evaluation(eligible=False)).synthesize(None)
    assert result.claims[0].judge_eligible is False
    assert result.claims[0].classification is ConfidenceClassification.INSUFFICIENT_EVIDENCE
    assert result.excluded_original_evidence_ids == ("source-1",)


def test_replication_cannot_compose_with_ineligible_evaluation():
    source = replace(replication(ReplicationClaimStatus.INSUFFICIENT), claims=(),
        replication_evidence_ids=())
    with pytest.raises(ResearchConfidenceContractError):
        ResearchConfidenceSynthesisRuntime(evaluation(eligible=False)).synthesize(source)


def test_replication_status_cannot_conceal_contradiction():
    source = replication(ReplicationClaimStatus.INSUFFICIENT,
        attempts=(attempt(ReplicationAttemptOutcome.CONTRADICTORY),))
    with pytest.raises(ResearchConfidenceContractError):
        ResearchConfidenceSynthesisRuntime(evaluation()).synthesize(source)


@pytest.mark.parametrize("change", [
    {"mission_id": "foreign"}, {"evaluation_id": "foreign"},
    {"source_evidence_ids": ("foreign",)},
])
def test_foreign_or_composed_replication_evidence_fails_closed(change):
    source = replication(ReplicationClaimStatus.REPLICATED)
    with pytest.raises(ResearchConfidenceContractError):
        ResearchConfidenceSynthesisRuntime(evaluation()).synthesize(replace(source, **change))


def test_forged_replication_identity_fails_closed():
    source = replication(ReplicationClaimStatus.REPLICATED,
        attempts=(attempt(ReplicationAttemptOutcome.SUCCESSFUL),))
    with pytest.raises(ResearchConfidenceContractError):
        ResearchConfidenceSynthesisRuntime(evaluation()).synthesize(
            replace(source, replication_id="replication-forged"))


def test_missing_or_extra_replication_claim_fails_closed():
    source = replication(ReplicationClaimStatus.REPLICATED,
        attempts=(attempt(ReplicationAttemptOutcome.SUCCESSFUL),))
    with pytest.raises(ResearchConfidenceContractError):
        ResearchConfidenceSynthesisRuntime(evaluation()).synthesize(replace(source, claims=()))
    foreign = replace(source.claims[0], claim_id="foreign")
    with pytest.raises(ResearchConfidenceContractError):
        ResearchConfidenceSynthesisRuntime(evaluation()).synthesize(replace(source, claims=(foreign,)))


def test_original_and_replication_provenance_remain_separate():
    source = replication(ReplicationClaimStatus.REPLICATED,
        attempts=(attempt(ReplicationAttemptOutcome.SUCCESSFUL),))
    claim = ResearchConfidenceSynthesisRuntime(evaluation()).synthesize(source).claims[0]
    assert claim.original_evidence_ids == ("source-1",)
    assert claim.replication_evidence_ids == ("replication-evidence",)
    assert not set(claim.original_evidence_ids) & set(claim.replication_evidence_ids)


def test_deterministic_idempotent_thread_safe_and_immutable():
    runtime = ResearchConfidenceSynthesisRuntime(evaluation())
    source = replication(ReplicationClaimStatus.REPLICATED,
        attempts=(attempt(ReplicationAttemptOutcome.SUCCESSFUL),))
    expected = runtime.synthesize(source)
    assert runtime.synthesize(source) == expected
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(item == expected for item in pool.map(lambda _: runtime.synthesize(source), range(32)))
    with pytest.raises(FrozenInstanceError): expected.claims[0].classification = ConfidenceClassification.CONTRADICTED


def test_mutated_evaluation_after_capture_fails_closed():
    source = evaluation(); runtime = ResearchConfidenceSynthesisRuntime(source)
    object.__setattr__(source, "mission_id", "mutated")
    with pytest.raises(ResearchConfidenceContractError): runtime.synthesize(None)
