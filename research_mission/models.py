"""Domain-neutral contracts for governed research mission orchestration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from federation.capability import NodeCapability
from federation.task_request import AuthorizationLevel, TaskRequest


def _identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _capabilities(values, field_name: str) -> frozenset[NodeCapability]:
    try:
        result = frozenset(values)
    except TypeError as exc:
        raise TypeError(f"{field_name} must be an iterable of NodeCapability values") from exc
    if not all(isinstance(value, NodeCapability) for value in result):
        raise TypeError(f"{field_name} must contain only NodeCapability values")
    return result


class ResearchRole(str, Enum):
    """A required perspective in a research mission plan."""

    RESEARCHER = "researcher"
    CHALLENGER = "challenger"
    JUDGE = "judge"


class ResearchMissionStatus(str, Enum):
    """Lifecycle states managed by :class:`ResearchMissionRuntime`."""

    CREATED = "created"
    PLANNED = "planned"
    ROUTED = "routed"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True, frozen=True)
class ResearchTaskSpec:
    """A planned, non-executing unit of governed research work."""

    task_id: str
    mission_id: str
    role: ResearchRole
    sequence: int
    objective: str
    research_context: dict[str, Any]
    expected_result: str
    authorization_level: AuthorizationLevel
    approval_required: bool
    required_capabilities: frozenset[NodeCapability] = field(default_factory=frozenset)
    preferred_capabilities: frozenset[NodeCapability] = field(default_factory=frozenset)
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _identifier(self.task_id, "task_id"))
        object.__setattr__(self, "mission_id", _identifier(self.mission_id, "mission_id"))
        if not isinstance(self.role, ResearchRole):
            raise TypeError("role must be a ResearchRole")
        if (
            not isinstance(self.sequence, int)
            or isinstance(self.sequence, bool)
            or self.sequence < 1
        ):
            raise ValueError("sequence must be a positive integer")
        object.__setattr__(self, "objective", _identifier(self.objective, "objective"))
        if not isinstance(self.research_context, dict):
            raise TypeError("research_context must be a dict")
        object.__setattr__(self, "research_context", deepcopy(self.research_context))
        if not isinstance(self.expected_result, str) or not self.expected_result.strip():
            raise ValueError("expected_result must be a non-empty string")
        if not isinstance(self.authorization_level, AuthorizationLevel):
            raise TypeError("authorization_level must be an AuthorizationLevel")
        if not isinstance(self.approval_required, bool):
            raise TypeError("approval_required must be a boolean")
        required = _capabilities(self.required_capabilities, "required_capabilities")
        preferred = _capabilities(self.preferred_capabilities, "preferred_capabilities")
        if required & preferred:
            raise ValueError("required_capabilities and preferred_capabilities must not overlap")
        object.__setattr__(self, "required_capabilities", required)
        object.__setattr__(self, "preferred_capabilities", preferred)
        dependencies = tuple(self.depends_on)
        for dependency in dependencies:
            _identifier(dependency, "depends_on entry")
        object.__setattr__(self, "depends_on", dependencies)

    def to_task_request(self) -> TaskRequest:
        """Convert this spec to the federation's canonical routing contract."""

        return TaskRequest(
            task_id=self.task_id,
            mission_id=self.mission_id,
            required_capabilities=set(self.required_capabilities),
            preferred_capabilities=set(self.preferred_capabilities),
            authorization_level=self.authorization_level,
            approval_required=self.approval_required,
            input_data={
                "research_mission": {
                    "mission_id": self.mission_id,
                    "objective": self.objective,
                    "role": self.role.value,
                    "sequence": self.sequence,
                    "depends_on": list(self.depends_on),
                    "context": deepcopy(self.research_context),
                }
            },
            expected_result=self.expected_result,
        )


@dataclass(slots=True, frozen=True)
class ResearchMissionPlan:
    """An ordered deterministic collection of research task specifications."""

    mission_id: str
    tasks: tuple[ResearchTaskSpec, ...]

    def __post_init__(self) -> None:
        _identifier(self.mission_id, "mission_id")
        tasks = tuple(self.tasks)
        if not tasks:
            raise ValueError("tasks must not be empty")
        if not all(isinstance(task, ResearchTaskSpec) for task in tasks):
            raise TypeError("tasks must contain only ResearchTaskSpec values")
        if any(task.mission_id != self.mission_id for task in tasks):
            raise ValueError("every task must belong to the plan mission_id")
        if len({task.task_id for task in tasks}) != len(tasks):
            raise ValueError("task_id values must be unique within a plan")
        if tuple(task.sequence for task in tasks) != tuple(range(1, len(tasks) + 1)):
            raise ValueError("task sequence values must match deterministic plan order")
        earlier_task_ids: set[str] = set()
        for task in tasks:
            if len(set(task.depends_on)) != len(task.depends_on):
                raise ValueError("depends_on entries must be unique")
            if not set(task.depends_on).issubset(earlier_task_ids):
                raise ValueError("every dependency must identify an earlier task")
            earlier_task_ids.add(task.task_id)
        roles = {task.role for task in tasks}
        if not set(ResearchRole).issubset(roles):
            raise ValueError("plan must include researcher, challenger, and judge roles")
        object.__setattr__(self, "tasks", tasks)

    def to_task_requests(self) -> tuple[TaskRequest, ...]:
        return tuple(task.to_task_request() for task in self.tasks)


@dataclass(slots=True)
class ResearchMission:
    """Mutable lifecycle aggregate; it contains contracts, never research results."""

    mission_id: str
    objective: str
    research_context: dict[str, Any] = field(default_factory=dict)
    authorization_level: AuthorizationLevel = AuthorizationLevel.INTERNAL
    approval_required: bool = False
    required_capabilities: frozenset[NodeCapability] = field(default_factory=frozenset)
    preferred_capabilities: frozenset[NodeCapability] = field(default_factory=frozenset)
    status: ResearchMissionStatus = ResearchMissionStatus.CREATED
    plan: ResearchMissionPlan | None = field(default=None, init=False)
    routing_decisions: tuple[Any, ...] = field(default=(), init=False)
    dispatch_offers: tuple[Any, ...] = field(default=(), init=False)

    def __post_init__(self) -> None:
        self.mission_id = _identifier(self.mission_id, "mission_id")
        self.objective = _identifier(self.objective, "objective")
        if not isinstance(self.research_context, dict):
            raise TypeError("research_context must be a dict")
        self.research_context = deepcopy(self.research_context)
        if not isinstance(self.authorization_level, AuthorizationLevel):
            raise TypeError("authorization_level must be an AuthorizationLevel")
        if not isinstance(self.approval_required, bool):
            raise TypeError("approval_required must be a boolean")
        self.required_capabilities = _capabilities(
            self.required_capabilities, "required_capabilities"
        )
        self.preferred_capabilities = _capabilities(
            self.preferred_capabilities, "preferred_capabilities"
        )
        if self.required_capabilities & self.preferred_capabilities:
            raise ValueError("required_capabilities and preferred_capabilities must not overlap")
        if not isinstance(self.status, ResearchMissionStatus):
            raise TypeError("status must be a ResearchMissionStatus")
