from pathlib import Path

import pytest

from sports.data.repositories.sqlite_autonomy_repository import (
    SQLiteAutonomyRepository,
)
from sports.intelligence.autonomy_service import AutonomyService
from sports.intelligence.learning_intelligence_service import (
    LearningIntelligenceService,
)
from sports.intelligence.player_intelligence_service import (
    PlayerIntelligenceService,
)
from tests.test_advanced_basketball_intelligence import log


def _seed_alert_outcomes(
    repository: SQLiteAutonomyRepository,
) -> None:
    autonomy = AutonomyService(repository)
    autonomy.add_watch("player", "p1", "minutes_gain")

    rows = [
        log("2025-26", 1, 10, minutes=10),
        log("2025-26", 2, 12, minutes=12),
        log("2025-26", 3, 20, minutes=26),
        log("2025-26", 4, 22, minutes=28),
    ]
    player = PlayerIntelligenceService().analyze("p1", rows, ["2025-26"])
    alerts = autonomy.run_once(
        lambda: None,
        lambda: [player] if player is not None else [],
    )
    assert alerts

    autonomy.evaluate(
        alerts[0],
        later_value=24,
        later_minutes=27,
        notes="Held role for two games.",
    )

    lifecycle = autonomy.start_research(
        "Minutes increased by 8 over baseline.",
        "Minutes spikes often hold for at least two games.",
        "Track next two games after spike.",
    )
    autonomy.learn(
        lifecycle,
        "Production remained above baseline in both games.",
        "Minutes spikes were predictive for this profile.",
    )


def test_evaluate_past_alerts_returns_metrics(tmp_path: Path) -> None:
    repository = SQLiteAutonomyRepository(tmp_path / "autonomy.db")
    _seed_alert_outcomes(repository)

    service = LearningIntelligenceService(repository)
    summary = service.evaluate_past_alerts()

    assert summary["evaluated_alerts"] == 1
    assert summary["accuracy"] == pytest.approx(1.0)
    assert summary["avg_confidence"] > 0
    assert summary["avg_confidence_error"] <= 1
    assert "minutes_gain" in summary["by_signal"]


def test_calibrate_confidence_and_error(tmp_path: Path) -> None:
    repository = SQLiteAutonomyRepository(tmp_path / "autonomy.db")
    _seed_alert_outcomes(repository)

    service = LearningIntelligenceService(repository)
    buckets = service.calibrate_confidence(bucket_count=4)

    assert buckets
    assert all(0 <= bucket.lower < bucket.upper <= 1 for bucket in buckets)
    assert all(bucket.count >= 1 for bucket in buckets)
    calibration_error = service.confidence_calibration_error(bucket_count=4)
    assert 0 <= calibration_error <= 1


def test_propose_hypothesis_updates_and_reusable_knowledge(
    tmp_path: Path,
) -> None:
    repository = SQLiteAutonomyRepository(tmp_path / "autonomy.db")
    _seed_alert_outcomes(repository)

    service = LearningIntelligenceService(repository)
    updates = service.propose_hypothesis_updates()

    assert updates
    assert updates[0].status in {"strengthen", "monitor", "revise", "pending"}
    assert 0 <= updates[0].support_rate <= 1

    knowledge = service.build_reusable_knowledge(limit=5)
    assert knowledge
    assert any("minutes" in item.lower() for item in knowledge)


def test_update_hypothesis_text_persists_tag(tmp_path: Path) -> None:
    repository = SQLiteAutonomyRepository(tmp_path / "autonomy.db")
    autonomy = AutonomyService(repository)
    lifecycle = autonomy.start_research(
        "Observation",
        "Players improve in expanded roles.",
        "Compare next three games.",
    )

    service = LearningIntelligenceService(repository)
    updated = service.update_hypothesis_text(lifecycle, confidence_score=0.8)

    assert updated.id == lifecycle.id
    assert updated.hypothesis.startswith("[high-confidence]")
    stored = repository.list_lifecycles()
    assert stored
    assert stored[0].hypothesis.startswith("[high-confidence]")
