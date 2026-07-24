from pathlib import Path

from sports.data.repositories.sqlite_autonomy_repository import (
    SQLiteAutonomyRepository,
)
from sports.data.repositories.sqlite_player_game_log_repository import (
    SQLitePlayerGameLogRepository,
)
from sports.intelligence.autonomy_service import AutonomyService
from sports.intelligence.player_detail_service import PlayerDetailService
from sports.intelligence.player_intelligence_service import (
    PlayerIntelligenceService,
)

from tests.test_advanced_basketball_intelligence import log


def test_player_detail_contains_history_chart_comparisons_and_evidence(
    tmp_path: Path,
) -> None:
    repository = SQLitePlayerGameLogRepository(tmp_path / "basketball.db")
    repository.save_many(
        [
            log("2024-25", 1, 10),
            log("2025-26", 2, 20),
            log("2025-26", 3, 25, competition="playoffs"),
        ]
    )

    detail = PlayerDetailService(repository).get(
        "p1",
        ["2024-25", "2025-26"],
    )

    assert detail is not None
    assert len(detail["games"]) == 3
    assert len(detail["trend_chart"]) == 3
    assert "NBA:regular" in detail["intelligence"].profiles
    assert detail["explanation"]
    assert detail["evidence"]


def test_watch_alert_outcome_and_research_lifecycle_are_durable(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "basketball.db"
    repository = SQLiteAutonomyRepository(database_path)
    service = AutonomyService(repository)
    watch = service.add_watch("player", "p1", "minutes_gain")
    rows = [
        log("2025-26", 1, 10, minutes=10),
        log("2025-26", 2, 12, minutes=12),
        log("2025-26", 3, 20, minutes=25),
        log("2025-26", 4, 22, minutes=28),
    ]
    player = PlayerIntelligenceService().analyze(
        "p1",
        rows,
        ["2025-26"],
    )
    refresh_calls: list[bool] = []

    alerts = service.run_once(
        lambda: refresh_calls.append(True),
        lambda: [player] if player is not None else [],
    )

    assert watch.id is not None
    assert refresh_calls == [True]
    assert len(alerts) == 1
    assert repository.list_alerts() == alerts

    outcome = service.evaluate(
        alerts[0],
        later_value=25,
        later_minutes=30,
        notes="Role held for three games.",
    )
    assert outcome.correct is True
    assert repository.get_outcome(int(alerts[0].id or 0)) == outcome

    lifecycle = service.start_research(
        "Bench player gained eight minutes.",
        "Extra minutes may lift production for three games.",
        "Compare the next three games with the prior baseline.",
    )
    learned = service.learn(
        lifecycle,
        "Production remained above baseline.",
        "Sustained minutes gains were predictive in this case.",
    )
    assert learned.id == lifecycle.id
    assert learned.learned_knowledge is not None
