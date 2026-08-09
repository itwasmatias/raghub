"""Deterministic confidence synthesis without proof or truth inference."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from threading import RLock

from research_mission.evaluation import ResearchEvaluation
from research_mission.replication import (
    ReplicationAttemptOutcome,
    ReplicationClaimStatus,
    ResearchReplicationResult,
    _evaluation_identity,
)


class ResearchConfidenceError(ValueError):
    """Base confidence synthesis error."""


class ResearchConfidenceContractError(ResearchConfidenceError):
    """Evaluation and replication evidence do not compose exactly."""


class ConfidenceClassification(str, Enum):
    UNREPLICATED = "unreplicated"
    PARTIALLY_REPLICATED = "partially_replicated"
    REPLICATED = "replicated"
    CONTRADICTED = "contradicted"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True, slots=True)
class ClaimConfidence:
    claim_id: str
    statement: str
    judge_eligible: bool
    judge_outcome: str
    classification: ConfidenceClassification
    original_evidence_ids: tuple[str, ...]
    replication_evidence_ids: tuple[str, ...]
    replication_attempt_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResearchConfidenceSynthesis:
    confidence_id: str
    mission_id: str
    evaluation_id: str
    replication_id: str | None
    claims: tuple[ClaimConfidence, ...]
    contradiction_ids: tuple[str, ...]
    included_original_evidence_ids: tuple[str, ...]
    excluded_original_evidence_ids: tuple[str, ...]
    replication_evidence_ids: tuple[str, ...]


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


class ResearchConfidenceSynthesisRuntime:
    """Combine supplied judge and replication classifications, never infer proof."""

    def __init__(self, evaluation):
        if type(evaluation) is not ResearchEvaluation:
            raise TypeError("evaluation must be a ResearchEvaluation")
        self._evaluation = evaluation
        self._identity = _evaluation_identity(evaluation)
        self._lock = RLock()

    def synthesize(self, replication):
        if replication is not None and type(replication) is not ResearchReplicationResult:
            raise TypeError("replication must be ResearchReplicationResult or None")
        with self._lock:
            self._require_evaluation()
            result = self._synthesize(replication)
            self._require_evaluation()
            return result

    def _require_evaluation(self):
        if _evaluation_identity(self._evaluation) != self._identity:
            raise ResearchConfidenceContractError("authoritative evaluation mutated")

    def _synthesize(self, replication):
        evaluation = self._evaluation
        eligible = {claim.claim_id: claim for claim in evaluation.synthesis.eligible_claims}
        replication_by_claim = {}
        if replication is not None:
            if not eligible:
                raise ResearchConfidenceContractError(
                    "replication cannot compose with an ineligible evaluation")
            if (replication.mission_id != evaluation.mission_id
                    or replication.evaluation_id != evaluation.evaluation_id
                    or replication.source_evidence_ids != evaluation.source_evidence_ids):
                raise ResearchConfidenceContractError("foreign replication evidence")
            if {item.claim_id for item in replication.claims} != set(eligible):
                raise ResearchConfidenceContractError("replication claims do not match eligible claims")
            if len({item.claim_id for item in replication.claims}) != len(replication.claims):
                raise ResearchConfidenceContractError("replication claim identity is ambiguous")
            if len(set(replication.replication_evidence_ids)) != len(replication.replication_evidence_ids):
                raise ResearchConfidenceContractError("replication evidence identity is ambiguous")
            if set(replication.replication_evidence_ids) & set(evaluation.source_evidence_ids):
                raise ResearchConfidenceContractError("replication evidence overlaps original evidence")
            attempt_ids = set()
            replicators = set()
            projected_evidence = set()
            for item in replication.claims:
                claim = eligible[item.claim_id]
                if item.statement != claim.statement or item.original_evidence_ids != claim.evidence_ids:
                    raise ResearchConfidenceContractError("replication claim provenance mismatch")
                item_attempt_ids = tuple(attempt.attempt_id for attempt in item.attempts)
                if len(set(item_attempt_ids)) != len(item_attempt_ids):
                    raise ResearchConfidenceContractError("replication attempts are ambiguous")
                if attempt_ids & set(item_attempt_ids):
                    raise ResearchConfidenceContractError("replication attempt identity is composed")
                attempt_ids.update(item_attempt_ids)
                for attempt in item.attempts:
                    if (attempt.mission_id != evaluation.mission_id
                            or attempt.evaluation_id != evaluation.evaluation_id
                            or attempt.claim_id != item.claim_id
                            or attempt.original_evidence_ids != claim.evidence_ids):
                        raise ResearchConfidenceContractError("foreign replication attempt")
                    if attempt.replicator_id in evaluation.source_result_task_ids:
                        raise ResearchConfidenceContractError("replicator is not independent")
                    replicator = (item.claim_id, attempt.replicator_id)
                    if replicator in replicators:
                        raise ResearchConfidenceContractError("replicator is duplicated for claim")
                    replicators.add(replicator)
                evidence_ids = tuple(sorted(
                    evidence for attempt in item.attempts
                    for evidence in attempt.replication_evidence_ids))
                if evidence_ids != item.replication_evidence_ids:
                    raise ResearchConfidenceContractError("replication evidence projection is contradictory")
                if projected_evidence & set(evidence_ids):
                    raise ResearchConfidenceContractError("replication evidence identity is composed")
                projected_evidence.update(evidence_ids)
                outcomes = tuple(attempt.outcome for attempt in item.attempts)
                if (ReplicationAttemptOutcome.CONTRADICTORY in outcomes
                        and item.status is not ReplicationClaimStatus.CONTRADICTORY):
                    raise ResearchConfidenceContractError(
                        "replication status conceals contradictory evidence")
                if item.status is ReplicationClaimStatus.REPLICATED and (
                    ReplicationAttemptOutcome.SUCCESSFUL not in outcomes
                    or ReplicationAttemptOutcome.CONTRADICTORY in outcomes
                ):
                    raise ResearchConfidenceContractError("replicated status lacks supporting attempt")
                if item.status is ReplicationClaimStatus.CONTRADICTORY and (
                    ReplicationAttemptOutcome.CONTRADICTORY not in outcomes
                ):
                    raise ResearchConfidenceContractError("contradicted status lacks supporting attempt")
                if item.status is ReplicationClaimStatus.FAILED and (
                    ReplicationAttemptOutcome.FAILED not in outcomes
                ):
                    raise ResearchConfidenceContractError("failed status lacks supporting attempt")
                replication_by_claim[item.claim_id] = item
            if tuple(sorted(projected_evidence)) != replication.replication_evidence_ids:
                raise ResearchConfidenceContractError("replication evidence projection is incomplete")
            payload = {
                "evaluation_id": evaluation.evaluation_id,
                "claims": [(item.claim_id, item.status.value,
                            [(attempt.attempt_id, attempt.mission_id, attempt.evaluation_id,
                              attempt.claim_id, attempt.original_evidence_ids,
                              attempt.replicator_id, attempt.outcome.value,
                              attempt.replication_evidence_ids, attempt.summary,
                              attempt.observed_at.isoformat())
                             for attempt in item.attempts]) for item in replication.claims],
            }
            if replication.replication_id != "replication-" + _digest(payload):
                raise ResearchConfidenceContractError("replication identity is not authoritative")
        elif eligible:
            replication_by_claim = {}

        claims = []
        all_replication_evidence = set()
        for claim in evaluation.claims:
            judge_eligible = claim.claim_id in eligible
            item = replication_by_claim.get(claim.claim_id)
            if not judge_eligible:
                classification = ConfidenceClassification.INSUFFICIENT_EVIDENCE
            elif item is None:
                classification = ConfidenceClassification.UNREPLICATED
            elif any(attempt.outcome is ReplicationAttemptOutcome.CONTRADICTORY
                     for attempt in item.attempts):
                classification = ConfidenceClassification.CONTRADICTED
            elif item.status is ReplicationClaimStatus.REPLICATED:
                classification = ConfidenceClassification.REPLICATED
            elif any(attempt.outcome is ReplicationAttemptOutcome.SUCCESSFUL
                     for attempt in item.attempts):
                classification = ConfidenceClassification.PARTIALLY_REPLICATED
            else:
                classification = ConfidenceClassification.INSUFFICIENT_EVIDENCE
            replication_evidence = () if item is None else item.replication_evidence_ids
            all_replication_evidence.update(replication_evidence)
            claims.append(ClaimConfidence(claim.claim_id, claim.statement, judge_eligible,
                evaluation.judge_decision.outcome.value, classification, claim.evidence_ids,
                replication_evidence, () if item is None else tuple(
                    attempt.attempt_id for attempt in item.attempts)))
        payload = {
            "evaluation_id": evaluation.evaluation_id,
            "replication_id": None if replication is None else replication.replication_id,
            "claims": [(item.claim_id, item.judge_eligible, item.classification.value,
                        item.original_evidence_ids, item.replication_evidence_ids,
                        item.replication_attempt_ids) for item in claims],
            "contradictions": evaluation.synthesis.contradiction_ids,
        }
        return ResearchConfidenceSynthesis("confidence-" + _digest(payload),
            evaluation.mission_id, evaluation.evaluation_id,
            None if replication is None else replication.replication_id, tuple(claims),
            evaluation.synthesis.contradiction_ids,
            evaluation.synthesis.included_evidence_ids,
            evaluation.synthesis.excluded_evidence_ids,
            tuple(sorted(all_replication_evidence)))
