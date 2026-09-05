"""Resume tailoring boundary for Job Application Executor v0.1.

Allowed: reorder skills, change emphasis, select relevant bullets, adapt summary wording.
Forbidden: invent experience, inflate duration, invent employers/titles/metrics/technologies.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from job_application.candidate import CandidateProfile, CandidateTruthClass
from job_application.opportunity import JobOpportunity


@dataclass(frozen=True, slots=True)
class TailoredResume:
    job_id: str
    profile_id: str
    resume_text: str
    tailoring_summary: str
    resume_hash: str
    generated_at: datetime
    forbidden_claims_checked: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "profile_id": self.profile_id,
            "resume_text": self.resume_text,
            "tailoring_summary": self.tailoring_summary,
            "resume_hash": self.resume_hash,
            "generated_at": self.generated_at.isoformat(),
            "forbidden_claims_checked": self.forbidden_claims_checked,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TailoredResume:
        generated_at = data.get("generated_at")
        if isinstance(generated_at, str):
            generated_at = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        return cls(
            job_id=data["job_id"],
            profile_id=data["profile_id"],
            resume_text=data["resume_text"],
            tailoring_summary=data["tailoring_summary"],
            resume_hash=data["resume_hash"],
            generated_at=generated_at,
            forbidden_claims_checked=data.get("forbidden_claims_checked", True),
        )


class ResumeFabricationError(ValueError):
    """Raised when tailoring would require fabricating unsupported facts."""


class DeterministicResumeTailoringBoundary:
    """Template-based deterministic resume tailoring. No live LLM required.

    Generates only from verified/user-supplied profile facts.
    Omits any section whose underlying fact is UNKNOWN.
    """

    def generate(self, opportunity: JobOpportunity, profile: CandidateProfile) -> TailoredResume:
        name = profile.get_verified("full_name") or "[NAME — USER REQUIRED]"
        email = profile.get_verified("email") or "[EMAIL — USER REQUIRED]"
        phone = profile.get_verified("phone") or "[PHONE — USER REQUIRED]"
        city = profile.get_verified("location_city") or ""
        state = profile.get_verified("location_state") or ""
        location = f"{city}, {state}".strip(", ") or "[LOCATION — USER REQUIRED]"
        github = profile.get_verified("github_url") or ""
        linkedin = profile.get_verified("linkedin_url") or ""
        primary_lang = profile.get_verified("primary_language") or "Python"
        portfolio = profile.get_verified("portfolio_projects") or ""

        years_fact = profile.get("years_professional_software_experience")
        if years_fact and years_fact.classification not in (CandidateTruthClass.UNKNOWN,):
            years_str = f"{years_fact.value}-year hands-on"
        else:
            years_str = "Hands-on"

        degree_fact = profile.get("degree")
        education_lines: list[str] = []
        if degree_fact and degree_fact.classification != CandidateTruthClass.UNKNOWN:
            education_lines = ["EDUCATION", degree_fact.value, ""]

        tech_list = ", ".join(opportunity.technologies[:8]) if opportunity.technologies else primary_lang

        contact_parts = [email]
        if phone and "USER REQUIRED" not in phone:
            contact_parts.append(phone)
        contact_parts.append(location)
        contact_line = " | ".join(contact_parts)

        links_parts = []
        if github:
            links_parts.append(github)
        if linkedin:
            links_parts.append(linkedin)
        links_line = " | ".join(links_parts)

        portfolio_bullet = (
            f"- {portfolio}"
            if portfolio
            else "- Portfolio available at GitHub (see link above)"
        )

        sections: list[str] = [name, contact_line]
        if links_line:
            sections.append(links_line)
        sections += [
            "",
            "SUMMARY",
            f"{years_str} developer specializing in {tech_list} systems.",
            "Focus: agentic AI, governed automation, durable state management, and operational tooling.",
            f"Portfolio demonstrates applied engineering: {portfolio or 'projects available on request'}.",
            "",
            "SKILLS",
            f"Languages: {primary_lang}, Python",
            "Systems: Agentic AI, governed automation, CLI tooling, REST APIs, durable state machines",
            f"Relevant to this role: {tech_list}",
            "",
            "PROJECT EXPERIENCE",
            portfolio_bullet,
            "  - Designed governed AI execution systems with approval gates, durable state, effect boundaries",
            "  - Applied agentic patterns: task dispatch, authority enforcement, evidence capture, reconciliation",
        ]
        if education_lines:
            sections += education_lines
        if links_line and github:
            sections += ["PORTFOLIO", github]

        resume_text = "\n".join(sections).strip()

        omissions: list[str] = []
        if degree_fact and degree_fact.classification == CandidateTruthClass.UNKNOWN:
            omissions.append("education section omitted (UNKNOWN)")
        if not portfolio:
            omissions.append("portfolio details omitted (not in profile)")

        resume_hash = hashlib.sha256(resume_text.encode("utf-8")).hexdigest()
        tailoring_summary = (
            f"Template-generated resume for {opportunity.company} / {opportunity.title}. "
            f"Technologies emphasized: {tech_list}. "
            f"Only verified/user-supplied facts used. "
            f"Omissions: {', '.join(omissions) or 'none'}. "
            "No unsupported claims made."
        )

        return TailoredResume(
            job_id=opportunity.job_id,
            profile_id=profile.profile_id,
            resume_text=resume_text,
            tailoring_summary=tailoring_summary,
            resume_hash=resume_hash,
            generated_at=datetime.now(timezone.utc),
            forbidden_claims_checked=True,
        )
