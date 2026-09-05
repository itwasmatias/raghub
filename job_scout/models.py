"""Core models for MissionaryX Job Scout v0.1.

ScoredOpportunity is the primary output: a JobOpportunity with
Rootstock similarity, candidate fit, match classification, and scout state.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from revenue_bridge.events import _canonical_bytes
from job_application.opportunity import JobOpportunity
from job_scout.similarity import RootstockSimilarityScore
from job_scout.candidate_fit import ScoutFitScore
from job_scout.classifier import ClassificationResult, MatchClassification
from job_scout.requirements import ClassifiedRequirement


class ScoutState(str, Enum):
    DISCOVERED = "discovered"
    CAPTURED = "captured"
    SCORED = "scored"
    STRONG_APPLY = "strong_apply"
    APPLY = "apply"
    REVIEW = "review"
    STRETCH = "stretch"
    SKIP = "skip"
    STALE = "stale"
    DUPLICATE = "duplicate"
    HANDED_TO_EXECUTOR = "handed_to_executor"


class AvailabilityStatus(str, Enum):
    ACTIVE = "active"
    UNVERIFIED_CURRENT = "unverified_current"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ScoredOpportunity:
    opportunity: JobOpportunity
    rootstock_similarity: RootstockSimilarityScore
    candidate_fit: ScoutFitScore
    match_result: ClassificationResult
    requirement_classifications: tuple[ClassifiedRequirement, ...]
    scout_state: ScoutState
    discovered_at: datetime
    availability_status: AvailabilityStatus
    source_provenance: tuple[str, ...]  # all source names this appeared in
    posted_at: str | None = None
    last_verified_at: datetime | None = None
    executor_application_id: str | None = None  # set after handoff

    @property
    def job_id(self) -> str:
        return self.opportunity.job_id

    @property
    def company(self) -> str:
        return self.opportunity.company

    @property
    def title(self) -> str:
        return self.opportunity.title

    @property
    def match_classification(self) -> MatchClassification:
        return self.match_result.classification

    @property
    def hard_blockers(self) -> tuple[str, ...]:
        return tuple(r.text for r in self.requirement_classifications
                     if r.classification.value == "hard_blocker")

    @property
    def score_key(self) -> tuple[int, int]:
        """For ranking: higher Rootstock similarity first, then candidate fit."""
        return (self.rootstock_similarity.total, self.candidate_fit.total)

    @property
    def scored_opportunity_hash(self) -> str:
        payload = {
            "job_id": self.opportunity.job_id,
            "rootstock_total": self.rootstock_similarity.total,
            "candidate_fit_total": self.candidate_fit.total,
            "match_classification": self.match_result.classification.value,
        }
        return hashlib.sha256(_canonical_bytes(payload)).hexdigest()

    def with_state(self, new_state: ScoutState) -> ScoredOpportunity:
        return replace(self, scout_state=new_state)

    def with_executor_id(self, application_id: str) -> ScoredOpportunity:
        return replace(self, executor_application_id=application_id, scout_state=ScoutState.HANDED_TO_EXECUTOR)

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity": self.opportunity.to_dict(),
            "rootstock_similarity": self.rootstock_similarity.to_dict(),
            "candidate_fit": self.candidate_fit.to_dict(),
            "match_result": self.match_result.to_dict(),
            "requirement_classifications": [r.to_dict() for r in self.requirement_classifications],
            "scout_state": self.scout_state.value,
            "discovered_at": self.discovered_at.isoformat(),
            "availability_status": self.availability_status.value,
            "source_provenance": list(self.source_provenance),
            "posted_at": self.posted_at,
            "last_verified_at": self.last_verified_at.isoformat() if self.last_verified_at else None,
            "executor_application_id": self.executor_application_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScoredOpportunity:
        from job_application.opportunity import JobOpportunity
        from job_scout.similarity import RootstockSimilarityScore
        from job_scout.candidate_fit import ScoutFitScore
        from job_scout.classifier import ClassificationResult
        from job_scout.requirements import ClassifiedRequirement

        discovered_at = data["discovered_at"]
        if isinstance(discovered_at, str):
            discovered_at = datetime.fromisoformat(discovered_at.replace("Z", "+00:00"))

        last_verified_at = data.get("last_verified_at")
        if isinstance(last_verified_at, str):
            last_verified_at = datetime.fromisoformat(last_verified_at.replace("Z", "+00:00"))

        return cls(
            opportunity=JobOpportunity.from_dict(data["opportunity"]),
            rootstock_similarity=RootstockSimilarityScore.from_dict(data["rootstock_similarity"]),
            candidate_fit=ScoutFitScore.from_dict(data["candidate_fit"]),
            match_result=ClassificationResult.from_dict(data["match_result"]),
            requirement_classifications=tuple(
                ClassifiedRequirement.from_dict(r) for r in data.get("requirement_classifications", [])
            ),
            scout_state=ScoutState(data["scout_state"]),
            discovered_at=discovered_at,
            availability_status=AvailabilityStatus(data.get("availability_status", "unknown")),
            source_provenance=tuple(data.get("source_provenance", [])),
            posted_at=data.get("posted_at"),
            last_verified_at=last_verified_at,
            executor_application_id=data.get("executor_application_id"),
        )


def _scout_state_from_classification(result: ClassificationResult) -> ScoutState:
    """Map classification to initial scout state."""
    return {
        MatchClassification.DIRECT_MATCH: ScoutState.STRONG_APPLY,
        MatchClassification.NONTRADITIONAL_STRONG_MATCH: ScoutState.STRONG_APPLY,
        MatchClassification.REALISTIC_STRETCH: ScoutState.APPLY,
        MatchClassification.WEAK_MATCH: ScoutState.REVIEW,
        MatchClassification.HARD_BLOCK: ScoutState.SKIP,
    }.get(result.classification, ScoutState.REVIEW)
