from pathlib import Path

import pytest

from sports.data.repositories.sqlite_lifecycle_repository import (
    SQLiteLifecycleRepository,
)
from sports.intelligence.lifecycle_service import IntelligenceLifecycleService
from sports.intelligence.models.lifecycle import LifecycleStage
from app import create_app


def test_situation_lifecycle_is_rebuilt_from_append_only_events(
    tmp_path: Path,
) -> None:
    repository = SQLiteLifecycleRepository(tmp_path / "lifecycle.db")
    service = IntelligenceLifecycleService(repository)

    situation = service.create_situation(
        situation_id="nba:p1:2026-07-24",
        title="Backup guard opportunity",
        objective="Determine whether the player will exceed 18.5 points.",
        observed_at="2026-07-24T10:02:00+00:00",
        observation="Starting guard was ruled out.",
    )
    service.record_evidence(
        situation.id,
        evidence={
            "id": "availability-1",
            "claim": "Starting guard is unavailable.",
            "source": "Official team report",
            "url": "https://example.com/team-report",
            "retrieved_at": "2026-07-24T10:04:00+00:00",
            "reliability": 0.95,
            "freshness": "current",
            "source_type": "primary",
        },
        occurred_at="2026-07-24T10:04:00+00:00",
    )
    service.record_evidence_gap(
        situation.id,
        "Confirmed starting lineup is not available yet.",
        occurred_at="2026-07-24T10:05:00+00:00",
    )
    service.add_hypothesis(
        situation.id,
        {
            "id": "usage-shift",
            "explanation": "Backup guard receives more minutes and shot attempts.",
            "confidence": 0.68,
            "falsification_criteria": ["Player remains outside starting lineup."],
        },
        occurred_at="2026-07-24T10:06:00+00:00",
    )
    service.add_forecast(
        situation.id,
        {
            "id": "points-over",
            "predicted_event": "Backup guard exceeds 18.5 points.",
            "probability": 0.7,
            "baseline_probability": 0.46,
            "horizon": "next game",
            "model_version": "player-opportunity-v1",
        },
        occurred_at="2026-07-24T10:08:00+00:00",
    )
    service.add_recommended_action(
        situation.id,
        "Monitor the confirmed lineup and compare complete sportsbook quotes.",
        occurred_at="2026-07-24T10:09:00+00:00",
    )
    service.add_monitoring_rule(
        situation.id,
        {
            "signal": "player points line",
            "condition": "moves by at least 1.0 point",
            "source": "normalized sportsbook feed",
        },
        occurred_at="2026-07-24T10:10:00+00:00",
    )

    restored = service.get_situation(situation.id)

    assert restored is not None
    assert restored.current_stage == LifecycleStage.MONITOR
    assert restored.intelligence_objective.startswith("Determine")
    assert restored.new_observations == ["Starting guard was ruled out."]
    assert restored.evidence_collected[0]["source_type"] == "primary"
    assert restored.evidence_gaps
    assert restored.active_hypotheses[0]["id"] == "usage-shift"
    assert restored.forecasts[0]["probability"] == pytest.approx(0.7)
    assert restored.recommended_actions
    assert restored.monitoring_rules
    assert restored.final_outcome is None
    assert restored.lessons_learned == []

    events = repository.list_events(situation.id)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert [event.occurred_at for event in events] == sorted(
        event.occurred_at for event in events
    )
    assert events[0].event_type == "situation_created"
    assert events[-1].stage == LifecycleStage.MONITOR


def test_outcome_and_lesson_complete_the_learning_loop(tmp_path: Path) -> None:
    service = IntelligenceLifecycleService(
        SQLiteLifecycleRepository(tmp_path / "lifecycle.db")
    )
    situation = service.create_situation(
        situation_id="nba:p2:game-1",
        title="Player points forecast",
        objective="Evaluate a measurable points forecast.",
        observed_at="2026-07-24T10:00:00+00:00",
        observation="Minutes projection increased.",
    )
    service.record_outcome(
        situation.id,
        {
            "occurred": True,
            "actual_value": 22,
            "forecast_id": "points-over",
            "notes": "Player scored 22 points.",
        },
        occurred_at="2026-07-24T23:15:00+00:00",
    )
    service.record_lesson(
        situation.id,
        "Stable prior rotation minutes made the opportunity more reliable.",
        occurred_at="2026-07-24T23:17:00+00:00",
    )

    restored = service.get_situation(situation.id)

    assert restored is not None
    assert restored.current_stage == LifecycleStage.LEARN
    assert restored.final_outcome["actual_value"] == 22
    assert restored.lessons_learned == [
        "Stable prior rotation minutes made the opportunity more reliable."
    ]
    assert service.timeline(situation.id)[-1]["description"].startswith(
        "Lesson recorded"
    )


def test_events_cannot_be_replaced_and_probabilities_are_validated(
    tmp_path: Path,
) -> None:
    repository = SQLiteLifecycleRepository(tmp_path / "lifecycle.db")
    service = IntelligenceLifecycleService(repository)
    situation = service.create_situation(
        situation_id="nba:p3:game-1",
        title="Forecast validation",
        objective="Keep forecasts measurable.",
        observed_at="2026-07-24T10:00:00+00:00",
        observation="Role changed.",
    )

    with pytest.raises(ValueError, match="between 0 and 1"):
        service.add_forecast(
            situation.id,
            {
                "predicted_event": "Player starts.",
                "probability": 1.2,
                "baseline_probability": 0.5,
                "horizon": "next game",
                "model_version": "v1",
            },
        )

    first_event = repository.list_events(situation.id)[0]
    with pytest.raises(NotImplementedError, match="append-only"):
        repository.replace_event(first_event)


def test_claim_graph_traces_source_to_outcome(tmp_path: Path) -> None:
    service = IntelligenceLifecycleService(
        SQLiteLifecycleRepository(tmp_path / "lifecycle.db")
    )
    situation = service.create_situation(
        situation_id="nba:p4:game-1",
        title="Traceable player forecast",
        objective="Trace every conclusion to evidence.",
        observed_at="2026-07-24T10:00:00+00:00",
        observation="Player availability changed.",
    )
    service.record_evidence(
        situation.id,
        {
            "id": "e1",
            "claim": "Starter is officially unavailable.",
            "source": "Official injury report",
            "url": "https://example.com/injury",
            "retrieved_at": "2026-07-24T10:02:00+00:00",
            "reliability": 0.98,
            "freshness": "current",
            "source_type": "primary",
            "independent_source_count": 1,
            "conflicting_reports": [],
        },
    )
    service.add_hypothesis(
        situation.id,
        {
            "id": "h1",
            "explanation": "Backup guard receives starter-level minutes.",
            "supporting_evidence": ["e1"],
            "contradicting_evidence": [],
            "competing_explanations": ["Team uses a committee rotation."],
            "expected_indicators": ["Backup guard is announced as starter."],
            "falsification_criteria": ["Backup guard remains below 20 minutes."],
            "confidence": 0.72,
            "confidence_history": [{"value": 0.72, "reason": "Official report"}],
            "status": "active",
        },
    )
    service.add_forecast(
        situation.id,
        {
            "id": "f1",
            "hypothesis_ids": ["h1"],
            "predicted_event": "Backup guard exceeds 18.5 points.",
            "probability": 0.7,
            "baseline_probability": 0.46,
            "horizon": "next game",
            "model_version": "player-opportunity-v1",
        },
    )
    service.add_recommended_action(situation.id, "Monitor the confirmed lineup.")
    service.record_outcome(
        situation.id,
        {"forecast_id": "f1", "occurred": True, "actual_value": 22},
    )

    graph = service.claim_graph(situation.id)

    node_types = {node["type"] for node in graph["nodes"]}
    relationships = {edge["relationship"] for edge in graph["edges"]}
    assert {
        "source",
        "evidence",
        "claim",
        "hypothesis",
        "forecast",
        "decision",
        "outcome",
    } <= node_types
    assert {"supports", "derived_from", "confirms"} <= relationships
    evidence_node = next(
        node for node in graph["nodes"] if node["id"] == "evidence:e1"
    )
    assert evidence_node["attributes"]["reliability"] == pytest.approx(0.98)
    assert evidence_node["attributes"]["retrieved_at"]
    assert evidence_node["attributes"]["source_type"] == "primary"


def test_forecast_quality_uses_brier_score_and_calibration(tmp_path: Path) -> None:
    service = IntelligenceLifecycleService(
        SQLiteLifecycleRepository(tmp_path / "lifecycle.db")
    )
    examples = [
        ("s1", 0.7, True),
        ("s2", 0.7, False),
    ]
    for situation_id, probability, occurred in examples:
        service.create_situation(
            situation_id=situation_id,
            title="Measurable forecast",
            objective="Measure forecast quality.",
            observation="Opportunity signal detected.",
        )
        service.add_forecast(
            situation_id,
            {
                "id": f"forecast-{situation_id}",
                "predicted_event": "Player exceeds the points line.",
                "probability": probability,
                "baseline_probability": 0.5,
                "horizon": "next game",
                "confidence_level": "medium",
                "supporting_factors": ["Projected minutes increased."],
                "invalidation_factors": ["Player is minutes-restricted."],
                "model_version": "player-opportunity-v1",
                "revision_history": [],
            },
        )
        service.record_outcome(
            situation_id,
            {
                "forecast_id": f"forecast-{situation_id}",
                "occurred": occurred,
                "actual_value": 20 if occurred else 12,
            },
        )

    quality = service.evaluate_forecasts()

    assert quality["evaluated_forecasts"] == 2
    assert quality["accuracy"] == pytest.approx(0.5)
    assert quality["brier_score"] == pytest.approx((0.3**2 + 0.7**2) / 2)
    assert quality["calibration_error"] == pytest.approx(0.2)
    assert quality["by_forecast_type"]["Player exceeds the points line."][
        "count"
    ] == 2


def test_lifecycle_api_exposes_state_history_and_claim_graph(
    tmp_path: Path,
) -> None:
    service = IntelligenceLifecycleService(
        SQLiteLifecycleRepository(tmp_path / "lifecycle.db")
    )
    client = create_app(lifecycle_service=service).test_client()

    created = client.post(
        "/api/intelligence/situations",
        json={
            "id": "nba:p5:game-1",
            "title": "API lifecycle",
            "objective": "Test an opportunity forecast.",
            "observation": "Starter was ruled out.",
            "observed_at": "2026-07-24T10:00:00+00:00",
        },
    )
    evidence = client.post(
        "/api/intelligence/situations/nba:p5:game-1/events",
        json={
            "event_type": "evidence_collected",
            "occurred_at": "2026-07-24T10:02:00+00:00",
            "evidence": {
                "id": "e1",
                "claim": "Starter is unavailable.",
                "source": "Official report",
                "url": "https://example.com/report",
                "retrieved_at": "2026-07-24T10:02:00+00:00",
            },
        },
    )

    state = client.get("/api/intelligence/situations/nba:p5:game-1")
    history = client.get(
        "/api/intelligence/situations/nba:p5:game-1/history"
    )
    graph = client.get("/api/intelligence/situations/nba:p5:game-1/graph")

    assert created.status_code == 201
    assert evidence.status_code == 201
    assert state.get_json()["current_stage"] == "retrieve"
    assert len(history.get_json()["events"]) == 2
    assert graph.get_json()["nodes"]


def test_research_memory_reuses_lessons_and_source_reliability(
    tmp_path: Path,
) -> None:
    service = IntelligenceLifecycleService(
        SQLiteLifecycleRepository(tmp_path / "lifecycle.db")
    )
    situation = service.create_situation(
        situation_id="memory-1",
        title="Rotation opportunity",
        objective="Learn which early indicators matter.",
        observation="Starter was ruled out.",
    )
    service.record_evidence(
        situation.id,
        {
            "id": "e1",
            "claim": "Replacement already has stable rotation minutes.",
            "source": "normalized NBA game logs",
            "url": "https://example.com/logs",
            "retrieved_at": "2026-07-24T10:00:00+00:00",
            "reliability": 0.9,
        },
    )
    service.add_hypothesis(
        situation.id,
        {
            "id": "h1",
            "explanation": "Stable rotation minutes improve opportunity reliability.",
            "confidence": 0.7,
        },
    )
    service.add_forecast(
        situation.id,
        {
            "id": "f1",
            "predicted_event": "Replacement exceeds the points line.",
            "probability": 0.7,
            "baseline_probability": 0.5,
            "horizon": "next game",
            "model_version": "v1",
        },
    )
    service.record_outcome(
        situation.id,
        {"forecast_id": "f1", "occurred": True},
    )
    service.record_lesson(
        situation.id,
        "Stable prior rotation minutes were an early useful indicator.",
    )

    memory = service.build_research_memory()

    assert memory["lessons"][0]["lesson"].startswith("Stable prior")
    assert memory["successful_hypotheses"][0]["hypothesis_id"] == "h1"
    assert memory["source_reliability"]["normalized NBA game logs"][
        "average_reliability"
    ] == pytest.approx(0.9)


def test_hypothesis_evaluation_preserves_confidence_history(
    tmp_path: Path,
) -> None:
    service = IntelligenceLifecycleService(
        SQLiteLifecycleRepository(tmp_path / "lifecycle.db")
    )
    situation = service.create_situation(
        situation_id="hypothesis-1",
        title="Role change test",
        objective="Actively test a role-change explanation.",
        observation="Player was projected to start.",
    )
    service.add_hypothesis(
        situation.id,
        {
            "id": "h1",
            "explanation": "Player receives starter-level minutes.",
            "supporting_evidence": [],
            "contradicting_evidence": [],
            "competing_explanations": ["Committee rotation"],
            "expected_indicators": ["Player starts"],
            "falsification_criteria": ["Player remains below 20 minutes"],
            "confidence": 0.7,
            "confidence_history": [{"value": 0.7, "reason": "Initial model"}],
            "status": "active",
        },
    )

    service.evaluate_hypothesis(
        situation.id,
        "h1",
        confidence=0.4,
        status="weakened",
        indicator_results={"Player starts": False},
        supporting_evidence=[],
        contradicting_evidence=["lineup-evidence"],
        reason="Confirmed lineup did not include the player.",
    )

    restored = service.get_situation(situation.id)
    hypothesis = restored.active_hypotheses[0]
    assert hypothesis["confidence"] == pytest.approx(0.4)
    assert hypothesis["status"] == "weakened"
    assert len(hypothesis["confidence_history"]) == 2
    assert hypothesis["indicator_results"]["Player starts"] is False
