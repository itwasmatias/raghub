from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Watch:
    id: int | None
    target_type: str
    target_value: str
    condition: str
    active: bool = True


@dataclass(frozen=True, slots=True)
class IntelligenceAlert:
    id: int | None
    watch_id: int
    player_id: str
    created_at: str
    signal: str
    baseline_value: float
    observed_value: float
    confidence: float
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AlertOutcome:
    alert_id: int
    evaluated_at: str
    continued: bool
    role_grew: bool
    correct: bool
    confidence_error: float
    notes: str


@dataclass(frozen=True, slots=True)
class ResearchLifecycle:
    id: int | None
    observation: str
    hypothesis: str
    experiment: str
    outcome: str | None
    learned_knowledge: str | None
