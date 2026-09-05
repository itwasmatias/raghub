"""Tests for ScoutPipeline — deterministic, fixture-based."""
from __future__ import annotations

import pytest
import tempfile
from pathlib import Path

from job_application.candidate import synthetic_test_profile
from job_scout.feed import DurableScoutFeed
from job_scout.models import ScoutState, MatchClassification
from job_scout.pipeline import ScoutConfig, ScoutPipeline
from job_scout.sources.fixture import (
    FixtureJobSource,
    FIXTURE_AGENT_ENGINEER,
    FIXTURE_WEIRD_TITLE_AGENT,
    FIXTURE_GENERIC_AI_BACKEND,
    FIXTURE_ML_RESEARCH,
    FIXTURE_SENIOR_HARD_BLOCKER,
    FIXTURE_DUPLICATE_SOURCE_A,
    FIXTURE_DUPLICATE_SOURCE_B,
    FIXTURE_STALE,
    FIXTURE_PREFERRED_DEGREE_ONLY,
    FIXTURE_MANDATORY_DEGREE,
    FIXTURE_MCP_EVALS,
    FIXTURE_PORTFOLIO_EMPHASIS,
)


@pytest.fixture
def feed_path(tmp_path):
    return tmp_path / "test_scout_feed.jsonl"


@pytest.fixture
def profile():
    return synthetic_test_profile()


@pytest.fixture
def default_config():
    return ScoutConfig(
        rootstock_similarity_threshold=40,
        candidate_fit_threshold=30,
        max_results=20,
        include_weak_matches=True,
    )


def make_pipeline(feed_path, profile, fixtures=None, config=None):
    feed = DurableScoutFeed(feed_path)
    source = FixtureJobSource(fixtures)
    return ScoutPipeline(
        sources=[source],
        feed=feed,
        profile=profile,
        config=config or ScoutConfig(
            rootstock_similarity_threshold=30,
            candidate_fit_threshold=20,
            max_results=20,
            include_weak_matches=True,
        ),
    ), feed


class TestScoutPipelineBasic:
    def test_pipeline_runs_and_returns_report(self, feed_path, profile):
        pipeline, feed = make_pipeline(feed_path, profile)
        report = pipeline.run()
        assert report.raw_fetched > 0
        assert report.after_dedup >= 0
        assert isinstance(report.top_opportunities, list)

    def test_fixture_source_used(self, feed_path, profile):
        pipeline, feed = make_pipeline(feed_path, profile)
        report = pipeline.run()
        assert "fixture" in report.sources_used

    def test_opportunities_persisted_to_feed(self, feed_path, profile):
        pipeline, feed = make_pipeline(feed_path, profile)
        report = pipeline.run()
        stored = feed.load_all()
        assert len(stored) > 0

    def test_second_run_does_not_duplicate(self, feed_path, profile):
        pipeline, feed = make_pipeline(feed_path, profile)
        report1 = pipeline.run()
        report2 = pipeline.run()
        stored = feed.load_all()
        job_ids = [s.job_id for s in stored]
        assert len(job_ids) == len(set(job_ids))


class TestDuplication:
    def test_duplicate_postings_collapsed(self, feed_path, profile):
        # Fixtures A and B are the same job from two sources
        fixtures = [FIXTURE_DUPLICATE_SOURCE_A, FIXTURE_DUPLICATE_SOURCE_B]
        pipeline, feed = make_pipeline(feed_path, profile, fixtures)
        report = pipeline.run()
        assert report.after_dedup == 1, f"Expected 1 after dedup, got {report.after_dedup}"
        stored = feed.load_all()
        assert len(stored) == 1


class TestRanking:
    def test_rootstock_style_ranks_above_generic(self, feed_path, profile):
        fixtures = [FIXTURE_AGENT_ENGINEER, FIXTURE_GENERIC_AI_BACKEND]
        pipeline, feed = make_pipeline(feed_path, profile, fixtures)
        report = pipeline.run()
        stored = sorted(feed.load_all(), key=lambda o: o.score_key, reverse=True)
        top_company = stored[0].company
        assert top_company == "NeuralLoop AI", f"Expected NeuralLoop AI first, got {top_company}"

    def test_weird_title_agent_can_rank_above_ai_generic(self, feed_path, profile):
        fixtures = [FIXTURE_WEIRD_TITLE_AGENT, FIXTURE_GENERIC_AI_BACKEND]
        pipeline, feed = make_pipeline(feed_path, profile, fixtures)
        report = pipeline.run()
        stored = sorted(feed.load_all(), key=lambda o: o.score_key, reverse=True)
        # Weird title with agent duties should outrank generic backend with AI mention
        weird_score = next(s for s in stored if s.company == "Coherent Systems").rootstock_similarity.total
        generic_score = next(s for s in stored if s.company == "TechCorp").rootstock_similarity.total
        assert weird_score > generic_score

    def test_rank_order_stable_for_same_inputs(self, feed_path, profile):
        fixtures = [FIXTURE_AGENT_ENGINEER, FIXTURE_MCP_EVALS, FIXTURE_GENERIC_AI_BACKEND]
        pipeline, feed = make_pipeline(feed_path, profile, fixtures)
        report1 = pipeline.run()
        order1 = [o.job_id for o in sorted(feed.load_all(), key=lambda o: o.score_key, reverse=True)]

        # Re-run pipeline with fresh feed
        feed2 = DurableScoutFeed(feed_path.parent / "feed2.jsonl")
        pipeline2 = ScoutPipeline(
            sources=[FixtureJobSource(fixtures)],
            feed=feed2,
            profile=profile,
            config=ScoutConfig(rootstock_similarity_threshold=30, candidate_fit_threshold=20,
                               max_results=20, include_weak_matches=True),
        )
        pipeline2.run()
        order2 = [o.job_id for o in sorted(feed2.load_all(), key=lambda o: o.score_key, reverse=True)]
        assert order1 == order2


class TestHardBlockers:
    def test_senior_hard_blocker_is_skipped(self, feed_path, profile):
        fixtures = [FIXTURE_SENIOR_HARD_BLOCKER]
        config = ScoutConfig(
            rootstock_similarity_threshold=0,
            candidate_fit_threshold=0,
            max_results=20,
            reject_hard_blockers=True,
            include_weak_matches=False,
        )
        pipeline, feed = make_pipeline(feed_path, profile, fixtures, config)
        report = pipeline.run()
        # Hard blocker should be rejected
        assert report.rejected_hard_blockers >= 1
        stored = feed.load_all()
        # Should not be persisted (rejected)
        assert len(stored) == 0

    def test_hard_blocker_present_in_classification(self, feed_path, profile):
        # Use include_weak_matches to keep hard blockers for inspection
        fixtures = [FIXTURE_SENIOR_HARD_BLOCKER]
        config = ScoutConfig(
            rootstock_similarity_threshold=0,
            candidate_fit_threshold=0,
            max_results=20,
            reject_hard_blockers=False,
            include_weak_matches=True,
        )
        pipeline, feed = make_pipeline(feed_path, profile, fixtures, config)
        report = pipeline.run()
        stored = feed.load_all()
        if stored:
            blocker_opps = [s for s in stored if s.match_result.classification == MatchClassification.HARD_BLOCK]
            assert len(blocker_opps) >= 1


class TestStaleness:
    def test_stale_posting_marked_stale(self, feed_path, profile):
        fixtures = [FIXTURE_STALE]
        config = ScoutConfig(
            rootstock_similarity_threshold=0,
            candidate_fit_threshold=0,
            max_results=20,
            include_weak_matches=True,
        )
        pipeline, feed = make_pipeline(feed_path, profile, fixtures, config)
        pipeline.run()
        stored = feed.load_all()
        assert len(stored) > 0
        stale_found = any(s.scout_state == ScoutState.STALE for s in stored)
        assert stale_found, "Stale posting should be marked STALE"


class TestThresholding:
    def test_high_threshold_rejects_weak_matches(self, feed_path, profile):
        fixtures = [FIXTURE_GENERIC_AI_BACKEND]
        config = ScoutConfig(
            rootstock_similarity_threshold=70,
            candidate_fit_threshold=60,
            max_results=20,
            reject_hard_blockers=True,
            include_weak_matches=False,
        )
        pipeline, feed = make_pipeline(feed_path, profile, fixtures, config)
        report = pipeline.run()
        # Rejected by either hard blocker or threshold — nothing persisted
        rejected = report.rejected_below_threshold + report.rejected_hard_blockers
        assert rejected >= 1
        assert len(feed.load_all()) == 0

    def test_low_threshold_includes_stretch_roles(self, feed_path, profile):
        fixtures = [FIXTURE_GENERIC_AI_BACKEND]
        config = ScoutConfig(
            rootstock_similarity_threshold=0,
            candidate_fit_threshold=0,
            max_results=20,
            reject_hard_blockers=False,
            include_weak_matches=True,
        )
        pipeline, feed = make_pipeline(feed_path, profile, fixtures, config)
        pipeline.run()
        assert len(feed.load_all()) >= 1


class TestPreferenceModel:
    def test_mcp_evals_posting_scores_highly(self, feed_path, profile):
        fixtures = [FIXTURE_MCP_EVALS]
        pipeline, feed = make_pipeline(feed_path, profile, fixtures)
        pipeline.run()
        stored = feed.load_all()
        assert len(stored) > 0
        mcp_opp = stored[0]
        assert mcp_opp.rootstock_similarity.total >= 55

    def test_portfolio_emphasis_posting_has_high_portfolio_friendliness(self, feed_path, profile):
        fixtures = [FIXTURE_PORTFOLIO_EMPHASIS]
        pipeline, feed = make_pipeline(feed_path, profile, fixtures)
        pipeline.run()
        stored = feed.load_all()
        assert len(stored) > 0
        port_opp = stored[0]
        assert port_opp.rootstock_similarity.portfolio_friendliness.score > 0

    def test_preferred_degree_is_soft_gap_not_blocker(self, feed_path, profile):
        fixtures = [FIXTURE_PREFERRED_DEGREE_ONLY]
        config = ScoutConfig(
            rootstock_similarity_threshold=0, candidate_fit_threshold=0,
            max_results=20, include_weak_matches=True,
        )
        pipeline, feed = make_pipeline(feed_path, profile, fixtures, config)
        pipeline.run()
        stored = feed.load_all()
        assert len(stored) > 0
        assert not stored[0].candidate_fit.education_gap_penalty.is_hard_blocker

    def test_mandatory_degree_is_hard_blocker(self, feed_path, profile):
        fixtures = [FIXTURE_MANDATORY_DEGREE]
        config = ScoutConfig(
            rootstock_similarity_threshold=0, candidate_fit_threshold=0,
            max_results=20, reject_hard_blockers=False, include_weak_matches=True,
        )
        pipeline, feed = make_pipeline(feed_path, profile, fixtures, config)
        pipeline.run()
        stored = feed.load_all()
        assert len(stored) > 0
        assert stored[0].candidate_fit.education_gap_penalty.is_hard_blocker


class TestEvidenceRecommendations:
    def test_missionaryx_evidence_recommended_for_agent_roles(self, feed_path, profile):
        fixtures = [FIXTURE_AGENT_ENGINEER]
        pipeline, feed = make_pipeline(feed_path, profile, fixtures)
        pipeline.run()
        stored = feed.load_all()
        assert len(stored) > 0
        evidence = stored[0].candidate_fit.best_evidence_to_show
        assert len(evidence) > 0

    def test_similarity_and_fit_are_separate(self, feed_path, profile):
        fixtures = [FIXTURE_AGENT_ENGINEER]
        pipeline, feed = make_pipeline(feed_path, profile, fixtures)
        pipeline.run()
        stored = feed.load_all()
        assert len(stored) > 0
        s = stored[0]
        # Scores are separate objects
        assert s.rootstock_similarity is not s.candidate_fit
        assert isinstance(s.rootstock_similarity.total, int)
        assert isinstance(s.candidate_fit.total, int)
