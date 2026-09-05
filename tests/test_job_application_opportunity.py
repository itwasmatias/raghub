"""Tests for JobOpportunity model and DurableOpportunityStore."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from job_application.opportunity import (
    DurableOpportunityStore,
    EmploymentType,
    JobOpportunity,
    OpportunityStoreCorruptionError,
    RemoteStatus,
    ingest_from_text,
)

_FIXTURE_POSTING = """
Software Engineer — AI Systems

About the role:
We are looking for a Python developer with experience in agentic AI systems.

Requirements:
- 3+ years of Python development
- Experience with LLM APIs (OpenAI, Anthropic)
- Familiarity with PostgreSQL and Redis

Preferred:
- Experience with RAG systems
- Knowledge of Docker and Kubernetes

Responsibilities:
- Build and maintain AI pipelines
- Design governed execution systems
"""


class TestJobOpportunity:
    def test_ingest_from_text_captures_posting_hash(self):
        opp = ingest_from_text(
            posting_text=_FIXTURE_POSTING,
            company="Acme AI",
            title="Software Engineer",
        )
        expected_hash = JobOpportunity.compute_posting_hash(_FIXTURE_POSTING)
        assert opp.posting_hash == expected_hash

    def test_posting_hash_differs_for_different_texts(self):
        opp1 = ingest_from_text("text A", "Co", "Role")
        opp2 = ingest_from_text("text B", "Co", "Role")
        assert opp1.posting_hash != opp2.posting_hash

    def test_technologies_extracted_from_text(self):
        opp = ingest_from_text(
            posting_text="We use Python, PostgreSQL, and Docker.",
            company="Co", title="Role",
        )
        assert "python" in opp.technologies
        assert "postgresql" in opp.technologies
        assert "docker" in opp.technologies

    def test_roundtrip_serialization(self):
        opp = ingest_from_text(
            posting_text=_FIXTURE_POSTING,
            company="Acme AI",
            title="Software Engineer",
            location="Kansas City, MO",
            remote_status=RemoteStatus.HYBRID,
            employment_type=EmploymentType.FULL_TIME,
        )
        restored = JobOpportunity.from_dict(opp.to_dict())
        assert restored.job_id == opp.job_id
        assert restored.posting_hash == opp.posting_hash
        assert restored.company == opp.company
        assert restored.remote_status == opp.remote_status

    def test_immutability(self):
        opp = ingest_from_text("posting", "Co", "Role")
        with pytest.raises((AttributeError, TypeError)):
            opp.company = "mutated"

    def test_requires_nonempty_company(self):
        with pytest.raises(ValueError):
            JobOpportunity(
                job_id="j1", source="manual_text", source_url=None, application_url=None,
                company="", title="Role", location="KC",
                remote_status=RemoteStatus.UNKNOWN, employment_type=EmploymentType.UNKNOWN,
                salary_text=None, salary_min=None, salary_max=None,
                posting_text="text", posting_hash="hash",
                captured_at=datetime.now(timezone.utc),
                requirements=(), preferred_requirements=(), responsibilities=(),
                technologies=(), experience_requirements=(), education_requirements=(),
                work_authorization_requirements=(), application_deadline=None, ats_provider=None,
            )


class TestDurableOpportunityStore:
    def test_store_and_retrieve(self, tmp_path: Path):
        store = DurableOpportunityStore(tmp_path / "opps.jsonl")
        opp = ingest_from_text("posting text", "Acme", "Dev")
        store.store(opp)
        retrieved = store.get(opp.job_id)
        assert retrieved is not None
        assert retrieved.job_id == opp.job_id
        assert retrieved.posting_hash == opp.posting_hash

    def test_duplicate_job_id_rejected(self, tmp_path: Path):
        store = DurableOpportunityStore(tmp_path / "opps.jsonl")
        opp = ingest_from_text("posting text", "Acme", "Dev")
        store.store(opp)
        with pytest.raises(ValueError, match="already exists"):
            store.store(opp)

    def test_find_duplicate_by_posting_hash(self, tmp_path: Path):
        store = DurableOpportunityStore(tmp_path / "opps.jsonl")
        opp1 = ingest_from_text("identical posting text", "Acme", "Dev")
        store.store(opp1)
        opp2 = ingest_from_text("identical posting text", "Acme", "Dev")
        dup = store.find_duplicate(opp2)
        assert dup is not None
        assert dup.job_id == opp1.job_id

    def test_find_duplicate_by_company_title(self, tmp_path: Path):
        store = DurableOpportunityStore(tmp_path / "opps.jsonl")
        opp1 = ingest_from_text("posting 1", "Acme", "Dev")
        store.store(opp1)
        opp2 = ingest_from_text("posting 2 different", "Acme", "Dev")
        dup = store.find_duplicate(opp2)
        assert dup is not None

    def test_no_duplicate_for_different_company(self, tmp_path: Path):
        store = DurableOpportunityStore(tmp_path / "opps.jsonl")
        opp1 = ingest_from_text("posting", "Acme", "Dev")
        store.store(opp1)
        opp2 = ingest_from_text("other posting", "BetaCorp", "Engineer")
        dup = store.find_duplicate(opp2)
        assert dup is None

    def test_empty_store_returns_empty_tuple(self, tmp_path: Path):
        store = DurableOpportunityStore(tmp_path / "opps.jsonl")
        assert store.load_all() == ()

    def test_survives_restart(self, tmp_path: Path):
        path = tmp_path / "opps.jsonl"
        opp = ingest_from_text("posting text", "Acme", "Dev")

        store1 = DurableOpportunityStore(path)
        store1.store(opp)

        store2 = DurableOpportunityStore(path)
        retrieved = store2.get(opp.job_id)
        assert retrieved is not None
        assert retrieved.posting_hash == opp.posting_hash

    def test_corruption_detection(self, tmp_path: Path):
        path = tmp_path / "opps.jsonl"
        path.write_bytes(b'{"record_type":"opportunity","data":{}}')  # no trailing newline
        store = DurableOpportunityStore(path)
        with pytest.raises(OpportunityStoreCorruptionError):
            store.load_all()
