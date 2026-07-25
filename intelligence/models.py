from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from typing import Any


def _serialize(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _serialize(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


@dataclass(slots=True)
class IntelligenceEvidence:
    title: str
    source: str
    summary: str
    url: str = ""
    observed_at: str = ""
    evidence_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass(slots=True)
class SituationAlert:
    alert_id: str
    category: str
    severity: str
    title: str
    what_happened: str
    why_it_matters: str
    who_is_exposed: list[str]
    what_could_happen_next: str
    evidence_supports: list[IntelligenceEvidence]
    invalidation_conditions: list[str]
    confidence: float
    forecast_horizon: str
    tags: list[str] = field(default_factory=list)
    situation_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass(slots=True)
class UserImpactConclusion:
    alert_id: str
    title: str
    impact_level: str
    affected_areas: list[str]
    observed_trigger: str
    direct_effect: str
    second_order_effect: str
    user_implication: str
    recommended_action: str
    confidence: float
    forecast_horizon: str
    evidence_titles: list[str] = field(default_factory=list)
    invalidation_conditions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass(slots=True)
class IntelligenceGraphNode:
    node_id: str
    label: str
    node_type: str
    score: float = 0.0
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass(slots=True)
class IntelligenceGraphEdge:
    source: str
    target: str
    relationship: str
    weight: float = 0.0
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass(slots=True)
class RiskDimension:
    label: str
    score: float
    status: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass(slots=True)
class RiskProfile:
    subject: str
    subject_type: str
    summary: str
    confidence: float
    dimensions: list[RiskDimension] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass(slots=True)
class Investigation:
    title: str
    hypothesis: str
    status: str
    confidence: float
    supporting_entities: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    situation_id: str = ""
    investigation_id: str = ""
    objective: str = ""
    evidence_collected: list[dict[str, Any]] = field(default_factory=list)
    contradicting_evidence: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    current_conclusion: str = ""
    related_forecasts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass(slots=True)
class DecisionJournalEntry:
    timestamp: str
    title: str
    hypothesis: str
    probability: float
    horizon: str
    invalidation_conditions: list[str] = field(default_factory=list)
    situation_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass(slots=True)
class MacroRegime:
    regime: str
    confidence: float
    summary: str
    indicators: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass(slots=True)
class ForecastModelScore:
    name: str
    domain: str
    calibration: float
    accuracy: float
    note: str
    data_classification: str = "simulated_benchmark"

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass(slots=True)
class Situation:
    id: str
    title: str
    summary: str
    status: str
    severity: str
    confidence: float
    current_lifecycle_stage: str = "observe"
    intelligence_objective: str = ""
    new_observations: list[str] = field(default_factory=list)
    evidence_collected: list[dict[str, Any]] = field(default_factory=list)
    evidence_gaps: list[str] = field(default_factory=list)
    active_hypotheses: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[IntelligenceEvidence] = field(default_factory=list)
    entities: list[dict[str, str]] = field(default_factory=list)
    forecasts: list[dict[str, Any]] = field(default_factory=list)
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    recommended_actions: list[dict[str, str]] = field(default_factory=list)
    investigations: list[Investigation] = field(default_factory=list)
    monitoring_rules: list[dict[str, Any]] = field(default_factory=list)
    final_outcome: dict[str, Any] | None = None
    lessons_learned: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass(slots=True)
class SituationRoomSnapshot:
    generated_at: str
    alerts: list[SituationAlert] = field(default_factory=list)
    user_impact_conclusions: list[UserImpactConclusion] = field(default_factory=list)
    plain_language_summary: dict[str, Any] = field(default_factory=dict)
    suggestion_prompts: list[dict[str, str]] = field(default_factory=list)
    situations: list[Situation] = field(default_factory=list)
    primary_situation_id: str = ""
    data_integrity: dict[str, Any] = field(default_factory=dict)
    graph_nodes: list[IntelligenceGraphNode] = field(default_factory=list)
    graph_edges: list[IntelligenceGraphEdge] = field(default_factory=list)
    graph_summary: dict[str, Any] = field(default_factory=dict)
    government_actions: list[dict[str, Any]] = field(default_factory=list)
    opportunities: list[dict[str, Any]] = field(default_factory=list)
    risk_profiles: list[RiskProfile] = field(default_factory=list)
    macro_regime: MacroRegime | None = None
    investigations: list[Investigation] = field(default_factory=list)
    decision_journal: list[DecisionJournalEntry] = field(default_factory=list)
    forecast_tournament: list[ForecastModelScore] = field(default_factory=list)
    source_status: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)
