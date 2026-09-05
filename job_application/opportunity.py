"""Job opportunity model and durable store for Job Application Executor v0.1."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from revenue_bridge.events import _require_text, _require_timestamp
from tools.ai_controller._locking import FileLock


class RemoteStatus(str, Enum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ON_SITE = "on_site"
    UNKNOWN = "unknown"


class EmploymentType(str, Enum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class JobOpportunity:
    job_id: str
    source: str
    source_url: str | None
    application_url: str | None
    company: str
    title: str
    location: str
    remote_status: RemoteStatus
    employment_type: EmploymentType
    salary_text: str | None
    salary_min: float | None
    salary_max: float | None
    posting_text: str
    posting_hash: str
    captured_at: datetime
    requirements: tuple[str, ...]
    preferred_requirements: tuple[str, ...]
    responsibilities: tuple[str, ...]
    technologies: tuple[str, ...]
    experience_requirements: tuple[str, ...]
    education_requirements: tuple[str, ...]
    work_authorization_requirements: tuple[str, ...]
    application_deadline: str | None
    ats_provider: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_id", _require_text(self.job_id, "job_id"))
        object.__setattr__(self, "source", _require_text(self.source, "source"))
        object.__setattr__(self, "company", _require_text(self.company, "company"))
        object.__setattr__(self, "title", _require_text(self.title, "title"))
        object.__setattr__(self, "location", _require_text(self.location, "location"))
        object.__setattr__(self, "posting_text", _require_text(self.posting_text, "posting_text"))
        object.__setattr__(self, "posting_hash", _require_text(self.posting_hash, "posting_hash"))
        object.__setattr__(self, "captured_at", _require_timestamp(self.captured_at, "captured_at"))
        for field_name in (
            "requirements", "preferred_requirements", "responsibilities",
            "technologies", "experience_requirements", "education_requirements",
            "work_authorization_requirements",
        ):
            val = getattr(self, field_name)
            if not isinstance(val, tuple):
                object.__setattr__(self, field_name, tuple(val))
        if not isinstance(self.remote_status, RemoteStatus):
            object.__setattr__(self, "remote_status", RemoteStatus.UNKNOWN)
        if not isinstance(self.employment_type, EmploymentType):
            object.__setattr__(self, "employment_type", EmploymentType.UNKNOWN)

    @staticmethod
    def compute_posting_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "source": self.source,
            "source_url": self.source_url,
            "application_url": self.application_url,
            "company": self.company,
            "title": self.title,
            "location": self.location,
            "remote_status": self.remote_status.value,
            "employment_type": self.employment_type.value,
            "salary_text": self.salary_text,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "posting_text": self.posting_text,
            "posting_hash": self.posting_hash,
            "captured_at": self.captured_at.isoformat(),
            "requirements": list(self.requirements),
            "preferred_requirements": list(self.preferred_requirements),
            "responsibilities": list(self.responsibilities),
            "technologies": list(self.technologies),
            "experience_requirements": list(self.experience_requirements),
            "education_requirements": list(self.education_requirements),
            "work_authorization_requirements": list(self.work_authorization_requirements),
            "application_deadline": self.application_deadline,
            "ats_provider": self.ats_provider,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobOpportunity:
        captured_at = data["captured_at"]
        if isinstance(captured_at, str):
            captured_at = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        return cls(
            job_id=data["job_id"],
            source=data["source"],
            source_url=data.get("source_url"),
            application_url=data.get("application_url"),
            company=data["company"],
            title=data["title"],
            location=data["location"],
            remote_status=RemoteStatus(data.get("remote_status", "unknown")),
            employment_type=EmploymentType(data.get("employment_type", "unknown")),
            salary_text=data.get("salary_text"),
            salary_min=data.get("salary_min"),
            salary_max=data.get("salary_max"),
            posting_text=data["posting_text"],
            posting_hash=data["posting_hash"],
            captured_at=captured_at,
            requirements=tuple(data.get("requirements", [])),
            preferred_requirements=tuple(data.get("preferred_requirements", [])),
            responsibilities=tuple(data.get("responsibilities", [])),
            technologies=tuple(data.get("technologies", [])),
            experience_requirements=tuple(data.get("experience_requirements", [])),
            education_requirements=tuple(data.get("education_requirements", [])),
            work_authorization_requirements=tuple(data.get("work_authorization_requirements", [])),
            application_deadline=data.get("application_deadline"),
            ats_provider=data.get("ats_provider"),
        )


_TECH_KEYWORDS = [
    "python", "javascript", "typescript", "go", "rust", "java", "c++", "c#",
    "react", "vue", "angular", "node.js", "fastapi", "django", "flask",
    "postgresql", "mysql", "redis", "mongodb", "elasticsearch",
    "aws", "gcp", "azure", "docker", "kubernetes", "terraform",
    "llm", "openai", "anthropic", "langchain", "rag", "vector",
    "pytorch", "tensorflow", "scikit-learn",
]

_BULLET = re.compile(r"^[\-•*·]\s+(.+)$")


def _extract_section(text: str, keywords: list[str]) -> list[str]:
    lines = text.splitlines()
    capturing = False
    results: list[str] = []
    for line in lines:
        lower = line.lower().strip()
        if any(kw in lower for kw in keywords):
            capturing = True
        if capturing:
            m = _BULLET.match(line.strip())
            if m:
                results.append(m.group(1).strip())
    return results[:20]


def _extract_technologies(text: str) -> list[str]:
    lower = text.lower()
    return [t for t in _TECH_KEYWORDS if t in lower]


def ingest_from_text(
    posting_text: str,
    company: str,
    title: str,
    location: str = "Unknown",
    source_url: str | None = None,
    application_url: str | None = None,
    remote_status: RemoteStatus = RemoteStatus.UNKNOWN,
    employment_type: EmploymentType = EmploymentType.UNKNOWN,
    job_id: str | None = None,
    captured_at: datetime | None = None,
) -> JobOpportunity:
    """Create a JobOpportunity from supplied raw posting text."""
    now = captured_at or datetime.now(timezone.utc)
    jid = job_id or f"job_{uuid.uuid4().hex[:12]}"
    posting_hash = JobOpportunity.compute_posting_hash(posting_text)

    requirements = _extract_section(posting_text, ["requirements", "qualifications", "required"])
    preferred = _extract_section(posting_text, ["preferred", "nice to have", "bonus"])
    responsibilities = _extract_section(posting_text, ["responsibilities", "what you'll do", "duties"])
    technologies = _extract_technologies(posting_text)
    experience = _extract_section(posting_text, ["years of experience", "years experience"])
    education = _extract_section(posting_text, ["education", "degree", "bachelor", "master"])
    work_auth = _extract_section(posting_text, ["work authorization", "authorized to work", "sponsorship"])

    return JobOpportunity(
        job_id=jid,
        source="manual_text",
        source_url=source_url,
        application_url=application_url,
        company=company,
        title=title,
        location=location,
        remote_status=remote_status,
        employment_type=employment_type,
        salary_text=None,
        salary_min=None,
        salary_max=None,
        posting_text=posting_text,
        posting_hash=posting_hash,
        captured_at=now,
        requirements=tuple(requirements),
        preferred_requirements=tuple(preferred),
        responsibilities=tuple(responsibilities),
        technologies=tuple(technologies),
        experience_requirements=tuple(experience),
        education_requirements=tuple(education),
        work_authorization_requirements=tuple(work_auth),
        application_deadline=None,
        ats_provider=None,
    )


class OpportunityStoreCorruptionError(RuntimeError):
    """Opportunity store cannot be trusted or replayed."""


class DurableOpportunityStore:
    """Append-only JSONL store for JobOpportunity records."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def load_all(self) -> tuple[JobOpportunity, ...]:
        with FileLock(self.lock_path):
            return self._load_locked()

    def get(self, job_id: str) -> JobOpportunity | None:
        for opp in self.load_all():
            if opp.job_id == job_id:
                return opp
        return None

    def store(self, opportunity: JobOpportunity) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path):
            existing = self._load_locked()
            for opp in existing:
                if opp.job_id == opportunity.job_id:
                    raise ValueError(f"Opportunity {opportunity.job_id} already exists in store")
            self._append_locked({"record_type": "opportunity", "data": opportunity.to_dict()})

    def find_duplicate(self, opportunity: JobOpportunity) -> JobOpportunity | None:
        """Search for a likely duplicate using hash, company+title, or URL."""
        for existing in self.load_all():
            if existing.posting_hash == opportunity.posting_hash:
                return existing
            if (
                existing.company.lower() == opportunity.company.lower()
                and existing.title.lower() == opportunity.title.lower()
            ):
                return existing
            if (
                existing.application_url
                and opportunity.application_url
                and existing.application_url == opportunity.application_url
            ):
                return existing
        return None

    def _load_locked(self) -> tuple[JobOpportunity, ...]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return ()
        return self._replay(raw)

    def _replay(self, raw: bytes) -> tuple[JobOpportunity, ...]:
        if raw and not raw.endswith(b"\n"):
            raise OpportunityStoreCorruptionError(f"Incomplete final record: {self.path}")
        text = raw.decode("utf-8")
        opportunities: list[JobOpportunity] = []
        seen_ids: set[str] = set()
        for lineno, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                raise OpportunityStoreCorruptionError(f"Empty record at line {lineno}: {self.path}")
            try:
                data = json.loads(line)
                if data.get("record_type") != "opportunity":
                    raise ValueError(f"Unknown record_type: {data.get('record_type')!r}")
                opp = JobOpportunity.from_dict(data["data"])
                if opp.job_id in seen_ids:
                    raise ValueError(f"Duplicate job_id: {opp.job_id}")
                seen_ids.add(opp.job_id)
                opportunities.append(opp)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise OpportunityStoreCorruptionError(
                    f"Malformed record at line {lineno}: {exc}"
                ) from exc
        return tuple(opportunities)

    def _append_locked(self, record: dict[str, Any]) -> None:
        line = (
            json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
            + "\n"
        )
        with self.path.open("ab") as handle:
            handle.write(line.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
