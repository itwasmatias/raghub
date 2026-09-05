"""Application packet for Job Application Executor v0.1.

The packet is the complete, hashable record of what will be submitted.
Material changes to any content component change the packet_hash,
which invalidates any prior Gate B (SUBMIT_APPLICATION) approval.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from revenue_bridge.events import _canonical_bytes
from job_application.candidate import CandidateProfile
from job_application.cover_letter import CoverLetter
from job_application.fit import FitAssessment
from job_application.opportunity import JobOpportunity
from job_application.questions import ApplicationQuestion, AnswerAuthority
from job_application.resume import TailoredResume


@dataclass(frozen=True, slots=True)
class ApplicationPacket:
    application_id: str
    job_id: str
    company: str
    title: str
    posting_hash: str
    candidate_profile_hash: str
    resume_hash: str
    cover_letter_hash: str
    fit_score: int
    fit_recommendation: str
    fit_rationale: str
    known_answers: tuple[ApplicationQuestion, ...]
    unknown_questions: tuple[ApplicationQuestion, ...]
    user_required_questions: tuple[ApplicationQuestion, ...]
    portfolio_links: tuple[str, ...]
    salary_strategy: str
    submission_checklist: tuple[str, ...]
    packet_hash: str
    created_at: datetime

    @staticmethod
    def compute_hash(
        job_id: str,
        posting_hash: str,
        candidate_profile_hash: str,
        resume_hash: str,
        cover_letter_hash: str,
        known_answers: tuple[ApplicationQuestion, ...],
    ) -> str:
        payload = {
            "job_id": job_id,
            "posting_hash": posting_hash,
            "candidate_profile_hash": candidate_profile_hash,
            "resume_hash": resume_hash,
            "cover_letter_hash": cover_letter_hash,
            "answers": sorted(
                [{"qid": q.question_id, "answer": q.answer} for q in known_answers],
                key=lambda x: x["qid"],
            ),
        }
        return hashlib.sha256(_canonical_bytes(payload)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "job_id": self.job_id,
            "company": self.company,
            "title": self.title,
            "posting_hash": self.posting_hash,
            "candidate_profile_hash": self.candidate_profile_hash,
            "resume_hash": self.resume_hash,
            "cover_letter_hash": self.cover_letter_hash,
            "fit_score": self.fit_score,
            "fit_recommendation": self.fit_recommendation,
            "fit_rationale": self.fit_rationale,
            "known_answers": [q.to_dict() for q in self.known_answers],
            "unknown_questions": [q.to_dict() for q in self.unknown_questions],
            "user_required_questions": [q.to_dict() for q in self.user_required_questions],
            "portfolio_links": list(self.portfolio_links),
            "salary_strategy": self.salary_strategy,
            "submission_checklist": list(self.submission_checklist),
            "packet_hash": self.packet_hash,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApplicationPacket:
        created_at = data.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return cls(
            application_id=data["application_id"],
            job_id=data["job_id"],
            company=data.get("company", ""),
            title=data.get("title", ""),
            posting_hash=data["posting_hash"],
            candidate_profile_hash=data["candidate_profile_hash"],
            resume_hash=data["resume_hash"],
            cover_letter_hash=data["cover_letter_hash"],
            fit_score=data["fit_score"],
            fit_recommendation=data["fit_recommendation"],
            fit_rationale=data.get("fit_rationale", ""),
            known_answers=tuple(ApplicationQuestion.from_dict(q) for q in data.get("known_answers", [])),
            unknown_questions=tuple(ApplicationQuestion.from_dict(q) for q in data.get("unknown_questions", [])),
            user_required_questions=tuple(
                ApplicationQuestion.from_dict(q) for q in data.get("user_required_questions", [])
            ),
            portfolio_links=tuple(data.get("portfolio_links", [])),
            salary_strategy=data.get("salary_strategy", ""),
            submission_checklist=tuple(data.get("submission_checklist", [])),
            packet_hash=data["packet_hash"],
            created_at=created_at,
        )


_CHECKLIST = (
    "Verify resume hash matches intended resume artifact",
    "Verify cover letter hash matches intended letter",
    "Confirm all USER_REQUIRED questions answered by creator",
    "Confirm PROHIBITED_TO_INFER questions left blank",
    "Confirm salary expectation not auto-submitted",
    "Confirm work authorization answer is explicit creator decision",
    "Gate A (BEGIN_APPLICATION) approval obtained and bound to this packet",
    "Gate B (SUBMIT_APPLICATION) approval required before final submit",
)


def build_packet(
    opportunity: JobOpportunity,
    profile: CandidateProfile,
    fit: FitAssessment,
    resume: TailoredResume,
    cover_letter: CoverLetter,
    all_questions: list[ApplicationQuestion],
    application_id: str | None = None,
) -> ApplicationPacket:
    aid = application_id or f"app_{uuid.uuid4().hex[:12]}"

    known = tuple(
        q for q in all_questions
        if q.answer_authority == AnswerAuthority.PROFILE_FACT and q.answer is not None
    )
    user_required = tuple(
        q for q in all_questions
        if q.answer_authority in (
            AnswerAuthority.USER_REQUIRED,
            AnswerAuthority.PROHIBITED_TO_INFER,
            AnswerAuthority.JOB_SPECIFIC_GENERATED,
        )
    )
    unknown = tuple(
        q for q in all_questions
        if q.answer is None and q.answer_authority != AnswerAuthority.PROFILE_FACT
    )

    portfolio_links: list[str] = []
    github = profile.get_verified("github_url")
    if github:
        portfolio_links.append(github)
    linkedin = profile.get_verified("linkedin_url")
    if linkedin:
        portfolio_links.append(linkedin)

    packet_hash = ApplicationPacket.compute_hash(
        job_id=opportunity.job_id,
        posting_hash=opportunity.posting_hash,
        candidate_profile_hash=profile.profile_hash,
        resume_hash=resume.resume_hash,
        cover_letter_hash=cover_letter.letter_hash,
        known_answers=known,
    )

    return ApplicationPacket(
        application_id=aid,
        job_id=opportunity.job_id,
        company=opportunity.company,
        title=opportunity.title,
        posting_hash=opportunity.posting_hash,
        candidate_profile_hash=profile.profile_hash,
        resume_hash=resume.resume_hash,
        cover_letter_hash=cover_letter.letter_hash,
        fit_score=fit.overall_score,
        fit_recommendation=fit.recommendation.value,
        fit_rationale=fit.fit_rationale,
        known_answers=known,
        unknown_questions=unknown,
        user_required_questions=user_required,
        portfolio_links=tuple(portfolio_links),
        salary_strategy="USER_REQUIRED — creator must set salary expectation explicitly before submission",
        submission_checklist=_CHECKLIST,
        packet_hash=packet_hash,
        created_at=datetime.now(timezone.utc),
    )
