from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from sports.data.repositories.sqlite_intelligence_store import (
    SQLiteIntelligenceStore,
)


class BulkDatasetProvider(Protocol):
    name: str

    def fetch_changed(
        self, dataset: str, since: str | None
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class DatasetRefreshResult:
    dataset: str
    provider: str | None
    status: str
    attempts: int
    received: int
    deduplicated: int
    changed: int
    cached_records: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PipelineRefreshResult:
    status: str
    refreshed_at: str
    datasets: dict[str, DatasetRefreshResult]


class ReleaseIngestionPipeline:
    REQUIRED_DATASETS = (
        "teams",
        "players",
        "schedules",
        "rosters",
        "lineups",
        "injuries",
        "game_logs",
        "player_statistics",
        "sportsbook_markets",
        "game_results",
    )

    def __init__(
        self,
        store: SQLiteIntelligenceStore,
        *,
        providers: dict[str, BulkDatasetProvider],
        required_datasets: tuple[str, ...] | None = None,
        retry_limit: int = 2,
    ) -> None:
        self.store = store
        self.providers = dict(providers)
        self.required_datasets = required_datasets or self.REQUIRED_DATASETS
        self.retry_limit = max(0, retry_limit)

    def refresh(
        self, *, now: datetime | None = None
    ) -> PipelineRefreshResult:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        timestamp = current.isoformat()
        results: dict[str, DatasetRefreshResult] = {}
        for dataset in self.required_datasets:
            provider = self.providers.get(dataset)
            if provider is None:
                self.store.record_state(
                    dataset,
                    provider=None,
                    status="unconfigured",
                    attempted_at=timestamp,
                    successful_at=None,
                    error="No provider configured.",
                    received=0,
                    changed=0,
                )
                results[dataset] = DatasetRefreshResult(
                    dataset=dataset,
                    provider=None,
                    status="unconfigured",
                    attempts=0,
                    received=0,
                    deduplicated=0,
                    changed=0,
                    cached_records=self.store.count_records(dataset),
                    error="No provider configured.",
                )
                continue
            attempts = 0
            error: Exception | None = None
            rows: list[dict[str, Any]] | None = None
            while attempts <= self.retry_limit:
                attempts += 1
                try:
                    rows = provider.fetch_changed(
                        dataset,
                        self.store.last_successful_refresh(dataset),
                    )
                    error = None
                    break
                except (TimeoutError, ConnectionError) as caught:
                    error = caught
            if rows is None:
                cached = self.store.count_records(dataset)
                status = "failed_cached" if cached else "failed"
                message = str(error or "Provider refresh failed.")
                self.store.record_state(
                    dataset,
                    provider=provider.name,
                    status=status,
                    attempted_at=timestamp,
                    successful_at=None,
                    error=message,
                    received=0,
                    changed=0,
                )
                results[dataset] = DatasetRefreshResult(
                    dataset=dataset,
                    provider=provider.name,
                    status=status,
                    attempts=attempts,
                    received=0,
                    deduplicated=0,
                    changed=0,
                    cached_records=cached,
                    error=message,
                )
                continue
            try:
                received, deduplicated, changed = self.store.upsert_records(
                    dataset,
                    rows,
                    provider=provider.name,
                    retrieved_at=timestamp,
                )
                status = "healthy" if rows else "empty"
                self.store.record_state(
                    dataset,
                    provider=provider.name,
                    status=status,
                    attempted_at=timestamp,
                    successful_at=timestamp,
                    error=None,
                    received=received,
                    changed=changed,
                )
                results[dataset] = DatasetRefreshResult(
                    dataset=dataset,
                    provider=provider.name,
                    status=status,
                    attempts=attempts,
                    received=received,
                    deduplicated=deduplicated,
                    changed=changed,
                    cached_records=self.store.count_records(dataset),
                )
            except (TypeError, ValueError) as caught:
                message = str(caught)
                cached = self.store.count_records(dataset)
                status = "failed_cached" if cached else "failed"
                self.store.record_state(
                    dataset,
                    provider=provider.name,
                    status=status,
                    attempted_at=timestamp,
                    successful_at=None,
                    error=message,
                    received=len(rows),
                    changed=0,
                )
                results[dataset] = DatasetRefreshResult(
                    dataset=dataset,
                    provider=provider.name,
                    status=status,
                    attempts=attempts,
                    received=len(rows),
                    deduplicated=0,
                    changed=0,
                    cached_records=cached,
                    error=message,
                )
        overall = (
            "healthy"
            if all(item.status == "healthy" for item in results.values())
            else "degraded"
        )
        return PipelineRefreshResult(overall, timestamp, results)

