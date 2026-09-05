"""Tests for JobSource implementations — deterministic fixture source."""
from __future__ import annotations

from job_scout.sources.fixture import (
    FixtureJobSource,
    ALL_FIXTURES,
    FIXTURE_AGENT_ENGINEER,
    FIXTURE_GENERIC_AI_BACKEND,
    FIXTURE_STALE,
)
from job_scout.sources.base import RawJobListing
from job_scout.deduplication import OpportunityDeduplicator


class TestFixtureJobSource:
    def test_returns_all_fixtures_by_default(self):
        source = FixtureJobSource()
        listings = source.fetch(["agent"])
        assert len(listings) == len(ALL_FIXTURES)

    def test_custom_fixtures_returned(self):
        fixtures = [FIXTURE_AGENT_ENGINEER, FIXTURE_GENERIC_AI_BACKEND]
        source = FixtureJobSource(fixtures)
        listings = source.fetch(["agent"])
        assert len(listings) == 2

    def test_source_name_is_fixture(self):
        source = FixtureJobSource()
        assert source.source_name == "fixture"

    def test_health_check_true(self):
        source = FixtureJobSource()
        assert source.health_check() is True

    def test_fixture_has_required_fields(self):
        for listing in ALL_FIXTURES:
            assert listing.company
            assert listing.title
            assert listing.posting_text
            assert listing.source_name
            assert listing.captured_at is not None

    def test_fixture_agent_engineer_has_remote_true(self):
        assert FIXTURE_AGENT_ENGINEER.remote is True

    def test_fixture_generic_backend_has_remote_false(self):
        assert FIXTURE_GENERIC_AI_BACKEND.remote is False

    def test_stale_fixture_has_old_date(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        age_days = (now - FIXTURE_STALE.captured_at).days
        assert age_days > 60


class TestOpportunityDeduplicator:
    def setup_method(self):
        self.dedup = OpportunityDeduplicator()

    def test_unique_listings_all_kept(self):
        from job_scout.sources.fixture import FIXTURE_AGENT_ENGINEER, FIXTURE_MCP_EVALS
        result = self.dedup.deduplicate([FIXTURE_AGENT_ENGINEER, FIXTURE_MCP_EVALS])
        assert len(result.listings) == 2
        assert result.duplicate_count == 0

    def test_same_url_deduplicates(self):
        from job_scout.sources.fixture import FIXTURE_DUPLICATE_SOURCE_A, FIXTURE_DUPLICATE_SOURCE_B
        result = self.dedup.deduplicate([FIXTURE_DUPLICATE_SOURCE_A, FIXTURE_DUPLICATE_SOURCE_B])
        assert len(result.listings) == 1
        assert result.duplicate_count == 1

    def test_same_company_and_title_deduplicates(self):
        from job_scout.sources.base import RawJobListing
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        a = RawJobListing(
            source_name="s1",
            source_url="https://a.invalid/job1",
            application_url="https://a.invalid/apply1",
            company="Acme Corp",
            title="AI Engineer",
            location="Remote",
            posting_text="Agent workflow. Python.",
            captured_at=now,
        )
        b = RawJobListing(
            source_name="s2",
            source_url="https://b.invalid/job1",
            application_url="https://b.invalid/apply2",  # different URL
            company="Acme Corp",  # same company
            title="AI Engineer",  # same title
            location="Remote",
            posting_text="Agent workflow. Python. Different text.",
            captured_at=now,
        )
        result = self.dedup.deduplicate([a, b])
        assert len(result.listings) == 1
        assert result.duplicate_count == 1

    def test_empty_input(self):
        result = self.dedup.deduplicate([])
        assert len(result.listings) == 0
        assert result.duplicate_count == 0

    def test_single_item_no_duplicate(self):
        result = self.dedup.deduplicate([FIXTURE_AGENT_ENGINEER])
        assert len(result.listings) == 1
        assert result.duplicate_count == 0


class TestRawJobListingNormalization:
    def test_to_dict_has_required_keys(self):
        d = FIXTURE_AGENT_ENGINEER.to_dict()
        assert "source_name" in d
        assert "company" in d
        assert "title" in d
        assert "posting_text" in d
        assert "captured_at" in d
        assert "application_url" in d
        assert "source_url" in d
