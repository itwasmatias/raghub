"""Scout-specific candidate fit assessor for MissionaryX Job Scout v0.1.

Scores how competitive the candidate is for a specific role.
Separate from Rootstock similarity — a role can resemble Rootstock strongly
but the candidate may face gaps (experience, education, etc.).

All scoring is deterministic. No LLM required.
Gaps are reported, not suppressed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from job_application.candidate import CandidateProfile, CandidateTruthClass
from job_application.opportunity import JobOpportunity, RemoteStatus


_MISSIONARYX_EVIDENCE = [
    "missionaryx",
    "agent orchestration",
    "worker relay",
    "governed effects",
    "approval gates",
    "effect reconciliation",
    "indeterminate-outcome",
    "deterministic verification",
    "durable mission state",
    "agent failure recovery",
    "job application executor",
    "giveaway assistant",
    "git worktree isolation",
    "ai-assisted implementation",
    "multi-agent workflow",
    "multi-provider ai",
]

_PORTFOLIO_EVIDENCE_MAP: dict[str, list[str]] = {
    "multi-agent coding workflows": ["missionaryx", "multi-agent"],
    "worker relay": ["worker relay"],
    "governed external effects": ["governed effects", "approval gates"],
    "deterministic verification": ["deterministic verification", "effect reconciliation"],
    "durable mission state": ["durable mission state"],
    "agent failure recovery": ["agent failure recovery", "indeterminate"],
    "git worktree isolation": ["git worktree isolation", "worktree"],
    "ai-assisted implementation": ["ai-assisted implementation"],
    "Job Application Executor": ["job application executor"],
    "Giveaway Assistant": ["giveaway assistant"],
}


@dataclass(frozen=True, slots=True)
class ScoutFitDimension:
    name: str
    score: int  # 0-100
    evidence: tuple[str, ...]
    gaps: tuple[str, ...]
    is_hard_blocker: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": self.score,
            "evidence": list(self.evidence),
            "gaps": list(self.gaps),
            "is_hard_blocker": self.is_hard_blocker,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScoutFitDimension:
        return cls(
            name=data["name"],
            score=data["score"],
            evidence=tuple(data.get("evidence", [])),
            gaps=tuple(data.get("gaps", [])),
            is_hard_blocker=data.get("is_hard_blocker", False),
        )


_CANDIDATE_FIT_WEIGHTS: dict[str, int] = {
    "agent_workflow_fit": 15,
    "building_with_ai_fit": 12,
    "agent_management_fit": 10,
    "creative_problem_solving_fit": 5,
    "technical_operations_fit": 5,
    "verification_evaluation_fit": 8,
    "portfolio_evidence_fit": 12,
    "python_api_fit": 8,
    "linux_git_fit": 5,
    "experience_gap_penalty": 8,
    "education_gap_penalty": 5,
    "location_fit": 4,
    "remote_fit": 3,
    "application_friction": 0,  # informational only
}

assert sum(v for k, v in _CANDIDATE_FIT_WEIGHTS.items() if not k.endswith("_penalty")) + \
       sum(v for k, v in _CANDIDATE_FIT_WEIGHTS.items() if k.endswith("_penalty")) == 100


@dataclass(frozen=True, slots=True)
class ScoutFitScore:
    agent_workflow_fit: ScoutFitDimension
    building_with_ai_fit: ScoutFitDimension
    agent_management_fit: ScoutFitDimension
    creative_problem_solving_fit: ScoutFitDimension
    technical_operations_fit: ScoutFitDimension
    verification_evaluation_fit: ScoutFitDimension
    portfolio_evidence_fit: ScoutFitDimension
    python_api_fit: ScoutFitDimension
    linux_git_fit: ScoutFitDimension
    experience_gap_penalty: ScoutFitDimension
    education_gap_penalty: ScoutFitDimension
    location_fit: ScoutFitDimension
    remote_fit: ScoutFitDimension
    application_friction: ScoutFitDimension
    best_evidence_to_show: tuple[str, ...]
    total: int  # 0-100

    def hard_blockers(self) -> tuple[ScoutFitDimension, ...]:
        return tuple(d for d in self._all_dims() if d.is_hard_blocker)

    def _all_dims(self) -> tuple[ScoutFitDimension, ...]:
        return (
            self.agent_workflow_fit,
            self.building_with_ai_fit,
            self.agent_management_fit,
            self.creative_problem_solving_fit,
            self.technical_operations_fit,
            self.verification_evaluation_fit,
            self.portfolio_evidence_fit,
            self.python_api_fit,
            self.linux_git_fit,
            self.experience_gap_penalty,
            self.education_gap_penalty,
            self.location_fit,
            self.remote_fit,
            self.application_friction,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_workflow_fit": self.agent_workflow_fit.to_dict(),
            "building_with_ai_fit": self.building_with_ai_fit.to_dict(),
            "agent_management_fit": self.agent_management_fit.to_dict(),
            "creative_problem_solving_fit": self.creative_problem_solving_fit.to_dict(),
            "technical_operations_fit": self.technical_operations_fit.to_dict(),
            "verification_evaluation_fit": self.verification_evaluation_fit.to_dict(),
            "portfolio_evidence_fit": self.portfolio_evidence_fit.to_dict(),
            "python_api_fit": self.python_api_fit.to_dict(),
            "linux_git_fit": self.linux_git_fit.to_dict(),
            "experience_gap_penalty": self.experience_gap_penalty.to_dict(),
            "education_gap_penalty": self.education_gap_penalty.to_dict(),
            "location_fit": self.location_fit.to_dict(),
            "remote_fit": self.remote_fit.to_dict(),
            "application_friction": self.application_friction.to_dict(),
            "best_evidence_to_show": list(self.best_evidence_to_show),
            "total": self.total,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScoutFitScore:
        return cls(
            agent_workflow_fit=ScoutFitDimension.from_dict(data["agent_workflow_fit"]),
            building_with_ai_fit=ScoutFitDimension.from_dict(data["building_with_ai_fit"]),
            agent_management_fit=ScoutFitDimension.from_dict(data["agent_management_fit"]),
            creative_problem_solving_fit=ScoutFitDimension.from_dict(data["creative_problem_solving_fit"]),
            technical_operations_fit=ScoutFitDimension.from_dict(data["technical_operations_fit"]),
            verification_evaluation_fit=ScoutFitDimension.from_dict(data["verification_evaluation_fit"]),
            portfolio_evidence_fit=ScoutFitDimension.from_dict(data["portfolio_evidence_fit"]),
            python_api_fit=ScoutFitDimension.from_dict(data["python_api_fit"]),
            linux_git_fit=ScoutFitDimension.from_dict(data["linux_git_fit"]),
            experience_gap_penalty=ScoutFitDimension.from_dict(data["experience_gap_penalty"]),
            education_gap_penalty=ScoutFitDimension.from_dict(data["education_gap_penalty"]),
            location_fit=ScoutFitDimension.from_dict(data["location_fit"]),
            remote_fit=ScoutFitDimension.from_dict(data["remote_fit"]),
            application_friction=ScoutFitDimension.from_dict(data["application_friction"]),
            best_evidence_to_show=tuple(data.get("best_evidence_to_show", [])),
            total=data["total"],
        )


class ScoutFitAssessor:
    """Deterministic candidate fit assessor for Scout scoring. No LLM required."""

    def assess(self, opportunity: JobOpportunity, profile: CandidateProfile) -> ScoutFitScore:
        text = opportunity.posting_text.lower()
        portfolio = (profile.get_verified("portfolio_projects") or "").lower()

        dims = {
            "agent_workflow_fit": self._assess_agent_workflow(text, portfolio),
            "building_with_ai_fit": self._assess_building_with_ai(text, portfolio),
            "agent_management_fit": self._assess_agent_management(text, portfolio),
            "creative_problem_solving_fit": self._assess_creative(text),
            "technical_operations_fit": self._assess_tech_ops(text),
            "verification_evaluation_fit": self._assess_verification(text, portfolio),
            "portfolio_evidence_fit": self._assess_portfolio_evidence(text, portfolio, profile),
            "python_api_fit": self._assess_python_api(text, profile),
            "linux_git_fit": self._assess_linux_git(text, profile),
            "experience_gap_penalty": self._assess_experience_gap(opportunity, profile),
            "education_gap_penalty": self._assess_education_gap(opportunity, profile),
            "location_fit": self._assess_location(opportunity, profile),
            "remote_fit": self._assess_remote(opportunity),
            "application_friction": self._assess_friction(opportunity, profile),
        }

        # Weighted score excluding penalty dimensions (which reduce the total)
        positive_dims = [k for k in _CANDIDATE_FIT_WEIGHTS if not k.endswith("_penalty") and k != "application_friction"]
        penalty_dims = [k for k in _CANDIDATE_FIT_WEIGHTS if k.endswith("_penalty")]

        positive_total = sum(
            dims[k].score * _CANDIDATE_FIT_WEIGHTS[k]
            for k in positive_dims
        )
        positive_weight_sum = sum(_CANDIDATE_FIT_WEIGHTS[k] for k in positive_dims)

        # Penalty reduces score: 100% gap penalty removes its weight-fraction
        penalty_reduction = sum(
            (100 - dims[k].score) * _CANDIDATE_FIT_WEIGHTS[k]
            for k in penalty_dims
        )
        penalty_weight_sum = sum(_CANDIDATE_FIT_WEIGHTS[k] for k in penalty_dims)

        base = int(positive_total / positive_weight_sum) if positive_weight_sum else 0
        penalty = int(penalty_reduction / penalty_weight_sum) if penalty_weight_sum else 0

        # Penalty can reduce base by up to penalty_weight_sum percentage points
        total = max(0, min(100, base - int(penalty * penalty_weight_sum / 100)))

        best_evidence = self._best_evidence(text, portfolio, profile)

        return ScoutFitScore(
            agent_workflow_fit=dims["agent_workflow_fit"],
            building_with_ai_fit=dims["building_with_ai_fit"],
            agent_management_fit=dims["agent_management_fit"],
            creative_problem_solving_fit=dims["creative_problem_solving_fit"],
            technical_operations_fit=dims["technical_operations_fit"],
            verification_evaluation_fit=dims["verification_evaluation_fit"],
            portfolio_evidence_fit=dims["portfolio_evidence_fit"],
            python_api_fit=dims["python_api_fit"],
            linux_git_fit=dims["linux_git_fit"],
            experience_gap_penalty=dims["experience_gap_penalty"],
            education_gap_penalty=dims["education_gap_penalty"],
            location_fit=dims["location_fit"],
            remote_fit=dims["remote_fit"],
            application_friction=dims["application_friction"],
            best_evidence_to_show=tuple(best_evidence[:6]),
            total=total,
        )

    def _assess_agent_workflow(self, text: str, portfolio: str) -> ScoutFitDimension:
        signals = ["agent", "workflow", "orchestration", "agentic", "pipeline", "automation"]
        matches = [s for s in signals if s in text]
        has_portfolio = any(e in portfolio for e in ["missionaryx", "agent", "agentic"])
        score = min(100, len(matches) * 15 + (30 if has_portfolio else 0))
        evidence = [f"role mentions: {m}" for m in matches]
        if has_portfolio:
            evidence.append("candidate has agent workflow portfolio evidence (MissionaryX)")
        return ScoutFitDimension("agent_workflow_fit", score, tuple(evidence), ())

    def _assess_building_with_ai(self, text: str, portfolio: str) -> ScoutFitDimension:
        signals = ["claude", "chatgpt", "openai", "codex", "llm", "language model", "ai tools", "ai coding"]
        matches = [s for s in signals if s in text]
        has_portfolio = any(e in portfolio for e in ["missionaryx", "ai", "llm"])
        score = min(100, len(matches) * 14 + (28 if has_portfolio else 0))
        evidence = [f"role uses: {m}" for m in matches]
        if has_portfolio:
            evidence.append("candidate builds with AI tools (MissionaryX demonstrates this)")
        return ScoutFitDimension("building_with_ai_fit", score, tuple(evidence), ())

    def _assess_agent_management(self, text: str, portfolio: str) -> ScoutFitDimension:
        signals = ["multi-agent", "agent coordination", "agent management", "direct agent", "delegate"]
        matches = [s for s in signals if s in text]
        has_portfolio = "missionaryx" in portfolio
        score = min(100, len(matches) * 20 + (40 if has_portfolio else 0))
        evidence = [f"multi-agent signal: {m}" for m in matches]
        if has_portfolio:
            evidence.append("MissionaryX demonstrates multi-agent coordination")
        return ScoutFitDimension("agent_management_fit", score, tuple(evidence), ())

    def _assess_creative(self, text: str) -> ScoutFitDimension:
        signals = ["creative", "problem solving", "innovative", "novel", "experimental", "research"]
        matches = [s for s in signals if s in text]
        score = min(100, 50 + len(matches) * 12)
        return ScoutFitDimension("creative_problem_solving_fit", score, tuple(f"signal: {m}" for m in matches), ())

    def _assess_tech_ops(self, text: str) -> ScoutFitDimension:
        signals = ["operations", "ops", "infrastructure", "deploy", "monitoring", "production"]
        matches = [s for s in signals if s in text]
        score = min(100, len(matches) * 18)
        return ScoutFitDimension("technical_operations_fit", score, tuple(f"ops signal: {m}" for m in matches), ())

    def _assess_verification(self, text: str, portfolio: str) -> ScoutFitDimension:
        signals = ["verification", "evaluation", "evals", "testing", "quality", "reliability", "benchmark"]
        matches = [s for s in signals if s in text]
        has_portfolio = any(e in portfolio for e in ["missionaryx", "verification"])
        score = min(100, len(matches) * 14 + (28 if has_portfolio else 0))
        evidence = [f"eval signal: {m}" for m in matches]
        if has_portfolio:
            evidence.append("MissionaryX includes deterministic verification systems")
        return ScoutFitDimension("verification_evaluation_fit", score, tuple(evidence), ())

    def _assess_portfolio_evidence(
        self, text: str, portfolio: str, profile: CandidateProfile
    ) -> ScoutFitDimension:
        portfolio_friendly = any(
            kw in text for kw in ["portfolio", "github", "open source", "project", "demo", "show"]
        )
        has_portfolio = bool(portfolio)
        github = profile.get_verified("github_url")

        evidence: list[str] = []
        if portfolio:
            evidence.append(f"Candidate portfolio: {portfolio[:60]}")
        if github:
            evidence.append(f"GitHub: {github}")
        if portfolio_friendly:
            evidence.append("Employer values portfolio/projects (detected in posting)")

        score = 0
        if has_portfolio:
            score += 50
        if github:
            score += 20
        if portfolio_friendly:
            score += 30
        score = min(100, score)
        return ScoutFitDimension("portfolio_evidence_fit", score, tuple(evidence), ())

    def _assess_python_api(self, text: str, profile: CandidateProfile) -> ScoutFitDimension:
        lang = (profile.get_verified("primary_language") or "").lower()
        python_in_posting = "python" in text
        candidate_python = "python" in lang
        api_signal = any(s in text for s in ["api", "sdk", "integration", "rest"])

        evidence: list[str] = []
        score = 30
        if python_in_posting and candidate_python:
            score += 50
            evidence.append("Python required and candidate primary language")
        elif candidate_python:
            score += 20
            evidence.append("Candidate knows Python")
        if api_signal:
            score += 20
            evidence.append("API/SDK integration mentioned in posting")
        score = min(100, score)
        return ScoutFitDimension("python_api_fit", score, tuple(evidence), ())

    def _assess_linux_git(self, text: str, profile: CandidateProfile) -> ScoutFitDimension:
        signals = ["linux", "git", "unix", "bash", "cli", "terminal", "command line"]
        matches = [s for s in signals if s in text]
        score = min(100, 40 + len(matches) * 15)
        return ScoutFitDimension("linux_git_fit", score, tuple(f"env signal: {m}" for m in matches), ())

    def _assess_experience_gap(self, opp: JobOpportunity, profile: CandidateProfile) -> ScoutFitDimension:
        years_fact = profile.get("years_professional_software_experience")
        if years_fact is None or years_fact.classification == CandidateTruthClass.UNKNOWN:
            return ScoutFitDimension(
                "experience_gap_penalty", 40, (),
                ("Candidate professional experience years unknown",),
            )
        try:
            candidate_years = float(years_fact.value)
        except ValueError:
            return ScoutFitDimension(
                "experience_gap_penalty", 30, (),
                ("Cannot parse candidate years of experience",),
            )

        year_matches = re.findall(r"(\d+)\+?\s*(?:years?|yrs?)", opp.posting_text.lower())
        required_years = max((int(m) for m in year_matches), default=0)

        if required_years == 0:
            return ScoutFitDimension(
                "experience_gap_penalty", 100,
                (f"No explicit experience requirement; candidate has {candidate_years} years",),
                (),
            )
        if candidate_years >= required_years:
            return ScoutFitDimension(
                "experience_gap_penalty", 100,
                (f"Candidate {candidate_years} yr(s) meets required {required_years} yr(s)",),
                (),
            )
        gap = required_years - candidate_years
        ratio = candidate_years / required_years
        score = int(100 * ratio)
        gaps = (
            f"Experience gap: need {required_years} yr(s), have {candidate_years} yr(s) "
            f"({gap:.0f} yr(s) short). Portfolio evidence may offset but cannot close gap.",
        )
        return ScoutFitDimension(
            "experience_gap_penalty", score,
            (f"Candidate: {candidate_years} yr(s) professional experience",),
            gaps,
        )

    def _assess_education_gap(self, opp: JobOpportunity, profile: CandidateProfile) -> ScoutFitDimension:
        degree_fact = profile.get("degree")
        degree_unknown = (
            degree_fact is None or degree_fact.classification == CandidateTruthClass.UNKNOWN
        )
        has_education_req = bool(opp.education_requirements)
        posting_lower = opp.posting_text.lower()
        mandatory = any(
            phrase in posting_lower
            for phrase in [
                "must have a degree", "degree required", "bachelor's required",
                "master's required", "bachelor required", "degree is required",
                "required: bachelor", "required: master",
            ]
        )
        preferred_only = any(
            phrase in posting_lower
            for phrase in ["degree preferred", "bachelor preferred", "preferred degree", "nice to have degree"]
        )

        if mandatory and degree_unknown:
            return ScoutFitDimension(
                "education_gap_penalty", 30,
                (),
                ("Degree appears mandatory and candidate degree status is UNKNOWN — potential hard blocker",),
                is_hard_blocker=True,
            )
        if has_education_req and degree_unknown:
            return ScoutFitDimension(
                "education_gap_penalty", 60, (),
                ("Education requirement present; candidate degree UNKNOWN (soft gap)",),
            )
        if preferred_only:
            return ScoutFitDimension(
                "education_gap_penalty", 85,
                ("Degree is preferred, not required",), (),
            )
        return ScoutFitDimension("education_gap_penalty", 100, ("No mandatory degree requirement detected",), ())

    def _assess_location(self, opp: JobOpportunity, profile: CandidateProfile) -> ScoutFitDimension:
        if opp.remote_status == RemoteStatus.REMOTE:
            return ScoutFitDimension("location_fit", 100, ("Fully remote position",), ())
        city = profile.get_verified("location_city") or ""
        state = profile.get_verified("location_state") or ""
        if city.lower() in opp.location.lower() or state.lower() in opp.location.lower():
            return ScoutFitDimension(
                "location_fit", 85,
                (f"Candidate ({city}, {state}) matches posting location",), (),
            )
        if opp.remote_status == RemoteStatus.HYBRID:
            return ScoutFitDimension("location_fit", 55, (), ("Hybrid role — commute may be required",))
        return ScoutFitDimension("location_fit", 35, (), ("On-site role — candidate location likely incompatible",))

    def _assess_remote(self, opp: JobOpportunity) -> ScoutFitDimension:
        if opp.remote_status == RemoteStatus.REMOTE:
            return ScoutFitDimension("remote_fit", 100, ("Fully remote",), ())
        if opp.remote_status == RemoteStatus.HYBRID:
            return ScoutFitDimension("remote_fit", 65, ("Hybrid",), ())
        if opp.remote_status == RemoteStatus.UNKNOWN:
            return ScoutFitDimension("remote_fit", 50, ("Remote status unknown",), ())
        return ScoutFitDimension("remote_fit", 30, (), ("On-site required",))

    def _assess_friction(self, opp: JobOpportunity, profile: CandidateProfile) -> ScoutFitDimension:
        friction: list[str] = []
        if opp.ats_provider:
            friction.append(f"ATS: {opp.ats_provider}")
        work_auth = profile.get("work_authorization")
        if (
            work_auth is None or work_auth.classification == CandidateTruthClass.UNKNOWN
        ) and opp.work_authorization_requirements:
            friction.append("Work auth required but candidate status UNKNOWN")
        score = max(20, 100 - len(friction) * 20)
        return ScoutFitDimension("application_friction", score, (), tuple(friction))

    def _best_evidence(self, text: str, portfolio: str, profile: CandidateProfile) -> list[str]:
        evidence: list[str] = []
        for project_name, search_terms in _PORTFOLIO_EVIDENCE_MAP.items():
            if any(term in portfolio for term in search_terms):
                evidence.append(project_name)
        github = profile.get_verified("github_url")
        if github:
            evidence.append(f"GitHub portfolio: {github}")
        return evidence
