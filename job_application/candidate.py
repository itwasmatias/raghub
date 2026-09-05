"""Candidate truth model for MissionaryX Job Application Executor v0.1.

Key Invariants:
- Every candidate datum carries a truth classification and provenance.
- UNKNOWN facts cannot be elevated to application claims.
- DERIVED_POSITIONING may inform framing but may not be asserted as verified fact.
- Sensitive fields are excluded from normal logging.
- No personal data is hardcoded; profiles load from external private artifacts.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from revenue_bridge.events import _canonical_bytes, _require_text


class CandidateTruthClass(str, Enum):
    VERIFIED_FACT = "verified_fact"
    USER_SUPPLIED_FACT = "user_supplied_fact"
    DERIVED_POSITIONING = "derived_positioning"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CandidateFact:
    key: str
    value: str
    classification: CandidateTruthClass
    source: str
    verified_at: datetime | None = None
    reusable: bool = True
    sensitive: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _require_text(self.key, "key"))
        if not isinstance(self.classification, CandidateTruthClass):
            raise TypeError("classification must be CandidateTruthClass")
        object.__setattr__(self, "source", _require_text(self.source, "source"))

    def safe_repr(self) -> str:
        if self.sensitive:
            return f"CandidateFact(key={self.key!r}, classification={self.classification.value}, [SENSITIVE REDACTED])"
        return f"CandidateFact(key={self.key!r}, value={self.value!r}, classification={self.classification.value})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "classification": self.classification.value,
            "source": self.source,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "reusable": self.reusable,
            "sensitive": self.sensitive,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandidateFact:
        verified_at = data.get("verified_at")
        if isinstance(verified_at, str):
            verified_at = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
        return cls(
            key=data["key"],
            value=data["value"],
            classification=CandidateTruthClass(data["classification"]),
            source=data["source"],
            verified_at=verified_at,
            reusable=data.get("reusable", True),
            sensitive=data.get("sensitive", False),
        )


class ProfileDataError(ValueError):
    """Raised when candidate profile data violates truth invariants."""


@dataclass(frozen=True, slots=True)
class CandidateProfile:
    facts: tuple[CandidateFact, ...]
    profile_id: str
    source_path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.facts, tuple):
            object.__setattr__(self, "facts", tuple(self.facts))
        _require_text(self.profile_id, "profile_id")

    def get(self, key: str) -> CandidateFact | None:
        for f in self.facts:
            if f.key == key:
                return f
        return None

    def get_verified(self, key: str) -> str | None:
        """Return value only for VERIFIED_FACT or USER_SUPPLIED_FACT; None otherwise."""
        f = self.get(key)
        if f is None:
            return None
        if f.classification not in (
            CandidateTruthClass.VERIFIED_FACT,
            CandidateTruthClass.USER_SUPPLIED_FACT,
        ):
            return None
        return f.value

    def assert_as_application_claim(self, key: str) -> str:
        """Return value for an application claim, or raise ProfileDataError.

        Raises ProfileDataError for UNKNOWN or DERIVED_POSITIONING — these must
        not silently become application assertions.
        """
        f = self.get(key)
        if f is None:
            raise ProfileDataError(
                f"Candidate profile has no entry for {key!r}; cannot make application claim."
            )
        if f.classification == CandidateTruthClass.UNKNOWN:
            raise ProfileDataError(
                f"Fact {key!r} is UNKNOWN; cannot assert as application claim. "
                "Mark USER_REQUIRED or supply verified evidence."
            )
        if f.classification == CandidateTruthClass.DERIVED_POSITIONING:
            raise ProfileDataError(
                f"Fact {key!r} is DERIVED_POSITIONING; cannot assert as verified claim. "
                "Use as framing only, not a factual claim."
            )
        return f.value

    @property
    def profile_hash(self) -> str:
        payload = {
            "profile_id": self.profile_id,
            "facts": [f.to_dict() for f in sorted(self.facts, key=lambda x: x.key)],
        }
        return hashlib.sha256(_canonical_bytes(payload)).hexdigest()

    def safe_summary(self) -> dict[str, str]:
        """Return a log-safe summary that redacts sensitive values."""
        return {f.key: ("[SENSITIVE]" if f.sensitive else f.value) for f in self.facts}

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "source_path": self.source_path,
            "facts": [f.to_dict() for f in self.facts],
            "profile_hash": self.profile_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandidateProfile:
        return cls(
            facts=tuple(CandidateFact.from_dict(f) for f in data.get("facts", [])),
            profile_id=data["profile_id"],
            source_path=data.get("source_path"),
        )


def load_candidate_profile(path: Path) -> CandidateProfile:
    """Load candidate profile from external private artifact (JSON)."""
    data = json.loads(path.read_text("utf-8"))
    return CandidateProfile.from_dict(data)


def synthetic_test_profile() -> CandidateProfile:
    """Fully synthetic candidate profile for deterministic testing. No real personal data."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    facts = (
        CandidateFact(
            key="full_name", value="Alex Testworthy",
            classification=CandidateTruthClass.VERIFIED_FACT, source="test_fixture",
            verified_at=now, sensitive=True,
        ),
        CandidateFact(
            key="email", value="alex.testworthy@example.invalid",
            classification=CandidateTruthClass.VERIFIED_FACT, source="test_fixture",
            sensitive=True,
        ),
        CandidateFact(
            key="phone", value="+1-555-000-0000",
            classification=CandidateTruthClass.VERIFIED_FACT, source="test_fixture",
            sensitive=True,
        ),
        CandidateFact(
            key="location_city", value="Kansas City",
            classification=CandidateTruthClass.VERIFIED_FACT, source="test_fixture",
        ),
        CandidateFact(
            key="location_state", value="MO",
            classification=CandidateTruthClass.VERIFIED_FACT, source="test_fixture",
        ),
        CandidateFact(
            key="github_url", value="https://github.com/testworthy-synthetic",
            classification=CandidateTruthClass.VERIFIED_FACT, source="test_fixture",
        ),
        CandidateFact(
            key="linkedin_url", value="https://linkedin.com/in/testworthy-synthetic",
            classification=CandidateTruthClass.VERIFIED_FACT, source="test_fixture",
        ),
        CandidateFact(
            key="work_authorization", value="UNKNOWN",
            classification=CandidateTruthClass.UNKNOWN, source="test_fixture",
        ),
        CandidateFact(
            key="sponsorship_required", value="UNKNOWN",
            classification=CandidateTruthClass.UNKNOWN, source="test_fixture",
        ),
        CandidateFact(
            key="years_professional_software_experience", value="2",
            classification=CandidateTruthClass.USER_SUPPLIED_FACT, source="test_fixture",
        ),
        CandidateFact(
            key="primary_language", value="Python",
            classification=CandidateTruthClass.VERIFIED_FACT, source="test_fixture",
        ),
        CandidateFact(
            key="portfolio_projects", value="MissionaryX agentic system, KC news radar",
            classification=CandidateTruthClass.VERIFIED_FACT, source="test_fixture",
        ),
        CandidateFact(
            key="degree", value="UNKNOWN",
            classification=CandidateTruthClass.UNKNOWN, source="test_fixture",
        ),
        CandidateFact(
            key="salary_expectation", value="UNKNOWN",
            classification=CandidateTruthClass.UNKNOWN, source="test_fixture",
        ),
        CandidateFact(
            key="resume_canonical_path", value="NONE",
            classification=CandidateTruthClass.USER_SUPPLIED_FACT, source="test_fixture",
        ),
    )
    return CandidateProfile(
        facts=facts,
        profile_id="synthetic_test_profile_v1",
        source_path=None,
    )
