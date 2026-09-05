"""Application question classification for Job Application Executor v0.1.

Answer authority determines who controls the answer:
  AUTO_SAFE           — safe to answer automatically from static facts
  PROFILE_FACT        — answered from verified candidate profile
  JOB_SPECIFIC_GENERATED — requires job-specific generation, user must review
  USER_REQUIRED       — must be explicitly provided by creator
  PROHIBITED_TO_INFER — must never be inferred (demographics, legal attestations)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from job_application.candidate import CandidateProfile, CandidateTruthClass


class QuestionCategory(str, Enum):
    IDENTITY = "identity"
    CONTACT = "contact"
    LOCATION = "location"
    WORK_AUTHORIZATION = "work_authorization"
    SPONSORSHIP = "sponsorship"
    SALARY = "salary"
    EDUCATION = "education"
    YEARS_EXPERIENCE = "years_experience"
    TECHNOLOGY_EXPERIENCE = "technology_experience"
    PORTFOLIO = "portfolio"
    GITHUB = "github"
    LINKEDIN = "linkedin"
    WHY_COMPANY = "why_company"
    WHY_ROLE = "why_role"
    EMPLOYMENT_HISTORY = "employment_history"
    AVAILABILITY = "availability"
    DEMOGRAPHIC_EEO = "demographic_eeo"
    LEGAL_ATTESTATION = "legal_attestation"
    CUSTOM_TECHNICAL = "custom_technical"
    UNKNOWN = "unknown"


class AnswerAuthority(str, Enum):
    AUTO_SAFE = "auto_safe"
    PROFILE_FACT = "profile_fact"
    JOB_SPECIFIC_GENERATED = "job_specific_generated"
    USER_REQUIRED = "user_required"
    PROHIBITED_TO_INFER = "prohibited_to_infer"


_PROHIBITED_CATEGORIES = frozenset({
    QuestionCategory.DEMOGRAPHIC_EEO,
    QuestionCategory.LEGAL_ATTESTATION,
})

_USER_REQUIRED_CATEGORIES = frozenset({
    QuestionCategory.WORK_AUTHORIZATION,
    QuestionCategory.SPONSORSHIP,
    QuestionCategory.SALARY,
    QuestionCategory.AVAILABILITY,
})


@dataclass(frozen=True, slots=True)
class ApplicationQuestion:
    question_id: str
    question_text: str
    category: QuestionCategory
    answer_authority: AnswerAuthority
    required: bool
    answer: str | None
    notes: str

    def needs_user_input(self) -> bool:
        return self.answer_authority in (
            AnswerAuthority.USER_REQUIRED,
            AnswerAuthority.PROHIBITED_TO_INFER,
            AnswerAuthority.JOB_SPECIFIC_GENERATED,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "question_text": self.question_text,
            "category": self.category.value,
            "answer_authority": self.answer_authority.value,
            "required": self.required,
            "answer": self.answer,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApplicationQuestion:
        return cls(
            question_id=data["question_id"],
            question_text=data["question_text"],
            category=QuestionCategory(data["category"]),
            answer_authority=AnswerAuthority(data["answer_authority"]),
            required=data["required"],
            answer=data.get("answer"),
            notes=data.get("notes", ""),
        )


class QuestionClassifier:
    """Classify application questions and determine answer authority."""

    def classify(
        self,
        question_text: str,
        question_id: str,
        required: bool,
        profile: CandidateProfile,
    ) -> ApplicationQuestion:
        lower = question_text.lower()
        category = self._detect_category(lower)
        authority, answer, notes = self._determine_authority(category, lower, profile)
        return ApplicationQuestion(
            question_id=question_id,
            question_text=question_text,
            category=category,
            answer_authority=authority,
            required=required,
            answer=answer,
            notes=notes,
        )

    def classify_all(
        self,
        questions: list[tuple[str, str, bool]],
        profile: CandidateProfile,
    ) -> list[ApplicationQuestion]:
        """Classify a list of (question_id, question_text, required) tuples."""
        return [self.classify(text, qid, req, profile) for qid, text, req in questions]

    def _detect_category(self, lower: str) -> QuestionCategory:
        if any(w in lower for w in [
            "race", "ethnicity", "gender", "disability", "veteran",
            "eeo", "equal opportunity", "color", "national origin",
        ]):
            return QuestionCategory.DEMOGRAPHIC_EEO
        if any(w in lower for w in [
            "certify", "attest", "affirm i agree", "i acknowledge",
            "i certify", "by submitting", "i understand that",
        ]):
            return QuestionCategory.LEGAL_ATTESTATION
        if any(w in lower for w in ["sponsorship", "h-1b", "h1b", "opt", "cpt", "visa sponsor"]):
            return QuestionCategory.SPONSORSHIP
        if any(w in lower for w in [
            "authorized to work", "work authorization", "eligible to work",
            "work in the us", "work in the united states", "work permit",
        ]):
            return QuestionCategory.WORK_AUTHORIZATION
        if any(w in lower for w in ["salary", "compensation", "pay expectation", "desired pay", "desired salary"]):
            return QuestionCategory.SALARY
        if "linkedin" in lower:
            return QuestionCategory.LINKEDIN
        if any(w in lower for w in ["github", "code sample", "repository", "git"]):
            return QuestionCategory.GITHUB
        if any(w in lower for w in ["resume", "cv", "upload your"]):
            return QuestionCategory.PORTFOLIO
        if any(w in lower for w in ["phone", "telephone", "mobile number"]):
            return QuestionCategory.CONTACT
        if "email" in lower or "e-mail" in lower:
            return QuestionCategory.CONTACT
        if any(w in lower for w in ["first name", "last name", "full name", "legal name"]):
            return QuestionCategory.IDENTITY
        if any(w in lower for w in ["address", "city", "state", "zip code", "postal", "location"]):
            return QuestionCategory.LOCATION
        if any(w in lower for w in ["degree", "education", "college", "university", "bachelor", "master", "phd"]):
            return QuestionCategory.EDUCATION
        if any(w in lower for w in ["years of experience", "years experience", "how many years"]):
            return QuestionCategory.YEARS_EXPERIENCE
        if any(w in lower for w in ["why this company", "why us", "why are you interested", "why do you want to work"]):
            return QuestionCategory.WHY_COMPANY
        if any(w in lower for w in ["why this role", "why this position", "what draws you to"]):
            return QuestionCategory.WHY_ROLE
        if any(w in lower for w in ["previous employer", "employment history", "past job", "work history"]):
            return QuestionCategory.EMPLOYMENT_HISTORY
        if any(w in lower for w in ["start date", "available to start", "notice period", "when can you"]):
            return QuestionCategory.AVAILABILITY
        if any(w in lower for w in ["experience with", "proficient in", "knowledge of", "familiar with"]):
            return QuestionCategory.TECHNOLOGY_EXPERIENCE
        return QuestionCategory.UNKNOWN

    def _determine_authority(
        self,
        category: QuestionCategory,
        lower: str,
        profile: CandidateProfile,
    ) -> tuple[AnswerAuthority, str | None, str]:
        if category in _PROHIBITED_CATEGORIES:
            return (
                AnswerAuthority.PROHIBITED_TO_INFER,
                None,
                f"Category {category.value} must never be inferred — creator decides only",
            )

        if category in _USER_REQUIRED_CATEGORIES:
            return (
                AnswerAuthority.USER_REQUIRED,
                None,
                f"Category {category.value} requires explicit creator input",
            )

        if category == QuestionCategory.IDENTITY:
            val = profile.get_verified("full_name")
            if val:
                return AnswerAuthority.PROFILE_FACT, val, "Full name from verified profile"
            return AnswerAuthority.USER_REQUIRED, None, "Name not found in verified profile"

        if category == QuestionCategory.CONTACT:
            if any(w in lower for w in ["phone", "telephone", "mobile"]):
                val = profile.get_verified("phone")
                if val:
                    return AnswerAuthority.PROFILE_FACT, val, "Phone from verified profile"
                return AnswerAuthority.USER_REQUIRED, None, "Phone not in verified profile"
            val = profile.get_verified("email")
            if val:
                return AnswerAuthority.PROFILE_FACT, val, "Email from verified profile"
            return AnswerAuthority.USER_REQUIRED, None, "Email not in verified profile"

        if category == QuestionCategory.GITHUB:
            val = profile.get_verified("github_url")
            if val:
                return AnswerAuthority.PROFILE_FACT, val, "GitHub URL from verified profile"
            return AnswerAuthority.USER_REQUIRED, None, "GitHub URL not in verified profile"

        if category == QuestionCategory.LINKEDIN:
            val = profile.get_verified("linkedin_url")
            if val:
                return AnswerAuthority.PROFILE_FACT, val, "LinkedIn URL from verified profile"
            return AnswerAuthority.USER_REQUIRED, None, "LinkedIn URL not in verified profile"

        if category == QuestionCategory.LOCATION:
            city = profile.get_verified("location_city") or ""
            state = profile.get_verified("location_state") or ""
            combined = f"{city}, {state}".strip(", ")
            if combined:
                return AnswerAuthority.PROFILE_FACT, combined, "Location from verified profile"
            return AnswerAuthority.USER_REQUIRED, None, "Location not in verified profile"

        if category in (QuestionCategory.WHY_COMPANY, QuestionCategory.WHY_ROLE):
            return (
                AnswerAuthority.JOB_SPECIFIC_GENERATED,
                None,
                "Requires job-specific generation and creator review before use",
            )

        if category == QuestionCategory.EDUCATION:
            fact = profile.get("degree")
            if fact is None or fact.classification == CandidateTruthClass.UNKNOWN:
                return AnswerAuthority.USER_REQUIRED, None, "Education status is UNKNOWN in profile"
            return AnswerAuthority.PROFILE_FACT, fact.value, "Education from profile"

        if category == QuestionCategory.YEARS_EXPERIENCE:
            fact = profile.get("years_professional_software_experience")
            if fact is None or fact.classification == CandidateTruthClass.UNKNOWN:
                return AnswerAuthority.USER_REQUIRED, None, "Years experience is UNKNOWN"
            if fact.classification in (
                CandidateTruthClass.VERIFIED_FACT,
                CandidateTruthClass.USER_SUPPLIED_FACT,
            ):
                return AnswerAuthority.PROFILE_FACT, fact.value, "Years from profile (user-supplied)"
            return AnswerAuthority.USER_REQUIRED, None, "Years experience classification insufficient"

        if category == QuestionCategory.PORTFOLIO:
            return AnswerAuthority.USER_REQUIRED, None, "Resume upload requires creator file selection"

        # Default for CUSTOM_TECHNICAL, EMPLOYMENT_HISTORY, UNKNOWN
        return AnswerAuthority.USER_REQUIRED, None, f"Category {category.value} requires creator review"
