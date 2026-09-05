"""Tests for NontraditionalMatchClassifier — deterministic."""
from __future__ import annotations

from job_scout.classifier import MatchClassification, NontraditionalMatchClassifier
from job_scout.similarity import RootstockSimilarityScorer
from job_scout.candidate_fit import ScoutFitAssessor
from job_application.opportunity import ingest_from_text
from job_application.candidate import synthetic_test_profile


def _make_opp(text: str):
    return ingest_from_text(posting_text=text, company="Co", title="Role")


ROOTSTOCK_POSTING = """
Agent Workflow Engineer — Remote.
Build AI agents using Claude Code and OpenAI Codex. Tool calling, function calling, MCP.
Evals and LLM evaluation pipelines. Human-in-the-loop approval. Rapid prototyping.
Portfolio and GitHub preferred. No degree required. Python, Linux, Git.
"""

GENERIC_POSTING = """
Senior Backend Engineer — On-site.
7+ years Go and distributed systems. Kafka, Redis, PostgreSQL.
Bachelor's required. Computer science fundamentals.
"""

WEIRD_TITLE_AGENT_POSTING = """
AI Operations Specialist
Build AI agents, agentic workflow orchestration, tool calling integrations.
LLM evaluation and evals. Human-in-the-loop approval workflows.
Portfolio and demonstrated projects matter. No degree required.
Python, Linux, Git.
"""


class TestNontraditionalMatchClassifier:
    def setup_method(self):
        self.classifier = NontraditionalMatchClassifier()
        self.sim_scorer = RootstockSimilarityScorer()
        self.fit_assessor = ScoutFitAssessor()
        self.profile = synthetic_test_profile()

    def _classify(self, posting: str, hard_blockers: tuple[str, ...] = ()):
        opp = _make_opp(posting)
        sim = self.sim_scorer.score(opp)
        fit = self.fit_assessor.assess(opp, self.profile)
        return self.classifier.classify(sim, fit, hard_blockers)

    def test_hard_blocker_gives_hard_block(self):
        result = self._classify(ROOTSTOCK_POSTING, hard_blockers=("active security clearance",))
        assert result.classification == MatchClassification.HARD_BLOCK

    def test_hard_block_prevents_strong_apply_regardless_of_score(self):
        result = self._classify(ROOTSTOCK_POSTING, hard_blockers=("phd required",))
        assert result.classification == MatchClassification.HARD_BLOCK

    def test_rootstock_posting_classifies_strongly(self):
        result = self._classify(ROOTSTOCK_POSTING)
        assert result.classification in (
            MatchClassification.DIRECT_MATCH,
            MatchClassification.NONTRADITIONAL_STRONG_MATCH,
        )

    def test_generic_backend_classifies_weak_or_stretch(self):
        result = self._classify(GENERIC_POSTING)
        assert result.classification in (
            MatchClassification.WEAK_MATCH,
            MatchClassification.REALISTIC_STRETCH,
        )

    def test_weird_title_strong_agent_duties_classifies_well(self):
        result = self._classify(WEIRD_TITLE_AGENT_POSTING)
        assert result.classification in (
            MatchClassification.DIRECT_MATCH,
            MatchClassification.NONTRADITIONAL_STRONG_MATCH,
            MatchClassification.REALISTIC_STRETCH,
        )

    def test_rationale_is_non_empty(self):
        result = self._classify(ROOTSTOCK_POSTING)
        assert result.rationale

    def test_hard_block_includes_blocker_text(self):
        result = self._classify(ROOTSTOCK_POSTING, hard_blockers=("top secret clearance",))
        assert "top secret clearance" in result.rationale.lower() or \
               result.primary_gap == "top secret clearance" or \
               "top secret" in (result.primary_gap or "").lower()

    def test_classification_is_deterministic(self):
        opp = _make_opp(ROOTSTOCK_POSTING)
        sim = self.sim_scorer.score(opp)
        fit = self.fit_assessor.assess(opp, self.profile)
        r1 = self.classifier.classify(sim, fit, ())
        r2 = self.classifier.classify(sim, fit, ())
        assert r1.classification == r2.classification

    def test_serialization_round_trip(self):
        result = self._classify(ROOTSTOCK_POSTING)
        from job_scout.classifier import ClassificationResult
        restored = ClassificationResult.from_dict(result.to_dict())
        assert restored.classification == result.classification
        assert restored.rationale == result.rationale
