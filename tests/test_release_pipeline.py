from datetime import datetime, timezone
from pathlib import Path

from sports.data.repositories.sqlite_intelligence_store import (
    SQLiteIntelligenceStore,
)
from sports.data.services.release_ingestion_pipeline import (
    ReleaseIngestionPipeline,
)


NOW = datetime(2026, 7, 24, 18, tzinfo=timezone.utc)


class FakeBulkProvider:
    name = "fixture-provider"

    def __init__(self, rows, failures: int = 0):
        self.rows = rows
        self.failures = failures
        self.calls = []

    def fetch_changed(self, dataset: str, since: str | None):
        self.calls.append((dataset, since))
        if self.failures:
            self.failures -= 1
            raise TimeoutError("temporary provider timeout")
        return list(self.rows)


def test_release_pipeline_bulk_upserts_deduplicates_and_refreshes_changed_rows(
    tmp_path: Path,
) -> None:
    store = SQLiteIntelligenceStore(tmp_path / "intelligence.db")
    provider = FakeBulkProvider(
        [
            {
                "canonical_id": "nba:player:p1",
                "player_name": "Player One",
                "team_id": "nba:team:chi",
                "updated_at": "2026-07-24T17:00:00+00:00",
            },
            {
                "canonical_id": "nba:player:p1",
                "player_name": "Player One",
                "team_id": "nba:team:chi",
                "updated_at": "2026-07-24T17:00:00+00:00",
            },
        ]
    )
    pipeline = ReleaseIngestionPipeline(
        store,
        providers={"players": provider},
        required_datasets=("players",),
        retry_limit=1,
    )

    first = pipeline.refresh(now=NOW)
    second = pipeline.refresh(now=NOW)

    assert first.status == "healthy"
    assert first.datasets["players"].received == 2
    assert first.datasets["players"].deduplicated == 1
    assert first.datasets["players"].changed == 1
    assert second.datasets["players"].changed == 0
    assert len(store.list_records("players")) == 1
    assert provider.calls[0][1] is None
    assert provider.calls[1][1] == NOW.isoformat()


def test_pipeline_retries_temporary_failure_and_preserves_previous_snapshot(
    tmp_path: Path,
) -> None:
    store = SQLiteIntelligenceStore(tmp_path / "intelligence.db")
    initial = FakeBulkProvider(
        [{"canonical_id": "nba:injury:p1", "status": "questionable"}]
    )
    ReleaseIngestionPipeline(
        store,
        providers={"injuries": initial},
        required_datasets=("injuries",),
    ).refresh(now=NOW)

    failing = FakeBulkProvider([], failures=5)
    result = ReleaseIngestionPipeline(
        store,
        providers={"injuries": failing},
        required_datasets=("injuries",),
        retry_limit=2,
    ).refresh(now=NOW)

    assert result.status == "degraded"
    assert result.datasets["injuries"].status == "failed_cached"
    assert result.datasets["injuries"].attempts == 3
    assert result.datasets["injuries"].cached_records == 1
    assert store.list_records("injuries")[0]["status"] == "questionable"
    health = store.source_health()
    assert health["injuries"]["last_successful_refresh"]
    assert health["injuries"]["last_error"] == "temporary provider timeout"


def test_pipeline_reports_missing_required_dataset_without_inventing_rows(
    tmp_path: Path,
) -> None:
    store = SQLiteIntelligenceStore(tmp_path / "intelligence.db")
    result = ReleaseIngestionPipeline(
        store,
        providers={},
        required_datasets=(
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
        ),
    ).refresh(now=NOW)

    assert result.status == "degraded"
    assert all(
        item.status == "unconfigured" for item in result.datasets.values()
    )
    assert store.total_records() == 0
