"""Deterministic replication evaluation over eligible research claims."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from threading import RLock

from research_mission.evaluation import ResearchEvaluation, ResearchEvaluationStatus


class ResearchReplicationError(ValueError):
    """Base replication contract error."""


class ResearchReplicationContractError(ResearchReplicationError):
    """Replication evidence does not compose with authoritative evaluation."""


class ReplicationAttemptOutcome(str, Enum):
    SUCCESSFUL = "successful"
    FAILED = "failed"
    CONTRADICTORY = "contradictory"
    INSUFFICIENT = "insufficient"


class ReplicationClaimStatus(str, Enum):
    REPLICATED = "replicated"
    FAILED = "failed"
    CONTRADICTORY = "contradictory"
    INSUFFICIENT = "insufficient"


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a canonical non-empty string")
    return value


def _identifiers(values, name):
    result = tuple(_identifier(item, f"{name} entry") for item in values)
    if len(set(result)) != len(result): raise ValueError(f"{name} must be unique")
    return result


def _time(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    return value.astimezone(timezone.utc)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ReplicationAttempt:
    attempt_id: str
    mission_id: str
    evaluation_id: str
    claim_id: str
    original_evidence_ids: tuple[str, ...]
    replicator_id: str
    outcome: ReplicationAttemptOutcome
    replication_evidence_ids: tuple[str, ...]
    summary: str
    observed_at: datetime

    def __post_init__(self):
        for name in ("attempt_id", "mission_id", "evaluation_id", "claim_id",
                     "replicator_id", "summary"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        object.__setattr__(self, "original_evidence_ids",
            _identifiers(self.original_evidence_ids, "original_evidence_ids"))
        object.__setattr__(self, "replication_evidence_ids",
            _identifiers(self.replication_evidence_ids, "replication_evidence_ids"))
        if not self.replication_evidence_ids:
            raise ValueError("replication_evidence_ids must not be empty")
        try: object.__setattr__(self, "outcome", ReplicationAttemptOutcome(self.outcome))
        except (TypeError, ValueError) as exc: raise ValueError("outcome is invalid") from exc
        object.__setattr__(self, "observed_at", _time(self.observed_at))


@dataclass(frozen=True, slots=True)
class ReplicationClaimEvaluation:
    claim_id: str
    statement: str
    status: ReplicationClaimStatus
    original_evidence_ids: tuple[str, ...]
    replication_evidence_ids: tuple[str, ...]
    attempts: tuple[ReplicationAttempt, ...]


@dataclass(frozen=True, slots=True)
class ResearchReplicationResult:
    replication_id: str
    mission_id: str
    evaluation_id: str
    claims: tuple[ReplicationClaimEvaluation, ...]
    source_evidence_ids: tuple[str, ...]
    replication_evidence_ids: tuple[str, ...]


def _evaluation_identity(value):
    return (
        value.evaluation_id, value.mission_id, value.researcher_task_ids,
        value.challenger_task_id, value.judge_task_id,
        tuple((claim.claim_id, claim.mission_id, claim.task_id, claim.statement,
               claim.evidence_ids) for claim in value.claims),
        (value.challenge.challenger_task_id, value.challenge.challenged_claims,
         value.challenge.weaknesses,
         tuple((item.contradiction_id, item.mission_id, item.challenger_task_id,
                item.statement) for item in value.challenge.contradictions),
         value.challenge.missing_evidence, value.challenge.alternative_explanations,
         value.challenge.evidence_ids),
        tuple((item.contradiction_id, item.mission_id, item.challenger_task_id,
               item.statement) for item in value.contradictions),
        (value.judge_decision.judge_task_id, value.judge_decision.outcome.value,
         value.judge_decision.rationale, value.judge_decision.eligible_claim_ids,
         value.judge_decision.ineligible_claim_ids,
         value.judge_decision.eligible_evidence_ids,
         value.judge_decision.ineligible_evidence_ids,
         value.judge_decision.judge_evidence_ids),
        value.synthesis.synthesis_id, value.synthesis.status.value,
        tuple(claim.claim_id for claim in value.synthesis.eligible_claims),
        value.synthesis.included_evidence_ids, value.synthesis.excluded_evidence_ids,
        value.synthesis.contradiction_ids, value.source_result_task_ids,
        value.source_evidence_ids,
    )


class ResearchReplicationRuntime:
    """Classify independent attempts without executing or inferring research."""

    def __init__(self, evaluation, *, minimum_successful_replications=1):
        if type(evaluation) is not ResearchEvaluation:
            raise TypeError("evaluation must be a ResearchEvaluation")
        if evaluation.synthesis.status is not ResearchEvaluationStatus.ELIGIBLE or not evaluation.synthesis.eligible_claims:
            raise ResearchReplicationContractError("replication requires eligible synthesized claims")
        if (not isinstance(minimum_successful_replications, int)
                or isinstance(minimum_successful_replications, bool)
                or minimum_successful_replications < 1):
            raise ValueError("minimum_successful_replications must be a positive integer")
        self._evaluation = evaluation
        self._identity = _evaluation_identity(evaluation)
        self._minimum = minimum_successful_replications
        self._lock = RLock()

    def evaluate(self, attempts):
        items = tuple(attempts)
        if not all(type(item) is ReplicationAttempt for item in items):
            raise TypeError("attempts must contain ReplicationAttempt values")
        with self._lock:
            self._require_evaluation()
            result = self._evaluate(items)
            self._require_evaluation()
            return result

    def _require_evaluation(self):
        if _evaluation_identity(self._evaluation) != self._identity:
            raise ResearchReplicationContractError("authoritative evaluation mutated")

    def _evaluate(self, attempts):
        evaluation = self._evaluation
        eligible = {claim.claim_id: claim for claim in evaluation.synthesis.eligible_claims}
        if len({item.attempt_id for item in attempts}) != len(attempts):
            raise ResearchReplicationContractError("duplicate replication attempt identity")
        replicators = set()
        by_claim = {claim_id: [] for claim_id in eligible}
        replication_evidence = set()
        original_source = set(evaluation.source_evidence_ids)
        for item in attempts:
            claim = eligible.get(item.claim_id)
            if (item.mission_id != evaluation.mission_id
                    or item.evaluation_id != evaluation.evaluation_id or claim is None):
                raise ResearchReplicationContractError("foreign replication authority")
            if item.original_evidence_ids != claim.evidence_ids:
                raise ResearchReplicationContractError("replication source evidence mismatch")
            if item.replicator_id in evaluation.source_result_task_ids:
                raise ResearchReplicationContractError("source-role worker is not an independent replicator")
            key = (item.claim_id, item.replicator_id)
            if key in replicators:
                raise ResearchReplicationContractError("replicator duplicated for claim")
            replicators.add(key)
            if original_source & set(item.replication_evidence_ids):
                raise ResearchReplicationContractError("replication evidence reuses original evidence identity")
            if replication_evidence & set(item.replication_evidence_ids):
                raise ResearchReplicationContractError("replication evidence identity is ambiguous")
            replication_evidence.update(item.replication_evidence_ids)
            by_claim[item.claim_id].append(item)
        claim_results = []
        for claim in evaluation.synthesis.eligible_claims:
            claim_attempts = tuple(sorted(by_claim[claim.claim_id], key=lambda item: item.attempt_id))
            outcomes = [item.outcome for item in claim_attempts]
            successes = outcomes.count(ReplicationAttemptOutcome.SUCCESSFUL)
            if ReplicationAttemptOutcome.CONTRADICTORY in outcomes:
                status = ReplicationClaimStatus.CONTRADICTORY
            elif successes >= self._minimum:
                status = ReplicationClaimStatus.REPLICATED
            elif ReplicationAttemptOutcome.FAILED in outcomes:
                status = ReplicationClaimStatus.FAILED
            else:
                status = ReplicationClaimStatus.INSUFFICIENT
            evidence_ids = tuple(sorted(
                evidence_id for item in claim_attempts
                for evidence_id in item.replication_evidence_ids))
            claim_results.append(ReplicationClaimEvaluation(claim.claim_id, claim.statement,
                status, claim.evidence_ids, evidence_ids, claim_attempts))
        payload = {
            "evaluation_id": evaluation.evaluation_id,
            "claims": [(item.claim_id, item.status.value,
                        [(attempt.attempt_id, attempt.mission_id, attempt.evaluation_id,
                          attempt.claim_id, attempt.original_evidence_ids,
                          attempt.replicator_id, attempt.outcome.value,
                          attempt.replication_evidence_ids, attempt.summary,
                          attempt.observed_at.isoformat())
                         for attempt in item.attempts]) for item in claim_results],
        }
        return ResearchReplicationResult("replication-" + _digest(payload),
            evaluation.mission_id, evaluation.evaluation_id, tuple(claim_results),
            evaluation.source_evidence_ids, tuple(sorted(replication_evidence)))
