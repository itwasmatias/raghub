"""Requirement classifier for MissionaryX Job Scout v0.1.

Classifies job requirements into HARD_BLOCKER through UNKNOWN.
Hard blockers prevent STRONG_APPLY regardless of other scores.
Soft gaps are reported but do not automatically reject.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from job_application.opportunity import JobOpportunity


class RequirementClass(str, Enum):
    HARD_BLOCKER = "hard_blocker"
    HARD_REQUIREMENT = "hard_requirement"
    PREFERRED_REQUIREMENT = "preferred_requirement"
    LIKELY_FILTER = "likely_filter"
    SOFT_GAP = "soft_gap"
    POSSIBLE_STRETCH = "possible_stretch"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ClassifiedRequirement:
    text: str
    classification: RequirementClass
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "classification": self.classification.value,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClassifiedRequirement:
        return cls(
            text=data["text"],
            classification=RequirementClass(data["classification"]),
            reason=data["reason"],
        )


_HARD_BLOCKER_PATTERNS: list[tuple[str, str]] = [
    ("security clearance", "Active security clearance required — hard blocker"),
    ("active clearance", "Active security clearance required — hard blocker"),
    ("top secret", "Top secret clearance required — hard blocker"),
    ("secret clearance", "Security clearance required — hard blocker"),
    ("us citizen only", "US citizenship required — verify eligibility"),
    ("must be a us citizen", "US citizenship required — verify eligibility"),
    ("10+ years", "Requires 10+ years professional experience — likely unrealistic"),
    ("15+ years", "Requires 15+ years professional experience — hard blocker"),
    ("mandatory relocation", "Mandatory relocation required — likely hard blocker"),
    ("phd required", "PhD required — hard blocker"),
    ("phd is required", "PhD required — hard blocker"),
    ("vp of", "VP-level role — hard blocker"),
    ("chief ", "C-suite role — hard blocker"),
    ("director of", "Director-level role — possible hard blocker"),
]

_HARD_REQUIREMENT_PATTERNS: list[tuple[str, str]] = [
    ("degree is required", "Degree explicitly required — hard requirement"),
    ("bachelor's required", "Bachelor's degree required"),
    ("master's required", "Master's degree required"),
    ("required: bachelor", "Bachelor's degree required"),
    ("required: master", "Master's degree required"),
    ("must have degree", "Degree required — hard requirement"),
    ("must have a degree", "Degree required — hard requirement"),
    ("7+ years", "7+ years professional experience — significant gap"),
    ("8+ years", "8+ years professional experience — hard requirement"),
]

_PREFERRED_PATTERNS: list[tuple[str, str]] = [
    ("degree preferred", "Degree preferred — soft gap only"),
    ("bachelor preferred", "Bachelor's preferred — soft gap"),
    ("master preferred", "Master's preferred — soft gap"),
    ("preferred:", "Preferred requirement — not blocking"),
    ("nice to have", "Nice-to-have — not blocking"),
    ("bonus points", "Bonus — not blocking"),
    ("would be a plus", "Plus — not blocking"),
]

_LIKELY_FILTER_PATTERNS: list[tuple[str, str]] = [
    ("3+ years", "3+ years experience — common ATS filter; may screen out"),
    ("5+ years", "5+ years experience — common ATS filter"),
    ("2+ years", "2+ years experience — may be ATS filter"),
    ("production experience", "Production experience — likely filter"),
]


class RequirementClassifier:
    """Classify job requirements without LLM. Deterministic pattern matching."""

    def classify_all(self, opportunity: JobOpportunity) -> tuple[ClassifiedRequirement, ...]:
        text = opportunity.posting_text.lower()
        results: list[ClassifiedRequirement] = []

        for pattern, reason in _HARD_BLOCKER_PATTERNS:
            if pattern.lower() in text:
                results.append(ClassifiedRequirement(pattern, RequirementClass.HARD_BLOCKER, reason))

        for pattern, reason in _HARD_REQUIREMENT_PATTERNS:
            if pattern.lower() in text:
                if not any(r.text == pattern for r in results):
                    results.append(ClassifiedRequirement(pattern, RequirementClass.HARD_REQUIREMENT, reason))

        for pattern, reason in _PREFERRED_PATTERNS:
            if pattern.lower() in text:
                results.append(ClassifiedRequirement(pattern, RequirementClass.PREFERRED_REQUIREMENT, reason))

        for pattern, reason in _LIKELY_FILTER_PATTERNS:
            if pattern.lower() in text:
                if not any(r.text == pattern for r in results):
                    results.append(ClassifiedRequirement(pattern, RequirementClass.LIKELY_FILTER, reason))

        # Classify listed requirements
        for req in opportunity.requirements:
            if not any(r.text == req for r in results):
                cls = self._classify_single(req.lower())
                results.append(ClassifiedRequirement(req, cls, self._reason_for(cls)))

        return tuple(results)

    def hard_blockers(self, classified: tuple[ClassifiedRequirement, ...]) -> tuple[str, ...]:
        return tuple(r.text for r in classified if r.classification == RequirementClass.HARD_BLOCKER)

    def _classify_single(self, req: str) -> RequirementClass:
        for pattern, _ in _HARD_BLOCKER_PATTERNS:
            if pattern.lower() in req:
                return RequirementClass.HARD_BLOCKER
        for pattern, _ in _HARD_REQUIREMENT_PATTERNS:
            if pattern.lower() in req:
                return RequirementClass.HARD_REQUIREMENT
        for pattern, _ in _PREFERRED_PATTERNS:
            if pattern.lower() in req:
                return RequirementClass.PREFERRED_REQUIREMENT
        for pattern, _ in _LIKELY_FILTER_PATTERNS:
            if pattern.lower() in req:
                return RequirementClass.LIKELY_FILTER
        return RequirementClass.SOFT_GAP

    def _reason_for(self, cls: RequirementClass) -> str:
        return {
            RequirementClass.HARD_BLOCKER: "Detected as hard blocker",
            RequirementClass.HARD_REQUIREMENT: "Detected as hard requirement",
            RequirementClass.PREFERRED_REQUIREMENT: "Detected as preferred (not blocking)",
            RequirementClass.LIKELY_FILTER: "Likely ATS filter",
            RequirementClass.SOFT_GAP: "Soft gap — not automatically blocking",
            RequirementClass.POSSIBLE_STRETCH: "Possible stretch",
            RequirementClass.UNKNOWN: "Unknown classification",
        }.get(cls, "Unknown")
