"""Tests for Scout candidate fit assessor — deterministic, no LLM."""
from __future__ import annotations

import pytest

from job_application.candidate import (
    CandidateProfile, CandidateFact, CandidateTruthClass, synthetic_test_profile,
)
from job_application.opportunity import ingest_from_text, RemoteStatus
from job_scout.candidate_fit import ScoutFitAssessor, ScoutFitScore


def _make_opp(text: str, remote: RemoteStatus = RemoteStatus.REMOTE):
    return ingest_from_text(
        posting_text=text, company="Test Co", title="Test Role",
        remote_status=remote,
    )


def _profile_with_years(years: str) -> CandidateProfile:
    from datetime import datetime, timezone
    base = synthetic_test_profile()
    new_facts = tuple(
        CandidateFact(
            key=f.key,
            value=years if f.key == "years_professional_software_experience" else f.value,
            classification=f.classification,
            source=f.source,
        ) if f.key == "years_professional_software_experience" else f
        for f in base.facts
    )
    return CandidateProfile(facts=new_facts, profile_id="test_years_profile")


AGENT_POSTING = """
AI Agent Engineer — Fully remote.
Build AI agents using Python, Claude, and OpenAI APIs.
Implement tool calling and agent workflow orchestration.
Run evals and verification pipelines. GitHub portfolio encouraged.
No mandatory degree. 2+ years experience preferred.
"""

ONSITE_POSTING = """
Backend Engineer — On-site Chicago.
5+ years Python engineering.
Bachelor's required.
No AI work involved.
"""


class TestScoutFitAssessor:
    def setup_method(self):
        self.assessor = ScoutFitAssessor()

    def test_agent_role_scores_well_for_candidate(self):
        profile = synthetic_test_profile()
        opp = _make_opp(AGENT_POSTING)
        fit = self.assessor.assess(opp, profile)
        assert fit.total >= 50

    def test_onsite_role_penalizes_remote_fit(self):
        profile = synthetic_test_profile()
        opp = _make_opp(ONSITE_POSTING, remote=RemoteStatus.ON_SITE)
        fit = self.assessor.assess(opp, profile)
        assert fit.remote_fit.score < 50

    def test_experience_gap_is_visible_not_silent(self):
        profile = _profile_with_years("1")
        opp = _make_opp("5+ years professional Python experience required.")
        fit = self.assessor.assess(opp, profile)
        assert fit.experience_gap_penalty.score < 50
        assert fit.experience_gap_penalty.gaps

    def test_experience_gap_does_not_auto_reject(self):
        profile = _profile_with_years("1")
        opp = _make_opp(AGENT_POSTING + "\n5+ years experience required.")
        fit = self.assessor.assess(opp, profile)
        # Should still have positive score even with gap
        assert fit.total > 0
        assert not fit.experience_gap_penalty.is_hard_blocker

    def test_meets_experience_requirement(self):
        profile = _profile_with_years("5")
        opp = _make_opp("5+ years experience required.")
        fit = self.assessor.assess(opp, profile)
        assert fit.experience_gap_penalty.score == 100

    def test_mandatory_degree_is_hard_blocker(self):
        profile = synthetic_test_profile()  # degree is UNKNOWN
        opp = _make_opp("Bachelor's required in Computer Science. Must have a degree.")
        fit = self.assessor.assess(opp, profile)
        assert fit.education_gap_penalty.is_hard_blocker

    def test_preferred_degree_is_not_hard_blocker(self):
        profile = synthetic_test_profile()  # degree is UNKNOWN
        opp = _make_opp("Bachelor's degree preferred but not required.")
        fit = self.assessor.assess(opp, profile)
        assert not fit.education_gap_penalty.is_hard_blocker
        assert fit.education_gap_penalty.score >= 70

    def test_portfolio_evidence_detected(self):
        profile = synthetic_test_profile()
        opp = _make_opp("Portfolio and GitHub projects strongly preferred. Show your work.")
        fit = self.assessor.assess(opp, profile)
        assert fit.portfolio_evidence_fit.score >= 50

    def test_python_match_increases_score(self):
        profile = synthetic_test_profile()  # primary_language = Python
        opp = _make_opp("Python required. API integration experience needed.")
        fit = self.assessor.assess(opp, profile)
        assert fit.python_api_fit.score >= 70

    def test_missionaryx_portfolio_recognized(self):
        profile = synthetic_test_profile()  # portfolio_projects includes "MissionaryX agentic system"
        opp = _make_opp(AGENT_POSTING)
        fit = self.assessor.assess(opp, profile)
        assert "missionaryx" in fit.agent_workflow_fit.evidence[0].lower() or \
               any("missionaryx" in str(e).lower() for e in fit.agent_workflow_fit.evidence)

    def test_best_evidence_is_populated(self):
        profile = synthetic_test_profile()
        opp = _make_opp(AGENT_POSTING)
        fit = self.assessor.assess(opp, profile)
        assert len(fit.best_evidence_to_show) > 0

    def test_fit_score_bounded_0_to_100(self):
        profile = synthetic_test_profile()
        opp = _make_opp(AGENT_POSTING)
        fit = self.assessor.assess(opp, profile)
        assert 0 <= fit.total <= 100

    def test_fit_score_is_deterministic(self):
        profile = synthetic_test_profile()
        opp = _make_opp(AGENT_POSTING)
        f1 = self.assessor.assess(opp, profile)
        f2 = self.assessor.assess(opp, profile)
        assert f1.total == f2.total

    def test_hard_blockers_returns_blocker_dims(self):
        profile = synthetic_test_profile()
        opp = _make_opp("Bachelor's required. Must have a degree.")
        fit = self.assessor.assess(opp, profile)
        blockers = fit.hard_blockers()
        assert len(blockers) > 0

    def test_serialization_round_trip(self):
        profile = synthetic_test_profile()
        opp = _make_opp(AGENT_POSTING)
        fit = self.assessor.assess(opp, profile)
        restored = ScoutFitScore.from_dict(fit.to_dict())
        assert restored.total == fit.total
        assert restored.best_evidence_to_show == fit.best_evidence_to_show

    def test_remote_role_scores_location_fit_high(self):
        profile = synthetic_test_profile()
        opp = _make_opp(AGENT_POSTING, remote=RemoteStatus.REMOTE)
        fit = self.assessor.assess(opp, profile)
        assert fit.location_fit.score >= 85
        assert fit.remote_fit.score == 100
