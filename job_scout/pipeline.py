"""Scout pipeline for MissionaryX Job Scout v0.1.

Orchestrates the full discovery → scoring → ranking → persistence flow.

Steps:
1. Generate queries from archetype
2. Fetch raw listings from sources
3. Deduplicate
4. Normalize to JobOpportunity
5. Score: Rootstock similarity + candidate fit
6. Classify: nontraditional match type + requirements
7. Filter by threshold
8. Rank
9. Persist to DurableScoutFeed
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from job_application.candidate import CandidateProfile
from job_application.opportunity import (
    JobOpportunity,
    RemoteStatus,
    EmploymentType,
    ingest_from_text,
)
from job_scout.archetype import ArchetypeProfile, rootstock_archetype
from job_scout.candidate_fit import ScoutFitAssessor
from job_scout.classifier import NontraditionalMatchClassifier
from job_scout.deduplication import OpportunityDeduplicator
from job_scout.feed import DurableScoutFeed
from job_scout.models import (
    AvailabilityStatus,
    ScoredOpportunity,
    ScoutState,
    _scout_state_from_classification,
)
from job_scout.requirements import RequirementClassifier
from job_scout.similarity import RootstockSimilarityScorer
from job_scout.sources.base import JobSource, RawJobListing

logger = logging.getLogger(__name__)

_STALE_DAYS = 60


@dataclass
class ScoutConfig:
    rootstock_similarity_threshold: int = 55
    candidate_fit_threshold: int = 45
    max_results: int = 20
    reject_hard_blockers: bool = True
    include_weak_matches: bool = False


@dataclass
class ScoutRunReport:
    queries_used: list[str]
    sources_used: list[str]
    raw_fetched: int
    after_dedup: int
    scored: int
    above_threshold: int
    rejected_hard_blockers: int
    rejected_below_threshold: int
    persisted: int
    top_opportunities: list[ScoredOpportunity]
    live_discovery_status: str
    errors: list[str]


_ARCHETYPE_QUERIES = [
    "agent engineer",
    "applied AI engineer",
    "AI agent",
    "agentic systems",
    "AI workflow",
    "AI automation",
    "AI evaluation",
    "LLM evals",
    "agent reliability",
    "AI tooling",
    "developer tools AI",
    "tool calling",
    "AI implementation",
    "AI integration",
    "MCP",
    "Codex",
]


def _raw_to_opportunity(raw: RawJobListing) -> JobOpportunity:
    """Normalize a RawJobListing to JobOpportunity."""
    if raw.remote is True:
        remote_status = RemoteStatus.REMOTE
    elif raw.remote is False:
        remote_status = RemoteStatus.ON_SITE
    else:
        remote_text = raw.location.lower()
        if "remote" in remote_text:
            remote_status = RemoteStatus.REMOTE
        elif "hybrid" in remote_text:
            remote_status = RemoteStatus.HYBRID
        else:
            remote_status = RemoteStatus.UNKNOWN

    posting_hash = JobOpportunity.compute_posting_hash(raw.posting_text)
    job_id = f"scout_{uuid.uuid5(uuid.NAMESPACE_URL, raw.posting_text[:200]).hex[:12]}"

    return ingest_from_text(
        posting_text=raw.posting_text,
        company=raw.company,
        title=raw.title,
        location=raw.location or "Remote",
        source_url=raw.source_url,
        application_url=raw.application_url,
        remote_status=remote_status,
        job_id=job_id,
        captured_at=raw.captured_at,
    )


def _availability_status(raw: RawJobListing) -> AvailabilityStatus:
    now = datetime.now(timezone.utc)
    age_days = (now - raw.captured_at).days
    if age_days > _STALE_DAYS:
        return AvailabilityStatus.STALE
    if raw.posted_at:
        return AvailabilityStatus.UNVERIFIED_CURRENT
    return AvailabilityStatus.UNKNOWN


class ScoutPipeline:
    """Orchestrates the full job opportunity discovery and scoring flow."""

    def __init__(
        self,
        sources: list[JobSource],
        feed: DurableScoutFeed,
        profile: CandidateProfile,
        archetype: ArchetypeProfile | None = None,
        config: ScoutConfig | None = None,
    ) -> None:
        self.sources = sources
        self.feed = feed
        self.profile = profile
        self.archetype = archetype or rootstock_archetype()
        self.config = config or ScoutConfig()
        self._similarity_scorer = RootstockSimilarityScorer()
        self._fit_assessor = ScoutFitAssessor()
        self._classifier = NontraditionalMatchClassifier()
        self._req_classifier = RequirementClassifier()
        self._deduplicator = OpportunityDeduplicator()

    def run(self, queries: list[str] | None = None) -> ScoutRunReport:
        queries = queries or _ARCHETYPE_QUERIES
        errors: list[str] = []
        live_status = "NOT_ATTEMPTED"

        # 1. Fetch raw listings from all sources
        all_raw: list[RawJobListing] = []
        sources_used: list[str] = []
        for source in self.sources:
            try:
                listings = source.fetch(queries)
                all_raw.extend(listings)
                sources_used.append(source.source_name)
                if source.source_name != "fixture":
                    live_status = "ATTEMPTED"
            except Exception as exc:
                error_msg = f"{source.source_name}: {exc}"
                errors.append(error_msg)
                logger.warning("Source error: %s", error_msg)
                if source.source_name != "fixture":
                    live_status = f"BLOCKED: {exc}"

        raw_count = len(all_raw)
        logger.info("Fetched %d raw listings from %d source(s)", raw_count, len(sources_used))

        # 2. Deduplicate
        dedup_result = self._deduplicator.deduplicate(all_raw)
        unique_raw = dedup_result.listings
        logger.info(
            "After deduplication: %d unique (%d duplicates removed)",
            len(unique_raw), dedup_result.duplicate_count,
        )

        # 3. Normalize, score, and classify
        scored_opps: list[ScoredOpportunity] = []
        rejected_hard_blockers = 0
        rejected_threshold = 0

        for raw in unique_raw:
            try:
                opp = _raw_to_opportunity(raw)
                similarity = self._similarity_scorer.score(opp)
                fit = self._fit_assessor.assess(opp, self.profile)
                req_classifications = self._req_classifier.classify_all(opp)
                hard_blockers = self._req_classifier.hard_blockers(req_classifications)

                # Also include hard blockers from education gap
                if fit.education_gap_penalty.is_hard_blocker:
                    hard_blockers = hard_blockers + ("Potential mandatory degree requirement",)

                match_result = self._classifier.classify(similarity, fit, hard_blockers)

                avail = _availability_status(raw)
                scout_state = _scout_state_from_classification(match_result)
                if avail == AvailabilityStatus.STALE:
                    scout_state = ScoutState.STALE

                # Filter hard blockers
                if self.config.reject_hard_blockers and hard_blockers:
                    rejected_hard_blockers += 1
                    scout_state = ScoutState.SKIP
                    if not self.config.include_weak_matches:
                        continue

                # Filter by threshold
                below_threshold = (
                    similarity.total < self.config.rootstock_similarity_threshold
                    or fit.total < self.config.candidate_fit_threshold
                )
                if below_threshold:
                    rejected_threshold += 1
                    if not self.config.include_weak_matches:
                        continue

                scored = ScoredOpportunity(
                    opportunity=opp,
                    rootstock_similarity=similarity,
                    candidate_fit=fit,
                    match_result=match_result,
                    requirement_classifications=req_classifications,
                    scout_state=scout_state,
                    discovered_at=raw.captured_at,
                    availability_status=avail,
                    source_provenance=(raw.source_name,),
                    posted_at=raw.posted_at,
                    last_verified_at=None,
                    executor_application_id=None,
                )
                scored_opps.append(scored)
            except Exception as exc:
                errors.append(f"Scoring error for '{raw.title}' @ {raw.company}: {exc}")
                logger.warning("Scoring error", exc_info=exc)

        # 4. Rank by (rootstock_similarity, candidate_fit) descending
        scored_opps.sort(key=lambda s: s.score_key, reverse=True)
        top = scored_opps[: self.config.max_results]

        # 5. Persist to feed
        persisted = 0
        existing = {s.job_id for s in self.feed.load_all()}
        for scored in top:
            if scored.job_id not in existing:
                try:
                    self.feed.store(scored)
                    persisted += 1
                except Exception as exc:
                    errors.append(f"Persist error for {scored.job_id}: {exc}")

        return ScoutRunReport(
            queries_used=queries,
            sources_used=sources_used,
            raw_fetched=raw_count,
            after_dedup=len(unique_raw),
            scored=len(scored_opps),
            above_threshold=len(scored_opps),
            rejected_hard_blockers=rejected_hard_blockers,
            rejected_below_threshold=rejected_threshold,
            persisted=persisted,
            top_opportunities=top[:10],
            live_discovery_status=live_status,
            errors=errors,
        )
