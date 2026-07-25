from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sports.personal.repository import PersonalEditionRepository


LOGGER = logging.getLogger("sip.scheduler")


class PersonalEditionScheduler:
    def __init__(
        self,
        task: Callable[[], Any],
        repository: PersonalEditionRepository,
        *,
        interval_seconds: int = 900,
        retry_limit: int = 2,
    ) -> None:
        self.task = task
        self.repository = repository
        self.interval = timedelta(seconds=interval_seconds)
        self.retry_limit = retry_limit
        self._lock = threading.Lock()
        self.last_run_at: datetime | None = None
        self.next_run_at: datetime | None = None

    def run_pending(
        self, *, now: datetime | None = None, force: bool = False
    ) -> dict[str, Any]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if not force and self.next_run_at and current < self.next_run_at:
            return {"status": "not_due", "next_run_at": self.next_run_at.isoformat()}
        if not self._lock.acquire(blocking=False):
            return {"status": "already_running", "next_run_at": self._next_text()}
        job_id = self.repository.start_job("personal_refresh", current.isoformat())
        attempts = 0
        error = None
        try:
            while attempts <= self.retry_limit:
                attempts += 1
                try:
                    result = self.task()
                    error = None
                    break
                except (TimeoutError, ConnectionError) as caught:
                    error = caught
            if error is not None:
                raise error
            self.last_run_at = current
            self.next_run_at = current + self.interval
            self.repository.finish_job(
                job_id,
                finished_at=datetime.now(timezone.utc).isoformat(),
                status="succeeded",
                attempts=attempts,
                error=None,
            )
            LOGGER.info("scheduler_complete", extra={"attempts": attempts})
            return {
                "status": "succeeded",
                "attempts": attempts,
                "next_run_at": self._next_text(),
                "result": result,
            }
        except Exception as caught:
            message = f"{type(caught).__name__}: {caught}"
            self.next_run_at = current + self.interval
            self.repository.finish_job(
                job_id,
                finished_at=datetime.now(timezone.utc).isoformat(),
                status="failed",
                attempts=attempts,
                error=message[:500],
            )
            LOGGER.error("scheduler_failed", extra={"error_type": type(caught).__name__})
            return {
                "status": "failed",
                "attempts": attempts,
                "next_run_at": self._next_text(),
                "error": message,
            }
        finally:
            self._lock.release()

    def status(self) -> dict[str, Any]:
        return {
            "running": self._lock.locked(),
            "last_run_at": (
                self.last_run_at.isoformat() if self.last_run_at else None
            ),
            "next_run_at": self._next_text(),
            "recent_jobs": self.repository.recent_jobs(),
        }

    def _next_text(self) -> str | None:
        return self.next_run_at.isoformat() if self.next_run_at else None
