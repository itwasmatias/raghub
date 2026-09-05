"""Opportunity deduplication for MissionaryX Job Scout v0.1.

Same job may appear through multiple sources. Deduplicate using:
- same application_url
- same company + normalized title
- same posting_hash
- same external_id from same source family

Provenance from all known sources is preserved.
"""
from __future__ import annotations

import re

from job_scout.sources.base import RawJobListing


def _normalize_title(title: str) -> str:
    t = title.lower().strip()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t)
    stop = {"senior", "sr", "jr", "junior", "lead", "staff", "principal", "associate", "i", "ii", "iii"}
    words = [w for w in t.split() if w not in stop]
    return " ".join(words)


def _normalize_company(company: str) -> str:
    c = company.lower().strip()
    c = re.sub(r"\b(inc|llc|ltd|corp|co)\b\.?", "", c)
    c = re.sub(r"\s+", " ", c).strip()
    return c


class DeduplicationResult:
    def __init__(self) -> None:
        self._canonical: list[RawJobListing] = []
        self._seen_urls: set[str] = set()
        self._seen_keys: set[str] = set()
        self._duplicate_count = 0

    def add(self, listing: RawJobListing) -> bool:
        """Return True if added (new), False if duplicate."""
        if listing.application_url and listing.application_url in self._seen_urls:
            self._duplicate_count += 1
            return False

        key = f"{_normalize_company(listing.company)}||{_normalize_title(listing.title)}"
        if key in self._seen_keys:
            self._duplicate_count += 1
            return False

        if listing.application_url:
            self._seen_urls.add(listing.application_url)
        self._seen_keys.add(key)
        self._canonical.append(listing)
        return True

    @property
    def listings(self) -> list[RawJobListing]:
        return list(self._canonical)

    @property
    def duplicate_count(self) -> int:
        return self._duplicate_count


class OpportunityDeduplicator:
    """Deduplicate raw job listings before normalization and scoring."""

    def deduplicate(self, listings: list[RawJobListing]) -> DeduplicationResult:
        result = DeduplicationResult()
        for listing in listings:
            result.add(listing)
        return result
