"""Deterministic evaluation and synthesis over authoritative research evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from threading import RLock

from research_mission.models import ResearchRole
from research_mission.result_runtime import (
    ResearchMissionResultCoordinator,
    ResearchMissionResultState,
)
from research_mission.results import (
    JudgeOutcome,
    ResearchTaskResultStatus,
    _evidence_identity,
    _result_identity,
)


class ResearchEvaluationError(ValueError):
    """Base fail-closed evaluation error."""


class ResearchEvaluationIncompleteError(ResearchEvaluationError):
    """Required role results or evidence are absent."""


class ResearchEvaluationContractError(ResearchEvaluationError):
    """Authoritative identities or challenge/judge contracts conflict."""


class ResearchEvaluationStatus(str, Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    CONTESTED = "contested"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class ResearchClaim:
    claim_id: str
    mission_id: str
    task_id: str
    statement: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResearchContradiction:
    contradiction_id: str
    mission_id: str
    challenger_task_id: str
    statement: str


@dataclass(frozen=True, slots=True)
class ResearchChallenge:
    challenger_task_id: str
    challenged_claims: tuple[str, ...]
    weaknesses: tuple[str, ...]
    contradictions: tuple[ResearchContradiction, ...]
    missing_evidence: tuple[str, ...]
    alternative_explanations: tuple[str, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class JudgeEligibilityDecision:
    judge_task_id: str
    outcome: JudgeOutcome
    rationale: str
    eligible_claim_ids: tuple[str, ...]
    ineligible_claim_ids: tuple[str, ...]
    eligible_evidence_ids: tuple[str, ...]
    ineligible_evidence_ids: tuple[str, ...]
    judge_evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    synthesis_id: str
    mission_id: str
    status: ResearchEvaluationStatus
    eligible_claims: tuple[ResearchClaim, ...]
    included_evidence_ids: tuple[str, ...]
    excluded_evidence_ids: tuple[str, ...]
    contradiction_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResearchEvaluation:
    evaluation_id: str
    mission_id: str
    researcher_task_ids: tuple[str, ...]
    challenger_task_id: str
    judge_task_id: str
    claims: tuple[ResearchClaim, ...]
    challenge: ResearchChallenge
    contradictions: tuple[ResearchContradiction, ...]
    judge_decision: JudgeEligibilityDecision
    synthesis: SynthesisResult
    source_result_task_ids: tuple[str, ...]
    source_evidence_ids: tuple[str, ...]


def _digest(payload) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _state_identity(state: ResearchMissionResultState):
    return (
        state.mission_id,
        tuple(_result_identity(result) for result in state.results),
        tuple(_evidence_identity(evidence) for evidence in state.evidence),
        tuple(task.task_id for task in state.pending_tasks),
        tuple(task.task_id for task in state.unsatisfied_tasks),
    )


class ResearchEvaluationRuntime:
    """Evaluate one authoritative result coordinator without executing research."""

    def __init__(self, results: ResearchMissionResultCoordinator) -> None:
        if type(results) is not ResearchMissionResultCoordinator:
            raise TypeError("results must be a ResearchMissionResultCoordinator")
        self._results = results
        self._mission_id = results.mission_id
        self._lock = RLock()

    @property
    def mission_id(self) -> str:
        return self._mission_id

    def evaluate(self) -> ResearchEvaluation:
        with self._lock:
            before = self._results.inspect()
            evaluation = self._evaluate_snapshot(before)
            after = self._results.inspect()
            if _state_identity(before) != _state_identity(after):
                raise ResearchEvaluationContractError(
                    "authoritative results changed during evaluation"
                )
            return evaluation

    def _evaluate_snapshot(self, state: ResearchMissionResultState) -> ResearchEvaluation:
        if state.mission_id != self.mission_id:
            raise ResearchEvaluationContractError("foreign mission result state")
        if not state.results_sufficient or state.pending_tasks or state.unsatisfied_tasks:
            raise ResearchEvaluationIncompleteError(
                "evaluation requires completed researcher, challenger, and judge results"
            )
        completed = tuple(
            result
            for result in state.results
            if result.status is ResearchTaskResultStatus.COMPLETED
        )
        researchers = tuple(
            result for result in completed if result.role is ResearchRole.RESEARCHER
        )
        challengers = tuple(
            result for result in completed if result.role is ResearchRole.CHALLENGER
        )
        judges = tuple(result for result in completed if result.role is ResearchRole.JUDGE)
        if not researchers or len(challengers) != 1 or len(judges) != 1:
            raise ResearchEvaluationIncompleteError(
                "evaluation requires researchers and exactly one challenger and judge"
            )
        challenger = challengers[0]
        judge = judges[0]
        assessment = challenger.challenger_assessment
        decision = judge.judge_decision
        if assessment is None or decision is None:
            raise ResearchEvaluationIncompleteError(
                "completed challenger and judge payloads are required"
            )

        evidence_by_task: dict[str, tuple] = {}
        for result in completed:
            evidence_by_task[result.task_id] = tuple(
                item for item in state.evidence if item.task_id == result.task_id
            )
            if not evidence_by_task[result.task_id]:
                raise ResearchEvaluationIncompleteError(
                    f"required role evidence is missing for task {result.task_id!r}"
                )

        claims = tuple(
            ResearchClaim(
                claim_id="claim-"
                + _digest(
                    {
                        "mission_id": self.mission_id,
                        "task_id": result.task_id,
                        "statement": result.summary,
                    }
                ),
                mission_id=self.mission_id,
                task_id=result.task_id,
                statement=result.summary,
                evidence_ids=tuple(
                    item.evidence_id for item in evidence_by_task[result.task_id]
                ),
            )
            for result in researchers
        )
        contradictions = tuple(
            ResearchContradiction(
                contradiction_id="contradiction-"
                + _digest(
                    {
                        "mission_id": self.mission_id,
                        "challenger_task_id": challenger.task_id,
                        "statement": statement,
                    }
                ),
                mission_id=self.mission_id,
                challenger_task_id=challenger.task_id,
                statement=statement,
            )
            for statement in assessment.contradictions
        )
        challenge = ResearchChallenge(
            challenger_task_id=challenger.task_id,
            challenged_claims=assessment.challenged_findings,
            weaknesses=assessment.weaknesses,
            contradictions=contradictions,
            missing_evidence=assessment.missing_evidence,
            alternative_explanations=assessment.alternative_explanations,
            evidence_ids=tuple(
                item.evidence_id for item in evidence_by_task[challenger.task_id]
            ),
        )
        challenge_statements = set(
            assessment.challenged_findings
            + assessment.weaknesses
            + assessment.contradictions
            + assessment.missing_evidence
            + assessment.alternative_explanations
        )
        judge_dispositions = set(
            decision.accepted_findings
            + decision.rejected_findings
            + decision.contested_findings
            + decision.unresolved_questions
        )
        disposition_groups = (
            set(decision.accepted_findings),
            set(decision.rejected_findings),
            set(decision.contested_findings),
            set(decision.unresolved_questions),
        )
        if any(
            left & right
            for index, left in enumerate(disposition_groups)
            for right in disposition_groups[index + 1 :]
        ):
            raise ResearchEvaluationContractError(
                "judge disposition categories must be mutually exclusive"
            )
        ignored = sorted(challenge_statements - judge_dispositions)
        if ignored:
            raise ResearchEvaluationContractError(
                "judge decision does not address every challenger statement: "
                + ", ".join(ignored)
            )

        accepted = set(decision.accepted_findings)
        eligible_claims = tuple(
            claim
            for claim in claims
            if decision.outcome is JudgeOutcome.ACCEPTED
            and claim.statement in accepted
            and claim.evidence_ids
        )
        eligible_claim_ids = tuple(claim.claim_id for claim in eligible_claims)
        ineligible_claim_ids = tuple(
            claim.claim_id for claim in claims if claim.claim_id not in eligible_claim_ids
        )
        researcher_evidence_ids = tuple(
            evidence_id for claim in claims for evidence_id in claim.evidence_ids
        )
        eligible_evidence_ids = tuple(
            evidence_id for claim in eligible_claims for evidence_id in claim.evidence_ids
        )
        ineligible_evidence_ids = tuple(
            evidence_id
            for evidence_id in researcher_evidence_ids
            if evidence_id not in eligible_evidence_ids
        )
        status = {
            JudgeOutcome.ACCEPTED: ResearchEvaluationStatus.ELIGIBLE,
            JudgeOutcome.REJECTED: ResearchEvaluationStatus.INELIGIBLE,
            JudgeOutcome.CONTESTED: ResearchEvaluationStatus.CONTESTED,
            JudgeOutcome.INCONCLUSIVE: ResearchEvaluationStatus.INCONCLUSIVE,
        }[decision.outcome]
        if status is ResearchEvaluationStatus.ELIGIBLE and not eligible_claims:
            status = ResearchEvaluationStatus.INELIGIBLE
        judge_eligibility = JudgeEligibilityDecision(
            judge_task_id=judge.task_id,
            outcome=decision.outcome,
            rationale=decision.rationale,
            eligible_claim_ids=eligible_claim_ids,
            ineligible_claim_ids=ineligible_claim_ids,
            eligible_evidence_ids=eligible_evidence_ids,
            ineligible_evidence_ids=ineligible_evidence_ids,
            judge_evidence_ids=tuple(
                item.evidence_id for item in evidence_by_task[judge.task_id]
            ),
        )
        identity_payload = {
            "mission_id": self.mission_id,
            "results": [_result_identity(result) for result in completed],
            "evidence": [_evidence_identity(item) for item in state.evidence],
            "eligible_claim_ids": eligible_claim_ids,
            "eligible_evidence_ids": eligible_evidence_ids,
            "contradiction_ids": [item.contradiction_id for item in contradictions],
        }
        evaluation_id = "evaluation-" + _digest(identity_payload)
        synthesis = SynthesisResult(
            synthesis_id="synthesis-"
            + _digest({"evaluation_id": evaluation_id, "status": status.value}),
            mission_id=self.mission_id,
            status=status,
            eligible_claims=eligible_claims,
            included_evidence_ids=eligible_evidence_ids,
            excluded_evidence_ids=ineligible_evidence_ids,
            contradiction_ids=tuple(item.contradiction_id for item in contradictions),
        )
        return ResearchEvaluation(
            evaluation_id=evaluation_id,
            mission_id=self.mission_id,
            researcher_task_ids=tuple(result.task_id for result in researchers),
            challenger_task_id=challenger.task_id,
            judge_task_id=judge.task_id,
            claims=claims,
            challenge=challenge,
            contradictions=contradictions,
            judge_decision=judge_eligibility,
            synthesis=synthesis,
            source_result_task_ids=tuple(result.task_id for result in completed),
            source_evidence_ids=tuple(item.evidence_id for item in state.evidence),
        )
