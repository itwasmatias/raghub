"""Nontraditional match classifier for MissionaryX Job Scout v0.1.

Classifies each opportunity into a match category that accounts for
the candidate's nontraditional background (portfolio-based, AI-native workflow).

A role may rank highly on Rootstock similarity but have a professional
experience gap. This classifier names that pattern explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from job_scout.similarity import RootstockSimilarityScore
from job_scout.candidate_fit import ScoutFitScore


class MatchClassification(str, Enum):
    DIRECT_MATCH = "direct_match"
    NONTRADITIONAL_STRONG_MATCH = "nontraditional_strong_match"
    REALISTIC_STRETCH = "realistic_stretch"
    WEAK_MATCH = "weak_match"
    HARD_BLOCK = "hard_block"


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    classification: MatchClassification
    rationale: str
    primary_gap: str | None
    gap_classification: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification.value,
            "rationale": self.rationale,
            "primary_gap": self.primary_gap,
            "gap_classification": self.gap_classification,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClassificationResult:
        return cls(
            classification=MatchClassification(data["classification"]),
            rationale=data["rationale"],
            primary_gap=data.get("primary_gap"),
            gap_classification=data.get("gap_classification"),
        )


class NontraditionalMatchClassifier:
    """Classify match type accounting for nontraditional candidate background."""

    # Thresholds
    DIRECT_SIMILARITY_MIN = 80
    DIRECT_FIT_MIN = 75
    NONTRADITIONAL_SIMILARITY_MIN = 60
    NONTRADITIONAL_FIT_MIN = 55
    STRETCH_SIMILARITY_MIN = 50
    STRETCH_FIT_MIN = 40
    WEAK_SIMILARITY_MIN = 30

    def classify(
        self,
        similarity: RootstockSimilarityScore,
        candidate_fit: ScoutFitScore,
        hard_blockers: tuple[str, ...],
    ) -> ClassificationResult:
        if hard_blockers:
            return ClassificationResult(
                classification=MatchClassification.HARD_BLOCK,
                rationale=f"Hard blocker(s) detected: {'; '.join(hard_blockers[:2])}",
                primary_gap=hard_blockers[0] if hard_blockers else None,
                gap_classification="HARD_BLOCKER",
            )

        sim = similarity.total
        fit = candidate_fit.total
        exp_score = candidate_fit.experience_gap_penalty.score
        edu_score = candidate_fit.education_gap_penalty.score
        portfolio_score = candidate_fit.portfolio_evidence_fit.score

        # Direct match: role strongly resembles archetype AND candidate is competitive
        if sim >= self.DIRECT_SIMILARITY_MIN and fit >= self.DIRECT_FIT_MIN:
            return ClassificationResult(
                classification=MatchClassification.DIRECT_MATCH,
                rationale=(
                    f"Rootstock similarity {sim}/100 and candidate fit {fit}/100 "
                    "both meet direct-match thresholds."
                ),
                primary_gap=None,
                gap_classification=None,
            )

        # Nontraditional strong match: strong role resemblance, candidate gaps are soft
        # and portfolio evidence is present
        if (
            sim >= self.NONTRADITIONAL_SIMILARITY_MIN
            and fit >= self.NONTRADITIONAL_FIT_MIN
            and portfolio_score >= 50
        ):
            gaps = []
            gap_cls = None
            if exp_score < 70:
                gaps.append(self._exp_gap_text(candidate_fit))
                gap_cls = "POSSIBLE_STRETCH"
            if edu_score < 70:
                gaps.append("Education gap (degree not confirmed)")
                gap_cls = gap_cls or "SOFT_GAP"

            return ClassificationResult(
                classification=MatchClassification.NONTRADITIONAL_STRONG_MATCH,
                rationale=(
                    f"Rootstock similarity {sim}/100 strongly matches. "
                    f"Candidate fit {fit}/100. Portfolio evidence supports credibility. "
                    "Professional experience gap noted but not disqualifying."
                ),
                primary_gap=gaps[0] if gaps else None,
                gap_classification=gap_cls,
            )

        # Realistic stretch: role resembles archetype, candidate has meaningful gaps
        if sim >= self.STRETCH_SIMILARITY_MIN and fit >= self.STRETCH_FIT_MIN:
            primary_gap = None
            if exp_score < 60:
                primary_gap = self._exp_gap_text(candidate_fit)
            elif edu_score < 50:
                primary_gap = "Degree requirement is a significant gap"

            return ClassificationResult(
                classification=MatchClassification.REALISTIC_STRETCH,
                rationale=(
                    f"Rootstock similarity {sim}/100. Candidate fit {fit}/100. "
                    "Meaningful gaps exist but application is realistic."
                ),
                primary_gap=primary_gap,
                gap_classification="POSSIBLE_STRETCH" if primary_gap else None,
            )

        if sim >= self.WEAK_SIMILARITY_MIN:
            return ClassificationResult(
                classification=MatchClassification.WEAK_MATCH,
                rationale=f"Role similarity {sim}/100 is below strong threshold. Candidate fit {fit}/100.",
                primary_gap="Role does not closely resemble Rootstock archetype",
                gap_classification="SOFT_GAP",
            )

        return ClassificationResult(
            classification=MatchClassification.WEAK_MATCH,
            rationale=f"Rootstock similarity {sim}/100 too low for strong recommendation.",
            primary_gap="Role fundamentally differs from target archetype",
            gap_classification="SOFT_GAP",
        )

    def _exp_gap_text(self, fit: ScoutFitScore) -> str:
        gaps = fit.experience_gap_penalty.gaps
        if gaps:
            return gaps[0]
        return "Professional experience gap (years)"
