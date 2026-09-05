"""Executor handoff for MissionaryX Job Scout v0.1.

Transfers a ScoredOpportunity from the Scout to the Job Application Executor.
The opportunity's JobOpportunity is ingested into the Executor's ledger.
The Scout feed is updated to HANDED_TO_EXECUTOR state.

The user does not need to copy/paste job details.
All Scout evidence (similarity, fit, evidence recommendations) is preserved.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from job_application.executor import JobApplicationExecutor, DuplicateApplicationError
from job_application.ledger import ApplicationRecord
from job_scout.feed import DurableScoutFeed
from job_scout.models import ScoredOpportunity, ScoutState


@dataclass
class HandoffResult:
    job_id: str
    application_id: str
    success: bool
    error: str | None
    was_duplicate: bool
    existing_application_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "application_id": self.application_id,
            "success": self.success,
            "error": self.error,
            "was_duplicate": self.was_duplicate,
            "existing_application_id": self.existing_application_id,
        }


class ExecutorHandoff:
    """Transfers a ScoredOpportunity to the Job Application Executor."""

    def __init__(self, executor: JobApplicationExecutor, feed: DurableScoutFeed) -> None:
        self.executor = executor
        self.feed = feed

    def hand_to_executor(self, scored: ScoredOpportunity) -> HandoffResult:
        """Ingest opportunity into Executor and mark Scout state HANDED_TO_EXECUTOR."""
        try:
            record: ApplicationRecord = self.executor.ingest_opportunity(scored.opportunity)
            self.feed.update_state(scored.job_id, ScoutState.HANDED_TO_EXECUTOR)
            self.feed.update_executor_id(scored.job_id, record.application_id)
            return HandoffResult(
                job_id=scored.job_id,
                application_id=record.application_id,
                success=True,
                error=None,
                was_duplicate=False,
                existing_application_id=None,
            )
        except DuplicateApplicationError as exc:
            return HandoffResult(
                job_id=scored.job_id,
                application_id=exc.existing_application_id,
                success=False,
                error=f"Duplicate application already exists: {exc.existing_application_id}",
                was_duplicate=True,
                existing_application_id=exc.existing_application_id,
            )
        except Exception as exc:
            return HandoffResult(
                job_id=scored.job_id,
                application_id="",
                success=False,
                error=str(exc),
                was_duplicate=False,
                existing_application_id=None,
            )
