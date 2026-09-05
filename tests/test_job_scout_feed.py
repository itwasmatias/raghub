"""Tests for DurableScoutFeed — deterministic, no network."""
from __future__ import annotations

import pytest
from pathlib import Path

from job_application.candidate import synthetic_test_profile
from job_application.opportunity import ingest_from_text, RemoteStatus
from job_scout.feed import DurableScoutFeed, ScoutFeedCorruptionError
from job_scout.models import AvailabilityStatus, ScoredOpportunity, ScoutState
from job_scout.pipeline import ScoutConfig, ScoutPipeline
from job_scout.sources.fixture import FixtureJobSource, FIXTURE_AGENT_ENGINEER


def _make_scored(posting_text: str, company: str = "Test Co", title: str = "Test Role"):
    from datetime import datetime, timezone
    from job_scout.similarity import RootstockSimilarityScorer
    from job_scout.candidate_fit import ScoutFitAssessor
    from job_scout.classifier import NontraditionalMatchClassifier
    from job_scout.requirements import RequirementClassifier

    opp = ingest_from_text(posting_text=posting_text, company=company, title=title,
                           remote_status=RemoteStatus.REMOTE)
    profile = synthetic_test_profile()
    sim = RootstockSimilarityScorer().score(opp)
    fit = ScoutFitAssessor().assess(opp, profile)
    reqs = RequirementClassifier().classify_all(opp)
    match = NontraditionalMatchClassifier().classify(sim, fit, ())

    return ScoredOpportunity(
        opportunity=opp,
        rootstock_similarity=sim,
        candidate_fit=fit,
        match_result=match,
        requirement_classifications=reqs,
        scout_state=ScoutState.SCORED,
        discovered_at=datetime.now(timezone.utc),
        availability_status=AvailabilityStatus.UNVERIFIED_CURRENT,
        source_provenance=("test",),
    )


@pytest.fixture
def feed_path(tmp_path):
    return tmp_path / "test_feed.jsonl"


class TestDurableScoutFeed:
    def test_empty_feed_returns_no_records(self, feed_path):
        feed = DurableScoutFeed(feed_path)
        assert feed.load_all() == ()

    def test_store_and_load(self, feed_path):
        feed = DurableScoutFeed(feed_path)
        scored = _make_scored("Agent workflow orchestration. Python, Linux, Git.")
        feed.store(scored)
        loaded = feed.load_all()
        assert len(loaded) == 1
        assert loaded[0].job_id == scored.job_id

    def test_duplicate_store_raises(self, feed_path):
        feed = DurableScoutFeed(feed_path)
        scored = _make_scored("Agent workflow. Python.")
        feed.store(scored)
        with pytest.raises(ValueError):
            feed.store(scored)

    def test_get_by_job_id(self, feed_path):
        feed = DurableScoutFeed(feed_path)
        scored = _make_scored("Agent workflow. Python.")
        feed.store(scored)
        found = feed.get(scored.job_id)
        assert found is not None
        assert found.job_id == scored.job_id

    def test_get_unknown_job_id_returns_none(self, feed_path):
        feed = DurableScoutFeed(feed_path)
        assert feed.get("nonexistent-id") is None

    def test_update_state(self, feed_path):
        feed = DurableScoutFeed(feed_path)
        scored = _make_scored("Agent workflow. Python.")
        feed.store(scored)
        feed.update_state(scored.job_id, ScoutState.STRONG_APPLY)
        loaded = feed.get(scored.job_id)
        assert loaded.scout_state == ScoutState.STRONG_APPLY

    def test_update_state_unknown_job_raises(self, feed_path):
        feed = DurableScoutFeed(feed_path)
        with pytest.raises(ValueError):
            feed.update_state("nonexistent-id", ScoutState.SKIP)

    def test_update_executor_id(self, feed_path):
        feed = DurableScoutFeed(feed_path)
        scored = _make_scored("Agent workflow. Python.")
        feed.store(scored)
        feed.update_executor_id(scored.job_id, "app-12345")
        loaded = feed.get(scored.job_id)
        assert loaded.executor_application_id == "app-12345"
        assert loaded.scout_state == ScoutState.HANDED_TO_EXECUTOR

    def test_multiple_state_updates_latest_wins(self, feed_path):
        feed = DurableScoutFeed(feed_path)
        scored = _make_scored("Agent workflow. Python.")
        feed.store(scored)
        feed.update_state(scored.job_id, ScoutState.APPLY)
        feed.update_state(scored.job_id, ScoutState.STRONG_APPLY)
        loaded = feed.get(scored.job_id)
        assert loaded.scout_state == ScoutState.STRONG_APPLY

    def test_serialization_round_trip(self, feed_path):
        feed = DurableScoutFeed(feed_path)
        scored = _make_scored("Agent workflow. Python. Tool calling. Evals. Portfolio preferred.")
        feed.store(scored)
        loaded = feed.get(scored.job_id)
        assert loaded.rootstock_similarity.total == scored.rootstock_similarity.total
        assert loaded.candidate_fit.total == scored.candidate_fit.total
        assert loaded.match_result.classification == scored.match_result.classification

    def test_corruption_raises(self, feed_path):
        feed_path.write_bytes(b'{"incomplete": "json"')
        feed = DurableScoutFeed(feed_path)
        with pytest.raises(ScoutFeedCorruptionError):
            feed.load_all()

    def test_multiple_records_survive_reload(self, feed_path):
        feed = DurableScoutFeed(feed_path)
        for i in range(3):
            scored = _make_scored(f"Agent workflow job {i}. Python. Agentic systems.", company=f"Co{i}")
            feed.store(scored)
        # Reload fresh
        feed2 = DurableScoutFeed(feed_path)
        loaded = feed2.load_all()
        assert len(loaded) == 3

    def test_pipeline_persists_to_feed(self, feed_path):
        profile = synthetic_test_profile()
        feed = DurableScoutFeed(feed_path)
        pipeline = ScoutPipeline(
            sources=[FixtureJobSource([FIXTURE_AGENT_ENGINEER])],
            feed=feed,
            profile=profile,
            config=ScoutConfig(rootstock_similarity_threshold=0, candidate_fit_threshold=0,
                               max_results=20, include_weak_matches=True),
        )
        pipeline.run()
        stored = feed.load_all()
        assert len(stored) >= 1
