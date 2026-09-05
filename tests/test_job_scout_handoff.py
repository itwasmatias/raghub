"""Tests for ExecutorHandoff — integration with Job Application Executor."""
from __future__ import annotations

import pytest
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from job_application.browser import FixtureScenario, SimulatedATSBrowserAdapter
from job_application.candidate import synthetic_test_profile
from job_application.executor import JobApplicationExecutor
from job_application.ledger import DurableApplicationLedger
from job_application.opportunity import DurableOpportunityStore, ingest_from_text, RemoteStatus
from job_scout.candidate_fit import ScoutFitAssessor
from job_scout.classifier import NontraditionalMatchClassifier
from job_scout.feed import DurableScoutFeed
from job_scout.handoff import ExecutorHandoff
from job_scout.models import AvailabilityStatus, ScoredOpportunity, ScoutState
from job_scout.requirements import RequirementClassifier
from job_scout.similarity import RootstockSimilarityScorer


def _make_executor(tmp_path: Path) -> JobApplicationExecutor:
    return JobApplicationExecutor(
        ledger=DurableApplicationLedger(tmp_path / "applications.jsonl"),
        opportunity_store=DurableOpportunityStore(tmp_path / "opportunities.jsonl"),
        browser_adapter=SimulatedATSBrowserAdapter(FixtureScenario("single_page_success")),
    )


def _make_scored(posting_text: str, company: str = "Test Co", title: str = "Agent Engineer") -> ScoredOpportunity:
    opp = ingest_from_text(
        posting_text=posting_text,
        company=company,
        title=title,
        remote_status=RemoteStatus.REMOTE,
    )
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
        scout_state=ScoutState.STRONG_APPLY,
        discovered_at=datetime.now(timezone.utc),
        availability_status=AvailabilityStatus.UNVERIFIED_CURRENT,
        source_provenance=("test",),
    )


@pytest.fixture
def tmp_dirs(tmp_path):
    scout_path = tmp_path / "scout"
    app_path = tmp_path / "apps"
    scout_path.mkdir()
    app_path.mkdir()
    return scout_path, app_path


class TestExecutorHandoff:
    def test_handoff_succeeds(self, tmp_dirs):
        scout_path, app_path = tmp_dirs
        feed = DurableScoutFeed(scout_path / "feed.jsonl")
        executor = _make_executor(app_path)
        scored = _make_scored("Build AI agents. Tool calling. Python. LLM evals. Portfolio preferred.")
        feed.store(scored)

        handoff = ExecutorHandoff(executor=executor, feed=feed)
        result = handoff.hand_to_executor(scored)

        assert result.success
        assert result.application_id
        assert not result.was_duplicate

    def test_handoff_updates_scout_state(self, tmp_dirs):
        scout_path, app_path = tmp_dirs
        feed = DurableScoutFeed(scout_path / "feed.jsonl")
        executor = _make_executor(app_path)
        scored = _make_scored("Build AI agents. Tool calling. Python. Evals.")
        feed.store(scored)

        handoff = ExecutorHandoff(executor=executor, feed=feed)
        handoff.hand_to_executor(scored)

        updated = feed.get(scored.job_id)
        assert updated.scout_state == ScoutState.HANDED_TO_EXECUTOR

    def test_handoff_stores_executor_application_id(self, tmp_dirs):
        scout_path, app_path = tmp_dirs
        feed = DurableScoutFeed(scout_path / "feed.jsonl")
        executor = _make_executor(app_path)
        scored = _make_scored("Build AI agents. Python. Evals. Remote.")
        feed.store(scored)

        handoff = ExecutorHandoff(executor=executor, feed=feed)
        result = handoff.hand_to_executor(scored)

        updated = feed.get(scored.job_id)
        assert updated.executor_application_id == result.application_id

    def test_handoff_creates_executor_record(self, tmp_dirs):
        scout_path, app_path = tmp_dirs
        feed = DurableScoutFeed(scout_path / "feed.jsonl")
        executor = _make_executor(app_path)
        scored = _make_scored("AI workflow engineer. Agent systems. Python. Evals.")
        feed.store(scored)

        handoff = ExecutorHandoff(executor=executor, feed=feed)
        result = handoff.hand_to_executor(scored)

        record = executor.ledger.get(result.application_id)
        assert record is not None
        assert record.job_id == scored.job_id

    def test_duplicate_handoff_returns_existing(self, tmp_dirs):
        scout_path, app_path = tmp_dirs
        feed = DurableScoutFeed(scout_path / "feed.jsonl")
        executor = _make_executor(app_path)
        scored = _make_scored("AI agent engineer. Python. Tool calling. Evals.")
        feed.store(scored)

        handoff = ExecutorHandoff(executor=executor, feed=feed)
        result1 = handoff.hand_to_executor(scored)
        result2 = handoff.hand_to_executor(scored)

        assert result2.was_duplicate
        assert result2.existing_application_id == result1.application_id

    def test_handoff_preserves_posting_evidence(self, tmp_dirs):
        scout_path, app_path = tmp_dirs
        feed = DurableScoutFeed(scout_path / "feed.jsonl")
        executor = _make_executor(app_path)
        posting = "AI agent engineer. Build agentic workflows. Python. Tool calling. Evals."
        scored = _make_scored(posting)
        feed.store(scored)

        handoff = ExecutorHandoff(executor=executor, feed=feed)
        result = handoff.hand_to_executor(scored)

        opp = executor.opportunity_store.get(scored.job_id)
        assert opp is not None
        assert opp.posting_text == posting.strip()

    def test_handoff_result_has_correct_job_id(self, tmp_dirs):
        scout_path, app_path = tmp_dirs
        feed = DurableScoutFeed(scout_path / "feed.jsonl")
        executor = _make_executor(app_path)
        scored = _make_scored("AI agent. Python. Evals.")
        feed.store(scored)

        handoff = ExecutorHandoff(executor=executor, feed=feed)
        result = handoff.hand_to_executor(scored)

        assert result.job_id == scored.job_id
