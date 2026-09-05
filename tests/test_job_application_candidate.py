"""Tests for candidate truth model — truth invariants, privacy, profile loading."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from job_application.candidate import (
    CandidateFact,
    CandidateProfile,
    CandidateTruthClass,
    ProfileDataError,
    load_candidate_profile,
    synthetic_test_profile,
)


class TestCandidateFact:
    def test_requires_nonempty_key(self):
        with pytest.raises(ValueError):
            CandidateFact(
                key="", value="v", classification=CandidateTruthClass.VERIFIED_FACT, source="s"
            )

    def test_requires_nonempty_source(self):
        with pytest.raises(ValueError):
            CandidateFact(
                key="k", value="v", classification=CandidateTruthClass.VERIFIED_FACT, source=""
            )

    def test_requires_valid_classification(self):
        with pytest.raises(TypeError):
            CandidateFact(
                key="k", value="v", classification="not_a_class", source="s"
            )

    def test_sensitive_fact_redacted_in_safe_repr(self):
        fact = CandidateFact(
            key="email", value="real@email.com",
            classification=CandidateTruthClass.VERIFIED_FACT, source="test",
            sensitive=True,
        )
        repr_str = fact.safe_repr()
        assert "real@email.com" not in repr_str
        assert "[SENSITIVE REDACTED]" in repr_str

    def test_nonsensitive_fact_visible_in_safe_repr(self):
        fact = CandidateFact(
            key="primary_language", value="Python",
            classification=CandidateTruthClass.VERIFIED_FACT, source="test",
        )
        assert "Python" in fact.safe_repr()

    def test_roundtrip_serialization(self):
        fact = CandidateFact(
            key="years", value="5",
            classification=CandidateTruthClass.USER_SUPPLIED_FACT,
            source="test_fixture",
            verified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            reusable=True, sensitive=False,
        )
        restored = CandidateFact.from_dict(fact.to_dict())
        assert restored.key == fact.key
        assert restored.value == fact.value
        assert restored.classification == fact.classification

    def test_immutability(self):
        fact = CandidateFact(
            key="k", value="v", classification=CandidateTruthClass.VERIFIED_FACT, source="s"
        )
        with pytest.raises((AttributeError, TypeError)):
            fact.value = "mutated"


class TestCandidateProfile:
    def test_get_verified_returns_value_for_verified_fact(self):
        profile = synthetic_test_profile()
        val = profile.get_verified("primary_language")
        assert val == "Python"

    def test_get_verified_returns_none_for_unknown(self):
        profile = synthetic_test_profile()
        val = profile.get_verified("work_authorization")
        assert val is None

    def test_get_verified_returns_none_for_derived_positioning(self):
        fact = CandidateFact(
            key="market_positioning", value="Senior AI Engineer",
            classification=CandidateTruthClass.DERIVED_POSITIONING, source="test"
        )
        profile = CandidateProfile(
            facts=(fact,), profile_id="test_profile"
        )
        assert profile.get_verified("market_positioning") is None

    def test_assert_as_claim_raises_for_unknown(self):
        profile = synthetic_test_profile()
        with pytest.raises(ProfileDataError, match="UNKNOWN"):
            profile.assert_as_application_claim("work_authorization")

    def test_assert_as_claim_raises_for_derived_positioning(self):
        fact = CandidateFact(
            key="expertise_level", value="Expert",
            classification=CandidateTruthClass.DERIVED_POSITIONING, source="test"
        )
        profile = CandidateProfile(facts=(fact,), profile_id="p")
        with pytest.raises(ProfileDataError, match="DERIVED_POSITIONING"):
            profile.assert_as_application_claim("expertise_level")

    def test_assert_as_claim_raises_for_missing_key(self):
        profile = synthetic_test_profile()
        with pytest.raises(ProfileDataError, match="no entry"):
            profile.assert_as_application_claim("nonexistent_key")

    def test_assert_as_claim_succeeds_for_verified_fact(self):
        profile = synthetic_test_profile()
        val = profile.assert_as_application_claim("primary_language")
        assert val == "Python"

    def test_profile_hash_is_deterministic(self):
        profile = synthetic_test_profile()
        assert profile.profile_hash == profile.profile_hash

    def test_profile_hash_changes_with_facts(self):
        profile1 = synthetic_test_profile()
        fact = CandidateFact(
            key="extra_key", value="extra_value",
            classification=CandidateTruthClass.VERIFIED_FACT, source="test"
        )
        profile2 = CandidateProfile(
            facts=profile1.facts + (fact,),
            profile_id=profile1.profile_id,
        )
        assert profile1.profile_hash != profile2.profile_hash

    def test_safe_summary_redacts_sensitive(self):
        profile = synthetic_test_profile()
        summary = profile.safe_summary()
        assert summary.get("email") == "[SENSITIVE]"
        assert summary.get("phone") == "[SENSITIVE]"
        assert summary.get("primary_language") == "Python"

    def test_sensitive_values_absent_from_safe_summary_log(self):
        profile = synthetic_test_profile()
        summary_str = str(profile.safe_summary())
        assert "alex.testworthy@example.invalid" not in summary_str
        assert "+1-555-000-0000" not in summary_str


class TestProfileLoading:
    def test_load_candidate_profile_from_file(self, tmp_path: Path):
        profile_data = {
            "profile_id": "test_load",
            "facts": [
                {
                    "key": "primary_language",
                    "value": "Python",
                    "classification": "verified_fact",
                    "source": "file_test",
                    "verified_at": None,
                    "reusable": True,
                    "sensitive": False,
                }
            ],
        }
        profile_file = tmp_path / "test_profile.json"
        profile_file.write_text(json.dumps(profile_data), encoding="utf-8")
        profile = load_candidate_profile(profile_file)
        assert profile.profile_id == "test_load"
        assert profile.get_verified("primary_language") == "Python"

    def test_synthetic_test_profile_has_no_real_personal_data(self):
        profile = synthetic_test_profile()
        # Verify synthetic markers present
        assert "example.invalid" in (profile.get_verified("email") or "")
        assert "synthetic" in (profile.get_verified("github_url") or "")
        assert "Testworthy" in (profile.get_verified("full_name") or "")

    def test_synthetic_profile_work_authorization_is_unknown(self):
        profile = synthetic_test_profile()
        fact = profile.get("work_authorization")
        assert fact is not None
        assert fact.classification == CandidateTruthClass.UNKNOWN

    def test_synthetic_profile_sponsorship_is_unknown(self):
        profile = synthetic_test_profile()
        fact = profile.get("sponsorship_required")
        assert fact is not None
        assert fact.classification == CandidateTruthClass.UNKNOWN
