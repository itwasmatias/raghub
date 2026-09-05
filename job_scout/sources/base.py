"""JobSource abstraction for MissionaryX Job Scout v0.1.

Mirrors the BaseConnector / RetrievalProvider pattern.
Sources return raw job listings; normalization converts them to JobOpportunity.

All sources must be read-only. No authentication. No personal data transmission.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class RawJobListing:
    """Raw job posting evidence captured from a source before normalization."""

    source_name: str
    source_url: str | None
    application_url: str | None
    company: str
    title: str
    location: str
    posting_text: str
    captured_at: datetime
    posted_at: str | None = None
    salary_text: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    remote: bool | None = None
    external_id: str | None = None  # source-specific job ID

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "source_url": self.source_url,
            "application_url": self.application_url,
            "company": self.company,
            "title": self.title,
            "location": self.location,
            "posting_text": self.posting_text,
            "captured_at": self.captured_at.isoformat(),
            "posted_at": self.posted_at,
            "salary_text": self.salary_text,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "remote": self.remote,
            "external_id": self.external_id,
        }


class JobSource(ABC):
    """Abstract base class for read-only job listing sources."""

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Human-readable source identifier."""

    @abstractmethod
    def fetch(self, queries: list[str], limit_per_query: int = 10) -> list[RawJobListing]:
        """Fetch raw job listings for given queries. Read-only. No auth."""

    def health_check(self) -> bool:
        """Verify source is reachable. Override for live sources."""
        return True
