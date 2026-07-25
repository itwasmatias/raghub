from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class LifecycleStage(StrEnum):
    OBSERVE = "observe"
    RETRIEVE = "retrieve"
    ANALYZE = "analyze"
    FORECAST = "forecast"
    MONITOR = "monitor"
    LEARN = "learn"


@dataclass(frozen=True, slots=True)
class SituationEvent:
    id: int | None
    situation_id: str
    sequence: int
    occurred_at: str
    recorded_at: str
    stage: LifecycleStage
    event_type: str
    payload: dict[str, Any]


@dataclass(slots=True)
class IntelligenceSituation:
    id: str
    title: str
    current_stage: LifecycleStage
    intelligence_objective: str
    new_observations: list[str] = field(default_factory=list)
    evidence_collected: list[dict[str, Any]] = field(default_factory=list)
    evidence_gaps: list[str] = field(default_factory=list)
    active_hypotheses: list[dict[str, Any]] = field(default_factory=list)
    forecasts: list[dict[str, Any]] = field(default_factory=list)
    recommended_actions: list[str] = field(default_factory=list)
    monitoring_rules: list[dict[str, Any]] = field(default_factory=list)
    final_outcome: dict[str, Any] | None = None
    lessons_learned: list[str] = field(default_factory=list)
    history: list[SituationEvent] = field(default_factory=list)

