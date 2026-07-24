from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any


class BasketballScheduler:
    """Small embeddable scheduler for the demo autonomy loop."""

    def __init__(
        self,
        task: Callable[[], Any],
        interval: timedelta = timedelta(hours=6),
    ) -> None:
        if interval.total_seconds() <= 0:
            raise ValueError("interval must be positive")
        self.task = task
        self.interval = interval
        self.last_run_at: datetime | None = None
        self.next_run_at: datetime | None = None

    def run_pending(
        self,
        now: datetime | None = None,
    ) -> Any | None:
        current = now or datetime.now(timezone.utc)
        if self.next_run_at is not None and current < self.next_run_at:
            return None
        result = self.task()
        self.last_run_at = current
        self.next_run_at = current + self.interval
        return result
