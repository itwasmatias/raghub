"""Deterministic fit assessment for Job Application Executor v0.1.

All scoring is rule-based and reproducible without a live LLM.
LLM-assisted assessment can be added later behind the DeterministicFitAssessor interface.

Key invariants:
- UNKNOWN profile facts cannot fill gaps.
- Portfolio evidence may reduce a gap but cannot eliminate or falsify it.
- Years-of-experience gap is reported, not suppressed.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from revenue_bridge.events import _canonical_bytes
from job_application.candidate import CandidateProfile, CandidateTruthClass
from job_application.opportunity import JobOpportunity, RemoteStatus


class FitRecommendation(str, Enum):
    STRONG_APPLY = "strong_apply"
    APPLY = "apply"
    REVIEW = "review"
    STRETCH = "stretch"
    SKIP = "skip"


class RequirementKind(str, Enum):
    HARD_REQUIREMENT = "hard_requirement"
    PREFERRED_REQUIREMENT = "preferred_requirement"
    LIKELY_FILTER = "likely_filter"
    POSSIBLE_STRETCH = "possible_stretch"
    UNKNOWN_REQUIREMENT = "unknown_requirement"


@dataclass(frozen=True, slots=True)
class FitDimension:
    name: str
    score: int  # 0-100
    evidence: tuple[str, ...]
    gaps: tuple[str, ...]
    label: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": self.score,
            "evidence": list(self.evidence),
            "gaps": list(self.gaps),
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FitDimension:
        return cls(
            name=data["name"],
            score=data["score"],
            evidence=tuple(data.get("evidence", [])),
            gaps=tuple(data.get("gaps", [])),
            label=data.get("label", ""),
        )


@dataclass(frozen=True, slots=True)
class FitAssessment:
    job_id: str
    profile_id: str
    technical_skill_fit: FitDimension
    agentic_ai_fit: FitDimension
    operations_domain_fit: FitDimension
    experience_level_fit: FitDimension
    education_fit: FitDimension
    location_fit: FitDimension
    compensation_fit: FitDimension
    portfolio_fit: FitDimension
    application_friction: FitDimension
    overall_score: int
    recommendation: FitRecommendation
    strong_matches: tuple[str, ...]
    partial_matches: tuple[str, ...]
    gaps: tuple[str, ...]
    hard_blockers: tuple[str, ...]
    unknowns: tuple[str, ...]
    evidence_to_emphasize: tuple[str, ...]
    fit_rationale: str

    @property
    def assessment_hash(self) -> str:
        payload = {
            "job_id": self.job_id,
            "profile_id": self.profile_id,
            "overall_score": self.overall_score,
            "recommendation": self.recommendation.value,
        }
        return hashlib.sha256(_canonical_bytes(payload)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "profile_id": self.profile_id,
            "technical_skill_fit": self.technical_skill_fit.to_dict(),
            "agentic_ai_fit": self.agentic_ai_fit.to_dict(),
            "operations_domain_fit": self.operations_domain_fit.to_dict(),
            "experience_level_fit": self.experience_level_fit.to_dict(),
            "education_fit": self.education_fit.to_dict(),
            "location_fit": self.location_fit.to_dict(),
            "compensation_fit": self.compensation_fit.to_dict(),
            "portfolio_fit": self.portfolio_fit.to_dict(),
            "application_friction": self.application_friction.to_dict(),
            "overall_score": self.overall_score,
            "recommendation": self.recommendation.value,
            "strong_matches": list(self.strong_matches),
            "partial_matches": list(self.partial_matches),
            "gaps": list(self.gaps),
            "hard_blockers": list(self.hard_blockers),
            "unknowns": list(self.unknowns),
            "evidence_to_emphasize": list(self.evidence_to_emphasize),
            "fit_rationale": self.fit_rationale,
            "assessment_hash": self.assessment_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FitAssessment:
        return cls(
            job_id=data["job_id"],
            profile_id=data["profile_id"],
            technical_skill_fit=FitDimension.from_dict(data["technical_skill_fit"]),
            agentic_ai_fit=FitDimension.from_dict(data["agentic_ai_fit"]),
            operations_domain_fit=FitDimension.from_dict(data["operations_domain_fit"]),
            experience_level_fit=FitDimension.from_dict(data["experience_level_fit"]),
            education_fit=FitDimension.from_dict(data["education_fit"]),
            location_fit=FitDimension.from_dict(data["location_fit"]),
            compensation_fit=FitDimension.from_dict(data["compensation_fit"]),
            portfolio_fit=FitDimension.from_dict(data["portfolio_fit"]),
            application_friction=FitDimension.from_dict(data["application_friction"]),
            overall_score=data["overall_score"],
            recommendation=FitRecommendation(data["recommendation"]),
            strong_matches=tuple(data.get("strong_matches", [])),
            partial_matches=tuple(data.get("partial_matches", [])),
            gaps=tuple(data.get("gaps", [])),
            hard_blockers=tuple(data.get("hard_blockers", [])),
            unknowns=tuple(data.get("unknowns", [])),
            evidence_to_emphasize=tuple(data.get("evidence_to_emphasize", [])),
            fit_rationale=data.get("fit_rationale", ""),
        )


class DeterministicFitAssessor:
    """Rule-based fit assessor. No live LLM required; results are reproducible."""

    _AGENTIC_SIGNALS = [
        "agent", "agentic", "llm", "rag", "openai", "anthropic",
        "ai system", "autonomous", "langchain", "vector",
    ]
    _OPS_SIGNALS = [
        "operations", "manufacturing", "inventory", "receiving",
        "warehouse", "supply chain", "logistics",
    ]
    _WEIGHTS = {
        "technical_skill_fit": 20,
        "agentic_ai_fit": 15,
        "operations_domain_fit": 10,
        "experience_level_fit": 20,
        "education_fit": 10,
        "location_fit": 5,
        "compensation_fit": 5,
        "portfolio_fit": 10,
        "application_friction": 5,
    }

    def assess(self, opportunity: JobOpportunity, profile: CandidateProfile) -> FitAssessment:
        dims = {
            "technical_skill_fit": self._assess_technical(opportunity, profile),
            "agentic_ai_fit": self._assess_agentic(opportunity, profile),
            "operations_domain_fit": self._assess_operations(opportunity, profile),
            "experience_level_fit": self._assess_experience(opportunity, profile),
            "education_fit": self._assess_education(opportunity, profile),
            "location_fit": self._assess_location(opportunity, profile),
            "compensation_fit": self._assess_compensation(opportunity, profile),
            "portfolio_fit": self._assess_portfolio(opportunity, profile),
            "application_friction": self._assess_friction(opportunity, profile),
        }

        total_weight = sum(self._WEIGHTS.values())
        overall = int(
            sum(dims[k].score * self._WEIGHTS[k] for k in dims) / total_weight
        )
        overall = max(0, min(100, overall))

        all_gaps = [g for d in dims.values() for g in d.gaps]
        # Hard blockers: explicit "required" gaps from high-weight dimensions
        hard_blockers = [
            g for g in (list(dims["technical_skill_fit"].gaps) + list(dims["experience_level_fit"].gaps))
            if "hard blocker" in g.lower()
        ]

        if hard_blockers:
            recommendation = FitRecommendation.SKIP
        elif overall >= 75:
            recommendation = FitRecommendation.STRONG_APPLY
        elif overall >= 60:
            recommendation = FitRecommendation.APPLY
        elif overall >= 45:
            recommendation = FitRecommendation.REVIEW
        elif overall >= 30:
            recommendation = FitRecommendation.STRETCH
        else:
            recommendation = FitRecommendation.SKIP

        strong_matches = [e for d in dims.values() for e in d.evidence if d.score >= 70]
        partial_matches = [e for d in dims.values() for e in d.evidence if 40 <= d.score < 70]
        unknowns = self._collect_unknowns(profile)
        evidence_emphasis = self._evidence_to_emphasize(opportunity, profile)

        rationale = (
            f"Overall score {overall}/100 → {recommendation.value}. "
            f"Technical: {dims['technical_skill_fit'].score}, "
            f"Agentic AI: {dims['agentic_ai_fit'].score}, "
            f"Experience: {dims['experience_level_fit'].score}, "
            f"Portfolio: {dims['portfolio_fit'].score}."
        )

        return FitAssessment(
            job_id=opportunity.job_id,
            profile_id=profile.profile_id,
            technical_skill_fit=dims["technical_skill_fit"],
            agentic_ai_fit=dims["agentic_ai_fit"],
            operations_domain_fit=dims["operations_domain_fit"],
            experience_level_fit=dims["experience_level_fit"],
            education_fit=dims["education_fit"],
            location_fit=dims["location_fit"],
            compensation_fit=dims["compensation_fit"],
            portfolio_fit=dims["portfolio_fit"],
            application_friction=dims["application_friction"],
            overall_score=overall,
            recommendation=recommendation,
            strong_matches=tuple(strong_matches[:10]),
            partial_matches=tuple(partial_matches[:10]),
            gaps=tuple(all_gaps[:10]),
            hard_blockers=tuple(hard_blockers[:5]),
            unknowns=tuple(unknowns[:10]),
            evidence_to_emphasize=tuple(evidence_emphasis[:5]),
            fit_rationale=rationale,
        )

    def _assess_technical(self, opp: JobOpportunity, profile: CandidateProfile) -> FitDimension:
        profile_lang = (profile.get_verified("primary_language") or "").lower()
        matched = [t for t in opp.technologies if t.lower() in profile_lang or profile_lang in t.lower()]
        python_bonus = 50 if ("python" in opp.technologies and "python" in profile_lang) else 0
        score = min(100, len(matched) * 15 + python_bonus)
        evidence = [f"Technology match: {t}" for t in matched]
        gaps = [f"Technology gap: {t}" for t in opp.technologies if t not in matched][:3]
        return FitDimension("technical_skill_fit", score, tuple(evidence), tuple(gaps), f"{score}/100 technical")

    def _assess_agentic(self, opp: JobOpportunity, profile: CandidateProfile) -> FitDimension:
        posting_lower = opp.posting_text.lower()
        matches = [s for s in self._AGENTIC_SIGNALS if s in posting_lower]
        portfolio = (profile.get_verified("portfolio_projects") or "").lower()
        has_agentic = any(s in portfolio for s in ["missionaryx", "agent", "agentic", "llm"])
        score = min(100, len(matches) * 12 + (40 if has_agentic else 0))
        evidence = [f"Agentic signal: {m}" for m in matches]
        if has_agentic:
            evidence.append("Candidate has agentic AI portfolio evidence")
        return FitDimension("agentic_ai_fit", score, tuple(evidence), (), f"{score}/100 agentic AI")

    def _assess_operations(self, opp: JobOpportunity, profile: CandidateProfile) -> FitDimension:
        posting_lower = opp.posting_text.lower()
        matches = [s for s in self._OPS_SIGNALS if s in posting_lower]
        score = min(100, len(matches) * 18)
        return FitDimension(
            "operations_domain_fit", score,
            tuple(f"Ops signal: {m}" for m in matches), (),
            f"{score}/100 operations domain",
        )

    def _assess_experience(self, opp: JobOpportunity, profile: CandidateProfile) -> FitDimension:
        years_fact = profile.get("years_professional_software_experience")
        if years_fact is None or years_fact.classification == CandidateTruthClass.UNKNOWN:
            return FitDimension(
                "experience_level_fit", 40, (),
                ("Years of professional experience unknown",),
                "40/100 unknown experience",
            )
        try:
            candidate_years = float(years_fact.value)
        except ValueError:
            return FitDimension(
                "experience_level_fit", 30, (),
                ("Cannot parse years of experience value",),
                "30/100",
            )

        year_matches = re.findall(r"(\d+)\+?\s*(?:years?|yrs?)", opp.posting_text.lower())
        required_years = max((int(m) for m in year_matches), default=0)

        if required_years == 0:
            return FitDimension(
                "experience_level_fit", 70,
                (f"Candidate: {candidate_years} years; no explicit requirement found",),
                (), "70/100 no explicit requirement",
            )
        if candidate_years >= required_years:
            return FitDimension(
                "experience_level_fit", 80,
                (f"Candidate: {candidate_years} years ≥ required {required_years}",),
                (), "80/100 meets requirement",
            )
        gap = required_years - candidate_years
        score = max(20, int(80 * candidate_years / required_years))
        gaps = (
            f"Experience gap: {gap:.0f} yr(s). Required {required_years}, have {candidate_years}. "
            "Portfolio evidence may reduce but cannot eliminate this gap.",
        )
        return FitDimension(
            "experience_level_fit", score,
            (f"Candidate: {candidate_years} years",),
            gaps,
            f"{score}/100 experience gap",
        )

    def _assess_education(self, opp: JobOpportunity, profile: CandidateProfile) -> FitDimension:
        degree_fact = profile.get("degree")
        if degree_fact is None or degree_fact.classification == CandidateTruthClass.UNKNOWN:
            if opp.education_requirements:
                return FitDimension(
                    "education_fit", 40, (),
                    ("Degree unknown; posting has education requirements",),
                    "40/100",
                )
            return FitDimension("education_fit", 65, (), ("Degree unknown; no explicit requirement",), "65/100")
        return FitDimension(
            "education_fit", 70,
            (f"Education: {degree_fact.value}",), (),
            "70/100",
        )

    def _assess_location(self, opp: JobOpportunity, profile: CandidateProfile) -> FitDimension:
        if opp.remote_status == RemoteStatus.REMOTE:
            return FitDimension("location_fit", 100, ("Fully remote position",), (), "100/100")
        city = profile.get_verified("location_city") or ""
        state = profile.get_verified("location_state") or ""
        if city.lower() in opp.location.lower() or state.lower() in opp.location.lower():
            return FitDimension(
                "location_fit", 85,
                (f"Candidate location {city}, {state} matches posting",), (),
                "85/100",
            )
        if opp.remote_status == RemoteStatus.HYBRID:
            return FitDimension(
                "location_fit", 55, (),
                ("Hybrid role; commute may be required",),
                "55/100",
            )
        return FitDimension(
            "location_fit", 35, (),
            ("On-site role; candidate location may not match",),
            "35/100",
        )

    def _assess_compensation(self, opp: JobOpportunity, profile: CandidateProfile) -> FitDimension:
        if opp.salary_min is None:
            return FitDimension("compensation_fit", 60, ("Compensation not disclosed",), (), "60/100")
        salary_fact = profile.get("salary_expectation")
        if salary_fact is None or salary_fact.classification == CandidateTruthClass.UNKNOWN:
            return FitDimension(
                "compensation_fit", 60,
                (f"Posting range: {opp.salary_text}",),
                ("Candidate salary expectation unknown",),
                "60/100",
            )
        return FitDimension(
            "compensation_fit", 70,
            (f"Posting range: {opp.salary_text}",), (),
            "70/100",
        )

    def _assess_portfolio(self, opp: JobOpportunity, profile: CandidateProfile) -> FitDimension:
        portfolio = profile.get_verified("portfolio_projects") or ""
        if not portfolio:
            return FitDimension("portfolio_fit", 25, (), ("No portfolio projects in profile",), "25/100")
        projects = [p.strip() for p in portfolio.split(",") if p.strip()]
        score = min(100, len(projects) * 30)
        return FitDimension(
            "portfolio_fit", score,
            tuple(f"Portfolio project: {p}" for p in projects), (),
            f"{score}/100 portfolio",
        )

    def _assess_friction(self, opp: JobOpportunity, profile: CandidateProfile) -> FitDimension:
        friction: list[str] = []
        work_auth_fact = profile.get("work_authorization")
        if (
            work_auth_fact is None
            or work_auth_fact.classification == CandidateTruthClass.UNKNOWN
        ) and opp.work_authorization_requirements:
            friction.append("Work authorization unknown but posting has explicit requirements")
        if opp.ats_provider:
            friction.append(f"ATS provider: {opp.ats_provider}")
        score = max(20, 100 - len(friction) * 25)
        return FitDimension("application_friction", score, (), tuple(friction), f"{score}/100 friction")

    def _collect_unknowns(self, profile: CandidateProfile) -> list[str]:
        return [
            f"Profile fact UNKNOWN: {f.key}"
            for f in profile.facts
            if f.classification == CandidateTruthClass.UNKNOWN
        ]

    def _evidence_to_emphasize(
        self, opp: JobOpportunity, profile: CandidateProfile
    ) -> list[str]:
        evidence: list[str] = []
        portfolio = profile.get_verified("portfolio_projects")
        if portfolio:
            evidence.append(f"Portfolio: {portfolio}")
        github = profile.get_verified("github_url")
        if github:
            evidence.append(f"GitHub: {github}")
        return evidence
