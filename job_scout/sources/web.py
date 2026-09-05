"""Read-only public web job source for MissionaryX Job Scout v0.1.

Uses RemoteOK public JSON API — no authentication, no personal data, read-only.

Constraints enforced:
- No login or credential use
- No personal data transmission
- No application submission
- Source URL and raw posting text are always preserved
- Failures are visible (no silent swallowing)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from job_scout.sources.base import JobSource, RawJobListing

logger = logging.getLogger(__name__)

_REMOTEOK_API = "https://remoteok.com/api"
_RELEVANT_TAGS = {
    "ai", "ml", "machine-learning", "llm", "gpt", "openai", "anthropic",
    "agent", "nlp", "python", "langchain", "chatgpt", "rag",
    "developer-tools", "devtools", "automation",
}
_REQUEST_HEADERS = {
    "User-Agent": "MissionaryX-JobScout/0.1 (read-only job discovery; no personal data)",
}
_TIMEOUT = 15


class LiveDiscoveryBlockedError(RuntimeError):
    """Raised when live discovery cannot proceed safely."""


class RemoteOKJobSource(JobSource):
    """Read-only public RemoteOK API source. No auth. No personal data."""

    @property
    def source_name(self) -> str:
        return "remoteok_public"

    def fetch(self, queries: list[str], limit_per_query: int = 10) -> list[RawJobListing]:
        """Fetch jobs from RemoteOK public API. Raises LiveDiscoveryBlockedError on failure."""
        try:
            import requests
        except ImportError as exc:
            raise LiveDiscoveryBlockedError("requests library not available") from exc

        try:
            response = requests.get(_REMOTEOK_API, headers=_REQUEST_HEADERS, timeout=_TIMEOUT)
            response.raise_for_status()
        except Exception as exc:
            raise LiveDiscoveryBlockedError(f"RemoteOK API unreachable: {exc}") from exc

        try:
            raw_data = response.json()
        except Exception as exc:
            raise LiveDiscoveryBlockedError(f"Invalid JSON from RemoteOK: {exc}") from exc

        if not isinstance(raw_data, list):
            raise LiveDiscoveryBlockedError("Unexpected RemoteOK response structure")

        jobs = [item for item in raw_data if isinstance(item, dict) and "position" in item]
        logger.info("RemoteOK: fetched %d raw listings", len(jobs))

        query_keywords = {q.lower() for q in queries}
        relevant_jobs: list[dict[str, Any]] = []

        for job in jobs:
            job_tags = {t.lower() for t in (job.get("tags") or [])}
            position_lower = (job.get("position") or "").lower()
            description_lower = (job.get("description") or "").lower()

            if self._is_relevant(job_tags, position_lower, description_lower, query_keywords):
                relevant_jobs.append(job)

        logger.info("RemoteOK: %d relevant jobs after filtering", len(relevant_jobs))
        return [self._normalize(job) for job in relevant_jobs[:limit_per_query * len(queries)]]

    def health_check(self) -> bool:
        try:
            import requests
            r = requests.get(_REMOTEOK_API, headers=_REQUEST_HEADERS, timeout=_TIMEOUT)
            return r.status_code == 200
        except Exception:
            return False

    def _is_relevant(
        self,
        job_tags: set[str],
        position_lower: str,
        description_lower: str,
        query_keywords: set[str],
    ) -> bool:
        if job_tags & _RELEVANT_TAGS:
            return True
        for kw in query_keywords:
            if kw in position_lower or kw in description_lower:
                return True
        agent_signals = ["agent", "agentic", "llm", "workflow", "automation", "ai engineer", "applied ai"]
        return any(s in position_lower for s in agent_signals)

    def _normalize(self, job: dict[str, Any]) -> RawJobListing:
        epoch = job.get("epoch")
        captured_at = datetime.now(timezone.utc)
        posted_at = job.get("date") or (
            datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat() if epoch else None
        )

        description = job.get("description") or ""
        tags = job.get("tags") or []
        tag_line = f"\nTags: {', '.join(tags)}" if tags else ""

        posting_text = (
            f"{job.get('position', '')}\n"
            f"{job.get('company', '')}\n"
            f"{job.get('location', 'Remote')}\n\n"
            f"{description}"
            f"{tag_line}"
        ).strip()

        salary_min = job.get("salary_min")
        salary_max = job.get("salary_max")
        salary_text: str | None = None
        if salary_min and salary_max:
            salary_text = f"${int(salary_min):,}–${int(salary_max):,}"
        elif salary_min:
            salary_text = f"${int(salary_min):,}+"

        source_url = job.get("url") or None
        apply_url = job.get("apply_url") or source_url

        return RawJobListing(
            source_name=self.source_name,
            source_url=source_url,
            application_url=apply_url,
            company=str(job.get("company", "Unknown")),
            title=str(job.get("position", "Unknown")),
            location=str(job.get("location", "Remote")),
            posting_text=posting_text,
            captured_at=captured_at,
            posted_at=posted_at,
            salary_text=salary_text,
            salary_min=float(salary_min) if salary_min else None,
            salary_max=float(salary_max) if salary_max else None,
            remote=True,  # RemoteOK is all remote jobs
            external_id=str(job.get("id", "")),
        )
