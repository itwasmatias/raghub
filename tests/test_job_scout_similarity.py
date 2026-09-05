"""Tests for Rootstock similarity scorer — deterministic, no LLM."""
from __future__ import annotations

import pytest

from job_application.opportunity import ingest_from_text
from job_scout.similarity import RootstockSimilarityScorer, _DIMENSION_WEIGHTS


def _make_opp(text: str, title: str = "Test Role", company: str = "Test Co"):
    return ingest_from_text(posting_text=text, company=company, title=title)


ROOTSTOCK_POSTING = """
Agent Workflow Engineer
Build AI agents using Claude Code and OpenAI Codex.
Design agentic workflow orchestration, tool calling, function calling, MCP protocol.
Implement evals and LLM evaluation pipelines for agent reliability.
Human-in-the-loop approval workflows. Rapid prototyping and demo delivery.
Portfolio and GitHub projects strongly preferred. No degree required.
Python, Linux, Git.
"""

GENERIC_BACKEND_POSTING = """
Senior Backend Engineer
Design distributed microservices with Go and Kafka.
Optimize PostgreSQL queries. Build high-throughput data pipelines.
Some familiarity with ML infrastructure a bonus.
"""

WEAK_AI_POSTING = """
Data Entry Specialist with AI tools
Use AI-powered software for data processing tasks.
No coding required.
"""


class TestDimensionWeights:
    def test_weights_sum_to_100(self):
        assert sum(_DIMENSION_WEIGHTS.values()) == 100


class TestRootstockSimilarityScorer:
    def setup_method(self):
        self.scorer = RootstockSimilarityScorer()

    def test_rootstock_style_role_scores_high(self):
        opp = _make_opp(ROOTSTOCK_POSTING)
        score = self.scorer.score(opp)
        assert score.total >= 70, f"Expected >=70, got {score.total}"

    def test_generic_backend_scores_low(self):
        opp = _make_opp(GENERIC_BACKEND_POSTING)
        score = self.scorer.score(opp)
        assert score.total < 40, f"Expected <40, got {score.total}"

    def test_rootstock_scores_higher_than_generic(self):
        rootstock_score = self.scorer.score(_make_opp(ROOTSTOCK_POSTING)).total
        generic_score = self.scorer.score(_make_opp(GENERIC_BACKEND_POSTING)).total
        assert rootstock_score > generic_score

    def test_agent_building_dimension_detected(self):
        opp = _make_opp("Build AI agents using agentic systems and agent orchestration.")
        score = self.scorer.score(opp)
        assert score.agent_building.score > 0
        assert len(score.agent_building.matched_signals) > 0

    def test_tool_calling_dimension_detected(self):
        opp = _make_opp("Implement tool calling and function calling with MCP protocol.")
        score = self.scorer.score(opp)
        assert score.tool_use_and_integrations.score > 0

    def test_evals_dimension_detected(self):
        opp = _make_opp("Run evals and LLM evaluation benchmarks for agent reliability.")
        score = self.scorer.score(opp)
        assert score.evals_and_verification.score > 0

    def test_portfolio_dimension_detected(self):
        opp = _make_opp("Portfolio and GitHub projects strongly preferred. Show your work.")
        score = self.scorer.score(opp)
        assert score.portfolio_friendliness.score > 0

    def test_nontraditional_friendliness_detected(self):
        opp = _make_opp("Self-taught welcome. Skills over credentials. Demonstrated ability matters.")
        score = self.scorer.score(opp)
        assert score.nontraditional_background_friendliness.score > 0

    def test_score_is_bounded_0_to_100(self):
        opp = _make_opp(ROOTSTOCK_POSTING)
        score = self.scorer.score(opp)
        assert 0 <= score.total <= 100
        for dim in score.dimensions():
            assert 0 <= dim.score <= 100

    def test_score_is_deterministic(self):
        opp = _make_opp(ROOTSTOCK_POSTING)
        s1 = self.scorer.score(opp)
        s2 = self.scorer.score(opp)
        assert s1.total == s2.total
        assert s1.agent_building.score == s2.agent_building.score

    def test_empty_posting_scores_zero_total(self):
        opp = _make_opp("Generic job posting. No relevant keywords.")
        score = self.scorer.score(opp)
        assert score.total < 20

    def test_all_dimension_names_present(self):
        opp = _make_opp(ROOTSTOCK_POSTING)
        score = self.scorer.score(opp)
        for dim in score.dimensions():
            assert dim.name in _DIMENSION_WEIGHTS

    def test_serialization_round_trip(self):
        opp = _make_opp(ROOTSTOCK_POSTING)
        score = self.scorer.score(opp)
        from job_scout.similarity import RootstockSimilarityScore
        restored = RootstockSimilarityScore.from_dict(score.to_dict())
        assert restored.total == score.total
        assert restored.agent_building.score == score.agent_building.score

    def test_weird_title_with_agent_duties_scores_well(self):
        weird_title_posting = """
AI Operations Specialist
Despite the title, you will build AI agents, agentic workflow orchestration,
tool calling integrations, and LLM evaluation pipelines.
Portfolio of demonstrated projects is required.
"""
        opp = _make_opp(weird_title_posting, title="AI Operations Specialist")
        score = self.scorer.score(opp)
        assert score.total >= 40, f"Expected >=40, got {score.total}"

    def test_human_in_the_loop_detected(self):
        opp = _make_opp("Build human-in-the-loop approval workflows and human oversight systems.")
        score = self.scorer.score(opp)
        assert score.human_in_the_loop.score > 0

    def test_linux_git_python_detected(self):
        opp = _make_opp("Requirements: Python, Linux, Git, command line proficiency.")
        score = self.scorer.score(opp)
        assert score.linux_git_python_relevance.score > 0
