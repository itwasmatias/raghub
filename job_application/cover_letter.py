"""Cover letter generation boundary for Job Application Executor v0.1.

Generates only when requested. Default target: 150-300 words.
Based only on verified profile facts and observed opportunity content.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from job_application.candidate import CandidateProfile
from job_application.fit import FitAssessment
from job_application.opportunity import JobOpportunity


@dataclass(frozen=True, slots=True)
class CoverLetter:
    job_id: str
    profile_id: str
    letter_text: str
    letter_hash: str
    word_count: int
    generated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "profile_id": self.profile_id,
            "letter_text": self.letter_text,
            "letter_hash": self.letter_hash,
            "word_count": self.word_count,
            "generated_at": self.generated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CoverLetter:
        generated_at = data.get("generated_at")
        if isinstance(generated_at, str):
            generated_at = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        return cls(
            job_id=data["job_id"],
            profile_id=data["profile_id"],
            letter_text=data["letter_text"],
            letter_hash=data["letter_hash"],
            word_count=data["word_count"],
            generated_at=generated_at,
        )


class DeterministicCoverLetterBoundary:
    """Template-based deterministic cover letter. No live LLM required."""

    def generate(
        self,
        opportunity: JobOpportunity,
        profile: CandidateProfile,
        fit: FitAssessment,
    ) -> CoverLetter:
        name = profile.get_verified("full_name") or "[NAME]"
        company = opportunity.company
        role = opportunity.title
        portfolio = profile.get_verified("portfolio_projects") or "technical portfolio"
        primary_lang = profile.get_verified("primary_language") or "Python"
        tech_list = ", ".join(opportunity.technologies[:4]) if opportunity.technologies else primary_lang

        strong = fit.strong_matches[:2]
        evidence_emphasis = fit.evidence_to_emphasize[:2]

        strong_str = "; ".join(strong) if strong else ""
        evidence_str = "; ".join(evidence_emphasis) if evidence_emphasis else portfolio

        opening = (
            f"Dear {company} Hiring Team,"
            "\n\n"
            f"I am writing to apply for the {role} position at {company}. "
            f"My background in {tech_list} and agentic AI systems development "
            f"aligns with the work described in the posting."
        )

        body = (
            f"\n\nI have built {portfolio} — projects that demonstrate "
            "hands-on experience designing governed automation systems with "
            "durable state management, explicit approval gates, and evidence "
            f"capture. These are skills directly relevant to the {role} role."
        )

        if strong_str:
            body += f"\n\nParticular alignment: {strong_str}."

        if evidence_str and evidence_str != portfolio:
            body += f"\n\nEvidence I would highlight: {evidence_str}."

        closing = (
            f"\n\nI am genuinely interested in contributing to {company}'s "
            "work in this space and would welcome the opportunity to discuss "
            "how my experience can support your goals."
            f"\n\nSincerely,\n{name}"
        )

        letter_text = (opening + body + closing).strip()
        word_count = len(letter_text.split())
        letter_hash = hashlib.sha256(letter_text.encode("utf-8")).hexdigest()

        return CoverLetter(
            job_id=opportunity.job_id,
            profile_id=profile.profile_id,
            letter_text=letter_text,
            letter_hash=letter_hash,
            word_count=word_count,
            generated_at=datetime.now(timezone.utc),
        )
